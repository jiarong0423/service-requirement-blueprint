from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.db import apply_migrations, connect_database


SLICE4_MIGRATIONS = (
    "001_initial.sql",
    "002_slice3_invitations.sql",
    "003_slice4_discussion_json_b.sql",
)

PRESERVED_TABLES = (
    "users",
    "cases",
    "requirement_drafts",
    "artifacts",
    "audit_events",
    "invitations",
    "json_a_source_verifications",
    "case_issues",
    "case_messages",
    "issue_events",
    "json_b_drafts",
    "json_b_private_exports",
    "json_b_versions",
)

REQUIRED_SLICE4_INDEXES_AND_TRIGGERS = {
    "idx_artifacts_case",
    "idx_invitations_one_pending_per_case",
    "idx_invitations_designated_user",
    "idx_invitations_case",
    "idx_json_a_source_verifications_case",
    "idx_case_issues_case_state",
    "idx_case_messages_case_created",
    "idx_case_messages_issue",
    "idx_issue_events_issue_revision",
    "idx_json_b_private_exports_one_active",
    "idx_json_b_private_exports_case_created",
    "idx_json_b_versions_one_current",
    "idx_json_b_versions_case_version",
    "case_messages_append_only_update",
    "case_messages_append_only_delete",
    "issue_events_append_only_update",
    "issue_events_append_only_delete",
}


def _copy_migrations(project_root: Path, destination: Path, names: tuple[str, ...]) -> None:
    destination.mkdir()
    for name in names:
        shutil.copy2(project_root / "migrations" / name, destination / name)


def _insert_slice4_rows(connection: sqlite3.Connection) -> dict[str, str]:
    ids = {name: str(uuid.uuid4()) for name in (
        "owner",
        "contractor",
        "case",
        "json_a",
        "json_b",
        "invitation",
        "issue",
        "message",
        "event",
        "json_b_document",
    )}
    now = "2026-10-06T02:00:00.000000Z"
    expiry = "2026-10-13T02:00:00.000000Z"
    for role in ("owner", "contractor"):
        connection.execute(
            "INSERT INTO users(id, email_normalized, display_name, password_hash, system_role, status, "
            "session_version, created_at, updated_at) VALUES (?, ?, ?, 'hash', 'member', 'active', 1, ?, ?)",
            (ids[role], f"{role}-slice5@example.test", role.title(), now, now),
        )
    connection.execute(
        "INSERT INTO cases(id, owner_id, contractor_id, title, state, content_revision, "
        "current_json_a_artifact_id, current_json_b_artifact_id, created_at, updated_at) "
        "VALUES (?, ?, ?, 'Slice 5 migration case', 'JSON_B_REVIEW', 11, ?, ?, ?, ?)",
        (ids["case"], ids["owner"], ids["contractor"], ids["json_a"], ids["json_b"], now, now),
    )
    connection.execute(
        "INSERT INTO requirement_drafts(case_id, data_json, completed_sections_json, updated_at) "
        "VALUES (?, '{\"sentinel\":\"draft\"}', '[1,2,3,4,5,6,7,8]', ?)",
        (ids["case"], now),
    )
    artifacts = (
        (ids["json_a"], ids["owner"], "json_a", "json-a/1.1", "a" * 64, 128),
        (ids["json_b"], ids["contractor"], "json_b", "json-b/1.0", "b" * 64, 256),
    )
    for artifact_id, author_id, kind, schema_version, digest, byte_length in artifacts:
        connection.execute(
            "INSERT INTO artifacts(id, case_id, author_id, kind, version_no, visibility, lifecycle, "
            "relative_path, sha256, byte_length, schema_version, created_at) "
            "VALUES (?, ?, ?, ?, 1, 'party_only', 'active', ?, ?, ?, ?, ?)",
            (
                artifact_id,
                ids["case"],
                author_id,
                kind,
                f"cases/{ids['case']}/artifacts/{artifact_id}/content.json",
                digest,
                byte_length,
                schema_version,
                now,
            ),
        )
    connection.execute(
        "INSERT INTO invitations(id, case_id, owner_id, designated_user_id, source_artifact_id, "
        "source_version_no, source_sha256, source_byte_length, source_schema_version, "
        "source_document_id, source_content_revision, token_hash, state, created_at, expires_at, "
        "accepted_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, 128, 'json-a/1.1', ?, 11, ?, "
        "'accepted', ?, ?, ?, ?)",
        (
            ids["invitation"],
            ids["case"],
            ids["owner"],
            ids["contractor"],
            ids["json_a"],
            "a" * 64,
            ids["json_a"],
            "c" * 64,
            now,
            expiry,
            now,
            now,
        ),
    )
    for actor_role, actor_id in (("owner", ids["owner"]), ("contractor", ids["contractor"])):
        connection.execute(
            "INSERT INTO json_a_source_verifications(id, invitation_id, case_id, actor_id, actor_role, "
            "artifact_id, supplied_sha256, supplied_byte_length, schema_version, document_id, "
            "document_version, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, 128, 'json-a/1.1', ?, 1, ?)",
            (
                str(uuid.uuid4()),
                ids["invitation"],
                ids["case"],
                actor_id,
                actor_role,
                ids["json_a"],
                "a" * 64,
                ids["json_a"],
                now,
            ),
        )
    connection.execute(
        "INSERT INTO audit_events(id, actor_id, case_id, entity_id, action, result, server_created_at, "
        "request_correlation_id, locale_code) VALUES (?, ?, ?, ?, 'slice4_sentinel', 'ok', ?, ?, 'en')",
        (str(uuid.uuid4()), ids["owner"], ids["case"], ids["json_b"], now, str(uuid.uuid4())),
    )
    connection.execute(
        "INSERT INTO case_issues(id, case_id, kind, summary, opened_by_id, opened_by_role, state, "
        "revision, created_at, updated_at) VALUES (?, ?, 'CLARIFICATION', 'Preserve issue', ?, "
        "'client', 'OPEN', 1, ?, ?)",
        (ids["issue"], ids["case"], ids["owner"], now, now),
    )
    connection.execute(
        "INSERT INTO case_messages(id, case_id, issue_id, author_id, author_role, idempotency_key, "
        "body, server_created_at) VALUES (?, ?, ?, ?, 'client', ?, 'Preserve message', ?)",
        (ids["message"], ids["case"], ids["issue"], ids["owner"], str(uuid.uuid4()), now),
    )
    connection.execute(
        "INSERT INTO issue_events(id, issue_id, actor_id, actor_role, action, to_state, note, "
        "issue_revision, server_created_at) VALUES (?, ?, ?, 'client', 'OPENED', 'OPEN', "
        "'Preserve event', 1, ?)",
        (ids["event"], ids["issue"], ids["owner"], now),
    )
    business_json = '{"sentinel":"private draft"}'
    connection.execute(
        "INSERT INTO json_b_drafts(case_id, contractor_id, document_id, target_version, "
        "source_artifact_id, source_document_id, source_version_no, source_schema_version, "
        "source_sha256, source_byte_length, data_json, content_revision, current_export_artifact_id, "
        "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, 1, 'json-a/1.1', ?, 128, ?, 1, ?, ?, ?)",
        (
            ids["case"],
            ids["contractor"],
            ids["json_b_document"],
            ids["json_a"],
            ids["json_a"],
            "a" * 64,
            business_json,
            ids["json_b"],
            now,
            now,
        ),
    )
    connection.execute(
        "INSERT INTO json_b_private_exports(artifact_id, case_id, contractor_id, draft_revision, "
        "target_version, source_artifact_id, source_document_id, source_version_no, "
        "source_schema_version, source_sha256, source_byte_length, state, created_at, submitted_at) "
        "VALUES (?, ?, ?, 1, 1, ?, ?, 1, 'json-a/1.1', ?, 128, 'SUBMITTED', ?, ?)",
        (
            ids["json_b"],
            ids["case"],
            ids["contractor"],
            ids["json_a"],
            ids["json_a"],
            "a" * 64,
            now,
            now,
        ),
    )
    connection.execute(
        "INSERT INTO json_b_versions(artifact_id, case_id, contractor_id, document_id, version_no, "
        "source_artifact_id, source_document_id, source_version_no, source_schema_version, "
        "source_sha256, source_byte_length, state, quote_expires_at, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, 1, 'json-a/1.1', ?, 128, 'CURRENT', ?, ?)",
        (
            ids["json_b"],
            ids["case"],
            ids["contractor"],
            ids["json_b_document"],
            ids["json_a"],
            ids["json_a"],
            "a" * 64,
            expiry,
            now,
        ),
    )
    return ids


def _table_snapshot(connection: sqlite3.Connection) -> dict[str, tuple[tuple[object, ...], ...]]:
    snapshot: dict[str, tuple[tuple[object, ...], ...]] = {}
    for table in PRESERVED_TABLES:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        rows = connection.execute(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid"
        ).fetchall()
        snapshot[table] = tuple(tuple(row) for row in rows)
    return snapshot


def _foreign_key_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    return {
        table: tuple(tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({table})"))
        for table in PRESERVED_TABLES
    }


def _schema_object_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, str, str]]:
    return {
        row[1]: (row[0], row[2], row[3])
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('index', 'trigger') AND sql IS NOT NULL"
        )
        if row[2] in PRESERVED_TABLES
    }


def _insert_candidate_artifacts(
    connection: sqlite3.Connection,
    ids: dict[str, str],
    version: int,
) -> tuple[str, str, str]:
    artifact_ids = tuple(str(uuid.uuid4()) for _ in range(3))
    now = f"2026-10-06T03:00:0{version}.000000Z"
    for artifact_id, kind, filename, digest in zip(
        artifact_ids,
        ("candidate_manifest", "candidate_pdf", "candidate_svg"),
        ("manifest.json", "document.pdf", "diagram.svg"),
        ("d" * 64, "e" * 64, "f" * 64),
        strict=True,
    ):
        connection.execute(
            "INSERT INTO artifacts(id, case_id, author_id, kind, version_no, visibility, lifecycle, "
            "relative_path, sha256, byte_length, schema_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'party_only', 'active', ?, ?, 512, 'candidate/1.0', ?)",
            (
                artifact_id,
                ids["case"],
                ids["contractor"],
                kind,
                version,
                f"cases/{ids['case']}/artifacts/{artifact_id}/{filename}",
                digest,
                now,
            ),
        )
    return artifact_ids


def _candidate_values(
    ids: dict[str, str],
    candidate_id: str,
    artifacts: tuple[str, str, str],
    version: int,
    state: str,
) -> tuple[object, ...]:
    return (
        candidate_id,
        ids["case"],
        version,
        ids["contractor"],
        state,
        ids["json_a"],
        "a" * 64,
        128,
        ids["json_b"],
        "b" * 64,
        256,
        artifacts[0],
        artifacts[1],
        artifacts[2],
        "candidate-manifest/1.0",
        "candidate-document/1.0",
        "1" * 64,
        "2" * 64,
        "2026-10-06T03:00:00.000000Z",
        "2026-10-06T03:01:00.000000Z" if state == "SUPERSEDED" else None,
    )


CANDIDATE_INSERT = (
    "INSERT INTO candidate_versions(id, case_id, version_no, created_by_id, state, "
    "json_a_artifact_id, json_a_sha256, json_a_byte_length, json_b_artifact_id, json_b_sha256, "
    "json_b_byte_length, manifest_artifact_id, pdf_artifact_id, svg_artifact_id, "
    "manifest_schema_version, template_version, catalog_sha256, label_map_sha256, created_at, "
    "superseded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def test_slice5_populated_upgrade_preserves_slice4_and_candidate_constraints(
    tmp_path: Path,
    project_root: Path,
) -> None:
    slice4_root = tmp_path / "slice4"
    full_root = tmp_path / "full"
    _copy_migrations(project_root, slice4_root, SLICE4_MIGRATIONS)
    _copy_migrations(
        project_root,
        full_root,
        (*SLICE4_MIGRATIONS, "004_slice5_candidates.sql"),
    )
    connection = connect_database(tmp_path / "slice5.sqlite3")
    try:
        apply_migrations(connection, slice4_root)
        ids = _insert_slice4_rows(connection)
        before = _table_snapshot(connection)
        foreign_keys_before = _foreign_key_snapshot(connection)
        objects_before = _schema_object_snapshot(connection)

        apply_migrations(connection, full_root)
        apply_migrations(connection, full_root)

        assert _table_snapshot(connection) == before
        assert _foreign_key_snapshot(connection) == foreign_keys_before
        objects_after = _schema_object_snapshot(connection)
        assert {name: objects_after[name] for name in objects_before} == objects_before
        assert [
            row[1] for row in connection.execute("PRAGMA table_info(candidate_versions)")
        ] == [
            "id",
            "case_id",
            "version_no",
            "created_by_id",
            "state",
            "json_a_artifact_id",
            "json_a_sha256",
            "json_a_byte_length",
            "json_b_artifact_id",
            "json_b_sha256",
            "json_b_byte_length",
            "manifest_artifact_id",
            "pdf_artifact_id",
            "svg_artifact_id",
            "manifest_schema_version",
            "template_version",
            "catalog_sha256",
            "label_map_sha256",
            "created_at",
            "superseded_at",
        ]
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == [
            "001_initial",
            "002_slice3_invitations",
            "003_slice4_discussion_json_b",
            "004_slice5_candidates",
        ]
        preserved_objects = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger')"
            )
        }
        assert REQUIRED_SLICE4_INDEXES_AND_TRIGGERS <= preserved_objects
        assert {
            "idx_cases_owner",
            "idx_cases_contractor",
            "idx_candidate_versions_one_current",
            "idx_candidate_versions_case_version",
        } <= preserved_objects
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        with pytest.raises(sqlite3.IntegrityError, match="candidate_pointer_invalid"):
            connection.execute(
                "UPDATE cases SET state = 'CANDIDATE_REVIEW' WHERE id = ?",
                (ids["case"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE cases SET state = 'FORMAL' WHERE id = ?", (ids["case"],))

        first_candidate = str(uuid.uuid4())
        first_artifacts = _insert_candidate_artifacts(connection, ids, 1)
        connection.execute(
            CANDIDATE_INSERT,
            _candidate_values(ids, first_candidate, first_artifacts, 1, "CURRENT"),
        )
        connection.execute(
            "UPDATE cases SET state = 'CANDIDATE_REVIEW', active_candidate_id = ? WHERE id = ?",
            (first_candidate, ids["case"]),
        )

        second_candidate = str(uuid.uuid4())
        second_artifacts = _insert_candidate_artifacts(connection, ids, 2)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                CANDIDATE_INSERT,
                _candidate_values(ids, second_candidate, second_artifacts, 2, "CURRENT"),
            )
        connection.execute(
            CANDIDATE_INSERT,
            _candidate_values(ids, second_candidate, second_artifacts, 2, "SUPERSEDED"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            bad = list(_candidate_values(
                ids,
                str(uuid.uuid4()),
                _insert_candidate_artifacts(connection, ids, 3),
                3,
                "CURRENT",
            ))
            bad[6] = "not-a-sha256"
            connection.execute(CANDIDATE_INSERT, tuple(bad))
        with pytest.raises(sqlite3.IntegrityError):
            missing_case = list(_candidate_values(
                ids,
                str(uuid.uuid4()),
                _insert_candidate_artifacts(connection, ids, 4),
                4,
                "SUPERSEDED",
            ))
            missing_case[1] = str(uuid.uuid4())
            connection.execute(CANDIDATE_INSERT, tuple(missing_case))

        assert connection.execute(
            "SELECT active_candidate_id FROM cases WHERE id = ?", (ids["case"],)
        ).fetchone()[0] == first_candidate
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_versions WHERE case_id = ?", (ids["case"],)
        ).fetchone()[0] == 2
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        with pytest.raises(sqlite3.IntegrityError, match="case_messages_append_only"):
            connection.execute(
                "UPDATE case_messages SET body = 'Changed' WHERE id = ?", (ids["message"],)
            )
        with pytest.raises(sqlite3.IntegrityError, match="issue_events_append_only"):
            connection.execute("DELETE FROM issue_events WHERE id = ?", (ids["event"],))
    finally:
        connection.close()


def test_failed_slice5_migration_rolls_back_case_rebuild_and_candidate_table(
    tmp_path: Path,
    project_root: Path,
) -> None:
    slice4_root = tmp_path / "slice4"
    broken_root = tmp_path / "broken"
    _copy_migrations(project_root, slice4_root, SLICE4_MIGRATIONS)
    _copy_migrations(project_root, broken_root, SLICE4_MIGRATIONS)
    migration = (project_root / "migrations/004_slice5_candidates.sql").read_text(encoding="utf-8")
    (broken_root / "004_slice5_candidates.sql").write_text(
        migration + "\nINSERT INTO deliberately_missing_table(value) VALUES ('fail');\n",
        encoding="utf-8",
    )

    connection = connect_database(tmp_path / "rollback.sqlite3")
    try:
        apply_migrations(connection, slice4_root)
        ids = _insert_slice4_rows(connection)
        before = _table_snapshot(connection)
        foreign_keys_before = _foreign_key_snapshot(connection)
        objects_before = _schema_object_snapshot(connection)

        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, broken_root)

        assert _table_snapshot(connection) == before
        assert _foreign_key_snapshot(connection) == foreign_keys_before
        assert _schema_object_snapshot(connection) == objects_before
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'candidate_versions'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = '004_slice5_candidates'"
        ).fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE cases SET state = 'CANDIDATE_REVIEW' WHERE id = ?", (ids["case"],)
            )
        with pytest.raises(sqlite3.IntegrityError, match="case_messages_append_only"):
            connection.execute(
                "UPDATE case_messages SET body = 'Changed' WHERE id = ?", (ids["message"],)
            )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
