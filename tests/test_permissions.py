from __future__ import annotations

import uuid

from app.db import connect_database
import app.routes.localized as localized_routes
import app.security.permissions as permission_checks
from tests.helpers import complete_requirement, csrf, login


def test_owner_content_is_not_available_to_contractor_or_admin(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    with app.test_client() as owner:
        login(owner, "en", "owner@example.test", passwords["owner"])
        case_id = complete_requirement(owner, instance_root)
        assert owner.get(f"/en/owner/requirements/{case_id}/review").status_code == 200
    for email, password in (("contractor@example.test", passwords["contractor"]), ("admin@example.test", passwords["admin"])):
        with app.test_client() as outsider:
            login(outsider, "en", email, password)
            assert outsider.get(f"/en/owner/requirements/{case_id}/review").status_code == 404
            assert outsider.get(f"/en/owner/requirements/{case_id}/blueprint.svg").status_code == 404


def test_admin_projection_and_disable_revoke_member_session(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    owner = app.test_client()
    admin = app.test_client()
    login(owner, "en", "owner@example.test", passwords["owner"])
    login(admin, "en", "admin@example.test", passwords["admin"])
    case_id = complete_requirement(owner, instance_root)
    page = admin.get("/en/admin/accounts")
    assert page.status_code == 200
    assert case_id.encode() not in page.data
    assert b"Community Studio Website" not in page.data
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        owner_id = connection.execute("SELECT id FROM users WHERE email_normalized = 'owner@example.test'").fetchone()[0]
    finally:
        connection.close()
    result = admin.post(f"/en/admin/accounts/{owner_id}/status", data={"csrf_token": csrf(admin), "status": "disabled"})
    assert result.status_code == 302
    assert owner.get(f"/en/owner/requirements/{case_id}/review").status_code == 401


def test_anonymous_and_guessed_identifiers_do_not_enumerate(demo_runtime) -> None:
    app, _instance_root, _passwords = demo_runtime
    with app.test_client() as client:
        assert client.get("/en/owner/requirements/00000000-0000-0000-0000-000000000000/review").status_code in (401, 404)
        assert client.get("/fr/owner/requirements/00000000-0000-0000-0000-000000000000/review").status_code == 404


def test_admin_account_query_never_reads_case_or_artifact_tables(demo_runtime, monkeypatch) -> None:
    app, _instance_root, passwords = demo_runtime
    statements: list[str] = []
    original = localized_routes.get_db

    class TracingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=()):
            statements.append(statement.lower())
            return self.connection.execute(statement, parameters)

    monkeypatch.setattr(localized_routes, "get_db", lambda: TracingConnection(original()))
    monkeypatch.setattr(permission_checks, "get_db", lambda: TracingConnection(original()))
    with app.test_client() as client:
        result = login(client, "en", "admin@example.test", passwords["admin"])
        assert result.location.endswith("/en/admin/accounts")
        account = client.get("/en/account")
        assert account.status_code == 200
        account_html = account.get_data(as_text=True)
        assert "Manage accounts and service health" in account_html
        assert "Continue a private case" not in account_html
        assert "start a new requirement blueprint" not in account_html
        assert client.get("/en/admin/accounts").status_code == 200
        for path in (
            "/en/owner/requirements/00000000-0000-0000-0000-000000000000/review",
            "/en/owner/requirements/00000000-0000-0000-0000-000000000000/sections/1",
            "/en/owner/requirements/00000000-0000-0000-0000-000000000000/blueprint.svg",
            "/en/owner/requirements/00000000-0000-0000-0000-000000000000/json-a/00000000-0000-0000-0000-000000000001/download",
            "/en/owner/requirements/00000000-0000-0000-0000-000000000000/publish",
            "/en/invitations/00000000-0000-0000-0000-000000000000",
            "/en/cases/00000000-0000-0000-0000-000000000000",
            "/en/cases/00000000-0000-0000-0000-000000000000/json-a/import",
            "/en/cases/00000000-0000-0000-0000-000000000000/json-a/download",
            "/en/cases/00000000-0000-0000-0000-000000000000/json-b/draft",
            "/en/cases/00000000-0000-0000-0000-000000000000/json-b/reviews/1",
        ):
            assert client.get(path).status_code == 404
        assert client.post(
            "/en/cases/00000000-0000-0000-0000-000000000000/messages",
            data={
                "csrf_token": csrf(client),
                "idempotency_key": str(uuid.uuid4()),
                "body": "admin must be rejected before a case query",
            },
        ).status_code == 404
    forbidden = (
        " cases",
        " artifacts",
        " requirement_drafts",
        " audit_events",
        " invitations",
        " json_a_source_verifications",
        " case_messages",
        " case_issues",
        " issue_events",
        " json_b_drafts",
        " json_b_private_exports",
        " json_b_versions",
    )
    assert statements
    assert all(not any(name in statement for name in forbidden) for statement in statements)
