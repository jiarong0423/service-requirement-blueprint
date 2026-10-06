from __future__ import annotations

import sqlite3
import threading

import pytest

from app.db import connect_database
from app.services.candidate_workflow import CandidateWorkflowError
from app.services.formalization_workflow import (
    FormalizationWorkflowError,
    acknowledge_candidate,
    confirm_candidate,
    request_candidate_revision,
    withdraw_confirmation,
)
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
