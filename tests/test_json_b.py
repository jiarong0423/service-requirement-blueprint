from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Barrier, Event, Thread

import pytest

import app.services.json_b_workflow as json_b_module
from app import create_app
from app.db import connect_database
from app.services.artifact_store import MAX_JSON_ARTIFACT_BYTES, install_json_artifact, read_verified
from app.services.json_b_workflow import (
    JsonBWorkflowError,
    compute_fee_totals,
    export_private_draft,
    load_private_draft,
    load_private_export,
    load_review,
    read_review_bytes,
    save_private_draft,
    submit_private_export,
    validate_objection_pointer,
)
from tests.slice4_helpers import activate_private_case, valid_business_draft


def _encoded_business() -> bytes:
    return json.dumps(valid_business_draft(), ensure_ascii=False).encode("utf-8")


class _SteppedClock:
    values: list[datetime] = []

    @staticmethod
    def fromisoformat(value: str) -> datetime:
        return datetime.fromisoformat(value)

    @classmethod
    def now(cls, tz) -> datetime:
        assert cls.values, "unexpected clock read"
        return cls.values.pop(0).astimezone(tz)


def test_new_export_rejects_quote_expiring_at_transaction_boundary(
    demo_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = activate_private_case(demo_runtime)
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        revision = save_private_draft(
            connection, data_root, schema_path, context["case_id"], context["contractor_id"], 0, _encoded_business()
        )
        _SteppedClock.values = [
            datetime(2099, 12, 31, 23, 59, 58, tzinfo=timezone.utc),
            datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        ]
        monkeypatch.setattr(json_b_module, "datetime", _SteppedClock)
        with pytest.raises(JsonBWorkflowError, match="quote_expired"):
            export_private_draft(
                connection, data_root, schema_path, context["case_id"], context["contractor_id"], revision
            )
        assert _SteppedClock.values == []
        assert connection.execute("SELECT COUNT(*) FROM json_b_private_exports").fetchone()[0] == 0
        assert connection.execute(
            "SELECT current_export_artifact_id FROM json_b_drafts WHERE case_id = ?", (context["case_id"],)
        ).fetchone()[0] is None
    finally:
        connection.close()


def test_repeated_export_rejects_quote_expiring_inside_transaction(
    demo_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = activate_private_case(demo_runtime)
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        revision = save_private_draft(
            connection, data_root, schema_path, context["case_id"], context["contractor_id"], 0, _encoded_business()
        )
        exported = export_private_draft(
            connection, data_root, schema_path, context["case_id"], context["contractor_id"], revision
        )
        _SteppedClock.values = [
            datetime(2099, 12, 31, 23, 59, 58, tzinfo=timezone.utc),
            datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        ]
        monkeypatch.setattr(json_b_module, "datetime", _SteppedClock)
        with pytest.raises(JsonBWorkflowError, match="quote_expired"):
            export_private_draft(
                connection, data_root, schema_path, context["case_id"], context["contractor_id"], revision
            )
        assert _SteppedClock.values == []
        assert connection.execute("SELECT COUNT(*) FROM json_b_private_exports").fetchone()[0] == 1
        assert connection.execute(
            "SELECT current_export_artifact_id FROM json_b_drafts WHERE case_id = ?", (context["case_id"],)
        ).fetchone()[0] == exported.artifact_id
    finally:
        connection.close()


def test_conflict_winner_recheck_rejects_quote_at_expiry_boundary(
    demo_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = activate_private_case(demo_runtime)
    database = context["instance_root"] / "app.sqlite3"
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    first = connect_database(database)
    second = connect_database(database)
    try:
        revision = save_private_draft(
            first, data_root, schema_path, context["case_id"], context["contractor_id"], 0, _encoded_business()
        )
        _SteppedClock.values = [
            datetime(2099, 12, 31, 23, 59, 58, tzinfo=timezone.utc),
            datetime(2099, 12, 31, 23, 59, 58, tzinfo=timezone.utc),
            datetime(2099, 12, 31, 23, 59, 58, tzinfo=timezone.utc),
            datetime(2099, 12, 31, 23, 59, 58, tzinfo=timezone.utc),
            datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        ]
        monkeypatch.setattr(json_b_module, "datetime", _SteppedClock)
        original_install = json_b_module.install_json_artifact
        winner_id: list[str] = []

        def install_with_winner(root, case_id, artifact_id, raw):
            relative = original_install(root, case_id, artifact_id, raw)
            monkeypatch.setattr(json_b_module, "install_json_artifact", original_install)
            winner_id.append(export_private_draft(second, data_root, schema_path, case_id, context["contractor_id"], revision).artifact_id)
            return relative

        monkeypatch.setattr(json_b_module, "install_json_artifact", install_with_winner)
        with pytest.raises(JsonBWorkflowError, match="quote_expired"):
            export_private_draft(
                first, data_root, schema_path, context["case_id"], context["contractor_id"], revision
            )
        assert _SteppedClock.values == []
        assert first.execute("SELECT COUNT(*) FROM json_b_private_exports").fetchone()[0] == 1
        assert first.execute(
            "SELECT current_export_artifact_id FROM json_b_drafts WHERE case_id = ?", (context["case_id"],)
        ).fetchone()[0] == winner_id[0]
    finally:
        first.close()
        second.close()


def test_private_draft_export_and_exact_reimport_create_one_review(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        revision = save_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            0,
            _encoded_business(),
        )
        assert revision == 1
        with pytest.raises(JsonBWorkflowError, match="case_unavailable"):
            load_private_draft(connection, context["case_id"], context["owner_id"])

        exported = export_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            revision,
        )
        replay = export_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            revision,
        )
        assert replay.artifact_id == exported.artifact_id
        assert replay.created is False
        private = load_private_export(
            connection,
            context["case_id"],
            context["contractor_id"],
            exported.artifact_id,
        )
        raw = read_verified(data_root, private["relative_path"], private["sha256"], private["byte_length"], max_bytes=MAX_JSON_ARTIFACT_BYTES)
        review = submit_private_export(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            exported.artifact_id,
            raw,
        )
        assert review.created is True
        assert review.version_no == 1
        replayed_review = submit_private_export(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            exported.artifact_id,
            raw,
        )
        assert replayed_review == review.__class__(exported.artifact_id, 1, False)
        replayed_after_expiry = submit_private_export(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            exported.artifact_id,
            raw,
            now=datetime(2100, 1, 1, tzinfo=timezone.utc),
        )
        assert replayed_after_expiry == review.__class__(exported.artifact_id, 1, False)
        owner_review = load_review(
            connection,
            context["case_id"],
            context["owner_id"],
            1,
        )
        document, review_raw = read_review_bytes(data_root, owner_review)
        assert review_raw == raw
        assert document["computed_fee_totals"] == [
            {
                "amount": 1200,
                "billing_kind": "one_time",
                "billing_period": None,
                "cost_source": "contractor",
                "currency_code": "XTS",
                "included_in_quote": True,
            },
            {
                "amount": 20,
                "billing_kind": "recurring",
                "billing_period": "monthly",
                "cost_source": "third_party",
                "currency_code": "XTS",
                "included_in_quote": False,
            },
        ]
        case = connection.execute(
            "SELECT state, current_json_b_artifact_id FROM cases WHERE id = ?",
            (context["case_id"],),
        ).fetchone()
        assert tuple(case) == ("JSON_B_REVIEW", exported.artifact_id)
    finally:
        connection.close()


def test_reserialized_or_tampered_export_is_rejected_without_state_change(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        revision = save_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            0,
            _encoded_business(),
        )
        exported = export_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            revision,
        )
        private = load_private_export(
            connection,
            context["case_id"],
            context["contractor_id"],
            exported.artifact_id,
        )
        raw = read_verified(data_root, private["relative_path"], private["sha256"], private["byte_length"], max_bytes=MAX_JSON_ARTIFACT_BYTES)
        reformatted = json.dumps(json.loads(raw), ensure_ascii=False).encode("utf-8")
        assert reformatted != raw
        with pytest.raises(JsonBWorkflowError, match="exact_bytes_required"):
            submit_private_export(
                connection,
                data_root,
                schema_path,
                context["case_id"],
                context["contractor_id"],
                exported.artifact_id,
                reformatted,
            )
        assert connection.execute("SELECT COUNT(*) FROM json_b_versions").fetchone()[0] == 0
        assert connection.execute(
            "SELECT state FROM json_b_private_exports WHERE artifact_id = ?",
            (exported.artifact_id,),
        ).fetchone()[0] == "ACTIVE"
        assert connection.execute(
            "SELECT state FROM cases WHERE id = ?", (context["case_id"],)
        ).fetchone()[0] == "JSON_B_PRIVATE_DRAFT"
    finally:
        connection.close()


def test_new_draft_invalidates_private_export_and_v2_preserves_v1_bytes(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        first_revision = save_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            0,
            _encoded_business(),
        )
        first_export = export_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            first_revision,
        )
        first_artifact = load_private_export(
            connection,
            context["case_id"],
            context["contractor_id"],
            first_export.artifact_id,
        )
        first_raw = read_verified(data_root, first_artifact["relative_path"], first_artifact["sha256"], first_artifact["byte_length"], max_bytes=MAX_JSON_ARTIFACT_BYTES)
        submit_private_export(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            first_export.artifact_id,
            first_raw,
        )

        revised = valid_business_draft()
        revised["requirement_understanding"] = "Revised scope with an explicit maintenance boundary."
        second_revision = save_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            1,
            json.dumps(revised).encode("utf-8"),
        )
        second_export = export_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            second_revision,
        )
        second_artifact = load_private_export(
            connection,
            context["case_id"],
            context["contractor_id"],
            second_export.artifact_id,
        )
        second_raw = read_verified(data_root, second_artifact["relative_path"], second_artifact["sha256"], second_artifact["byte_length"], max_bytes=MAX_JSON_ARTIFACT_BYTES)
        submit_private_export(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            second_export.artifact_id,
            second_raw,
        )
        v1 = load_review(connection, context["case_id"], context["owner_id"], 1)
        v2 = load_review(connection, context["case_id"], context["owner_id"], 2)
        assert read_review_bytes(data_root, v1)[1] == first_raw
        assert read_review_bytes(data_root, v2)[1] == second_raw
        assert v1["state"] == "SUPERSEDED"
        assert v2["state"] == "CURRENT"
    finally:
        connection.close()

    for party in (context["owner"], context["contractor"]):
        old_review = party.get(f"/en/cases/{context['case_id']}/json-b/reviews/1")
        assert old_review.status_code == 200
        old_html = old_review.get_data(as_text=True)
        assert "Previous proposal" in old_html
        assert "Open the private case for the latest proposal" in old_html
        assert "Raise an objection to request a change" not in old_html
        assert "Create a confirmation version or revise the proposal" not in old_html
        assert 'data-ui="primary-action"' not in old_html


@pytest.mark.parametrize("bad_amount", [-1, 1.5, "100", True, 10_000_000_000])
def test_fee_totals_reject_non_integer_or_out_of_range_amounts(bad_amount) -> None:
    item = valid_business_draft()["fee_items"][0]
    item["amount"] = bad_amount
    with pytest.raises(JsonBWorkflowError, match="fee_items_invalid"):
        compute_fee_totals([item])


def test_fee_totals_do_not_mix_currency_period_source_or_inclusion() -> None:
    items = valid_business_draft()["fee_items"]
    extra = dict(items[1])
    extra["fee_id"] = "00000000-0000-4000-8000-000000000001"
    extra["currency_code"] = "XXX"
    totals = compute_fee_totals([*items, extra])
    assert {(row["currency_code"], row["billing_period"], row["cost_source"]) for row in totals} == {
        ("XTS", None, "contractor"),
        ("XTS", "monthly", "third_party"),
        ("XXX", "monthly", "third_party"),
    }


def test_objection_pointer_rejects_invalid_rfc6901_tokens_and_array_indexes() -> None:
    document = {"included_scope": [{"description": "Included"}], "excluded_scope": []}
    validate_objection_pointer(document, "/included_scope/0/description")
    for pointer in (
        "/included_scope/00/description",
        "/included_scope/٠/description",
        "/included_scope/0/descr~2iption",
        "/included_scope/0/description~",
    ):
        with pytest.raises(JsonBWorkflowError, match="field_pointer_invalid"):
            validate_objection_pointer(document, pointer)


def test_idempotent_export_fails_closed_when_saved_bytes_are_tampered(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    database = context["instance_root"] / "app.sqlite3"
    connection = connect_database(database)
    try:
        revision = save_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            0,
            _encoded_business(),
        )
        exported = export_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            revision,
        )
        row = load_private_export(
            connection,
            context["case_id"],
            context["contractor_id"],
            exported.artifact_id,
        )
        path = data_root / row["relative_path"]
    finally:
        connection.close()
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    restarted = connect_database(database)
    try:
        with pytest.raises(JsonBWorkflowError, match="export_unavailable"):
            export_private_draft(
                restarted,
                data_root,
                schema_path,
                context["case_id"],
                context["contractor_id"],
                revision,
            )
        assert restarted.execute(
            "SELECT COUNT(*) FROM json_b_private_exports WHERE case_id = ?",
            (context["case_id"],),
        ).fetchone()[0] == 1
    finally:
        restarted.close()


def test_active_submission_checks_expiry_after_transaction_begins(
    demo_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = activate_private_case(demo_runtime)
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        revision = save_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            0,
            _encoded_business(),
        )
        exported = export_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            revision,
        )
        row = load_private_export(
            connection,
            context["case_id"],
            context["contractor_id"],
            exported.artifact_id,
        )
        raw = read_verified(data_root, row["relative_path"], row["sha256"], row["byte_length"], max_bytes=MAX_JSON_ARTIFACT_BYTES)

        class ObservedDateTime:
            @classmethod
            def fromisoformat(cls, value: str):
                return datetime.fromisoformat(value)

            @classmethod
            def now(cls, tz):
                assert connection.in_transaction is True
                return datetime(2100, 1, 1, tzinfo=timezone.utc)

        monkeypatch.setattr(json_b_module, "datetime", ObservedDateTime)
        with pytest.raises(JsonBWorkflowError, match="quote_expired"):
            submit_private_export(
                connection,
                data_root,
                schema_path,
                context["case_id"],
                context["contractor_id"],
                exported.artifact_id,
                raw,
            )
        assert connection.execute(
            "SELECT state FROM json_b_private_exports WHERE artifact_id = ?",
            (exported.artifact_id,),
        ).fetchone()[0] == "ACTIVE"
        assert connection.execute("SELECT COUNT(*) FROM json_b_versions").fetchone()[0] == 0
    finally:
        connection.close()


def test_json_b_slow_operations_never_run_inside_immediate_transaction(
    demo_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = activate_private_case(demo_runtime)
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    writer = {"active": False}
    original_immediate = json_b_module.immediate_transaction

    @contextmanager
    def observed_immediate(conn):
        with original_immediate(conn):
            writer["active"] = True
            try:
                yield
            finally:
                writer["active"] = False

    monkeypatch.setattr(json_b_module, "immediate_transaction", observed_immediate)
    for name in ("_validate_source_file", "_validate_document", "ensure_supported_text", "install_json_artifact", "read_verified"):
        original = getattr(json_b_module, name)

        def outside_writer(*args, _original=original, _name=name, **kwargs):
            assert writer["active"] is False, f"{_name} ran inside writer transaction"
            return _original(*args, **kwargs)

        monkeypatch.setattr(json_b_module, name, outside_writer)

    try:
        revision = save_private_draft(
            connection, data_root, schema_path, context["case_id"], context["contractor_id"], 0, _encoded_business()
        )
        exported = export_private_draft(
            connection,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            revision,
            context["app"].config["DOCUMENT_ASSETS"],
        )
        row = load_private_export(connection, context["case_id"], context["contractor_id"], exported.artifact_id)
        raw = read_verified(data_root, row["relative_path"], row["sha256"], row["byte_length"], max_bytes=MAX_JSON_ARTIFACT_BYTES)
        submit_private_export(
            connection, data_root, schema_path, context["case_id"], context["contractor_id"], exported.artifact_id, raw
        )
    finally:
        connection.close()


def test_json_b_export_does_not_block_another_case_writer(
    demo_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = activate_private_case(demo_runtime)
    second = activate_private_case(demo_runtime)
    database = first["instance_root"] / "app.sqlite3"
    data_root = first["instance_root"] / "data"
    schema_path = first["app"].config["JSON_B_SCHEMA_PATH"]
    setup = connect_database(database)
    try:
        first_revision = save_private_draft(setup, data_root, schema_path, first["case_id"], first["contractor_id"], 0, _encoded_business())
    finally:
        setup.close()
    entered = Event()
    release = Event()
    original_install = json_b_module.install_json_artifact

    def paused_install(root, case_id, artifact_id, raw):
        relative = original_install(root, case_id, artifact_id, raw)
        if case_id == first["case_id"]:
            entered.set()
            assert release.wait(10)
        return relative

    monkeypatch.setattr(json_b_module, "install_json_artifact", paused_install)
    outcome: list[object] = []

    def export_first() -> None:
        connection = connect_database(database)
        try:
            outcome.append(export_private_draft(connection, data_root, schema_path, first["case_id"], first["contractor_id"], first_revision))
        except BaseException as error:
            outcome.append(error)
        finally:
            connection.close()

    thread = Thread(target=export_first)
    thread.start()
    assert entered.wait(10)
    other = connect_database(database)
    try:
        assert save_private_draft(other, data_root, schema_path, second["case_id"], second["contractor_id"], 0, _encoded_business()) == 1
    finally:
        other.close()
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert len(outcome) == 1 and not isinstance(outcome[0], BaseException)


def test_concurrent_private_exports_converge_and_quarantine_loser(
    demo_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = activate_private_case(demo_runtime)
    database = context["instance_root"] / "app.sqlite3"
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    setup = connect_database(database)
    try:
        revision = save_private_draft(setup, data_root, schema_path, context["case_id"], context["contractor_id"], 0, _encoded_business())
    finally:
        setup.close()
    barrier = Barrier(2)
    original_install = json_b_module.install_json_artifact

    def synchronized_install(root, case_id, artifact_id, raw):
        relative = original_install(root, case_id, artifact_id, raw)
        barrier.wait(timeout=10)
        return relative

    monkeypatch.setattr(json_b_module, "install_json_artifact", synchronized_install)
    outcomes: list[object] = []

    def worker() -> None:
        connection = connect_database(database)
        try:
            outcomes.append(export_private_draft(connection, data_root, schema_path, context["case_id"], context["contractor_id"], revision))
        except BaseException as error:
            outcomes.append(error)
        finally:
            connection.close()

    threads = [Thread(target=worker), Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert all(not thread.is_alive() for thread in threads)
    assert all(not isinstance(result, BaseException) for result in outcomes)
    assert len({result.artifact_id for result in outcomes}) == 1
    connection = connect_database(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM json_b_private_exports WHERE case_id = ?", (context["case_id"],)
        ).fetchone()[0] == 1
    finally:
        connection.close()
    case_root = data_root / "cases" / context["case_id"]
    assert len(list((case_root / "artifacts").iterdir())) == 2
    assert len([path for path in (case_root / "quarantine").iterdir() if path.name.startswith("artifacts-")]) == 1


def test_json_b_export_source_change_before_cas_quarantines_provisional(
    demo_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = activate_private_case(demo_runtime)
    database = context["instance_root"] / "app.sqlite3"
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    setup = connect_database(database)
    try:
        revision = save_private_draft(setup, data_root, schema_path, context["case_id"], context["contractor_id"], 0, _encoded_business())
    finally:
        setup.close()
    entered = Event()
    release = Event()
    original_install = json_b_module.install_json_artifact

    def paused_install(root, case_id, artifact_id, raw):
        relative = original_install(root, case_id, artifact_id, raw)
        entered.set()
        assert release.wait(10)
        return relative

    monkeypatch.setattr(json_b_module, "install_json_artifact", paused_install)
    outcomes: list[object] = []

    def worker() -> None:
        connection = connect_database(database)
        try:
            outcomes.append(export_private_draft(connection, data_root, schema_path, context["case_id"], context["contractor_id"], revision))
        except BaseException as error:
            outcomes.append(error)
        finally:
            connection.close()

    thread = Thread(target=worker)
    thread.start()
    assert entered.wait(10)
    changed = valid_business_draft()
    changed["requirement_understanding"] = "Changed while export bytes were being prepared."
    writer = connect_database(database)
    try:
        assert save_private_draft(
            writer,
            data_root,
            schema_path,
            context["case_id"],
            context["contractor_id"],
            revision,
            json.dumps(changed).encode("utf-8"),
        ) == revision + 1
    finally:
        writer.close()
        release.set()
        thread.join(10)
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], JsonBWorkflowError) and outcomes[0].code == "draft_conflict"
    connection = connect_database(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM json_b_private_exports WHERE case_id = ?", (context["case_id"],)
        ).fetchone()[0] == 0
    finally:
        connection.close()
    quarantine = data_root / "cases" / context["case_id"] / "quarantine"
    assert len([path for path in quarantine.iterdir() if path.name.startswith("artifacts-")]) == 1


def test_json_b_export_db_failure_quarantines_published_artifact(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    database = context["instance_root"] / "app.sqlite3"
    data_root = context["instance_root"] / "data"
    schema_path = context["app"].config["JSON_B_SCHEMA_PATH"]
    connection = connect_database(database)
    try:
        revision = save_private_draft(connection, data_root, schema_path, context["case_id"], context["contractor_id"], 0, _encoded_business())
        connection.execute(
            "CREATE TRIGGER reject_json_b_private_export BEFORE INSERT ON artifacts "
            "WHEN NEW.kind = 'json_b_private_export' BEGIN SELECT RAISE(ABORT, 'forced'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="forced"):
            export_private_draft(connection, data_root, schema_path, context["case_id"], context["contractor_id"], revision)
        assert connection.execute(
            "SELECT COUNT(*) FROM json_b_private_exports WHERE case_id = ?", (context["case_id"],)
        ).fetchone()[0] == 0
    finally:
        connection.close()
    case_root = data_root / "cases" / context["case_id"]
    assert len(list((case_root / "artifacts").iterdir())) == 1
    assert len([path for path in (case_root / "quarantine").iterdir() if path.name.startswith("artifacts-")]) == 1


def test_startup_quarantines_unreferenced_json_artifact(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    orphan_id = str(uuid.uuid4())
    data_root = context["instance_root"] / "data"
    relative = install_json_artifact(data_root, context["case_id"], orphan_id, b"{}\n")
    assert (data_root / relative).is_file()
    create_app(
        {
            "TESTING": False,
            "SECRET_KEY": (context["instance_root"] / "secret.key").read_bytes(),
            "INSTANCE_ROOT": context["instance_root"],
            "DATABASE": context["instance_root"] / "app.sqlite3",
            "DATA_ROOT": data_root,
            "SESSION_COOKIE_SECURE": False,
        }
    )
    assert not (data_root / relative).exists()
    quarantine = data_root / "cases" / context["case_id"] / "quarantine"
    assert any(path.name.startswith(f"artifacts-{orphan_id}-") for path in quarantine.iterdir())
