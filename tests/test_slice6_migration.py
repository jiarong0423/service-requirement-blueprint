from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

import app.services.formalization_workflow as formalization_workflow
from app.db import apply_migrations, connect_database
from tests.helpers import csrf
from tests.slice6_helpers import acknowledge, confirm, create_candidate_context
from tests.test_slice5_migration import (
    CANDIDATE_INSERT,
    SLICE4_MIGRATIONS,
    _candidate_values,
    _insert_candidate_artifacts,
    _insert_slice4_rows,
)


def _migration_root(project_root: Path, destination: Path, include_slice6: bool) -> Path:
    destination.mkdir()
    names = (*SLICE4_MIGRATIONS, "004_slice5_candidates.sql")
    if include_slice6:
        names = (*names, "005_slice6_formalization.sql")
    for name in names:
        shutil.copy2(project_root / "migrations" / name, destination / name)
    return destination


def test_populated_slice5_candidate_is_explicitly_invalidated_before_slice6(
    tmp_path: Path,
    project_root: Path,
) -> None:
    slice5_root = _migration_root(project_root, tmp_path / "slice5", False)
    slice6_root = _migration_root(project_root, tmp_path / "slice6", True)
    connection = connect_database(tmp_path / "migration.sqlite3")
    try:
        apply_migrations(connection, slice5_root)
        ids = _insert_slice4_rows(connection)
        candidate_id = str(uuid.uuid4())
        candidate_artifacts = _insert_candidate_artifacts(connection, ids, 1)
        connection.execute(
            CANDIDATE_INSERT,
            _candidate_values(ids, candidate_id, candidate_artifacts, 1, "CURRENT"),
        )
        connection.execute(
            "UPDATE cases SET state = 'CANDIDATE_REVIEW', active_candidate_id = ? WHERE id = ?",
            (candidate_id, ids["case"]),
        )
        before_paths = {
            row["id"]: (row["relative_path"], row["sha256"], row["byte_length"])
            for row in connection.execute(
                "SELECT id, relative_path, sha256, byte_length FROM artifacts"
            )
        }

        apply_migrations(connection, slice6_root)
        apply_migrations(connection, slice6_root)

        case = connection.execute(
            "SELECT state, active_candidate_id, formal_candidate_id FROM cases WHERE id = ?",
            (ids["case"],),
        ).fetchone()
        assert tuple(case) == ("JSON_B_REVIEW", None, None)
        candidate = connection.execute(
            "SELECT state, invalidated_at, invalidation_reason, formalized_at "
            "FROM candidate_versions WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        assert candidate["state"] == "INVALIDATED"
        assert candidate["invalidated_at"] is not None
        assert candidate["invalidation_reason"] == "fixed_document_lifecycle_wording_superseded"
        assert candidate["formalized_at"] is None
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_review_rounds"
        ).fetchone()[0] == 0
        lifecycles = {
            row["id"]: row["lifecycle"]
            for row in connection.execute("SELECT id, lifecycle FROM artifacts")
        }
        for artifact_id in candidate_artifacts:
            assert lifecycles[artifact_id] == "invalidated"
        assert lifecycles[ids["json_a"]] == "active"
        assert lifecycles[ids["json_b"]] == "active"
        after_paths = {
            row["id"]: (row["relative_path"], row["sha256"], row["byte_length"])
            for row in connection.execute(
                "SELECT id, relative_path, sha256, byte_length FROM artifacts"
            )
        }
        assert after_paths == before_paths
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = '005_slice6_formalization'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_slice6_schema_enforces_append_only_and_pointer_shapes(
    demo_runtime,
) -> None:
    _app, instance_root, _passwords = demo_runtime
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "candidate_review_rounds",
            "candidate_acknowledgements",
            "candidate_acknowledgement_renewals",
            "candidate_confirmations",
            "separate_proposal_requests",
        } <= tables
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE version = '007_acknowledgement_renewals'"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO cases(id, owner_id, title, state, content_revision, active_candidate_id, "
                "created_at, updated_at) SELECT ?, id, 'Invalid pointer', 'CANDIDATE_REVIEW', 1, NULL, ?, ? "
                "FROM users WHERE email_normalized = 'owner@example.test'",
                (str(uuid.uuid4()), "2026-10-06T00:00:00Z", "2026-10-06T00:00:00Z"),
            )
    finally:
        connection.close()


def test_migration_marks_pre_policy_reconfirmation_as_legacy(
    demo_runtime, project_root: Path
) -> None:
    context = create_candidate_context(demo_runtime)
    assert acknowledge(
        context["owner"], context["case_id"], context["candidate_id"]
    ).status_code == 302
    assert confirm(
        context["owner"], context["case_id"], context["candidate_id"]
    ).status_code == 302
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/candidates/{context['candidate_id']}/confirmations/withdraw",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": "1"},
    ).status_code == 302
    assert acknowledge(
        context["owner"], context["case_id"], context["candidate_id"]
    ).status_code == 302
    assert confirm(
        context["owner"], context["case_id"], context["candidate_id"]
    ).status_code == 302

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        connection.execute("DROP TABLE candidate_acknowledgement_renewals")
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = '007_acknowledgement_renewals'"
        )
        apply_migrations(connection, project_root / "migrations")
        apply_migrations(connection, project_root / "migrations")
        renewal = connection.execute(
            "SELECT renewal_kind, withdrawn_confirmation_id, acknowledgement_id "
            "FROM candidate_acknowledgement_renewals"
        ).fetchone()
        assert renewal is not None
        assert renewal["renewal_kind"] == "LEGACY_ACKNOWLEDGEMENT_REUSE"
        assert connection.execute(
            "SELECT state FROM candidate_confirmations "
            "WHERE id = ?",
            (renewal["withdrawn_confirmation_id"],),
        ).fetchone()[0] == "WITHDRAWN"
        assert connection.execute(
            "SELECT actor_role FROM candidate_acknowledgements WHERE id = ?",
            (renewal["acknowledgement_id"],),
        ).fetchone()[0] == "client"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_migration_leaves_only_final_withdrawal_outstanding_without_time_authorization(
    demo_runtime, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = create_candidate_context(demo_runtime)
    candidate_path = (
        f"/en/cases/{context['case_id']}/candidates/{context['candidate_id']}"
    )
    assert acknowledge(
        context["owner"], context["case_id"], context["candidate_id"]
    ).status_code == 302
    assert confirm(
        context["owner"], context["case_id"], context["candidate_id"]
    ).status_code == 302
    assert context["owner"].post(
        f"{candidate_path}/confirmations/withdraw",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": "1"},
    ).status_code == 302
    assert acknowledge(
        context["owner"], context["case_id"], context["candidate_id"]
    ).status_code == 302
    assert confirm(
        context["owner"], context["case_id"], context["candidate_id"]
    ).status_code == 302
    assert context["owner"].post(
        f"{candidate_path}/confirmations/withdraw",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": "1"},
    ).status_code == 302

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        connection.execute("DROP TABLE candidate_acknowledgement_renewals")
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = '007_acknowledgement_renewals'"
        )
        apply_migrations(connection, project_root / "migrations")
        apply_migrations(connection, project_root / "migrations")

        renewals = connection.execute(
            "SELECT renewal_kind, withdrawn_confirmation_id "
            "FROM candidate_acknowledgement_renewals"
        ).fetchall()
        assert [(row["renewal_kind"]) for row in renewals] == [
            "LEGACY_ACKNOWLEDGEMENT_REUSE"
        ]
        outstanding = connection.execute(
            "SELECT confirmation.id "
            "FROM candidate_confirmations AS confirmation "
            "LEFT JOIN candidate_acknowledgement_renewals AS renewal "
            "ON renewal.withdrawn_confirmation_id = confirmation.id "
            "WHERE confirmation.candidate_id = ? AND confirmation.actor_role = 'client' "
            "AND confirmation.state = 'WITHDRAWN' AND renewal.id IS NULL",
            (context["candidate_id"],),
        ).fetchall()
        assert len(outstanding) == 1
        assert outstanding[0]["id"] != renewals[0]["withdrawn_confirmation_id"]
        acknowledgement_id = connection.execute(
            "SELECT id FROM candidate_acknowledgements "
            "WHERE candidate_id = ? AND actor_role = 'client'",
            (context["candidate_id"],),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO candidate_acknowledgement_renewals("
                "id, acknowledgement_id, withdrawn_confirmation_id, renewal_kind, renewed_at) "
                "VALUES (?, ?, ?, 'EXPLICIT', 'not-a-timestamp')",
                (str(uuid.uuid4()), acknowledgement_id, outstanding[0]["id"]),
            )
    finally:
        connection.close()

    monkeypatch.setattr(
        formalization_workflow, "_now", lambda: "2000-01-01T00:00:00+00:00"
    )
    assert acknowledge(
        context["owner"], context["case_id"], context["candidate_id"]
    ).status_code == 302
    assert confirm(
        context["owner"], context["case_id"], context["candidate_id"]
    ).status_code == 302

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        renewal_rows = connection.execute(
            "SELECT renewal_kind, renewed_at FROM candidate_acknowledgement_renewals "
            "ORDER BY rowid"
        ).fetchall()
        assert [row["renewal_kind"] for row in renewal_rows] == [
            "LEGACY_ACKNOWLEDGEMENT_REUSE",
            "EXPLICIT",
        ]
        assert renewal_rows[1]["renewed_at"] == "2000-01-01T00:00:00+00:00"
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_confirmations "
            "WHERE candidate_id = ? AND actor_role = 'client' AND state = 'CURRENT'",
            (context["candidate_id"],),
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
