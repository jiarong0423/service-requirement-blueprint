from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import app.services.json_b_workflow as json_b_module
from app.db import connect_database
from app.services.artifact_store import read_verified
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
        raw = read_verified(data_root, private["relative_path"], private["sha256"])
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
        raw = read_verified(data_root, private["relative_path"], private["sha256"])
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
        first_raw = read_verified(data_root, first_artifact["relative_path"], first_artifact["sha256"])
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
        second_raw = read_verified(data_root, second_artifact["relative_path"], second_artifact["sha256"])
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
        raw = read_verified(data_root, row["relative_path"], row["sha256"])

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
