from __future__ import annotations

import io
import re
import uuid

from app.db import connect_database
from app.security.auth import hash_password
from app.services.case_workflow import utc_now
from tests.helpers import complete_requirement, csrf, login


def _prepare_owner_json_a(demo_runtime):
    app, instance_root, passwords = demo_runtime
    owner = app.test_client()
    assert login(owner, "en", "owner@example.test", passwords["owner"]).status_code == 302
    case_id, revision, artifact_data, raw = _generate_owner_json_a(owner, instance_root)
    return app, instance_root, passwords, owner, case_id, revision, artifact_data, raw


def _generate_owner_json_a(owner, instance_root):
    case_id = complete_requirement(owner, instance_root)
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        revision = connection.execute(
            "SELECT content_revision FROM cases WHERE id = ?", (case_id,)
        ).fetchone()[0]
    finally:
        connection.close()

    generated = owner.post(
        f"/en/owner/requirements/{case_id}/json-a",
        data={
            "csrf_token": csrf(owner),
            "expected_revision": str(revision),
            "review_confirmed": "yes",
        },
    )
    assert generated.status_code == 302
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        artifact = connection.execute(
            "SELECT id, relative_path, sha256, byte_length FROM artifacts "
            "WHERE case_id = ? AND kind = 'json_a' AND lifecycle = 'active'",
            (case_id,),
        ).fetchone()
        revision = connection.execute(
            "SELECT content_revision FROM cases WHERE id = ?", (case_id,)
        ).fetchone()[0]
        artifact_data = dict(artifact)
    finally:
        connection.close()
    raw = (instance_root / "data" / artifact_data["relative_path"]).read_bytes()
    return case_id, revision, artifact_data, raw


def _create_route_invitation(owner, case_id: str, revision: int, raw: bytes) -> tuple[str, str]:
    preview = owner.post(
        f"/en/owner/requirements/{case_id}/publish/preview",
        data={
            "csrf_token": csrf(owner),
            "expected_revision": str(revision),
            "json_a_file": (io.BytesIO(raw), "requirements.json"),
        },
    )
    assert preview.status_code == 200
    assert b"matches the currently registered JSON A" in preview.data
    created = owner.post(
        f"/en/owner/requirements/{case_id}/invitations",
        data={
            "csrf_token": csrf(owner),
            "expected_revision": str(revision),
            "contractor_email": "contractor@example.test",
            "json_a_file": (io.BytesIO(raw), "requirements.json"),
        },
    )
    assert created.status_code == 200
    assert created.headers["Cache-Control"] == "no-store, private"
    code_match = re.search(rb'class="code-panel code-value"><code>([^<]+)</code>', created.data)
    link_match = re.search(rb'/en/invitations/([0-9a-f-]{36})', created.data)
    assert code_match is not None and link_match is not None
    return link_match.group(1).decode("ascii"), code_match.group(1).decode("ascii")


def test_route_journey_binds_designated_contractor_and_reuses_exact_bytes(demo_runtime) -> None:
    app, instance_root, passwords, owner, case_id, revision, artifact, raw = _prepare_owner_json_a(demo_runtime)
    invitation_id, code = _create_route_invitation(owner, case_id, revision, raw)

    contractor = app.test_client()
    assert login(contractor, "en", "contractor@example.test", passwords["contractor"]).status_code == 302
    invitation = contractor.get(f"/en/invitations/{invitation_id}")
    assert invitation.status_code == 200
    assert raw not in invitation.data
    accepted = contractor.post(
        f"/en/invitations/{invitation_id}/accept",
        data={"csrf_token": csrf(contractor), "invitation_code": code},
    )
    assert accepted.status_code == 302
    assert accepted.location.endswith(f"/en/cases/{case_id}/json-a/import")

    assert contractor.get(f"/en/cases/{case_id}").status_code == 404
    assert contractor.get(f"/en/cases/{case_id}/json-a/download").status_code == 404
    imported = contractor.post(
        f"/en/cases/{case_id}/json-a/import",
        data={
            "csrf_token": csrf(contractor),
            "expected_revision": str(revision),
            "json_a_file": (io.BytesIO(raw), "requirements.json"),
        },
    )
    assert imported.status_code == 302
    assert imported.location.endswith(f"/en/cases/{case_id}")

    for client in (owner, contractor):
        room = client.get(f"/en/cases/{case_id}")
        assert room.status_code == 200
        downloaded = client.get(f"/en/cases/{case_id}/json-a/download")
        assert downloaded.status_code == 200
        assert downloaded.data == raw
        assert client.get(f"/en/cases/{case_id}/blueprint.svg").status_code == 200

    private_case_path = f"/en/cases/{case_id}".encode("ascii")
    owner_account = owner.get("/en/account")
    owner_review = owner.get(f"/en/owner/requirements/{case_id}/review")
    assert owner_account.status_code == 200
    assert owner_review.status_code == 200
    assert private_case_path in owner_account.data
    assert private_case_path in owner_review.data

    connection = connect_database(instance_root / "app.sqlite3")
    try:
        case = connection.execute(
            "SELECT state, contractor_id FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        contractor_id = connection.execute(
            "SELECT id FROM users WHERE email_normalized = 'contractor@example.test'"
        ).fetchone()[0]
        stored_artifact = connection.execute(
            "SELECT visibility, sha256, byte_length FROM artifacts WHERE id = ?", (artifact["id"],)
        ).fetchone()
        assert tuple(case) == ("CASE_ACTIVE", contractor_id)
        assert tuple(stored_artifact) == ("party_only", artifact["sha256"], artifact["byte_length"])
    finally:
        connection.close()


def test_preview_is_non_authoritative_and_invalid_create_has_no_workflow_write(demo_runtime) -> None:
    _app, instance_root, _passwords, owner, case_id, revision, _artifact, raw = _prepare_owner_json_a(demo_runtime)
    preview = owner.post(
        f"/en/owner/requirements/{case_id}/publish/preview",
        data={
            "csrf_token": csrf(owner),
            "expected_revision": str(revision),
            "json_a_file": (io.BytesIO(raw), "requirements.json"),
        },
    )
    assert preview.status_code == 200
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM invitations").fetchone()[0] == 0
        assert connection.execute("SELECT state FROM cases WHERE id = ?", (case_id,)).fetchone()[0] == "REQUIREMENT_DRAFT"
    finally:
        connection.close()

    changed = raw + b" "
    rejected = owner.post(
        f"/en/owner/requirements/{case_id}/invitations",
        data={
            "csrf_token": csrf(owner),
            "expected_revision": str(revision),
            "contractor_email": "contractor@example.test",
            "json_a_file": (io.BytesIO(changed), "requirements.json"),
        },
    )
    assert rejected.status_code == 422
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM invitations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM json_a_source_verifications").fetchone()[0] == 0
        assert connection.execute("SELECT state FROM cases WHERE id = ?", (case_id,)).fetchone()[0] == "REQUIREMENT_DRAFT"
    finally:
        connection.close()


def test_pending_invitation_freezes_requirement_edits_and_json_regeneration(demo_runtime) -> None:
    _app, instance_root, _passwords, owner, case_id, revision, _artifact, raw = _prepare_owner_json_a(demo_runtime)
    _create_route_invitation(owner, case_id, revision, raw)
    edit = owner.post(
        f"/en/owner/requirements/{case_id}/sections/1",
        data={
            "csrf_token": csrf(owner),
            "expected_revision": str(revision),
            "working_name": "Changed after invitation",
            "target_audience": "Anyone",
            "primary_purpose": "Change the source",
            "primary_visitor_action": "Continue",
            "supplemental_notes": "",
        },
    )
    regenerate = owner.post(
        f"/en/owner/requirements/{case_id}/json-a",
        data={
            "csrf_token": csrf(owner),
            "expected_revision": str(revision),
            "review_confirmed": "yes",
        },
    )
    assert edit.status_code == 409
    assert regenerate.status_code == 409
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        case = connection.execute(
            "SELECT title, state, content_revision FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        assert tuple(case) == ("Community Studio Website", "INVITATION_PENDING", revision)
        assert connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE case_id = ? AND kind = 'json_a'", (case_id,)
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_expired_invitation_is_not_actionable_and_owner_can_reissue(demo_runtime) -> None:
    app, instance_root, passwords, owner, case_id, revision, _artifact, raw = _prepare_owner_json_a(demo_runtime)
    invitation_id, _code = _create_route_invitation(owner, case_id, revision, raw)
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        connection.execute(
            "UPDATE invitations SET expires_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
            (invitation_id,),
        )
    finally:
        connection.close()

    owner_page = owner.get(f"/en/owner/requirements/{case_id}/publish")
    assert owner_page.status_code == 200
    assert b"Invitation Expired" in owner_page.data
    assert b'name="json_a_file"' in owner_page.data

    contractor = app.test_client()
    assert login(contractor, "en", "contractor@example.test", passwords["contractor"]).status_code == 302
    invitation_page = contractor.get(f"/en/invitations/{invitation_id}")
    assert invitation_page.status_code == 200
    assert b"Invitation Expired" in invitation_page.data
    assert f"/en/invitations/{invitation_id}/accept".encode() not in invitation_page.data

    replacement_id, _replacement_code = _create_route_invitation(owner, case_id, revision, raw)
    assert replacement_id != invitation_id
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        old_state = connection.execute(
            "SELECT state FROM invitations WHERE id = ?", (invitation_id,)
        ).fetchone()[0]
        new_state = connection.execute(
            "SELECT state FROM invitations WHERE id = ?", (replacement_id,)
        ).fetchone()[0]
        assert (old_state, new_state) == ("expired", "pending")
    finally:
        connection.close()

def test_wrong_account_admin_and_third_member_cannot_read_invitation_or_case(demo_runtime) -> None:
    app, instance_root, passwords, owner, case_id, revision, _artifact, raw = _prepare_owner_json_a(demo_runtime)
    invitation_id, code = _create_route_invitation(owner, case_id, revision, raw)
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        now = utc_now()
        connection.execute(
            "INSERT INTO users(id, email_normalized, display_name, password_hash, system_role, status, "
            "session_version, created_at, updated_at) VALUES (?, 'third@example.test', 'Third Member', ?, "
            "'member', 'active', 1, ?, ?)",
            (str(uuid.uuid4()), hash_password("third-member-password"), now, now),
        )
    finally:
        connection.close()

    for email, password in (
        ("admin@example.test", passwords["admin"]),
        ("third@example.test", "third-member-password"),
    ):
        outsider = app.test_client()
        assert login(outsider, "en", email, password).status_code == 302
        assert outsider.get(f"/en/invitations/{invitation_id}").status_code == 404
        assert outsider.get(f"/en/cases/{case_id}").status_code == 404
        assert outsider.get(f"/en/cases/{case_id}/json-a/download").status_code == 404

    contractor = app.test_client()
    assert login(contractor, "en", "contractor@example.test", passwords["contractor"]).status_code == 302
    wrong = contractor.post(
        f"/en/invitations/{invitation_id}/accept",
        data={"csrf_token": csrf(contractor), "invitation_code": f"{code}wrong"},
    )
    assert wrong.status_code == 404
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        invitation = connection.execute(
            "SELECT state FROM invitations WHERE id = ?", (invitation_id,)
        ).fetchone()[0]
        case = connection.execute(
            "SELECT state, contractor_id FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        assert invitation == "pending"
        assert tuple(case) == ("INVITATION_PENDING", None)
    finally:
        connection.close()


def test_revoke_route_binds_invitation_to_the_case_in_the_url(demo_runtime) -> None:
    _app, instance_root, _passwords, owner, case_a, revision_a, _artifact_a, raw_a = (
        _prepare_owner_json_a(demo_runtime)
    )
    invitation_a, _code_a = _create_route_invitation(owner, case_a, revision_a, raw_a)
    case_b, revision_b, _artifact_b, raw_b = _generate_owner_json_a(owner, instance_root)
    invitation_b, _code_b = _create_route_invitation(owner, case_b, revision_b, raw_b)

    connection = connect_database(instance_root / "app.sqlite3")
    try:
        before = {
            row["id"]: (row["state"], row["updated_at"])
            for row in connection.execute(
                "SELECT id, state, updated_at FROM cases WHERE id IN (?, ?)",
                (case_a, case_b),
            )
        }
        invitation_before = {
            row["id"]: (row["state"], row["updated_at"])
            for row in connection.execute(
                "SELECT id, state, updated_at FROM invitations WHERE id IN (?, ?)",
                (invitation_a, invitation_b),
            )
        }
    finally:
        connection.close()

    mismatched = owner.post(
        f"/en/owner/requirements/{case_a}/invitations/{invitation_b}/revoke",
        data={"csrf_token": csrf(owner)},
    )
    assert mismatched.status_code == 404

    connection = connect_database(instance_root / "app.sqlite3")
    try:
        after = {
            row["id"]: (row["state"], row["updated_at"])
            for row in connection.execute(
                "SELECT id, state, updated_at FROM cases WHERE id IN (?, ?)",
                (case_a, case_b),
            )
        }
        invitation_after = {
            row["id"]: (row["state"], row["updated_at"])
            for row in connection.execute(
                "SELECT id, state, updated_at FROM invitations WHERE id IN (?, ?)",
                (invitation_a, invitation_b),
            )
        }
        assert after == before
        assert invitation_after == invitation_before
    finally:
        connection.close()

    revoked = owner.post(
        f"/en/owner/requirements/{case_a}/invitations/{invitation_a}/revoke",
        data={"csrf_token": csrf(owner)},
    )
    assert revoked.status_code == 302

    connection = connect_database(instance_root / "app.sqlite3")
    try:
        case_states = {
            row["id"]: row["state"]
            for row in connection.execute(
                "SELECT id, state FROM cases WHERE id IN (?, ?)",
                (case_a, case_b),
            )
        }
        invitation_states = {
            row["id"]: row["state"]
            for row in connection.execute(
                "SELECT id, state FROM invitations WHERE id IN (?, ?)",
                (invitation_a, invitation_b),
            )
        }
        assert case_states == {case_a: "REQUIREMENT_DRAFT", case_b: "INVITATION_PENDING"}
        assert invitation_states == {invitation_a: "revoked", invitation_b: "pending"}
    finally:
        connection.close()
