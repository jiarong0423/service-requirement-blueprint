from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import threading
import uuid
from pathlib import Path

import pytest

from app import create_app
import app.cli as cli_module
from app.cli import initialize_demo, migrate_demo
from app.db import apply_migrations, connect_database


def _populate_slice3_database(connection: sqlite3.Connection) -> dict[str, str]:
    identifiers = {
        "owner": str(uuid.uuid4()),
        "contractor": str(uuid.uuid4()),
        "case": str(uuid.uuid4()),
        "artifact": str(uuid.uuid4()),
        "invitation": str(uuid.uuid4()),
    }
    now = "2026-10-06T00:00:00.000000Z"
    expires = "2026-10-13T00:00:00.000000Z"
    for key, email in (("owner", "owner@example.test"), ("contractor", "contractor@example.test")):
        connection.execute(
            "INSERT INTO users(id, email_normalized, display_name, password_hash, system_role, status, "
            "session_version, created_at, updated_at) VALUES (?, ?, ?, 'hash', 'member', 'active', 1, ?, ?)",
            (identifiers[key], email, key.title(), now, now),
        )
    connection.execute(
        "INSERT INTO cases(id, owner_id, contractor_id, title, state, content_revision, "
        "current_json_a_artifact_id, created_at, updated_at) "
        "VALUES (?, ?, ?, 'Preserved Case', 'CASE_ACTIVE', 9, ?, ?, ?)",
        (identifiers["case"], identifiers["owner"], identifiers["contractor"], identifiers["artifact"], now, now),
    )
    connection.execute(
        "INSERT INTO requirement_drafts(case_id, data_json, completed_sections_json, updated_at) "
        "VALUES (?, '{}', '[1,2,3,4,5,6,7,8]', ?)",
        (identifiers["case"], now),
    )
    connection.execute(
        "INSERT INTO artifacts(id, case_id, author_id, kind, version_no, visibility, lifecycle, "
        "relative_path, sha256, byte_length, schema_version, created_at) "
        "VALUES (?, ?, ?, 'json_a', 1, 'party_only', 'active', ?, ?, 128, 'json-a/1.1', ?)",
        (
            identifiers["artifact"],
            identifiers["case"],
            identifiers["owner"],
            f"cases/{identifiers['case']}/artifacts/{identifiers['artifact']}/content.json",
            "a" * 64,
            now,
        ),
    )
    connection.execute(
        "INSERT INTO invitations(id, case_id, owner_id, designated_user_id, source_artifact_id, "
        "source_version_no, source_sha256, source_byte_length, source_schema_version, "
        "source_document_id, source_content_revision, token_hash, state, created_at, expires_at, "
        "accepted_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, 128, 'json-a/1.1', ?, 9, ?, "
        "'accepted', ?, ?, ?, ?)",
        (
            identifiers["invitation"],
            identifiers["case"],
            identifiers["owner"],
            identifiers["contractor"],
            identifiers["artifact"],
            "a" * 64,
            identifiers["artifact"],
            "b" * 64,
            now,
            expires,
            now,
            now,
        ),
    )
    for actor_role, actor_id in (("owner", identifiers["owner"]), ("contractor", identifiers["contractor"])):
        connection.execute(
            "INSERT INTO json_a_source_verifications(id, invitation_id, case_id, actor_id, actor_role, "
            "artifact_id, supplied_sha256, supplied_byte_length, schema_version, document_id, "
            "document_version, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, 128, 'json-a/1.1', ?, 1, ?)",
            (
                str(uuid.uuid4()),
                identifiers["invitation"],
                identifiers["case"],
                actor_id,
                actor_role,
                identifiers["artifact"],
                "a" * 64,
                identifiers["artifact"],
                now,
            ),
        )
    connection.execute(
        "INSERT INTO audit_events(id, actor_id, case_id, entity_id, action, result, server_created_at, "
        "request_correlation_id, locale_code) VALUES (?, ?, ?, ?, 'slice3', 'ok', ?, ?, 'en')",
        (str(uuid.uuid4()), identifiers["owner"], identifiers["case"], identifiers["artifact"], now, str(uuid.uuid4())),
    )
    return identifiers


def test_migrate_demo_creates_one_private_backup_then_becomes_noop(
    tmp_path: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_project = tmp_path / "project"
    migration_root = isolated_project / "migrations"
    migration_root.mkdir(parents=True)
    shutil.copy2(project_root / "migrations/001_initial.sql", migration_root / "001_initial.sql")
    instance_root = tmp_path / "instance"
    monkeypatch.setenv("POC_INSTANCE_PATH", str(instance_root))
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", "owner-password-for-test")
    monkeypatch.setenv("DEMO_CONTRACTOR_PASSWORD", "contractor-password-for-test")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "admin-password-for-test")
    initialize_demo(isolated_project)

    shutil.copy2(
        project_root / "migrations/002_slice3_invitations.sql",
        migration_root / "002_slice3_invitations.sql",
    )
    migrated = migrate_demo(isolated_project)
    assert migrated["status"] == "migrated"
    assert migrated["versions"] == ["001_initial", "002_slice3_invitations"]
    backup_path = instance_root / "backups" / migrated["backup_file"]
    assert backup_path.is_file()
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600

    backups_before = sorted((instance_root / "backups").iterdir())
    repeated = migrate_demo(isolated_project)
    assert repeated == {
        "status": "already_current",
        "backup_file": None,
        "versions": ["001_initial", "002_slice3_invitations"],
    }
    assert sorted((instance_root / "backups").iterdir()) == backups_before


def test_runtime_fails_closed_when_database_is_missing_a_required_migration(
    tmp_path: Path, project_root: Path
) -> None:
    database = tmp_path / "stale.sqlite3"
    data_root = tmp_path / "data"
    data_root.mkdir(mode=0o700)
    isolated_migrations = tmp_path / "migrations"
    isolated_migrations.mkdir()
    shutil.copy2(project_root / "migrations/001_initial.sql", isolated_migrations / "001_initial.sql")

    from app.db import apply_migrations

    connection = connect_database(database)
    try:
        apply_migrations(connection, isolated_migrations)
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="migration_missing"):
        create_app(
            {
                "SECRET_KEY": os.urandom(32),
                "INSTANCE_ROOT": tmp_path,
                "DATABASE": database,
                "DATA_ROOT": data_root,
                "SESSION_COOKIE_HTTPONLY": True,
                "SESSION_COOKIE_SAMESITE": "Lax",
                "SESSION_COOKIE_SECURE": False,
                "SESSION_REFRESH_EACH_REQUEST": False,
            }
        )


def test_failed_post_migration_validation_rolls_back_canonical_database(
    tmp_path: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_project = tmp_path / "project"
    migration_root = isolated_project / "migrations"
    migration_root.mkdir(parents=True)
    shutil.copy2(project_root / "migrations/001_initial.sql", migration_root / "001_initial.sql")
    instance_root = tmp_path / "instance"
    monkeypatch.setenv("POC_INSTANCE_PATH", str(instance_root))
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", "owner-password-for-test")
    monkeypatch.setenv("DEMO_CONTRACTOR_PASSWORD", "contractor-password-for-test")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "admin-password-for-test")
    initialize_demo(isolated_project)
    shutil.copy2(
        project_root / "migrations/002_slice3_invitations.sql",
        migration_root / "002_slice3_invitations.sql",
    )
    (migration_root / "003_invalid.sql").write_text(
        "CREATE TABLE invalid_reference (user_id TEXT REFERENCES users(id));\n"
        "INSERT INTO invalid_reference(user_id) VALUES ('missing-user');\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="migration_foreign_key_failed"):
        migrate_demo(isolated_project)

    connection = connect_database(instance_root / "app.sqlite3")
    try:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
        assert versions == ["001_initial"]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'invitations'"
        ).fetchone() is None
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
    backups = list((instance_root / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_migration_backup_is_a_locked_pre_migration_snapshot(
    tmp_path: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_project = tmp_path / "project"
    migration_root = isolated_project / "migrations"
    migration_root.mkdir(parents=True)
    shutil.copy2(project_root / "migrations/001_initial.sql", migration_root / "001_initial.sql")
    instance_root = tmp_path / "instance"
    monkeypatch.setenv("POC_INSTANCE_PATH", str(instance_root))
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", "owner-password-for-test")
    monkeypatch.setenv("DEMO_CONTRACTOR_PASSWORD", "contractor-password-for-test")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "admin-password-for-test")
    initialize_demo(isolated_project)

    database_path = instance_root / "app.sqlite3"
    connection = connect_database(database_path)
    try:
        pre_migration_users = [
            tuple(row)
            for row in connection.execute(
                "SELECT id, email_normalized, display_name, system_role, status, session_version, "
                "created_at, updated_at FROM users ORDER BY id"
            )
        ]
    finally:
        connection.close()

    shutil.copy2(
        project_root / "migrations/002_slice3_invitations.sql",
        migration_root / "002_slice3_invitations.sql",
    )
    lock_held = threading.Event()
    writer_attempting = threading.Event()
    writer_committed = threading.Event()
    writer_errors: list[BaseException] = []
    original_backup = cli_module._create_locked_database_backup

    def observed_backup(source_path: Path, backup_path: Path) -> None:
        lock_held.set()
        assert writer_attempting.wait(timeout=2.0)
        original_backup(source_path, backup_path)
        assert not writer_committed.is_set()

    monkeypatch.setattr(cli_module, "_create_locked_database_backup", observed_backup)

    def competing_writer() -> None:
        try:
            assert lock_held.wait(timeout=2.0)
            writer_attempting.set()
            writer_connection = connect_database(database_path)
            try:
                writer_connection.execute(
                    "UPDATE users SET display_name = 'Concurrent Writer' "
                    "WHERE email_normalized = 'owner@example.test'"
                )
            finally:
                writer_connection.close()
            writer_committed.set()
        except BaseException as error:
            writer_errors.append(error)

    writer = threading.Thread(target=competing_writer, name="migration-competing-writer")
    writer.start()
    migrated = migrate_demo(isolated_project)
    writer.join(timeout=5.0)
    assert not writer.is_alive()
    assert writer_errors == []
    assert writer_committed.is_set()

    backup_path = instance_root / "backups" / migrated["backup_file"]
    backup = sqlite3.connect(backup_path, autocommit=True)
    try:
        assert [
            row[0] for row in backup.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == ["001_initial"]
        backup_users = list(
            backup.execute(
                "SELECT id, email_normalized, display_name, system_role, status, session_version, "
                "created_at, updated_at FROM users ORDER BY id"
            )
        )
        assert backup_users == pre_migration_users
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        backup.close()

    canonical = connect_database(database_path)
    try:
        assert canonical.execute(
            "SELECT display_name FROM users WHERE email_normalized = 'owner@example.test'"
        ).fetchone()[0] == "Concurrent Writer"
        assert [
            row[0]
            for row in canonical.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == ["001_initial", "002_slice3_invitations"]
    finally:
        canonical.close()


def test_slice4_populated_migration_preserves_slice3_and_is_idempotent(
    tmp_path: Path, project_root: Path
) -> None:
    first_root = tmp_path / "first"
    full_root = tmp_path / "full"
    first_root.mkdir()
    full_root.mkdir()
    for name in ("001_initial.sql", "002_slice3_invitations.sql"):
        shutil.copy2(project_root / "migrations" / name, first_root / name)
        shutil.copy2(project_root / "migrations" / name, full_root / name)
    shutil.copy2(
        project_root / "migrations/003_slice4_discussion_json_b.sql",
        full_root / "003_slice4_discussion_json_b.sql",
    )

    database = tmp_path / "slice4.sqlite3"
    connection = connect_database(database)
    try:
        apply_migrations(connection, first_root)
        identifiers = _populate_slice3_database(connection)
        apply_migrations(connection, full_root)
        apply_migrations(connection, full_root)

        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == ["001_initial", "002_slice3_invitations", "003_slice4_discussion_json_b"]
        assert tuple(
            connection.execute(
                "SELECT owner_id, contractor_id, title, state, content_revision, current_json_a_artifact_id "
                "FROM cases WHERE id = ?",
                (identifiers["case"],),
            ).fetchone()
        ) == (
            identifiers["owner"],
            identifiers["contractor"],
            "Preserved Case",
            "CASE_ACTIVE",
            9,
            identifiers["artifact"],
        )
        assert connection.execute("SELECT COUNT(*) FROM requirement_drafts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM invitations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM json_a_source_verifications").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        connection.execute(
            "UPDATE cases SET state = 'JSON_B_PRIVATE_DRAFT' WHERE id = ?",
            (identifiers["case"],),
        )
        connection.execute(
            "UPDATE cases SET state = 'JSON_B_REVIEW' WHERE id = ?",
            (identifiers["case"],),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE cases SET state = 'CANDIDATE_REVIEW' WHERE id = ?",
                (identifiers["case"],),
            )

        issue_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        now = "2026-10-06T01:00:00.000000Z"
        connection.execute(
            "INSERT INTO case_issues(id, case_id, kind, summary, opened_by_id, opened_by_role, state, "
            "revision, created_at, updated_at) VALUES (?, ?, 'CLARIFICATION', 'Clarify scope', ?, "
            "'client', 'OPEN', 1, ?, ?)",
            (issue_id, identifiers["case"], identifiers["owner"], now, now),
        )
        connection.execute(
            "INSERT INTO case_messages(id, case_id, issue_id, author_id, author_role, "
            "idempotency_key, body, server_created_at) "
            "VALUES (?, ?, ?, ?, 'client', ?, 'Original message', ?)",
            (
                message_id,
                identifiers["case"],
                issue_id,
                identifiers["owner"],
                str(uuid.uuid4()),
                now,
            ),
        )
        connection.execute(
            "INSERT INTO issue_events(id, issue_id, actor_id, actor_role, action, to_state, note, "
            "issue_revision, server_created_at) VALUES (?, ?, ?, 'client', 'OPENED', 'OPEN', '', 1, ?)",
            (event_id, issue_id, identifiers["owner"], now),
        )
        with pytest.raises(sqlite3.IntegrityError, match="case_messages_append_only"):
            connection.execute("UPDATE case_messages SET body = 'Changed' WHERE id = ?", (message_id,))
        with pytest.raises(sqlite3.IntegrityError, match="case_messages_append_only"):
            connection.execute("DELETE FROM case_messages WHERE id = ?", (message_id,))
        with pytest.raises(sqlite3.IntegrityError, match="issue_events_append_only"):
            connection.execute("UPDATE issue_events SET note = 'Changed' WHERE id = ?", (event_id,))
        with pytest.raises(sqlite3.IntegrityError, match="issue_events_append_only"):
            connection.execute("DELETE FROM issue_events WHERE id = ?", (event_id,))
    finally:
        connection.close()


def test_failed_slice4_migration_rolls_back_rebuild_and_new_tables(
    tmp_path: Path, project_root: Path
) -> None:
    first_root = tmp_path / "first"
    broken_root = tmp_path / "broken"
    first_root.mkdir()
    broken_root.mkdir()
    for name in ("001_initial.sql", "002_slice3_invitations.sql"):
        shutil.copy2(project_root / "migrations" / name, first_root / name)
        shutil.copy2(project_root / "migrations" / name, broken_root / name)
    migration = (project_root / "migrations/003_slice4_discussion_json_b.sql").read_text(encoding="utf-8")
    (broken_root / "003_slice4_discussion_json_b.sql").write_text(
        migration + "\nINSERT INTO deliberately_missing_table(value) VALUES ('fail');\n",
        encoding="utf-8",
    )

    database = tmp_path / "rollback.sqlite3"
    connection = connect_database(database)
    try:
        apply_migrations(connection, first_root)
        identifiers = _populate_slice3_database(connection)
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, broken_root)

        assert connection.execute(
            "SELECT state FROM cases WHERE id = ?", (identifiers["case"],)
        ).fetchone()[0] == "CASE_ACTIVE"
        assert connection.execute("SELECT COUNT(*) FROM invitations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM json_a_source_verifications").fetchone()[0] == 2
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'case_messages'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = '003_slice4_discussion_json_b'"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_apply_migrations_rolls_back_deferred_foreign_key_failure(tmp_path: Path) -> None:
    migration_root = tmp_path / "migrations"
    migration_root.mkdir()
    (migration_root / "001_deferred_failure.sql").write_text(
        "CREATE TABLE parent (id TEXT PRIMARY KEY);\n"
        "CREATE TABLE child (parent_id TEXT NOT NULL REFERENCES parent(id) "
        "DEFERRABLE INITIALLY DEFERRED);\n"
        "INSERT INTO child(parent_id) VALUES ('missing-parent');\n",
        encoding="utf-8",
    )
    connection = connect_database(tmp_path / "deferred.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            apply_migrations(connection, migration_root)
        assert connection.in_transaction is False
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'child'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_migration_parser_keeps_trigger_body_as_one_statement(tmp_path: Path) -> None:
    migration_root = tmp_path / "migrations"
    migration_root.mkdir()
    (migration_root / "001_trigger.sql").write_text(
        "CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL);\n"
        "CREATE TRIGGER records_append_only\n"
        "BEFORE UPDATE ON records\n"
        "BEGIN\n"
        "    SELECT CASE WHEN NEW.value <> OLD.value THEN RAISE(ABORT, 'append_only') END;\n"
        "END;\n",
        encoding="utf-8",
    )
    connection = connect_database(tmp_path / "parser.sqlite3")
    try:
        apply_migrations(connection, migration_root)
        connection.execute("INSERT INTO records(value) VALUES ('original')")
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            connection.execute("UPDATE records SET value = 'changed' WHERE id = 1")
        apply_migrations(connection, migration_root)
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    finally:
        connection.close()


def test_migration_parser_rejects_incomplete_statement(tmp_path: Path) -> None:
    migration_root = tmp_path / "migrations"
    migration_root.mkdir()
    (migration_root / "001_incomplete.sql").write_text(
        "CREATE TABLE incomplete (id INTEGER PRIMARY KEY);\n"
        "CREATE TRIGGER incomplete_trigger BEFORE UPDATE ON incomplete BEGIN\n",
        encoding="utf-8",
    )
    connection = connect_database(tmp_path / "incomplete.sqlite3")
    try:
        with pytest.raises(sqlite3.OperationalError, match="incomplete input"):
            apply_migrations(connection, migration_root)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'incomplete'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = '001_incomplete'"
        ).fetchone()[0] == 0
    finally:
        connection.close()
