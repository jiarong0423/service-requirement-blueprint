from __future__ import annotations

import json
import re
import uuid

from app.db import connect_database
from app.security.auth import hash_password
from app.services.case_workflow import utc_now
from tests.helpers import complete_requirement, csrf, login
from tests.slice4_helpers import activate_private_case, valid_business_draft
from tests.slice5_helpers import create_json_b_review


def _create_via_route(context) -> tuple[str, str]:
    response = context["contractor"].post(
        f"/en/cases/{context['case_id']}/candidates",
        data={"csrf_token": csrf(context["contractor"])},
    )
    assert response.status_code == 302
    match = re.search(r"/candidates/([0-9a-f-]{36})$", response.location)
    assert match is not None
    return match.group(1), response.location


def test_both_parties_receive_same_saved_candidate_bytes_and_writes_freeze(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    candidate_id, page_url = _create_via_route(context)
    owner_page = context["owner"].get(page_url)
    contractor_page = context["contractor"].get(page_url)
    assert owner_page.status_code == contractor_page.status_code == 200
    assert b"not a formal agreement" in owner_page.data
    for filename in ("manifest.json", "document.pdf", "diagram.svg"):
        url = f"/en/cases/{context['case_id']}/candidates/{candidate_id}/{filename}"
        owner_file = context["owner"].get(url)
        contractor_file = context["contractor"].get(url)
        assert owner_file.status_code == contractor_file.status_code == 200
        assert owner_file.data == contractor_file.data
        assert owner_file.headers["Cache-Control"] == "no-store, private"
        assert owner_file.headers["X-Content-Type-Options"] == "nosniff"
    assert context["contractor"].get(f"/en/cases/{context['case_id']}").status_code == 200
    assert context["contractor"].get(f"/en/cases/{context['case_id']}/json-b/draft").status_code == 404
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/issues",
        data={"csrf_token": csrf(context["owner"]), "summary": "Late change"},
    ).status_code == 404


def test_candidate_routes_reject_non_parties_and_csrf(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    no_csrf = context["contractor"].post(f"/en/cases/{context['case_id']}/candidates")
    assert no_csrf.status_code == 400
    candidate_id, page_url = _create_via_route(context)
    assert context["app"].test_client().get(page_url).status_code == 401

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        now = utc_now()
        connection.execute(
            "INSERT INTO users(id, email_normalized, display_name, password_hash, system_role, status, "
            "session_version, created_at, updated_at) VALUES (?, 'slice5-third@example.test', 'Third', ?, "
            "'member', 'active', 1, ?, ?)",
            (str(uuid.uuid4()), hash_password("slice5-third-password"), now, now),
        )
    finally:
        connection.close()
    for email, password in (
        ("admin@example.test", context["passwords"]["admin"]),
        ("slice5-third@example.test", "slice5-third-password"),
    ):
        outsider = context["app"].test_client()
        assert login(outsider, "en", email, password).status_code == 302
        assert outsider.get(page_url).status_code == 404
        assert outsider.get(
            f"/en/cases/{context['case_id']}/candidates/{candidate_id}/document.pdf"
        ).status_code == 404
        assert outsider.post(
            f"/en/cases/{context['case_id']}/candidates",
            data={"csrf_token": csrf(outsider)},
        ).status_code == 404


def test_candidate_build_rejects_open_issue(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    opened = context["owner"].post(
        f"/en/cases/{context['case_id']}/issues",
        data={"csrf_token": csrf(context["owner"]), "summary": "Resolve before candidate"},
    )
    assert opened.status_code == 302
    response = context["contractor"].post(
        f"/en/cases/{context['case_id']}/candidates",
        data={"csrf_token": csrf(context["contractor"])},
    )
    assert response.status_code == 422
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM candidate_versions").fetchone()[0] == 0
    finally:
        connection.close()


def test_preview_and_download_never_rerender(demo_runtime, monkeypatch) -> None:
    context = create_json_b_review(demo_runtime)
    candidate_id, page_url = _create_via_route(context)

    def forbidden_renderer(*_args, **_kwargs):
        raise AssertionError("saved candidate reads must not invoke the renderer")

    monkeypatch.setattr("app.services.candidate_workflow.build_candidate_bundle", forbidden_renderer)
    assert context["owner"].get(page_url).status_code == 200
    assert context["contractor"].get(
        f"/en/cases/{context['case_id']}/candidates/{candidate_id}/document.pdf"
    ).status_code == 200


def test_candidate_page_offers_inline_preview_and_exact_byte_download(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    candidate_id, page_url = _create_via_route(context)
    page = context["owner"].get(page_url)
    assert b"document.pdf?download=1" in page.data
    inline = context["owner"].get(
        f"/en/cases/{context['case_id']}/candidates/{candidate_id}/document.pdf"
    )
    download = context["owner"].get(
        f"/en/cases/{context['case_id']}/candidates/{candidate_id}/document.pdf?download=1"
    )
    assert inline.data == download.data
    assert inline.headers["Content-Disposition"].startswith("inline;")
    assert download.headers["Content-Disposition"].startswith("attachment;")


def test_unsupported_document_glyph_is_rejected_before_json_a_is_fixed(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    owner = app.test_client()
    assert login(owner, "en", "owner@example.test", passwords["owner"]).status_code == 302
    case_id = complete_requirement(owner, instance_root)
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        revision = connection.execute(
            "SELECT content_revision FROM cases WHERE id = ?", (case_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    saved = owner.post(
        f"/en/owner/requirements/{case_id}/sections/1",
        data={
            "csrf_token": csrf(owner),
            "expected_revision": str(revision),
            "working_name": "Community Studio Website",
            "target_audience": "Local visitors",
            "primary_purpose": "Explain available services",
            "primary_visitor_action": "Request an appointment",
            "supplemental_notes": "Unsupported document glyph 😀",
        },
    )
    assert saved.status_code == 302
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        revision = connection.execute(
            "SELECT content_revision FROM cases WHERE id = ?", (case_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    response = owner.post(
        f"/en/owner/requirements/{case_id}/json-a",
        data={
            "csrf_token": csrf(owner),
            "expected_revision": str(revision),
            "review_confirmed": "yes",
        },
    )
    assert response.status_code == 422
    assert b"fixed document font" in response.data
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE case_id = ? AND kind = 'json_a'",
            (case_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_unsupported_document_glyph_is_rejected_before_json_b_is_fixed(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    draft = valid_business_draft()
    draft["requirement_understanding"] = "Unsupported document glyph 😀"
    saved = context["contractor"].post(
        f"/en/cases/{context['case_id']}/json-b/draft",
        data={
            "csrf_token": csrf(context["contractor"]),
            "expected_revision": "0",
            "draft_json": json.dumps(draft, ensure_ascii=False),
        },
    )
    assert saved.status_code == 302
    response = context["contractor"].post(
        f"/en/cases/{context['case_id']}/json-b/draft/export",
        data={"csrf_token": csrf(context["contractor"]), "expected_revision": "1"},
    )
    assert response.status_code == 422
    assert b"fixed document font" in response.data
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM json_b_private_exports WHERE case_id = ?",
            (context["case_id"],),
        ).fetchone()[0] == 0
    finally:
        connection.close()
