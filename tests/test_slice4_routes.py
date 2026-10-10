from __future__ import annotations

import io
import json
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import app.services.json_b_workflow as json_b_module
from app.db import connect_database
from app.security.auth import hash_password
from app.services.case_workflow import utc_now
from app.services.json_b_workflow import BUSINESS_FIELDS
from tests.helpers import csrf, login
from tests.slice4_helpers import activate_private_case, valid_business_draft
from tests.slice5_helpers import create_json_b_review


def _open_json_b_objection(context: dict[str, object]) -> str:
    response = context["owner"].post(
        f"/en/cases/{context['case_id']}/json-b/reviews/1/objections",
        data={
            "csrf_token": csrf(context["owner"]),
            "field_pointer": "/included_scope/0/description",
            "summary": "Clarify the included validation cases.",
        },
    )
    assert response.status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        return connection.execute(
            "SELECT id FROM case_issues WHERE case_id = ? AND kind = 'JSON_B_OBJECTION'",
            (context["case_id"],),
        ).fetchone()[0]
    finally:
        connection.close()

def _publish_revised_json_b(context: dict[str, object]) -> str:
    case_id = context["case_id"]
    contractor = context["contractor"]
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
    revised = valid_business_draft()
    revised["requirement_understanding"] = "Revised proposal bound to a new review version."
    assert contractor.post(
        f"/en/cases/{case_id}/json-b/draft",
        data={
            "csrf_token": csrf(contractor),
            "expected_revision": str(revision),
            "draft_json": json.dumps(revised),
        },
    ).status_code == 302
    next_revision = revision + 1
    exported = contractor.post(
        f"/en/cases/{case_id}/json-b/draft/export",
        data={"csrf_token": csrf(contractor), "expected_revision": str(next_revision)},
    )
    assert exported.status_code == 302
    artifact_id = parse_qs(urlparse(exported.location).query)["artifact_id"][0]
    raw = contractor.get(
        f"/en/cases/{case_id}/json-b/draft/exports/{artifact_id}/download"
    ).data
    submitted = contractor.post(
        f"/en/cases/{case_id}/json-b/review/import",
        data={
            "csrf_token": csrf(contractor),
            "artifact_id": artifact_id,
            "json_b_file": (io.BytesIO(raw), "json-b.json"),
        },
    )
    assert submitted.status_code == 302
    assert submitted.location.endswith(f"/en/cases/{case_id}/json-b/reviews/2")
    return artifact_id


def test_expired_review_can_be_revised_but_requires_renewed_quote_before_export(
    demo_runtime, monkeypatch
) -> None:
    context = create_json_b_review(demo_runtime)
    case_id = context["case_id"]
    contractor = context["contractor"]
    data_root = context["instance_root"] / "data"
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        review = connection.execute(
            "SELECT a.relative_path, a.sha256 FROM json_b_versions AS v "
            "JOIN artifacts AS a ON a.id = v.artifact_id WHERE v.case_id = ? AND v.version_no = 1",
            (case_id,),
        ).fetchone()
        review_bytes = (data_root / review["relative_path"]).read_bytes()
    finally:
        connection.close()

    class FutureClock:
        current = datetime(2100, 1, 1, tzinfo=timezone.utc)

        @staticmethod
        def fromisoformat(value: str):
            return datetime.fromisoformat(value)

        @classmethod
        def now(cls, tz):
            return cls.current.astimezone(tz)

    monkeypatch.setattr(json_b_module, "datetime", FutureClock)
    monkeypatch.setattr(json_b_module, "utc_now", lambda: "2100-01-01T00:00:00Z")
    revised = contractor.post(
        f"/en/cases/{case_id}/json-b/reviews/1/revise",
        data={"csrf_token": csrf(contractor)},
    )
    assert revised.status_code == 302
    assert revised.location.endswith(f"/en/cases/{case_id}/json-b/draft")
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        draft = connection.execute(
            "SELECT data_json, content_revision FROM json_b_drafts WHERE case_id = ?", (case_id,)
        ).fetchone()
        assert json.loads(draft["data_json"])["quote_expires_at"] == "2099-12-31T23:59:59Z"
        assert (data_root / review["relative_path"]).read_bytes() == review_bytes
        assert connection.execute(
            "SELECT state FROM json_b_versions WHERE case_id = ? AND version_no = 1", (case_id,)
        ).fetchone()[0] == "CURRENT"
    finally:
        connection.close()

    business = json.loads(draft["data_json"])
    business["requirement_understanding"] = "Updated scope while the quote date remains expired."
    saved = contractor.post(
        f"/en/cases/{case_id}/json-b/draft",
        data={
            "csrf_token": csrf(contractor),
            "expected_revision": str(draft["content_revision"]),
            "draft_json": json.dumps(business),
        },
    )
    assert saved.status_code == 302
    blocked = contractor.post(
        f"/en/cases/{case_id}/json-b/draft/export",
        data={"csrf_token": csrf(contractor), "expected_revision": str(draft["content_revision"] + 1)},
    )
    assert blocked.status_code == 409
    assert b"quote expired" in blocked.data.lower()

    business["quote_expires_at"] = "not-a-date"
    invalid = contractor.post(
        f"/en/cases/{case_id}/json-b/draft",
        data={
            "csrf_token": csrf(contractor),
            "expected_revision": str(draft["content_revision"] + 1),
            "draft_json": json.dumps(business),
        },
    )
    assert invalid.status_code == 422

    business["quote_expires_at"] = "2100-01-02T00:00:00Z"
    renewed = contractor.post(
        f"/en/cases/{case_id}/json-b/draft",
        data={
            "csrf_token": csrf(contractor),
            "expected_revision": str(draft["content_revision"] + 1),
            "draft_json": json.dumps(business),
        },
    )
    assert renewed.status_code == 302
    exported = contractor.post(
        f"/en/cases/{case_id}/json-b/draft/export",
        data={"csrf_token": csrf(contractor), "expected_revision": str(draft["content_revision"] + 2)},
    )
    assert exported.status_code == 302
    artifact_id = parse_qs(urlparse(exported.location).query)["artifact_id"][0]
    raw = contractor.get(f"/en/cases/{case_id}/json-b/draft/exports/{artifact_id}/download").data
    FutureClock.current = datetime(2100, 1, 3, tzinfo=timezone.utc)
    expired_export = contractor.post(
        f"/en/cases/{case_id}/json-b/draft/export",
        data={"csrf_token": csrf(contractor), "expected_revision": str(draft["content_revision"] + 2)},
    )
    assert expired_export.status_code == 409
    assert b"quote expired" in expired_export.data.lower()
    expired_submission = contractor.post(
        f"/en/cases/{case_id}/json-b/review/import",
        data={
            "csrf_token": csrf(contractor),
            "artifact_id": artifact_id,
            "json_b_file": (io.BytesIO(raw), "json-b.json"),
        },
    )
    assert expired_submission.status_code == 409
    assert b"quote expired" in expired_submission.data.lower()
    FutureClock.current = datetime(2100, 1, 1, tzinfo=timezone.utc)
    submitted = contractor.post(
        f"/en/cases/{case_id}/json-b/review/import",
        data={
            "csrf_token": csrf(contractor),
            "artifact_id": artifact_id,
            "json_b_file": (io.BytesIO(raw), "json-b.json"),
        },
    )
    assert submitted.status_code == 302
    assert submitted.location.endswith(f"/en/cases/{case_id}/json-b/reviews/2")


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
    assert b'"item_id"' not in review_page.data
    assert b'"page_id"' not in review_page.data
    assert b"Responsive appointment form" in review_page.data
    assert b"Payment processing" in review_page.data
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
    blocked_review = contractor.get(f"/en/cases/{case_id}/json-b/reviews/1").get_data(as_text=True)
    assert "Create Confirmation Version" not in blocked_review
    assert "Revise Proposal" in blocked_review
    assert blocked_review.count('data-ui="primary-action"') == 1
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
    contractor_room = contractor.get(f"/en/cases/{case_id}").get_data(as_text=True)
    assert "Continue Proposal Revision" in contractor_room
    assert f'/en/cases/{case_id}/json-b/draft' in contractor_room
    owner_review_during_revision = owner.get(
        f"/en/cases/{case_id}/json-b/reviews/1"
    ).get_data(as_text=True)
    assert 'name="field_pointer"' in owner_review_during_revision
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


def test_json_b_objection_actions_are_role_bound_and_accept_same_review_version(
    demo_runtime,
) -> None:
    context = create_json_b_review(demo_runtime)
    case_id = context["case_id"]
    issue_id = _open_json_b_objection(context)

    client_open = context["owner"].get(f"/en/cases/{case_id}")
    contractor_open = context["contractor"].get(f"/en/cases/{case_id}")
    proposal_link = f'href="/en/cases/{case_id}/json-b/reviews/1"'.encode()
    assert proposal_link in contractor_open.data
    assert contractor_open.data.count(b'data-ui="primary-action"') == 1
    assert b'value="PROPOSE"' not in client_open.data
    assert b'value="PROPOSE"' in contractor_open.data
    assert context["owner"].post(
        f"/en/cases/{case_id}/issues/{issue_id}/events",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": "1",
            "action": "PROPOSE",
            "note": "The client cannot propose the contractor treatment.",
        },
    ).status_code == 422

    assert context["contractor"].post(
        f"/en/cases/{case_id}/issues/{issue_id}/events",
        data={
            "csrf_token": csrf(context["contractor"]),
            "expected_revision": "1",
            "action": "PROPOSE",
            "note": "The saved proposal includes the specified validation cases.",
        },
    ).status_code == 302
    client_proposed = context["owner"].get(f"/en/cases/{case_id}")
    contractor_proposed = context["contractor"].get(f"/en/cases/{case_id}")
    assert proposal_link in client_proposed.data
    assert client_proposed.data.count(b'data-ui="primary-action"') == 1
    assert b'value="ACCEPT"' in client_proposed.data
    assert b'value="ACCEPT"' not in contractor_proposed.data
    assert context["contractor"].post(
        f"/en/cases/{case_id}/issues/{issue_id}/events",
        data={
            "csrf_token": csrf(context["contractor"]),
            "expected_revision": "2",
            "action": "ACCEPT",
            "note": "The contractor cannot accept its own treatment.",
        },
    ).status_code == 422
    assert context["owner"].post(
        f"/en/cases/{case_id}/issues/{issue_id}/events",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": "2",
            "action": "ACCEPT",
            "note": "Accepted against the same saved proposal version.",
        },
    ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        event = connection.execute(
            "SELECT related_json_b_artifact_id FROM issue_events "
            "WHERE issue_id = ? AND issue_revision = 2",
            (issue_id,),
        ).fetchone()
        issue = connection.execute(
            "SELECT state, revision FROM case_issues WHERE id = ?", (issue_id,)
        ).fetchone()
        assert event["related_json_b_artifact_id"] == context["json_b_artifact_id"]
        assert tuple(issue) == ("RESOLVED", 3)
    finally:
        connection.close()


def test_json_b_objection_rejects_acceptance_after_current_review_changes(
    demo_runtime,
) -> None:
    context = create_json_b_review(demo_runtime)
    case_id = context["case_id"]
    issue_id = _open_json_b_objection(context)
    assert context["contractor"].post(
        f"/en/cases/{case_id}/issues/{issue_id}/events",
        data={
            "csrf_token": csrf(context["contractor"]),
            "expected_revision": "1",
            "action": "PROPOSE",
            "note": "Treatment proposed for proposal version 1.",
        },
    ).status_code == 302
    replacement_id = _publish_revised_json_b(context)
    assert replacement_id != context["json_b_artifact_id"]
    stale_acceptance = context["owner"].post(
        f"/en/cases/{case_id}/issues/{issue_id}/events",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": "2",
            "action": "ACCEPT",
            "note": "This must not accept treatment attached to version 1.",
        },
    )
    assert stale_acceptance.status_code == 409
    stale_page = context["owner"].get(f"/en/cases/{case_id}")
    assert b'value="ACCEPT"' not in stale_page.data
    assert b'value="REQUEST_REVISION"' in stale_page.data
    assert stale_page.data.count(b'data-ui="primary-action"') == 1
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        issue = connection.execute(
            "SELECT state, revision FROM case_issues WHERE id = ?", (issue_id,)
        ).fetchone()
        assert tuple(issue) == ("PROPOSED", 2)
    finally:
        connection.close()

    assert context["owner"].post(
        f"/en/cases/{case_id}/issues/{issue_id}/events",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": "2",
            "action": "REQUEST_REVISION",
            "note": "Re-propose the treatment against the current proposal version.",
        },
    ).status_code == 302
    contractor_page = context["contractor"].get(f"/en/cases/{case_id}")
    assert b'value="PROPOSE"' in contractor_page.data
    assert context["contractor"].post(
        f"/en/cases/{case_id}/issues/{issue_id}/events",
        data={
            "csrf_token": csrf(context["contractor"]),
            "expected_revision": "3",
            "action": "PROPOSE",
            "note": "Treatment re-proposed against proposal version 2.",
        },
    ).status_code == 302
    client_proposed_v2 = context["owner"].get(f"/en/cases/{case_id}")
    assert f'href="/en/cases/{case_id}/json-b/reviews/2"'.encode() in client_proposed_v2.data
    assert client_proposed_v2.data.count(b'data-ui="primary-action"') == 1
    assert context["owner"].post(
        f"/en/cases/{case_id}/issues/{issue_id}/events",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": "4",
            "action": "ACCEPT",
            "note": "Accepted against proposal version 2.",
        },
    ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        rebound = connection.execute(
            "SELECT related_json_b_artifact_id FROM issue_events "
            "WHERE issue_id = ? AND issue_revision = 4",
            (issue_id,),
        ).fetchone()
        issue = connection.execute(
            "SELECT state, revision FROM case_issues WHERE id = ?", (issue_id,)
        ).fetchone()
        assert rebound["related_json_b_artifact_id"] == replacement_id
        assert tuple(issue) == ("RESOLVED", 5)
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
    contractor_with_open_issue = contractor.get(f"/en/cases/{case_id}")
    assert f'href="/en/cases/{case_id}/json-b/draft"'.encode() in contractor_with_open_issue.data
    assert contractor_with_open_issue.data.count(b'data-ui="primary-action"') == 1
    assert b"Resolve the current scope question" in contractor_with_open_issue.data
    assert b"with the contractor" not in contractor_with_open_issue.data
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
    assert contractor_page.data.count(b'data-ui="primary-action"') == 1
    assert b'>Accept Resolution<' in contractor_page.data
    assert b'>Update Issue<' not in contractor_page.data
