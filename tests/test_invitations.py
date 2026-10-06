from __future__ import annotations

import base64
import hashlib
import json
import queue
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import apply_migrations, connect_database
from app.services.invitation_workflow import (
    INVITATION_TTL,
    InvitationWorkflowError,
    accept_invitation,
    create_invitation,
    import_json_a,
    preview_invitation,
    reject_invitation,
    revoke_invitation,
)
from app.services.json_contracts import decode_strict_json, encode_json_once
from tests.helpers import complete_requirement, csrf, login


FIXED_NOW = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)


def _prepare_source(demo_runtime):
    app, instance_root, passwords = demo_runtime
    with app.test_client() as client:
        assert login(client, "en", "owner@example.test", passwords["owner"]).status_code == 302
        case_id = complete_requirement(client, instance_root)
        connection = connect_database(instance_root / "app.sqlite3")
        try:
            revision = connection.execute(
                "SELECT content_revision FROM cases WHERE id = ?", (case_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        generated = client.post(
            f"/en/owner/requirements/{case_id}/json-a",
            data={
                "csrf_token": csrf(client),
                "expected_revision": revision,
                "review_confirmed": "yes",
            },
        )
        assert generated.status_code == 302
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        identities = {
            row["email_normalized"]: row["id"]
            for row in connection.execute("SELECT id, email_normalized FROM users")
        }
        artifact = connection.execute(
            "SELECT id, relative_path, sha256, byte_length FROM artifacts "
            "WHERE case_id = ? AND kind = 'json_a' AND lifecycle = 'active'",
            (case_id,),
        ).fetchone()
        artifact_data = dict(artifact)
    finally:
        connection.close()

    raw = (instance_root / "data" / artifact_data["relative_path"]).read_bytes()
    return {
        "app": app,
        "instance_root": instance_root,
        "database": instance_root / "app.sqlite3",
        "data_root": instance_root / "data",
        "schema_path": Path(app.config["SCHEMA_PATH"]),
        "case_id": case_id,
        "owner_id": identities["owner@example.test"],
        "contractor_id": identities["contractor@example.test"],
        "admin_id": identities["admin@example.test"],
        "artifact": artifact_data,
        "raw": raw,
    }


def _issue_invitation(context, *, now: datetime = FIXED_NOW):
    connection = connect_database(context["database"])
    try:
        return create_invitation(
            connection,
            context["data_root"],
            context["schema_path"],
            context["case_id"],
            context["owner_id"],
            context["contractor_id"],
            context["raw"],
            now=now,
        )
    finally:
        connection.close()


def test_populated_001_migrates_without_losing_relationships_or_legacy_data(tmp_path: Path, project_root: Path) -> None:
    first_root = tmp_path / "first"
    full_root = tmp_path / "full"
    first_root.mkdir()
    full_root.mkdir()
    shutil.copy2(project_root / "migrations/001_initial.sql", first_root / "001_initial.sql")
    shutil.copy2(project_root / "migrations/001_initial.sql", full_root / "001_initial.sql")
    shutil.copy2(
        project_root / "migrations/002_slice3_invitations.sql",
        full_root / "002_slice3_invitations.sql",
    )
    database = tmp_path / "migration.sqlite3"
    connection = connect_database(database)
    owner_id = str(uuid.uuid4())
    case_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())
    try:
        apply_migrations(connection, first_root)
        connection.execute(
            "INSERT INTO users(id, email_normalized, display_name, password_hash, system_role, status, "
            "session_version, created_at, updated_at) VALUES (?, 'owner@example.test', 'Owner', 'hash', "
            "'member', 'active', 1, 'before', 'before')",
            (owner_id,),
        )
        connection.execute(
            "INSERT INTO cases(id, owner_id, title, state, content_revision, current_json_a_artifact_id, "
            "invitation_token_hash, invitation_email_normalized, invitation_state, invitation_expires_at, "
            "created_at, updated_at) VALUES (?, ?, 'Preserved title', 'REQUIREMENT_DRAFT', 7, ?, "
            "'legacy-authority', 'legacy@example.test', 'legacy', 'legacy-time', 'before', 'before')",
            (case_id, owner_id, artifact_id),
        )
        connection.execute(
            "INSERT INTO requirement_drafts(case_id, data_json, completed_sections_json, updated_at) "
            "VALUES (?, '{\"preserved\":true}', '[1,2]', 'before')",
            (case_id,),
        )
        connection.execute(
            "INSERT INTO artifacts(id, case_id, author_id, kind, version_no, visibility, lifecycle, "
            "relative_path, sha256, byte_length, schema_version, created_at) "
            "VALUES (?, ?, ?, 'json_a', 1, 'private', 'active', 'relative/content.json', ?, 3, "
            "'json-a/1.1', 'before')",
            (artifact_id, case_id, owner_id, hashlib.sha256(b"raw").hexdigest()),
        )
        apply_migrations(connection, full_root)
        apply_migrations(connection, full_root)

        case = connection.execute(
            "SELECT id, owner_id, title, state, content_revision, current_json_a_artifact_id FROM cases"
        ).fetchone()
        assert tuple(case) == (
            case_id,
            owner_id,
            "Preserved title",
            "REQUIREMENT_DRAFT",
            7,
            artifact_id,
        )
        assert connection.execute("SELECT data_json FROM requirement_drafts").fetchone()[0] == '{"preserved":true}'
        assert connection.execute("SELECT id, sha256 FROM artifacts").fetchone()[0] == artifact_id
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(cases)")}
        assert {
            "invitation_token_hash",
            "invitation_email_normalized",
            "invitation_state",
            "invitation_expires_at",
        }.isdisjoint(columns)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")] == [
            "001_initial",
            "002_slice3_invitations",
        ]
    finally:
        connection.close()


def test_failed_002_migration_rolls_back_schema_and_data(tmp_path: Path, project_root: Path) -> None:
    first_root = tmp_path / "first"
    broken_root = tmp_path / "broken"
    first_root.mkdir()
    broken_root.mkdir()
    shutil.copy2(project_root / "migrations/001_initial.sql", first_root / "001_initial.sql")
    shutil.copy2(project_root / "migrations/001_initial.sql", broken_root / "001_initial.sql")
    migration = (project_root / "migrations/002_slice3_invitations.sql").read_text(encoding="utf-8")
    (broken_root / "002_slice3_invitations.sql").write_text(
        migration + "\nINSERT INTO deliberately_missing_table(value) VALUES ('fail');\n",
        encoding="utf-8",
    )
    database = tmp_path / "rollback.sqlite3"
    connection = connect_database(database)
    owner_id = str(uuid.uuid4())
    case_id = str(uuid.uuid4())
    try:
        apply_migrations(connection, first_root)
        connection.execute(
            "INSERT INTO users(id, email_normalized, display_name, password_hash, system_role, status, "
            "session_version, created_at, updated_at) VALUES (?, 'owner@example.test', 'Owner', 'hash', "
            "'member', 'active', 1, 'before', 'before')",
            (owner_id,),
        )
        connection.execute(
            "INSERT INTO cases(id, owner_id, title, state, content_revision, invitation_token_hash, "
            "created_at, updated_at) VALUES (?, ?, 'Still present', 'REQUIREMENT_DRAFT', 3, "
            "'legacy-still-present', 'before', 'before')",
            (case_id, owner_id),
        )
        with pytest.raises(sqlite3.OperationalError, match="deliberately_missing_table"):
            apply_migrations(connection, broken_root)

        columns = {row["name"] for row in connection.execute("PRAGMA table_info(cases)")}
        assert "invitation_token_hash" in columns
        case = connection.execute(
            "SELECT id, title, content_revision, invitation_token_hash FROM cases WHERE id = ?",
            (case_id,),
        ).fetchone()
        assert tuple(case) == (case_id, "Still present", 3, "legacy-still-present")
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = '002_slice3_invitations'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'invitations'"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_invitation_persists_only_digest_and_binds_exact_source(demo_runtime) -> None:
    context = _prepare_source(demo_runtime)
    issue = _issue_invitation(context)
    decoded = base64.urlsafe_b64decode(issue.code + "=")
    assert len(decoded) == 32
    expected_digest = hashlib.sha256(issue.code.encode("ascii")).hexdigest()

    connection = connect_database(context["database"])
    try:
        invitation = connection.execute("SELECT * FROM invitations WHERE id = ?", (issue.invitation_id,)).fetchone()
        case = connection.execute("SELECT state, contractor_id FROM cases WHERE id = ?", (context["case_id"],)).fetchone()
        verification = connection.execute(
            "SELECT actor_id, actor_role, artifact_id, supplied_sha256, supplied_byte_length "
            "FROM json_a_source_verifications WHERE invitation_id = ?",
            (issue.invitation_id,),
        ).fetchone()
        assert invitation["token_hash"] == expected_digest
        assert invitation["designated_user_id"] == context["contractor_id"]
        assert invitation["source_artifact_id"] == context["artifact"]["id"]
        assert invitation["source_sha256"] == context["artifact"]["sha256"]
        assert invitation["source_byte_length"] == len(context["raw"])
        assert datetime.fromisoformat(invitation["expires_at"].replace("Z", "+00:00")) == FIXED_NOW + INVITATION_TTL
        assert tuple(case) == ("INVITATION_PENDING", None)
        assert tuple(verification) == (
            context["owner_id"],
            "owner",
            context["artifact"]["id"],
            context["artifact"]["sha256"],
            len(context["raw"]),
        )
        with pytest.raises(InvitationWorkflowError, match="invitation_unavailable"):
            preview_invitation(
                connection,
                issue.invitation_id,
                issue.code,
                context["owner_id"],
                now=FIXED_NOW,
            )
        preview = preview_invitation(
            connection,
            issue.invitation_id,
            issue.code,
            context["contractor_id"],
            now=FIXED_NOW,
        )
        assert preview.case_id == context["case_id"]
        assert preview.source_sha256 == context["artifact"]["sha256"]
    finally:
        connection.close()

    persisted = b"".join(
        path.read_bytes()
        for path in (
            context["database"],
            Path(f"{context['database']}-wal"),
            Path(f"{context['database']}-shm"),
        )
        if path.exists()
    )
    assert issue.code.encode("ascii") not in persisted


def _invalid_variants(raw: bytes) -> list[bytes]:
    document = decode_strict_json(raw)
    unknown = dict(document)
    unknown["uploaded_role"] = "owner"
    wrong_version = dict(document)
    wrong_version["version"] = int(document["version"]) + 1
    compact = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return [
        b'\xef\xbb\xbf{}',
        b'{"same":1,"same":2}',
        encode_json_once(unknown),
        encode_json_once(wrong_version),
        compact,
    ]


def test_invalid_or_nonidentical_json_a_creates_no_workflow_write(demo_runtime) -> None:
    context = _prepare_source(demo_runtime)
    for supplied in _invalid_variants(context["raw"]):
        connection = connect_database(context["database"])
        try:
            before = (
                connection.execute("SELECT state, contractor_id, updated_at FROM cases WHERE id = ?", (context["case_id"],)).fetchone(),
                connection.execute("SELECT COUNT(*) FROM invitations").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM json_a_source_verifications").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
            )
            with pytest.raises(InvitationWorkflowError):
                create_invitation(
                    connection,
                    context["data_root"],
                    context["schema_path"],
                    context["case_id"],
                    context["owner_id"],
                    context["contractor_id"],
                    supplied,
                    now=FIXED_NOW,
                )
            after = (
                connection.execute("SELECT state, contractor_id, updated_at FROM cases WHERE id = ?", (context["case_id"],)).fetchone(),
                connection.execute("SELECT COUNT(*) FROM invitations").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM json_a_source_verifications").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
            )
            assert tuple(before[0]) == tuple(after[0])
            assert before[1:] == after[1:]
        finally:
            connection.close()


def test_accept_import_replay_and_restart_preserve_one_artifact(demo_runtime) -> None:
    context = _prepare_source(demo_runtime)
    original_file_bytes = (context["data_root"] / context["artifact"]["relative_path"]).read_bytes()
    issue = _issue_invitation(context)

    connection = connect_database(context["database"])
    try:
        accepted = accept_invitation(
            connection,
            context["data_root"],
            issue.invitation_id,
            issue.code,
            context["contractor_id"],
            now=FIXED_NOW + timedelta(minutes=1),
        )
        assert accepted.case_id == context["case_id"]
    finally:
        connection.close()

    restarted = connect_database(context["database"])
    try:
        before_replay = (
            restarted.execute("SELECT state, contractor_id, updated_at FROM cases WHERE id = ?", (context["case_id"],)).fetchone(),
            restarted.execute("SELECT COUNT(*) FROM json_a_source_verifications").fetchone()[0],
        )
        with pytest.raises(InvitationWorkflowError, match="invitation_unavailable"):
            accept_invitation(
                restarted,
                context["data_root"],
                issue.invitation_id,
                issue.code,
                context["contractor_id"],
                now=FIXED_NOW + timedelta(minutes=2),
            )
        after_replay = (
            restarted.execute("SELECT state, contractor_id, updated_at FROM cases WHERE id = ?", (context["case_id"],)).fetchone(),
            restarted.execute("SELECT COUNT(*) FROM json_a_source_verifications").fetchone()[0],
        )
        assert tuple(before_replay[0]) == tuple(after_replay[0])
        assert before_replay[1] == after_replay[1]

        artifact_count = restarted.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        imported = import_json_a(
            restarted,
            context["data_root"],
            context["schema_path"],
            issue.invitation_id,
            context["contractor_id"],
            context["raw"],
            now=FIXED_NOW + timedelta(minutes=3),
        )
        assert imported.already_verified is False
        assert restarted.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == artifact_count
        case = restarted.execute("SELECT state, contractor_id FROM cases WHERE id = ?", (context["case_id"],)).fetchone()
        artifact = restarted.execute("SELECT visibility FROM artifacts WHERE id = ?", (context["artifact"]["id"],)).fetchone()
        assert tuple(case) == ("CASE_ACTIVE", context["contractor_id"])
        assert artifact["visibility"] == "party_only"
        assert (context["data_root"] / context["artifact"]["relative_path"]).read_bytes() == original_file_bytes
    finally:
        restarted.close()

    second_restart = connect_database(context["database"])
    try:
        repeated = import_json_a(
            second_restart,
            context["data_root"],
            context["schema_path"],
            issue.invitation_id,
            context["contractor_id"],
            context["raw"],
            now=FIXED_NOW + timedelta(minutes=4),
        )
        assert repeated.already_verified is True
        assert second_restart.execute(
            "SELECT COUNT(*) FROM json_a_source_verifications WHERE invitation_id = ?",
            (issue.invitation_id,),
        ).fetchone()[0] == 2
    finally:
        second_restart.close()


def test_nonidentical_contractor_import_rolls_back_every_workflow_change(demo_runtime) -> None:
    context = _prepare_source(demo_runtime)
    issue = _issue_invitation(context)
    connection = connect_database(context["database"])
    try:
        accept_invitation(
            connection,
            context["data_root"],
            issue.invitation_id,
            issue.code,
            context["contractor_id"],
            now=FIXED_NOW + timedelta(minutes=1),
        )
        before_case = connection.execute(
            "SELECT state, contractor_id, updated_at FROM cases WHERE id = ?", (context["case_id"],)
        ).fetchone()
        before_invitation = connection.execute(
            "SELECT state, source_invalidated_at, updated_at FROM invitations WHERE id = ?",
            (issue.invitation_id,),
        ).fetchone()
        before_verifications = connection.execute(
            "SELECT COUNT(*) FROM json_a_source_verifications WHERE invitation_id = ?",
            (issue.invitation_id,),
        ).fetchone()[0]
        before_artifacts = connection.execute("SELECT COUNT(*), visibility FROM artifacts").fetchone()
        stored_before = (context["data_root"] / context["artifact"]["relative_path"]).read_bytes()

        document = decode_strict_json(context["raw"])
        reserialized = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert reserialized != context["raw"]
        with pytest.raises(InvitationWorkflowError, match="json_a_mismatch"):
            import_json_a(
                connection,
                context["data_root"],
                context["schema_path"],
                issue.invitation_id,
                context["contractor_id"],
                reserialized,
                now=FIXED_NOW + timedelta(minutes=2),
            )

        after_case = connection.execute(
            "SELECT state, contractor_id, updated_at FROM cases WHERE id = ?", (context["case_id"],)
        ).fetchone()
        after_invitation = connection.execute(
            "SELECT state, source_invalidated_at, updated_at FROM invitations WHERE id = ?",
            (issue.invitation_id,),
        ).fetchone()
        after_verifications = connection.execute(
            "SELECT COUNT(*) FROM json_a_source_verifications WHERE invitation_id = ?",
            (issue.invitation_id,),
        ).fetchone()[0]
        after_artifacts = connection.execute("SELECT COUNT(*), visibility FROM artifacts").fetchone()
        assert tuple(before_case) == tuple(after_case)
        assert tuple(before_invitation) == tuple(after_invitation)
        assert before_verifications == after_verifications == 1
        assert tuple(before_artifacts) == tuple(after_artifacts)
        assert after_artifacts["visibility"] == "private"
        assert (context["data_root"] / context["artifact"]["relative_path"]).read_bytes() == stored_before
    finally:
        connection.close()


def test_reject_revoke_and_expiry_are_terminal_and_reissuable(demo_runtime) -> None:
    context = _prepare_source(demo_runtime)
    first = _issue_invitation(context)
    connection = connect_database(context["database"])
    try:
        reject_invitation(
            connection,
            first.invitation_id,
            first.code,
            context["contractor_id"],
            now=FIXED_NOW + timedelta(minutes=1),
        )
        with pytest.raises(InvitationWorkflowError, match="invitation_unavailable"):
            reject_invitation(
                connection,
                first.invitation_id,
                first.code,
                context["contractor_id"],
                now=FIXED_NOW + timedelta(minutes=2),
            )
        assert connection.execute("SELECT state FROM invitations WHERE id = ?", (first.invitation_id,)).fetchone()[0] == "rejected"
    finally:
        connection.close()

    second = _issue_invitation(context, now=FIXED_NOW + timedelta(minutes=3))
    connection = connect_database(context["database"])
    try:
        revoke_invitation(
            connection,
            context["case_id"],
            second.invitation_id,
            context["owner_id"],
            now=FIXED_NOW + timedelta(minutes=4),
        )
        assert connection.execute("SELECT state FROM invitations WHERE id = ?", (second.invitation_id,)).fetchone()[0] == "revoked"
    finally:
        connection.close()

    third_created = FIXED_NOW + timedelta(minutes=5)
    third = _issue_invitation(context, now=third_created)
    connection = connect_database(context["database"])
    try:
        with pytest.raises(InvitationWorkflowError, match="invitation_expired"):
            accept_invitation(
                connection,
                context["data_root"],
                third.invitation_id,
                third.code,
                context["contractor_id"],
                now=third_created + INVITATION_TTL,
            )
        state = connection.execute("SELECT state, expired_at FROM invitations WHERE id = ?", (third.invitation_id,)).fetchone()
        case = connection.execute("SELECT state, contractor_id FROM cases WHERE id = ?", (context["case_id"],)).fetchone()
        assert state["state"] == "expired" and state["expired_at"] is not None
        assert tuple(case) == ("REQUIREMENT_DRAFT", None)
    finally:
        connection.close()

    replacement_created = third_created + INVITATION_TTL + timedelta(minutes=1)
    replacement = _issue_invitation(context, now=replacement_created)
    assert replacement.invitation_id != third.invitation_id
    after_unanswered_expiry = _issue_invitation(
        context,
        now=replacement_created + INVITATION_TTL + timedelta(minutes=1),
    )
    assert after_unanswered_expiry.invitation_id != replacement.invitation_id


def test_accept_and_revoke_race_has_one_atomic_terminal_result(demo_runtime) -> None:
    context = _prepare_source(demo_runtime)
    issue = _issue_invitation(context)
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[tuple[str, str]] = queue.Queue()

    def accept_worker() -> None:
        connection = connect_database(context["database"])
        try:
            barrier.wait()
            accept_invitation(
                connection,
                context["data_root"],
                issue.invitation_id,
                issue.code,
                context["contractor_id"],
                now=FIXED_NOW + timedelta(minutes=1),
            )
            outcomes.put(("accept", "ok"))
        except InvitationWorkflowError as error:
            outcomes.put(("accept", error.code))
        finally:
            connection.close()

    def revoke_worker() -> None:
        connection = connect_database(context["database"])
        try:
            barrier.wait()
            revoke_invitation(
                connection,
                context["case_id"],
                issue.invitation_id,
                context["owner_id"],
                now=FIXED_NOW + timedelta(minutes=1),
            )
            outcomes.put(("revoke", "ok"))
        except InvitationWorkflowError as error:
            outcomes.put(("revoke", error.code))
        finally:
            connection.close()

    threads = [threading.Thread(target=accept_worker), threading.Thread(target=revoke_worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    results = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert sum(result == "ok" for _action, result in results) == 1
    connection = connect_database(context["database"])
    try:
        invitation_state = connection.execute("SELECT state FROM invitations WHERE id = ?", (issue.invitation_id,)).fetchone()[0]
        case = connection.execute("SELECT state, contractor_id FROM cases WHERE id = ?", (context["case_id"],)).fetchone()
        if invitation_state == "accepted":
            assert tuple(case) == ("WAITING_JSON_A_IMPORT", context["contractor_id"])
        else:
            assert invitation_state == "revoked"
            assert tuple(case) == ("REQUIREMENT_DRAFT", None)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
