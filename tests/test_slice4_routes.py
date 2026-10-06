from __future__ import annotations

import io
import json
import re
import uuid
from urllib.parse import parse_qs, urlparse

from app.db import connect_database
from app.security.auth import hash_password
from app.services.case_workflow import utc_now
from app.services.json_b_workflow import BUSINESS_FIELDS
from tests.helpers import csrf, login
from tests.slice4_helpers import activate_private_case, valid_business_draft


def test_route_journey_keeps_draft_private_and_submits_same_bytes(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    case_id = context["case_id"]
    owner = context["owner"]
    contractor = context["contractor"]
    secret_text = "Contractor private estimate café-Δ"
    draft = valid_business_draft()
    draft["requirement_understanding"] = secret_text

    assert contractor.get(f"/en/cases/{case_id}/json-b/draft").status_code == 200
    assert owner.get(f"/en/cases/{case_id}/json-b/draft").status_code == 404
    saved = contractor.post(
        f"/en/cases/{case_id}/json-b/draft",
        data={
            "csrf_token": csrf(contractor),
            "expected_revision": "0",
            "draft_json": json.dumps(draft, ensure_ascii=False),
        },
    )
    assert saved.status_code == 302
    owner_room = owner.get(f"/en/cases/{case_id}")
    owner_account = owner.get("/en/account")
    assert owner_room.status_code == 200
    assert secret_text.encode("utf-8") not in owner_room.data
    assert owner_account.status_code == 200
    assert secret_text.encode("utf-8") not in owner_account.data
    assert b"JSON_B_PRIVATE_DRAFT" not in owner_account.data

    exported = contractor.post(
        f"/en/cases/{case_id}/json-b/draft/export",
        data={"csrf_token": csrf(contractor), "expected_revision": "1"},
    )
    assert exported.status_code == 302
    query = parse_qs(urlparse(exported.location).query)
    artifact_id = query["artifact_id"][0]
    private_url = f"/en/cases/{case_id}/json-b/draft/exports/{artifact_id}/download"
    private_download = contractor.get(private_url)
    assert private_download.status_code == 200
    assert owner.get(private_url).status_code == 404

    submitted = contractor.post(
        f"/en/cases/{case_id}/json-b/review/import",
        data={
            "csrf_token": csrf(contractor),
            "artifact_id": artifact_id,
            "json_b_file": (io.BytesIO(private_download.data), "json-b.json"),
        },
    )
    assert submitted.status_code == 302
    assert submitted.location.endswith(f"/en/cases/{case_id}/json-b/reviews/1")
    review_url = f"/en/cases/{case_id}/json-b/reviews/1/download"
    assert owner.get(review_url).data == private_download.data
    assert contractor.get(review_url).data == private_download.data
    review_page = owner.get(f"/en/cases/{case_id}/json-b/reviews/1")
    objection_pointers = {
        pointer.decode("ascii")
        for pointer in re.findall(rb'name="field_pointer" value="([^"]+)"', review_page.data)
    }
    assert objection_pointers == {f"/{field}" for field in BUSINESS_FIELDS}
    assert b'value="/source_json_a"' not in review_page.data
    assert b'value="/computed_fee_totals"' not in review_page.data
    assert b'value="/document_id"' not in review_page.data
    assert b"JSON Field Pointer" not in review_page.data
    contractor_review = contractor.get(f"/en/cases/{case_id}/json-b/reviews/1")
    assert b'name="field_pointer"' not in contractor_review.data

    objection = owner.post(
        f"/en/cases/{case_id}/json-b/reviews/1/objections",
        data={
            "csrf_token": csrf(owner),
            "field_pointer": "/included_scope/0/description",
            "summary": "Clarify which validation cases are included.",
        },
    )
    assert objection.status_code == 302
    forbidden = contractor.post(
        f"/en/cases/{case_id}/json-b/reviews/1/objections",
        data={
            "csrf_token": csrf(contractor),
            "field_pointer": "/included_scope/0/description",
            "summary": "Contractor cannot open a client objection.",
        },
    )
    assert forbidden.status_code == 404
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        issue = connection.execute(
            "SELECT kind, source_json_b_artifact_id, field_pointer, opened_by_role, state "
            "FROM case_issues WHERE case_id = ? AND kind = 'JSON_B_OBJECTION'",
            (case_id,),
        ).fetchone()
        assert tuple(issue) == (
            "JSON_B_OBJECTION",
            artifact_id,
            "/included_scope/0/description",
            "client",
            "OPEN",
        )
    finally:
        connection.close()

    assert contractor.post(
        f"/en/cases/{case_id}/json-b/reviews/1/revise",
        data={"csrf_token": csrf(contractor)},
    ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        revision = connection.execute(
            "SELECT content_revision FROM json_b_drafts WHERE case_id = ?", (case_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    edited = valid_business_draft()
    edited["requirement_understanding"] = "Manual revision that must survive a repeated revise request."
    assert contractor.post(
        f"/en/cases/{case_id}/json-b/draft",
        data={
            "csrf_token": csrf(contractor),
            "expected_revision": str(revision),
            "draft_json": json.dumps(edited),
        },
    ).status_code == 302
    assert contractor.post(
        f"/en/cases/{case_id}/json-b/reviews/1/revise",
        data={"csrf_token": csrf(contractor)},
    ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        preserved = connection.execute(
            "SELECT data_json, content_revision FROM json_b_drafts WHERE case_id = ?", (case_id,)
        ).fetchone()
        assert json.loads(preserved["data_json"])["requirement_understanding"] == edited["requirement_understanding"]
        assert preserved["content_revision"] == revision + 1
    finally:
        connection.close()


def test_admin_third_member_and_wrong_case_cannot_use_slice4_routes(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    app = context["app"]
    case_id = context["case_id"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        now = utc_now()
        connection.execute(
            "INSERT INTO users(id, email_normalized, display_name, password_hash, system_role, status, "
            "session_version, created_at, updated_at) VALUES (?, 'third-s4@example.test', 'Third Slice 4', ?, "
            "'member', 'active', 1, ?, ?)",
            (str(uuid.uuid4()), hash_password("third-slice4-password"), now, now),
        )
    finally:
        connection.close()

    for email, password in (
        ("admin@example.test", context["passwords"]["admin"]),
        ("third-s4@example.test", "third-slice4-password"),
    ):
        outsider = app.test_client()
        assert login(outsider, "en", email, password).status_code == 302
        assert outsider.get(f"/en/cases/{case_id}").status_code == 404
        assert outsider.get(f"/en/cases/{case_id}/json-b/draft").status_code == 404
        assert outsider.post(
            f"/en/cases/{case_id}/messages",
            data={
                "csrf_token": csrf(outsider),
                "idempotency_key": str(uuid.uuid4()),
                "body": "Unauthorized",
            },
        ).status_code == 404

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM case_messages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM json_b_drafts").fetchone()[0] == 0
    finally:
        connection.close()


def test_objection_rejects_server_owned_and_nonexistent_pointers(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    case_id = context["case_id"]
    contractor = context["contractor"]
    owner = context["owner"]
    draft = valid_business_draft()
    assert contractor.post(
        f"/en/cases/{case_id}/json-b/draft",
        data={
            "csrf_token": csrf(contractor),
            "expected_revision": "0",
            "draft_json": json.dumps(draft),
        },
    ).status_code == 302
    exported = contractor.post(
        f"/en/cases/{case_id}/json-b/draft/export",
        data={"csrf_token": csrf(contractor), "expected_revision": "1"},
    )
    artifact_id = parse_qs(urlparse(exported.location).query)["artifact_id"][0]
    raw = contractor.get(
        f"/en/cases/{case_id}/json-b/draft/exports/{artifact_id}/download"
    ).data
    assert contractor.post(
        f"/en/cases/{case_id}/json-b/review/import",
        data={
            "csrf_token": csrf(contractor),
            "artifact_id": artifact_id,
            "json_b_file": (io.BytesIO(raw), "json-b.json"),
        },
    ).status_code == 302

    for pointer in (
        "/document_id",
        "/computed_fee_totals/0/amount",
        "/included_scope/99",
        "/included_scope/00/description",
        "/included_scope/٠/description",
        "/included_scope/0/descr~2iption",
    ):
        response = owner.post(
            f"/en/cases/{case_id}/json-b/reviews/1/objections",
            data={
                "csrf_token": csrf(owner),
                "field_pointer": pointer,
                "summary": "This pointer must be rejected.",
            },
        )
        assert response.status_code == 422
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM case_issues WHERE kind = 'JSON_B_OBJECTION'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_case_room_exposes_corrections_history_and_only_valid_issue_actions(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    case_id = context["case_id"]
    owner = context["owner"]
    contractor = context["contractor"]
    assert owner.post(
        f"/en/cases/{case_id}/messages",
        data={
            "csrf_token": csrf(owner),
            "idempotency_key": str(uuid.uuid4()),
            "body": "Original hosting question.",
        },
    ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        message_id = connection.execute(
            "SELECT id FROM case_messages WHERE case_id = ?", (case_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    owner_page = owner.get(f"/en/cases/{case_id}")
    assert b"Correct This Message" in owner_page.data
    assert f'name="correction_of_id" value="{message_id}"'.encode() in owner_page.data
    assert contractor.get(f"/en/cases/{case_id}").data.count(b"Correct This Message") == 0
    assert owner.post(
        f"/en/cases/{case_id}/messages",
        data={
            "csrf_token": csrf(owner),
            "idempotency_key": str(uuid.uuid4()),
            "correction_of_id": message_id,
            "body": "Correction: confirm recurring hosting responsibility.",
        },
    ).status_code == 302
    assert owner.post(
        f"/en/cases/{case_id}/issues",
        data={"csrf_token": csrf(owner), "summary": "Who pays for recurring hosting?"},
    ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        issue = connection.execute(
            "SELECT id, revision FROM case_issues WHERE case_id = ?", (case_id,)
        ).fetchone()
    finally:
        connection.close()
    assert owner.post(
        f"/en/cases/{case_id}/issues/{issue['id']}/events",
        data={
            "csrf_token": csrf(owner),
            "expected_revision": str(issue["revision"]),
            "action": "PROPOSE",
            "note": "The client pays the provider directly.",
        },
    ).status_code == 302
    owner_page = owner.get(f"/en/cases/{case_id}")
    contractor_page = contractor.get(f"/en/cases/{case_id}")
    assert b"Issue History" in owner_page.data
    assert b"The client pays the provider directly." in owner_page.data
    assert b'value="ACCEPT"' not in owner_page.data
    assert b'value="PROPOSE"' not in owner_page.data
    assert b'value="ACCEPT"' in contractor_page.data
    assert b'value="REQUEST_REVISION"' in contractor_page.data
    assert b'value="PROPOSE"' not in contractor_page.data
