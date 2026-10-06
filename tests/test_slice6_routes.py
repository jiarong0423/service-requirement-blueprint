from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import replace

import pytest

from app import create_app
from app.db import connect_database
from app.security.auth import hash_password
from app.services.document_integrity import FileRecord
from app.services.case_workflow import utc_now
from tests.helpers import csrf, login
from tests.slice6_helpers import acknowledge, confirm, create_candidate_context


def _component(client, context: dict[str, object], filename: str) -> bytes:
    response = client.get(
        f"/en/cases/{context['case_id']}/candidates/{context['candidate_id']}/{filename}"
    )
    assert response.status_code == 200
    return response.data


def test_bilateral_confirmation_formalizes_same_saved_bytes_without_rerender(
    demo_runtime,
    monkeypatch,
) -> None:
    context = create_candidate_context(demo_runtime)
    before = {
        filename: _component(context["owner"], context, filename)
        for filename in ("manifest.json", "document.pdf", "diagram.svg")
    }
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        candidate_paths = connection.execute(
            "SELECT manifest.relative_path AS manifest_path, pdf.relative_path AS pdf_path, "
            "svg.relative_path AS svg_path FROM candidate_versions AS candidate "
            "JOIN artifacts AS manifest ON manifest.id = candidate.manifest_artifact_id "
            "JOIN artifacts AS pdf ON pdf.id = candidate.pdf_artifact_id "
            "JOIN artifacts AS svg ON svg.id = candidate.svg_artifact_id WHERE candidate.id = ?",
            (context["candidate_id"],),
        ).fetchone()
    finally:
        connection.close()
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        source_paths = connection.execute(
            "SELECT json_a.relative_path AS json_a_path, json_b.relative_path AS json_b_path "
            "FROM candidate_versions AS candidate "
            "JOIN artifacts AS json_a ON json_a.id = candidate.json_a_artifact_id "
            "JOIN artifacts AS json_b ON json_b.id = candidate.json_b_artifact_id "
            "WHERE candidate.id = ?",
            (context["candidate_id"],),
        ).fetchone()
    finally:
        connection.close()
    all_paths = dict(candidate_paths)
    all_paths.update(dict(source_paths))
    before_inodes = {
        key: (context["instance_root"] / "data" / relative_path).stat().st_ino
        for key, relative_path in all_paths.items()
    }
    assert acknowledge(context["owner"], context["case_id"], context["candidate_id"]).status_code == 302
    first = confirm(context["owner"], context["case_id"], context["candidate_id"])
    assert first.status_code == 302
    assert "/formal" not in first.location

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        case = connection.execute(
            "SELECT state, active_candidate_id, formal_candidate_id FROM cases WHERE id = ?",
            (context["case_id"],),
        ).fetchone()
        assert tuple(case) == ("CANDIDATE_REVIEW", context["candidate_id"], None)
    finally:
        connection.close()

    assert acknowledge(context["contractor"], context["case_id"], context["candidate_id"]).status_code == 302

    def forbidden_renderer(*_args, **_kwargs):
        raise AssertionError("formalization must not render or replace files")

    monkeypatch.setattr("app.services.candidate_workflow.build_candidate_bundle", forbidden_renderer)
    second = confirm(context["contractor"], context["case_id"], context["candidate_id"])
    assert second.status_code == 302
    assert second.location.endswith(f"/en/cases/{context['case_id']}/formal")
    formal_page = context["owner"].get(second.location)
    assert formal_page.status_code == 200
    assert b"Formal status applies to the exact files" in formal_page.data

    for filename, expected in before.items():
        formal = context["contractor"].get(
            f"/en/cases/{context['case_id']}/formal/{filename}"
        )
        assert formal.status_code == 200
        assert formal.data == expected
    assert {
        key: (context["instance_root"] / "data" / all_paths[key]).stat().st_ino
        for key in before_inodes
    } == before_inodes

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        case = connection.execute(
            "SELECT state, active_candidate_id, formal_candidate_id FROM cases WHERE id = ?",
            (context["case_id"],),
        ).fetchone()
        assert tuple(case) == ("FORMAL", None, context["candidate_id"])
        assert connection.execute(
            "SELECT state FROM candidate_versions WHERE id = ?", (context["candidate_id"],)
        ).fetchone()[0] == "FORMAL"
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_confirmations WHERE candidate_id = ? AND state = 'FORMALIZED'",
            (context["candidate_id"],),
        ).fetchone()[0] == 2
        expected_binding = connection.execute(
            "SELECT candidate.manifest_artifact_id, manifest.sha256, manifest.byte_length, "
            "candidate.pdf_artifact_id, pdf.sha256, pdf.byte_length, "
            "candidate.svg_artifact_id, svg.sha256, svg.byte_length, "
            "candidate.json_a_artifact_id, candidate.json_a_sha256, candidate.json_a_byte_length, "
            "candidate.json_b_artifact_id, candidate.json_b_sha256, candidate.json_b_byte_length "
            "FROM candidate_versions AS candidate "
            "JOIN artifacts AS manifest ON manifest.id = candidate.manifest_artifact_id "
            "JOIN artifacts AS pdf ON pdf.id = candidate.pdf_artifact_id "
            "JOIN artifacts AS svg ON svg.id = candidate.svg_artifact_id WHERE candidate.id = ?",
            (context["candidate_id"],),
        ).fetchone()
        acknowledgement_bindings = connection.execute(
            "SELECT manifest_artifact_id, manifest_sha256, manifest_byte_length, "
            "pdf_artifact_id, pdf_sha256, pdf_byte_length, svg_artifact_id, svg_sha256, svg_byte_length, "
            "json_a_artifact_id, json_a_sha256, json_a_byte_length, "
            "json_b_artifact_id, json_b_sha256, json_b_byte_length "
            "FROM candidate_acknowledgements WHERE candidate_id = ? ORDER BY actor_role",
            (context["candidate_id"],),
        ).fetchall()
        confirmation_bindings = connection.execute(
            "SELECT manifest_artifact_id, manifest_sha256, manifest_byte_length, "
            "pdf_artifact_id, pdf_sha256, pdf_byte_length, svg_artifact_id, svg_sha256, svg_byte_length, "
            "json_a_artifact_id, json_a_sha256, json_a_byte_length, "
            "json_b_artifact_id, json_b_sha256, json_b_byte_length "
            "FROM candidate_confirmations WHERE candidate_id = ? ORDER BY actor_role",
            (context["candidate_id"],),
        ).fetchall()
        assert len(acknowledgement_bindings) == len(confirmation_bindings) == 2
        assert all(tuple(row) == tuple(expected_binding) for row in acknowledgement_bindings)
        assert all(tuple(row) == tuple(expected_binding) for row in confirmation_bindings)
        with pytest.raises(sqlite3.IntegrityError, match="formal_case_immutable"):
            connection.execute(
                "UPDATE cases SET content_revision = content_revision + 1 WHERE id = ?",
                (context["case_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="formal_candidate_immutable"):
            connection.execute(
                "UPDATE candidate_versions SET version_no = version_no + 1 WHERE id = ?",
                (context["candidate_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="formal_artifact_immutable"):
            connection.execute(
                "UPDATE artifacts SET sha256 = ? WHERE id = ("
                "SELECT pdf_artifact_id FROM candidate_versions WHERE id = ?)",
                ("0" * 64, context["candidate_id"]),
            )
    finally:
        connection.close()

    restarted = create_app(
        {
            "TESTING": False,
            "SECRET_KEY": (context["instance_root"] / "secret.key").read_bytes(),
            "INSTANCE_ROOT": context["instance_root"],
            "DATABASE": context["instance_root"] / "app.sqlite3",
            "DATA_ROOT": context["instance_root"] / "data",
            "MAX_CONTENT_LENGTH": 2_097_152,
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "SESSION_COOKIE_SECURE": False,
            "SESSION_REFRESH_EACH_REQUEST": False,
        }
    )
    restarted_client = restarted.test_client()
    assert login(
        restarted_client,
        "en",
        "owner@example.test",
        context["passwords"]["owner"],
    ).status_code == 302
    assert restarted_client.get(f"/en/cases/{context['case_id']}/formal").status_code == 200
    assert restarted_client.get(
        f"/en/cases/{context['case_id']}/formal/document.pdf"
    ).data == before["document.pdf"]


def test_confirmation_requires_own_acknowledgement_and_csrf(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    no_csrf = context["owner"].post(
        f"/en/cases/{context['case_id']}/candidates/{context['candidate_id']}/confirmations",
        data={"expected_revision": "1", "confirmed": "yes"},
    )
    assert no_csrf.status_code == 400
    response = confirm(context["owner"], context["case_id"], context["candidate_id"])
    assert response.status_code == 422
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM candidate_confirmations").fetchone()[0] == 0
    finally:
        connection.close()


def test_confirmation_rejects_stale_revision_and_duplicate_current_is_idempotent(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    assert acknowledge(context["owner"], context["case_id"], context["candidate_id"]).status_code == 302
    stale = confirm(context["owner"], context["case_id"], context["candidate_id"], revision=999)
    assert stale.status_code == 409
    assert confirm(context["owner"], context["case_id"], context["candidate_id"]).status_code == 302
    assert confirm(context["owner"], context["case_id"], context["candidate_id"]).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        rows = connection.execute(
            "SELECT state FROM candidate_confirmations WHERE candidate_id = ? AND actor_role = 'client'",
            (context["candidate_id"],),
        ).fetchall()
        assert [row["state"] for row in rows] == ["CURRENT"]
    finally:
        connection.close()


def test_withdraw_then_reconfirm_preserves_confirmation_history(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    assert acknowledge(context["owner"], context["case_id"], context["candidate_id"]).status_code == 302
    assert confirm(context["owner"], context["case_id"], context["candidate_id"]).status_code == 302
    withdrawn = context["owner"].post(
        f"/en/cases/{context['case_id']}/candidates/{context['candidate_id']}/confirmations/withdraw",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": "1"},
    )
    assert withdrawn.status_code == 302
    assert confirm(context["owner"], context["case_id"], context["candidate_id"]).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        history = connection.execute(
            "SELECT state FROM candidate_confirmations WHERE candidate_id = ? ORDER BY confirmed_at, id",
            (context["candidate_id"],),
        ).fetchall()
        assert sorted(row[0] for row in history) == ["CURRENT", "WITHDRAWN"]
    finally:
        connection.close()


def test_revision_request_invalidates_round_but_preserves_candidate_bytes(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    before = _component(context["owner"], context, "document.pdf")
    assert acknowledge(context["owner"], context["case_id"], context["candidate_id"]).status_code == 302
    assert confirm(context["owner"], context["case_id"], context["candidate_id"]).status_code == 302
    response = context["contractor"].post(
        f"/en/cases/{context['case_id']}/candidates/{context['candidate_id']}/revision-requests",
        data={
            "csrf_token": csrf(context["contractor"]),
            "expected_revision": "1",
            "reason": "Clarify the acceptance boundary before a new candidate.",
        },
    )
    assert response.status_code == 302
    assert response.location.endswith(f"/en/cases/{context['case_id']}")
    after = _component(context["owner"], context, "document.pdf")
    assert after == before
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        case = connection.execute(
            "SELECT state, active_candidate_id, formal_candidate_id FROM cases WHERE id = ?",
            (context["case_id"],),
        ).fetchone()
        assert tuple(case) == ("JSON_B_REVIEW", None, None)
        assert connection.execute(
            "SELECT state, invalidation_reason FROM candidate_versions WHERE id = ?",
            (context["candidate_id"],),
        ).fetchone()[:] == ("INVALIDATED", "revision_requested")
        assert connection.execute(
            "SELECT state FROM candidate_review_rounds WHERE candidate_id = ?",
            (context["candidate_id"],),
        ).fetchone()[0] == "INVALIDATED"
        assert connection.execute(
            "SELECT state FROM candidate_confirmations WHERE candidate_id = ?",
            (context["candidate_id"],),
        ).fetchone()[0] == "INVALIDATED"
    finally:
        connection.close()
    assert confirm(context["owner"], context["case_id"], context["candidate_id"]).status_code == 404


def test_formal_routes_reject_admin_third_member_and_anonymous(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    for party in (context["owner"], context["contractor"]):
        assert acknowledge(party, context["case_id"], context["candidate_id"]).status_code == 302
        assert confirm(party, context["case_id"], context["candidate_id"]).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        now = utc_now()
        connection.execute(
            "INSERT INTO users(id, email_normalized, display_name, password_hash, system_role, status, "
            "session_version, created_at, updated_at) VALUES (?, 'slice6-third@example.test', 'Third', ?, "
            "'member', 'active', 1, ?, ?)",
            (str(uuid.uuid4()), hash_password("slice6-third-password"), now, now),
        )
    finally:
        connection.close()
    assert context["app"].test_client().get(f"/en/cases/{context['case_id']}/formal").status_code == 401
    for email, password in (
        ("admin@example.test", context["passwords"]["admin"]),
        ("slice6-third@example.test", "slice6-third-password"),
    ):
        outsider = context["app"].test_client()
        assert login(outsider, "en", email, password).status_code == 302
        assert outsider.get(f"/en/cases/{context['case_id']}/formal").status_code == 404
        assert outsider.get(f"/en/cases/{context['case_id']}/formal/document.pdf").status_code == 404


def test_formal_read_revalidates_saved_party_bindings_after_restart(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    for party in (context["owner"], context["contractor"]):
        assert acknowledge(party, context["case_id"], context["candidate_id"]).status_code == 302
        assert confirm(party, context["case_id"], context["candidate_id"]).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        connection.execute("DROP TRIGGER candidate_acknowledgements_append_only_update")
        connection.execute(
            "UPDATE candidate_acknowledgements SET manifest_sha256 = ? "
            "WHERE candidate_id = ? AND actor_role = 'client'",
            ("0" * 64, context["candidate_id"]),
        )
    finally:
        connection.close()
    restarted = create_app(
        {
            "TESTING": False,
            "SECRET_KEY": (context["instance_root"] / "secret.key").read_bytes(),
            "INSTANCE_ROOT": context["instance_root"],
            "DATABASE": context["instance_root"] / "app.sqlite3",
            "DATA_ROOT": context["instance_root"] / "data",
            "SESSION_COOKIE_SECURE": False,
        }
    )
    client = restarted.test_client()
    assert login(client, "en", "owner@example.test", context["passwords"]["owner"]).status_code == 302
    assert client.get(f"/en/cases/{context['case_id']}/formal").status_code == 404
    assert client.get(f"/en/cases/{context['case_id']}/formal/document.pdf").status_code == 404


def test_formal_read_survives_current_asset_rotation_but_rejects_saved_file_tamper(
    demo_runtime,
    monkeypatch,
) -> None:
    context = create_candidate_context(demo_runtime)
    before = _component(context["owner"], context, "document.pdf")
    for party in (context["owner"], context["contractor"]):
        assert acknowledge(party, context["case_id"], context["candidate_id"]).status_code == 302
        assert confirm(party, context["case_id"], context["candidate_id"]).status_code == 302
    original_assets = context["app"].config["DOCUMENT_ASSETS"]
    rotated_assets = replace(
        original_assets,
        catalog_record=FileRecord("catalog.en.json", "application/json", "1" * 64, 1),
        label_record=FileRecord("candidate-labels.v1.json", "application/json", "2" * 64, 1),
    )
    monkeypatch.setattr("app.load_and_validate_document_assets", lambda _root: rotated_assets)
    restarted = create_app(
        {
            "TESTING": False,
            "SECRET_KEY": (context["instance_root"] / "secret.key").read_bytes(),
            "INSTANCE_ROOT": context["instance_root"],
            "DATABASE": context["instance_root"] / "app.sqlite3",
            "DATA_ROOT": context["instance_root"] / "data",
            "SESSION_COOKIE_SECURE": False,
        }
    )
    client = restarted.test_client()
    assert login(client, "en", "owner@example.test", context["passwords"]["owner"]).status_code == 302
    response = client.get(f"/en/cases/{context['case_id']}/formal/document.pdf")
    assert response.status_code == 200
    assert response.data == before

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        relative_path = connection.execute(
            "SELECT relative_path FROM artifacts WHERE id = ("
            "SELECT pdf_artifact_id FROM candidate_versions WHERE id = ?)",
            (context["candidate_id"],),
        ).fetchone()["relative_path"]
    finally:
        connection.close()
    path = context["instance_root"] / "data" / relative_path
    path.chmod(0o600)
    path.write_bytes(before + b"tamper")
    path.chmod(0o400)
    assert client.get(f"/en/cases/{context['case_id']}/formal/document.pdf").status_code == 404


def test_separate_proposal_is_empty_parent_linked_and_idempotent(demo_runtime) -> None:
    context = create_candidate_context(demo_runtime)
    for party in (context["owner"], context["contractor"]):
        assert acknowledge(party, context["case_id"], context["candidate_id"]).status_code == 302
        assert confirm(party, context["case_id"], context["candidate_id"]).status_code == 302
    key = str(uuid.uuid4())
    url = f"/en/cases/{context['case_id']}/separate-proposals"
    first = context["owner"].post(url, data={"csrf_token": csrf(context["owner"]), "idempotency_key": key})
    second = context["owner"].post(url, data={"csrf_token": csrf(context["owner"]), "idempotency_key": key})
    assert first.status_code == second.status_code == 302
    assert first.location == second.location
    child_case_id = first.location.split("/owner/requirements/", 1)[1].split("/", 1)[0]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        child = connection.execute(
            "SELECT parent_case_id, owner_id, contractor_id, state, current_json_a_artifact_id, "
            "current_json_b_artifact_id, active_candidate_id, formal_candidate_id FROM cases WHERE id = ?",
            (child_case_id,),
        ).fetchone()
        assert tuple(child) == (
            context["case_id"], context["owner_id"], None, "REQUIREMENT_DRAFT",
            None, None, None, None,
        )
        draft = connection.execute(
            "SELECT data_json, completed_sections_json FROM requirement_drafts WHERE case_id = ?",
            (child_case_id,),
        ).fetchone()
        assert draft["completed_sections_json"] == "[]"
        assert json.loads(draft["data_json"]) == {
            "project": {},
            "users_and_scenarios": {},
            "pages_and_content": {},
            "features": {},
            "data_and_permissions": {},
            "external_services_and_assets": {},
            "visual_preferences": {},
            "delivery_and_constraints": {},
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM separate_proposal_requests WHERE parent_case_id = ?",
            (context["case_id"],),
        ).fetchone()[0] == 1
    finally:
        connection.close()
    contractor_attempt = context["contractor"].post(
        url,
        data={"csrf_token": csrf(context["contractor"]), "idempotency_key": str(uuid.uuid4())},
    )
    assert contractor_attempt.status_code == 404
