from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.db import apply_migrations, connect_database
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
            "candidate_confirmations",
            "separate_proposal_requests",
        } <= tables
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO cases(id, owner_id, title, state, content_revision, active_candidate_id, "
                "created_at, updated_at) SELECT ?, id, 'Invalid pointer', 'CANDIDATE_REVIEW', 1, NULL, ?, ? "
                "FROM users WHERE email_normalized = 'owner@example.test'",
                (str(uuid.uuid4()), "2026-10-06T00:00:00Z", "2026-10-06T00:00:00Z"),
            )
    finally:
        connection.close()
