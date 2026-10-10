from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import pytest

import app.services.formalization_workflow as formalization_module
from app.db import connect_database
from app.services.candidate_workflow import CandidateWorkflowError
from app.services.discussion_workflow import transition_issue
from app.services.formalization_workflow import (
    FormalizationWorkflowError,
    acknowledge_candidate,
    confirm_candidate,
    request_candidate_revision,
    withdraw_confirmation,
)
from tests.helpers import csrf
from tests.slice6_helpers import create_candidate_context


def _args(context: dict[str, object], connection, actor_id: str):
    return (
        connection,
        context["instance_root"] / "data",
        context["app"].config["DOCUMENT_ASSETS"],
        context["case_id"],
        context["candidate_id"],
        actor_id,
        1,
    )


def test_quote_expiry_after_acknowledgement_never_becomes_consent(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        acknowledge_candidate(*_args(context, connection, context["owner_id"]))
        connection.execute(
            "UPDATE json_b_versions SET quote_expires_at = '2000-01-01T00:00:00Z' "
            "WHERE artifact_id = ?",
            (context["json_b_artifact_id"],),
        )
        with pytest.raises(FormalizationWorkflowError, match="quote_expired"):
            confirm_candidate(*_args(context, connection, context["owner_id"]))
        assert connection.execute("SELECT COUNT(*) FROM candidate_confirmations").fetchone()[0] == 0
        assert connection.execute(
            "SELECT state FROM cases WHERE id = ?", (context["case_id"],)
        ).fetchone()[0] == "CANDIDATE_REVIEW"
    finally:
        connection.close()


def test_candidate_bundle_verification_never_runs_inside_writer_transaction(
    demo_runtime, monkeypatch
) -> None:
    context = create_candidate_context(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    writer_active = False
    original_transaction = formalization_module.immediate_transaction
    original_loader = formalization_module._load_verified_bundle

    @contextmanager
    def tracked_transaction(active_connection):
        nonlocal writer_active
        with original_transaction(active_connection):
            writer_active = True
            try:
                yield
            finally:
                writer_active = False

    def guarded_loader(*args, **kwargs):
        assert writer_active is False
        return original_loader(*args, **kwargs)

    monkeypatch.setattr(formalization_module, "immediate_transaction", tracked_transaction)
    monkeypatch.setattr(formalization_module, "_load_verified_bundle", guarded_loader)
    try:
        assert acknowledge_candidate(*_args(context, connection, context["owner_id"])) is True
        confirmation = confirm_candidate(*_args(context, connection, context["owner_id"]))
        assert confirmation.created is True
        withdraw_confirmation(*_args(context, connection, context["owner_id"]))
        request_candidate_revision(
            *_args(context, connection, context["owner_id"]),
            reason="The candidate needs a revised scope.",
        )
    finally:
        connection.close()


def test_revision_reason_is_visible_and_blocks_identical_candidate_rebuild(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    case_id = context["case_id"]
    candidate_id = context["candidate_id"]
    reason = "Please clarify whether the contact form is included before confirmation."
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        rows = connection.execute(
            "SELECT artifact.relative_path FROM candidate_versions AS candidate "
            "JOIN artifacts AS artifact ON artifact.id IN "
            "(candidate.manifest_artifact_id, candidate.pdf_artifact_id, candidate.svg_artifact_id) "
            "WHERE candidate.id = ? ORDER BY artifact.relative_path",
            (candidate_id,),
        ).fetchall()
        paths = [context["instance_root"] / "data" / row["relative_path"] for row in rows]
        original_bytes = [path.read_bytes() for path in paths]
    finally:
        connection.close()

    requested = context["owner"].post(
        f"/en/cases/{case_id}/candidates/{candidate_id}/revision-requests",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": "1",
            "reason": reason,
        },
    )
    assert requested.status_code == 302
    assert requested.location.endswith(f"/en/cases/{case_id}")
    case_page = context["contractor"].get(requested.location)
    assert case_page.status_code == 200
    assert reason.encode() in case_page.data
    assert b"1 recorded" in case_page.data

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        issue = connection.execute(
            "SELECT id, kind, summary, opened_by_id, opened_by_role, state, revision "
            "FROM case_issues WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        assert issue is not None
        assert tuple(issue)[1:] == (
            "CLARIFICATION", reason, context["owner_id"], "client", "OPEN", 1,
        )
        event = connection.execute(
            "SELECT action, from_state, to_state, note, issue_revision FROM issue_events "
            "WHERE issue_id = ?",
            (issue["id"],),
        ).fetchone()
        assert tuple(event) == ("OPENED", None, "OPEN", reason, 1)
        state = connection.execute(
            "SELECT state, active_candidate_id FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        assert tuple(state) == ("JSON_B_REVIEW", None)
        assert connection.execute(
            "SELECT state FROM candidate_versions WHERE id = ?", (candidate_id,)
        ).fetchone()[0] == "INVALIDATED"
    finally:
        connection.close()
    assert [path.read_bytes() for path in paths] == original_bytes

    rebuilt = context["contractor"].post(
        f"/en/cases/{case_id}/candidates",
        data={"csrf_token": csrf(context["contractor"])},
    )
    assert rebuilt.status_code == 422
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_versions WHERE case_id = ?", (case_id,)
        ).fetchone()[0] == 1
        assert transition_issue(
            connection, case_id, issue["id"], context["owner_id"],
            "PROPOSE", 1, "The contact form is excluded from this proposal.",
        ) == 2
        assert transition_issue(
            connection, case_id, issue["id"], context["contractor_id"],
            "ACCEPT", 2, "Agreed that the contact form is excluded.",
        ) == 3
    finally:
        connection.close()

    permitted = context["contractor"].post(
        f"/en/cases/{case_id}/candidates",
        data={"csrf_token": csrf(context["contractor"])},
    )
    assert permitted.status_code == 302


def test_fixed_candidate_blocks_source_pointer_revision_and_same_user_roles(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="fixed_candidate_sources_locked"):
            connection.execute(
                "UPDATE cases SET content_revision = content_revision + 1 WHERE id = ?",
                (context["case_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE cases SET contractor_id = owner_id WHERE id = ?",
                (context["case_id"],),
            )
    finally:
        connection.close()


def test_tampered_candidate_file_blocks_acknowledgement_without_writes(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        row = connection.execute(
            "SELECT a.relative_path FROM candidate_versions AS candidate "
            "JOIN artifacts AS a ON a.id = candidate.pdf_artifact_id WHERE candidate.id = ?",
            (context["candidate_id"],),
        ).fetchone()
    finally:
        connection.close()
    path = context["instance_root"] / "data" / row["relative_path"]
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"tamper")
    path.chmod(0o400)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        with pytest.raises(CandidateWorkflowError):
            acknowledge_candidate(*_args(context, connection, context["owner_id"]))
        assert connection.execute("SELECT COUNT(*) FROM candidate_acknowledgements").fetchone()[0] == 0
    finally:
        connection.close()


def test_simultaneous_distinct_confirmations_serialize_to_one_formalization(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    setup = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        acknowledge_candidate(*_args(context, setup, context["owner_id"]))
        acknowledge_candidate(*_args(context, setup, context["contractor_id"]))
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, bool] | tuple[str, str]] = []
    lock = threading.Lock()

    def worker(actor_id: str) -> None:
        connection = connect_database(context["instance_root"] / "app.sqlite3")
        try:
            barrier.wait()
            result = confirm_candidate(*_args(context, connection, actor_id))
            value: tuple[str, bool] | tuple[str, str] = ("ok", result.formalized)
        except FormalizationWorkflowError as error:
            value = ("error", error.code)
        finally:
            connection.close()
        with lock:
            outcomes.append(value)

    threads = [
        threading.Thread(target=worker, args=(context["owner_id"],)),
        threading.Thread(target=worker, args=(context["contractor_id"],)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(outcomes) == [("ok", False), ("ok", True)]

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute(
            "SELECT state FROM cases WHERE id = ?", (context["case_id"],)
        ).fetchone()[0] == "FORMAL"
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_confirmations WHERE state = 'FORMALIZED'"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_confirm_vs_revision_race_has_one_coherent_terminal_outcome(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    setup = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        acknowledge_candidate(*_args(context, setup, context["owner_id"]))
        acknowledge_candidate(*_args(context, setup, context["contractor_id"]))
        assert not confirm_candidate(*_args(context, setup, context["owner_id"])).formalized
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def confirming() -> None:
        connection = connect_database(context["instance_root"] / "app.sqlite3")
        try:
            barrier.wait()
            result = confirm_candidate(*_args(context, connection, context["contractor_id"]))
            value = ("confirm", "formal" if result.formalized else "pending")
        except FormalizationWorkflowError as error:
            value = ("confirm", error.code)
        finally:
            connection.close()
        with lock:
            outcomes.append(value)

    def revising() -> None:
        connection = connect_database(context["instance_root"] / "app.sqlite3")
        try:
            barrier.wait()
            request_candidate_revision(
                *_args(context, connection, context["owner_id"]),
                reason="A changed acceptance condition requires a new candidate.",
            )
            value = ("revision", "invalidated")
        except FormalizationWorkflowError as error:
            value = ("revision", error.code)
        finally:
            connection.close()
        with lock:
            outcomes.append(value)

    threads = [threading.Thread(target=confirming), threading.Thread(target=revising)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        state = connection.execute(
            "SELECT state FROM cases WHERE id = ?", (context["case_id"],)
        ).fetchone()[0]
        assert state in {"FORMAL", "JSON_B_REVIEW"}
        if state == "FORMAL":
            assert ("confirm", "formal") in outcomes
            assert any(value[0] == "revision" and value[1] != "invalidated" for value in outcomes)
        else:
            assert ("revision", "invalidated") in outcomes
            assert any(value[0] == "confirm" and value[1] != "formal" for value in outcomes)
    finally:
        connection.close()


def test_withdraw_vs_second_confirmation_is_serial_and_consistent(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    setup = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        acknowledge_candidate(*_args(context, setup, context["owner_id"]))
        acknowledge_candidate(*_args(context, setup, context["contractor_id"]))
        confirm_candidate(*_args(context, setup, context["owner_id"]))
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def withdrawing() -> None:
        connection = connect_database(context["instance_root"] / "app.sqlite3")
        try:
            barrier.wait()
            withdraw_confirmation(*_args(context, connection, context["owner_id"]))
            value = ("withdraw", "withdrawn")
        except FormalizationWorkflowError as error:
            value = ("withdraw", error.code)
        finally:
            connection.close()
        with lock:
            outcomes.append(value)

    def confirming() -> None:
        connection = connect_database(context["instance_root"] / "app.sqlite3")
        try:
            barrier.wait()
            result = confirm_candidate(*_args(context, connection, context["contractor_id"]))
            value = ("confirm", "formal" if result.formalized else "pending")
        except FormalizationWorkflowError as error:
            value = ("confirm", error.code)
        finally:
            connection.close()
        with lock:
            outcomes.append(value)

    threads = [threading.Thread(target=withdrawing), threading.Thread(target=confirming)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        state = connection.execute(
            "SELECT state FROM cases WHERE id = ?", (context["case_id"],)
        ).fetchone()[0]
        assert state in {"FORMAL", "CANDIDATE_REVIEW"}
        if state == "FORMAL":
            assert ("confirm", "formal") in outcomes
        else:
            rows = connection.execute(
                "SELECT actor_role, state FROM candidate_confirmations ORDER BY actor_role, confirmed_at"
            ).fetchall()
            assert ("client", "WITHDRAWN") in [tuple(row) for row in rows]
            assert ("contractor", "CURRENT") in [tuple(row) for row in rows]
    finally:
        connection.close()
