from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from pathlib import Path

import pytest

from app.db import connect_database
from tests.helpers import complete_requirement, csrf, login
from tests.slice4_helpers import activate_private_case
from tests.slice5_helpers import create_json_b_review
from tests.slice6_helpers import acknowledge, confirm, create_candidate_context
from tests.slice7_helpers import create_formal_context, current_workflow, delivery_payload
import tools.build_ui_review as build_ui_review
from tools.build_ui_review import render_artifact, source_freshness


WORKFLOW_STAGES = ("Clarify", "Quote", "Review", "Confirm", "Deliver", "Inspect", "Fix", "Evidence")


def write_public_export_fixture(root: Path) -> tuple[str, str]:
    source_commit = "a" * 40
    source_tree = "b" * 40
    files = {
        "app/static/css/app.css": b"body { color: #123456; }\n",
        "app/templates/base.html": b"<!doctype html><title>Base</title>\n",
        "app/templates/start.html": b"<h1>Start</h1>\n",
    }
    records = []
    for relative, raw in sorted(files.items()):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        records.append({"path": relative, "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = {
        "schema_version": "public-export-manifest/1.0",
        "package_kind": "public_source_candidate",
        "review_date": "2026-10-09",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "files": records,
    }
    (root / "public-export-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256()
    for relative in (
        "app/templates/base.html",
        "app/templates/start.html",
        "app/static/css/app.css",
    ):
        raw = files[relative]
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return source_commit, digest.hexdigest()


def assert_workflow_stage(html: str, expected_stage: str | None) -> None:
    match = re.search(r'<details class="workflow-drawer">(.*?)</details>', html, re.DOTALL)
    assert match is not None
    drawer = match.group(1)
    positions = [drawer.index(f"<strong>{stage}</strong>") for stage in WORKFLOW_STAGES]
    assert positions == sorted(positions)
    if expected_stage is None:
        assert 'aria-current="step"' not in drawer
        assert "workflow-current" not in drawer
    else:
        assert drawer.count('aria-current="step"') == 1
        assert re.search(
            rf'<li class="workflow-current" aria-current="step">\s*<strong>{expected_stage}</strong>',
            drawer,
        )


def test_home_first_viewport_content_contract(demo_runtime) -> None:
    app, _instance_root, passwords = demo_runtime
    with app.test_client() as client:
        response = client.get("/en/")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "one version both sides can approve" in html
        assert "Client clarifies" in html and "Contractor quotes" in html
        assert "Both confirm" in html and "Export evidence" in html
        assert "Client" in html and "Invited contractor" in html and "Administrator" in html
        assert "Invitation required" in html
        assert "Account and service operations only" in html
        assert "owner@example.test" not in html
        assert "contractor@example.test" not in html
        assert "admin@example.test" not in html
        assert "Requirement JSON A" not in html and "JSON B" not in html
        assert 'href="/en/login?account=client&amp;flow=start"' in html
        assert 'href="/en/login?account=contractor"' in html
        assert 'href="/en/login?account=admin"' in html
        assert 'href="/en/">Overview</a>' in html
        assert html.count('data-ui="primary-action"') == 1
        assert "Available after sign in" not in html
        assert 'data-ui="role">Visitor' in html
        assert "Choose the role that matches your work" in html
        assert "SHA-256" not in html and "manifest.json" not in html
        assert login(client, "en", "owner@example.test", passwords["owner"]).status_code == 302
        signed_in = client.get("/en/").get_data(as_text=True)
    assert 'href="/en/owner/guide"' in signed_in
    assert 'href="/en/account"' in signed_in
    assert 'data-ui="role">Member' in signed_in
    assert "Open your workspace or start a requirement" in signed_in
    assert 'data-ui="role">Visitor' not in signed_in
    csp = response.headers["Content-Security-Policy"]
    for directive in ("default-src 'self'", "img-src 'self'", "style-src 'self'", "script-src 'self'", "form-action 'self'"):
        assert directive in csp


def test_login_flow_is_an_action_hint_and_account_roles_stay_case_bound(demo_runtime) -> None:
    app, _instance_root, passwords = demo_runtime
    with app.test_client() as client:
        page = client.get("/en/login?account=client&flow=start").get_data(as_text=True)
        assert "Sign in to start or continue a requirement" in page
        assert 'name="email" type="email" value=""' in page
        assert "assigned during initialization" not in page
        assert 'name="password" type="password" required' in page
        assert 'name="password" type="password" value=' not in page
        assert "Back to overview" in page
        response = client.post(
            "/en/login",
            data={
                "csrf_token": csrf(client),
                "flow": "start",
                "account": "client",
                "email": "owner@example.test",
                "password": passwords["owner"],
            },
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/en/owner/guide")


def test_role_account_hint_is_allowlisted_and_never_grants_authority(demo_runtime) -> None:
    app, _instance_root, passwords = demo_runtime
    with app.test_client() as contractor:
        page = contractor.get("/en/login?account=contractor").get_data(as_text=True)
        assert "An invitation is required" in page
        assert 'name="email" type="email" value=""' in page
        response = contractor.post(
            "/en/login",
            data={
                "csrf_token": csrf(contractor),
                "flow": "",
                "account": "contractor",
                "email": "contractor@example.test",
                "password": passwords["contractor"],
            },
        )
        assert response.headers["Location"].endswith("/en/account")
        assert contractor.get("/en/invitations/00000000-0000-0000-0000-000000000000").status_code == 404

    with app.test_client() as member:
        page = member.get("/en/login?account=unknown").get_data(as_text=True)
        assert "Sign in to your workspace" in page
        assert "owner@example.test" not in page
        assert "contractor@example.test" not in page
        assert "admin@example.test" not in page
        response = member.post(
            "/en/login",
            data={
                "csrf_token": csrf(member),
                "flow": "",
                "account": "admin",
                "email": "owner@example.test",
                "password": passwords["owner"],
            },
        )
        assert response.headers["Location"].endswith("/en/account")

    with app.test_client() as admin:
        page = admin.get("/en/login?account=admin").get_data(as_text=True)
        assert "Administrators cannot read private case content" in page
        assert 'name="email" type="email" value=""' in page
        response = admin.post(
            "/en/login",
            data={
                "csrf_token": csrf(admin),
                "flow": "start",
                "account": "client",
                "email": "admin@example.test",
                "password": passwords["admin"],
            },
        )
        assert response.headers["Location"].endswith("/en/admin/accounts")


def test_generic_login_routes_by_system_role_without_role_claims(demo_runtime) -> None:
    app, _instance_root, passwords = demo_runtime
    with app.test_client() as member:
        response = login(member, "en", "owner@example.test", passwords["owner"])
        assert response.headers["Location"].endswith("/en/account")
        assert member.get("/en/login").headers["Location"].endswith("/en/account")

    with app.test_client() as admin:
        response = login(admin, "en", "admin@example.test", passwords["admin"])
        assert response.headers["Location"].endswith("/en/admin/accounts")
        assert admin.get("/en/owner/guide").status_code == 404
        homepage = admin.get("/en/").get_data(as_text=True)
        assert "Start requirement" not in homepage
        assert "Open administration" in homepage
        assert 'data-ui="role">Administrator' in homepage
        assert "Choose the role that matches your work" not in homepage


def test_incomplete_requirement_reopens_at_first_missing_section(demo_runtime) -> None:
    app, _instance_root, passwords = demo_runtime
    with app.test_client() as client:
        assert login(client, "en", "owner@example.test", passwords["owner"]).status_code == 302
        created = client.post(
            "/en/owner/requirements",
            data={"csrf_token": csrf(client), "title": "Incomplete Website Request"},
        )
        assert created.status_code == 302
        case_id = created.location.split("/")[4]
        account = client.get("/en/account").get_data(as_text=True)
        review = client.get(f"/en/owner/requirements/{case_id}/review").get_data(as_text=True)
    assert f'href="/en/owner/requirements/{case_id}/sections/1"' in account
    assert f'href="/en/owner/requirements/{case_id}/sections/1"' in review
    assert "Continue requirement · Section 1" in review


def test_requirement_shell_exposes_role_status_task_and_one_primary_action(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    with app.test_client() as client:
        assert login(client, "en", "owner@example.test", passwords["owner"]).status_code == 302
        case_id = complete_requirement(client, instance_root)
        response = client.get(f"/en/owner/requirements/{case_id}/sections/6")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-ui="role">Client' in html
    assert 'data-ui="status">Clarify · Step 6 of 8' in html
    assert 'data-ui="current-task"' in html
    assert html.count('data-ui="primary-action"') == 1
    assert '<details class="workflow-drawer" open' not in html
    assert_workflow_stage(html, "Clarify")

    jump = re.search(r'<details class="requirement-jump">(.*?)</details>', html, re.DOTALL)
    assert jump is not None
    jump_html = jump.group(1)
    assert "Jump to another section" in jump_html
    for group in ("Foundation", "Structure", "Operations", "Delivery"):
        assert f">{group}<" in jump_html
    for step in range(1, 9):
        assert f'href="/en/owner/requirements/{case_id}/sections/{step}"' in jump_html
    assert jump_html.count('aria-current="step"') == 1
    assert 'href="/en/owner/requirements/' + case_id + '/sections/6" aria-current="step"' in jump_html
    assert "step-rail" not in html
    assert html.count('<details class="guided-step" open>') == 1


def test_unset_requirement_choices_render_and_round_trip_neutral_defaults(demo_runtime) -> None:
    app, _instance_root, passwords = demo_runtime
    feature_codes = ("booking", "form", "membership", "search", "cart", "admin", "notification", "other")
    service_codes = ("domain", "hosting", "payment", "maps", "social", "email", "api")
    asset_codes = ("existing_site", "logo", "brand_materials", "database")

    def selected_value(html: str, name: str) -> str:
        select = re.search(rf'<select name="{re.escape(name)}">(.*?)</select>', html, re.DOTALL)
        assert select is not None
        selected = re.findall(r'<option value="([^"]+)" selected>', select.group(1))
        assert len(selected) == 1
        return selected[0]

    def revision(html: str) -> str:
        match = re.search(r'<input type="hidden" name="expected_revision" value="(\d+)">', html)
        assert match is not None
        return match.group(1)

    with app.test_client() as client:
        assert login(client, "en", "owner@example.test", passwords["owner"]).status_code == 302
        created = client.post(
            "/en/owner/requirements",
            data={"csrf_token": csrf(client), "title": "Neutral defaults"},
        )
        assert created.status_code == 302
        case_id = created.headers["Location"].split("/")[4]

        feature_path = f"/en/owner/requirements/{case_id}/sections/4"
        feature_html = client.get(feature_path).get_data(as_text=True)
        assert {selected_value(feature_html, f"feature_{code}") for code in feature_codes} == {"undecided"}
        feature_payload = {"csrf_token": csrf(client), "expected_revision": revision(feature_html)}
        for code in feature_codes:
            feature_payload[f"feature_{code}"] = selected_value(feature_html, f"feature_{code}")
            feature_payload[f"rules_{code}"] = ""
        saved = client.post(feature_path, data=feature_payload)
        assert saved.status_code == 302
        assert saved.headers["Location"].endswith(f"/en/owner/requirements/{case_id}/sections/5")
        feature_html = client.get(feature_path).get_data(as_text=True)
        assert {selected_value(feature_html, f"feature_{code}") for code in feature_codes} == {"undecided"}

        feature_payload = {"csrf_token": csrf(client), "expected_revision": revision(feature_html)}
        for code in feature_codes:
            feature_payload[f"feature_{code}"] = (
                "desired" if code == "booking" else "excluded" if code == "cart" else "undecided"
            )
            feature_payload[f"rules_{code}"] = "Call before booking" if code == "booking" else ""
        assert client.post(feature_path, data=feature_payload).status_code == 302
        feature_html = client.get(feature_path).get_data(as_text=True)
        assert selected_value(feature_html, "feature_booking") == "desired"
        assert selected_value(feature_html, "feature_cart") == "excluded"
        assert all(
            selected_value(feature_html, f"feature_{code}") == "undecided"
            for code in feature_codes
            if code not in {"booking", "cart"}
        )

        external_path = f"/en/owner/requirements/{case_id}/sections/6"
        external_html = client.get(external_path).get_data(as_text=True)
        assert {selected_value(external_html, f"service_{code}") for code in service_codes} == {"to_select"}
        assert {selected_value(external_html, f"asset_{code}") for code in asset_codes} == {"to_select"}
        external_payload = {"csrf_token": csrf(client), "expected_revision": revision(external_html)}
        for code in service_codes:
            external_payload[f"service_{code}"] = selected_value(external_html, f"service_{code}")
            external_payload[f"service_{code}_notes"] = ""
        for code in asset_codes:
            external_payload[f"asset_{code}"] = selected_value(external_html, f"asset_{code}")
            external_payload[f"asset_{code}_notes"] = ""
        saved = client.post(external_path, data=external_payload)
        assert saved.status_code == 302
        assert saved.headers["Location"].endswith(f"/en/owner/requirements/{case_id}/sections/7")
        external_html = client.get(external_path).get_data(as_text=True)
        assert {selected_value(external_html, f"service_{code}") for code in service_codes} == {"to_select"}
        assert {selected_value(external_html, f"asset_{code}") for code in asset_codes} == {"to_select"}

        external_payload = {"csrf_token": csrf(client), "expected_revision": revision(external_html)}
        for code in service_codes:
            external_payload[f"service_{code}"] = "existing" if code == "domain" else "to_select"
            external_payload[f"service_{code}_notes"] = "Client-owned" if code == "domain" else ""
        for code in asset_codes:
            external_payload[f"asset_{code}"] = "contractor_proposal" if code == "logo" else "to_select"
            external_payload[f"asset_{code}_notes"] = "Propose source files" if code == "logo" else ""
        assert client.post(external_path, data=external_payload).status_code == 302
        external_html = client.get(external_path).get_data(as_text=True)
        assert selected_value(external_html, "service_domain") == "existing"
        assert selected_value(external_html, "asset_logo") == "contractor_proposal"
        assert all(selected_value(external_html, f"service_{code}") == "to_select" for code in service_codes[1:])
        assert all(
            selected_value(external_html, f"asset_{code}") == "to_select"
            for code in asset_codes
            if code != "logo"
        )


def test_workflow_context_tracks_quote_and_review_without_marking_prior_stages_done(demo_runtime) -> None:
    quote = activate_private_case(demo_runtime)
    quote_html = quote["owner"].get(f"/en/cases/{quote['case_id']}").get_data(as_text=True)
    assert 'data-ui="status">Quote · Private case active' in quote_html
    assert "Waiting for the contractor&#39;s proposal" in quote_html
    assert "Resolve scope questions with the contractor" not in quote_html
    assert_workflow_stage(quote_html, "Quote")
    assert '<p class="eyebrow">Quote · Private Case Baseline</p>' in quote_html
    assert quote_html.count('<details class="case-section-disclosure">') == 2
    assert '<details class="case-section-disclosure" open' not in quote_html
    assert quote_html.count('class="empty-state"') == 2

    review = create_json_b_review(demo_runtime)
    review_html = review["owner"].get(
        f"/en/cases/{review['case_id']}/json-b/reviews/1"
    ).get_data(as_text=True)
    assert 'data-ui="status">Review · Proposal review in progress' in review_html
    assert "Raise an objection to request a change" in review_html
    assert 'name="field_pointer"' in review_html
    assert_workflow_stage(review_html, "Review")
    assert "workflow-done" not in quote_html + review_html


def test_imported_invitation_pages_point_both_parties_to_private_case(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    case_id = context["case_id"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        invitation_id = connection.execute(
            "SELECT id FROM invitations WHERE case_id = ? AND state = 'accepted'", (case_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    owner_html = context["owner"].get(
        f"/en/owner/requirements/{case_id}/publish"
    ).get_data(as_text=True)
    contractor_html = context["contractor"].get(
        f"/en/invitations/{invitation_id}"
    ).get_data(as_text=True)
    for html in (owner_html, contractor_html):
        assert "Open the private case" in html
        assert f'href="/en/cases/{case_id}"' in html
        assert "Waiting for contractor requirement import" not in html
        assert "Import the verified requirement file" not in html
        assert 'data-ui="status">Quote · Invitation Accepted' in html


def test_historical_invitation_uses_current_delivery_stage(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    case_id = context["case_id"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        invitation_id = connection.execute(
            "SELECT id FROM invitations WHERE case_id = ? AND state = 'accepted'", (case_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert context["contractor"].post(
        f"/en/cases/{case_id}/deliveries",
        data=delivery_payload(context["contractor"], context["criterion_id"]),
    ).status_code == 302
    response = context["contractor"].get(f"/en/invitations/{invitation_id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-ui="status">Inspect · Invitation Accepted' in html
    assert "Open the private case" in html
    assert f'href="/en/cases/{case_id}/delivery"' in html
    assert "Import the verified requirement file" not in html
    destination = context["contractor"].get(f"/en/cases/{case_id}/delivery")
    assert destination.status_code == 200
    assert 'data-ui="status">Inspect · Delivery submitted' in destination.get_data(as_text=True)


def test_step_eight_groups_fields_and_review_exposes_human_summary(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    with app.test_client() as client:
        assert login(client, "en", "owner@example.test", passwords["owner"]).status_code == 302
        case_id = complete_requirement(client, instance_root)
        step_eight = client.get(f"/en/owner/requirements/{case_id}/sections/8")
        step_seven = client.get(f"/en/owner/requirements/{case_id}/sections/7")
        review = client.get(f"/en/owner/requirements/{case_id}/review")
    assert step_seven.status_code == 200
    step_seven_html = step_seven.get_data(as_text=True)
    assert 'data-requires-visual-choice' in step_seven_html
    assert 'Add at least one brand style or one direction to avoid' in step_seven_html
    assert step_eight.status_code == 200
    step_html = step_eight.get_data(as_text=True)
    assert step_html.count('class="split-grid"') >= 2
    assert step_html.count('class="form-grid"') >= 4
    assert re.search(r'<details class="guided-step" open>\s*<summary>Deliverables and acceptance · Required</summary>', step_html)
    assert '<details class="guided-step"><summary>Ownership and operations · Required</summary>' in step_html
    assert '<details class="guided-step"><summary>Budget</summary>' in step_html
    for name in (
        "deliverables", "account_ownership", "acceptance_expectations",
        "source_code_ownership", "deployment_expectation", "maintenance_expectation",
    ):
        assert re.search(rf'name="{name}" required', step_html)
    assert re.search(r'class="form-grid"><label>Budget Status.*name="budget_status".*<label>Budget Currency.*name="budget_currency"', step_html, re.DOTALL)
    assert re.search(r'class="form-grid"><label>Minimum Budget Amount.*name="budget_min_amount".*<label>Maximum Budget Amount.*name="budget_max_amount"', step_html, re.DOTALL)

    assert review.status_code == 200
    review_html = review.get_data(as_text=True)
    assert "Review the summary, then save this requirement version." in review_html
    assert "Finish the remaining sections before saving a version." not in review_html
    assert re.search(r'href="/en/owner/requirements/[^\"]+/sections/1">Edit requirement sections</a>', review_html)
    for heading in ("Pages", "Required features", "Deliverables", "Acceptance expectations", "Items to resolve"):
        assert f">{heading}<" in review_html
    for value in ("Services and appointments", "Booking", "source", "listed criteria", "Hosting"):
        assert value in review_html
    assert '<summary>Review people, scope, services, and ownership</summary>' in review_html
    for value in ("Users and primary flow", "Included and excluded features", "Data and access", "External services and assets", "Ownership and maintenance", "Returning clients", "quoted separately"):
        assert value in review_html
    assert re.search(r'<details class="technical-disclosure">\s*<summary>Verify this saved requirement</summary>', review_html)


def test_expired_quote_review_offers_revision_instead_of_confirmation(demo_runtime, monkeypatch) -> None:
    context = create_json_b_review(demo_runtime)
    monkeypatch.setattr("app.routes.localized._quote_has_expired", lambda _value: True)
    response = context["contractor"].get(
        f"/en/cases/{context['case_id']}/json-b/reviews/1"
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Revise the expired proposal" in html
    assert "This quote has expired" in html
    assert "Create Confirmation Version" not in html
    assert html.count('data-ui="primary-action"') == 1


def test_proposal_review_task_changes_when_contractor_starts_revision(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    case_id = context["case_id"]
    contractor = context["contractor"]
    path = f"/en/cases/{case_id}/json-b/reviews/1"
    assert contractor.post(f"{path}/revise", data={"csrf_token": csrf(contractor)}).status_code == 302
    html = contractor.get(path).get_data(as_text=True)
    assert "Continue Proposal Revision" in html
    assert "Raise an objection to request a change" not in html
    assert "Create a confirmation version or revise the proposal" not in html
    assert html.count('data-ui="primary-action"') == 1


def test_requirement_review_keeps_one_primary_action_across_release_states(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    with app.test_client() as client:
        assert login(client, "en", "owner@example.test", passwords["owner"]).status_code == 302
        case_id = complete_requirement(client, instance_root)
        draft_html = client.get(f"/en/owner/requirements/{case_id}/review").get_data(as_text=True)
        assert draft_html.count('data-ui="primary-action"') == 1
        connection = connect_database(instance_root / "app.sqlite3")
        try:
            revision = connection.execute("SELECT content_revision FROM cases WHERE id = ?", (case_id,)).fetchone()[0]
        finally:
            connection.close()
        generated = client.post(
            f"/en/owner/requirements/{case_id}/json-a",
            data={"csrf_token": csrf(client), "expected_revision": str(revision), "review_confirmed": "yes"},
        )
        assert generated.status_code == 302
        response = client.get(f"/en/owner/requirements/{case_id}/review")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert html.count('data-ui="primary-action"') == 1
        assert "Create a new requirement version" in html
        assert "Save Requirement Version" in html

        connection = connect_database(instance_root / "app.sqlite3")
        try:
            artifact = connection.execute(
                "SELECT relative_path FROM artifacts WHERE case_id = ? AND kind = 'json_a' AND lifecycle = 'active'",
                (case_id,),
            ).fetchone()
            revision = connection.execute("SELECT content_revision FROM cases WHERE id = ?", (case_id,)).fetchone()[0]
        finally:
            connection.close()
        raw = (instance_root / "data" / artifact["relative_path"]).read_bytes()
        created = client.post(
            f"/en/owner/requirements/{case_id}/invitations",
            data={
                "csrf_token": csrf(client),
                "expected_revision": str(revision),
                "contractor_email": "contractor@example.test",
                "json_a_file": (io.BytesIO(raw), "requirements.json"),
            },
        )
        assert created.status_code == 200
        pending_html = client.get(f"/en/owner/requirements/{case_id}/review").get_data(as_text=True)
        assert pending_html.count('data-ui="primary-action"') == 1
        assert "View invitation status" in pending_html
        assert f'href="/en/owner/requirements/{case_id}/publish"' in client.get("/en/account").get_data(as_text=True)
        invitation_match = re.search(rb'/en/invitations/([0-9a-f-]{36})', created.data)
        code_match = re.search(rb'class="code-panel code-value"><code>([^<]+)</code>', created.data)
        assert invitation_match is not None and code_match is not None

        contractor = app.test_client()
        assert login(contractor, "en", "contractor@example.test", passwords["contractor"]).status_code == 302
        invitation_id = invitation_match.group(1).decode("ascii")
        assert contractor.post(
            f"/en/invitations/{invitation_id}/accept",
            data={"csrf_token": csrf(contractor), "invitation_code": code_match.group(1).decode("ascii")},
        ).status_code == 302
        assert contractor.post(
            f"/en/cases/{case_id}/json-a/import",
            data={"csrf_token": csrf(contractor), "expected_revision": str(revision), "json_a_file": (io.BytesIO(raw), "requirements.json")},
        ).status_code == 302
        old_invitation = contractor.get(f"/en/invitations/{invitation_id}").get_data(as_text=True)
        assert f'href="/en/cases/{case_id}"' in old_invitation
        assert f'href="/en/cases/{case_id}/json-a/import"' not in old_invitation
        active_html = client.get(f"/en/owner/requirements/{case_id}/review").get_data(as_text=True)
        assert active_html.count('data-ui="primary-action"') == 1
        assert '<p class="eyebrow">Quote · Review</p>' in active_html


def test_candidate_and_formal_pages_keep_technical_values_collapsed(demo_runtime) -> None:
    candidate = create_candidate_context(demo_runtime)
    page = candidate["owner"].get(candidate["candidate_url"])
    assert page.status_code == 200
    candidate_html = page.get_data(as_text=True)
    assert candidate_html.count('data-ui="primary-action"') == 1
    assert "Your action · Review and acknowledge this version" in candidate_html
    assert_workflow_stage(candidate_html, "Confirm")
    assert 'data-ui="status">Confirm · Confirmation in progress' in candidate_html
    assert '<p class="eyebrow">Confirm · Shared version</p>' in candidate_html
    assert re.search(r'<details class="technical-disclosure">\s*<summary>Verify this confirmation version</summary>', candidate_html)
    assert '<details class="technical-disclosure" open' not in candidate_html

    assert acknowledge(candidate["owner"], candidate["case_id"], candidate["candidate_id"]).status_code == 302
    acknowledged = candidate["owner"].get(candidate["candidate_url"]).get_data(as_text=True)
    assert acknowledged.count('data-ui="primary-action"') == 1
    assert "Your action · Confirm this version or request a revision" in acknowledged
    assert confirm(candidate["owner"], candidate["case_id"], candidate["candidate_id"]).status_code == 302
    waiting = candidate["owner"].get(candidate["candidate_url"]).get_data(as_text=True)
    assert waiting.count('data-ui="primary-action"') == 0
    assert "Waiting for the other party confirmation" in waiting
    invalidated = candidate["owner"].post(
        f"/en/cases/{candidate['case_id']}/candidates/{candidate['candidate_id']}/revision-requests",
        data={"csrf_token": csrf(candidate["owner"]), "expected_revision": "1", "reason": "Clarify the delivery boundary"},
    )
    assert invalidated.status_code == 302
    invalidated_html = candidate["owner"].get(candidate["candidate_url"]).get_data(as_text=True)
    assert invalidated_html.count('data-ui="primary-action"') == 0
    assert "Revision requested · New version required" in invalidated_html
    assert "Return to the private case for the revision" in invalidated_html
    assert "This version cannot be confirmed" in invalidated_html
    assert "Your action · Confirm this version" not in invalidated_html
    assert '<p class="eyebrow">Review · Shared version</p>' in invalidated_html

    formal = create_formal_context(demo_runtime)
    page = formal["owner"].get(f"/en/cases/{formal['case_id']}/formal")
    assert page.status_code == 200
    formal_html = page.get_data(as_text=True)
    assert formal_html.count('data-ui="primary-action"') == 1
    assert "Continue to delivery review" in formal_html
    case_html = formal["owner"].get(f"/en/cases/{formal['case_id']}").get_data(as_text=True)
    assert "Open the confirmed version" in case_html
    assert "Review the current proposal" not in case_html
    assert_workflow_stage(formal_html, "Deliver")
    assert 'data-ui="status">Deliver · Confirmed version ready' in formal_html
    assert re.search(r'<details class="technical-disclosure">\s*<summary>Verify this confirmed version</summary>', formal_html)
    assert '<details class="technical-disclosure" open' not in formal_html
    requirement_html = formal["owner"].get(
        f"/en/owner/requirements/{formal['case_id']}/review"
    ).get_data(as_text=True)
    assert "This saved requirement is included in the confirmed agreement." in requirement_html
    assert "This Version Is Not Yet the Parties' Formal Agreement" not in requirement_html
    assert f'href="/en/cases/{formal["case_id"]}/formal"' in requirement_html


def test_admin_shell_states_private_case_boundary(demo_runtime) -> None:
    app, _instance_root, passwords = demo_runtime
    with app.test_client() as client:
        assert login(client, "en", "admin@example.test", passwords["admin"]).status_code == 302
        response = client.get("/en/admin/accounts")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-ui="role">Administrator' in html
    assert "They cannot access private case content" in html
    assert "/cases/" not in html
    assert '<nav class="segmented-nav" aria-label="Administration sections">' in html
    assert 'href="/en/admin/accounts" aria-current="page"' in html
    assert html.count('class="admin-slide') == 4
    assert "3 approved accounts" in html
    assert "3</strong> active" in html
    assert "0</strong> disabled" in html
    assert "0</strong> pending" in html
    assert '<details class="admin-details"><summary>Manage member access</summary>' in html
    assert '<details class="admin-details" open' not in html
    assert html.count('data-ui="primary-action"') == 0
    topbar = html.split("</header>", 1)[0]
    assert topbar.count(">Administration<") == 1
    assert ">Service health<" not in topbar

    with app.test_client() as client:
        assert login(client, "en", "admin@example.test", passwords["admin"]).status_code == 302
        health_html = client.get("/en/admin/service-health").get_data(as_text=True)
    assert 'href="/en/admin/service-health" aria-current="page"' in health_html
    assert "Service ready" in health_html
    assert "Core service checks are ready" in health_html
    assert "Document rendering assets" in health_html
    assert '<details class="admin-details"><summary>Review service checks</summary>' in health_html
    assert health_html.count('data-ui="primary-action"') == 0


def test_ui_copy_contract_uses_reviewed_catalog_bytes_and_self_only_assets(project_root: Path) -> None:
    catalog = project_root / "app/i18n/catalog.en.json"
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == "9d2242dddcb8a8ca89f8c68c62a20b8c8c936c9da2b008f8b7d0a0e80e635fc6"
    for template in (project_root / "app/templates").glob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert not re.search(r'(?:src|href|action|formaction)=["\']https?://', text), template
        assert "target=\"_blank\"" not in text
    for asset in (project_root / "app/static").rglob("*"):
        if asset.is_file():
            text = asset.read_text(encoding="utf-8")
            assert "http://" not in text and "https://" not in text, asset
    app_source = (project_root / "app/__init__.py").read_text(encoding="utf-8")
    assert "default-src 'self'" in app_source
    assert "img-src 'self'" in app_source
    assert "style-src 'self'" in app_source
    assert "script-src 'self'" in app_source
    assert "form-action 'self'" in app_source


def test_every_post_form_preserves_its_action_encoding_and_field_contract(project_root: Path) -> None:
    expected = {
        "admin.html": [("/{{ locale }}/admin/accounts/{{ user.id }}/status", "", ("csrf_token", "status"))],
        "base.html": [("/{{ locale }}/logout", "", ("csrf_token",))],
        "candidate_review.html": [
            ("/{{ locale }}/cases/{{ case_id }}/candidates/{{ candidate.id }}/acknowledgements", "", ("csrf_token", "expected_revision", "acknowledged")),
            ("/{{ locale }}/cases/{{ case_id }}/candidates/{{ candidate.id }}/confirmations", "", ("csrf_token", "expected_revision", "confirmed")),
            ("/{{ locale }}/cases/{{ case_id }}/candidates/{{ candidate.id }}/confirmations/withdraw", "", ("csrf_token", "expected_revision")),
            ("/{{ locale }}/cases/{{ case_id }}/candidates/{{ candidate.id }}/revision-requests", "", ("csrf_token", "expected_revision", "reason")),
        ],
        "case_room.html": [
            ("/{{ locale }}/cases/{{ case.id }}/issues/{{ primary_issue.id }}/events", "", ("csrf_token", "expected_revision", "note", "action", "action", "action", "action", "action", "action", "action")),
            ("/{{ locale }}/cases/{{ case.id }}/messages", "", ("csrf_token", "idempotency_key", "correction_of_id", "body")),
            ("/{{ locale }}/cases/{{ case.id }}/messages", "", ("csrf_token", "idempotency_key", "body")),
            ("/{{ locale }}/cases/{{ case.id }}/issues", "", ("csrf_token", "summary")),
        ],
        "delivery_dashboard.html": [
            ("/{{ locale }}/cases/{{ view.formal.case.id }}/evidence-exports?download=1", "", ("csrf_token",)),
            ("/{{ locale }}/cases/{{ view.formal.case.id }}/deliveries", "", ("csrf_token", "idempotency_key", "summary", "note_{{ criterion.criterion_id }}", "evidence_{{ criterion.criterion_id }}", "test_outcome_{{ criterion.criterion_id }}", "observation_{{ criterion.criterion_id }}")),
            ("/{{ locale }}/cases/{{ view.formal.case.id }}/deliveries/{{ view.workflow.current_delivery_id }}/begin-inspection", "", ("csrf_token", "expected_revision")),
            ("/{{ locale }}/cases/{{ view.formal.case.id }}/deliveries/{{ view.workflow.current_delivery_id }}/inspections", "", ("csrf_token", "expected_revision", "idempotency_key", "result_{{ criterion.criterion_id }}", "reason_{{ criterion.criterion_id }}", "explicit_acceptance")),
            ("/{{ locale }}/cases/{{ view.formal.case.id }}/disputes", "", ("csrf_token", "expected_revision", "reason")),
            ("/{{ locale }}/cases/{{ view.formal.case.id }}/disputes/{{ view.dispute.id }}/positions", "", ("csrf_token", "position_text")),
        ],
        "formal_verification.html": [("/{{ locale }}/cases/{{ formal.case.id }}/separate-proposals", "", ("csrf_token", "idempotency_key"))],
        "invitation_review.html": [
            ("/{{ locale }}/invitations/{{ invitation.id }}/accept", "", ("csrf_token", "expected_revision", "invitation_code")),
            ("/{{ locale }}/invitations/{{ invitation.id }}/reject", "", ("csrf_token", "expected_revision", "invitation_code")),
        ],
        "json_a_import.html": [("/{{ locale }}/cases/{{ case.id }}/json-a/import", "multipart/form-data", ("csrf_token", "expected_revision", "json_a_file"))],
        "json_b_draft.html": [
            ("", "", ("csrf_token", "expected_revision", "draft_json")),
            ("/{{ locale }}/cases/{{ case_id }}/json-b/draft/export", "", ("csrf_token", "expected_revision")),
        ],
        "json_b_import.html": [("", "multipart/form-data", ("csrf_token", "artifact_id", "json_b_file"))],
        "json_b_review.html": [
            ("/{{ locale }}/cases/{{ case.id }}/candidates", "", ("csrf_token",)),
            ("/{{ locale }}/cases/{{ case.id }}/json-b/reviews/{{ review.version_no }}/revise", "", ("csrf_token",)),
            ("/{{ locale }}/cases/{{ case.id }}/json-b/reviews/{{ review.version_no }}/objections", "", ("csrf_token", "field_pointer", "summary")),
        ],
        "login.html": [("", "", ("csrf_token", "flow", "account", "email", "password"))],
        "owner_guide.html": [("/{{ locale }}/owner/requirements", "", ("csrf_token", "title"))],
        "publish_invitation.html": [
            ("/{{ locale }}/owner/requirements/{{ case.id }}/invitations/{{ invitation.id }}/cancel-before-import", "", ("csrf_token", "expected_revision")),
            ("/{{ locale }}/owner/requirements/{{ case.id }}/invitations/{{ invitation.id }}/revoke", "", ("csrf_token", "expected_revision")),
            ("/{{ locale }}/owner/requirements/{{ case.id }}/invitations", "multipart/form-data", ("csrf_token", "expected_revision", "expected_artifact_id", "expected_sha256", "json_a_file", "contractor_email")),
            ("/{{ locale }}/owner/requirements/{{ case.id }}/publish/preview", "multipart/form-data", ("csrf_token", "expected_revision", "json_a_file")),
        ],
        "requirement_section.html": [("", "", ("csrf_token", "expected_revision", "{{ name }}", "{{ name }}", "{{ name }}", "pages", "language_requirements", "feature_{{ code }}", "rules_{{ code }}", "feature_{{ code }}", "rules_{{ code }}", "data_categories", "read_rights", "write_rights", "contains_customer_data", "{{ name }}", "service_{{ code }}", "service_{{ code }}_notes", "asset_{{ code }}", "asset_{{ code }}_notes", "{{ name }}", "reference_sites", "deliverables", "account_ownership", "launch_conditions", "acceptance_expectations", "constraints", "source_code_ownership", "deployment_expectation", "maintenance_expectation", "required_deadline", "budget_status", "budget_currency", "budget_min_amount", "budget_max_amount"))],
        "review.html": [("/{{ locale }}/owner/requirements/{{ case.id }}/json-a", "", ("csrf_token", "expected_revision", "review_confirmed"))],
    }

    actual: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {}
    for template in sorted((project_root / "app/templates").glob("*.html")):
        forms = []
        for attributes, body in re.findall(r"<form\b([^>]*)>(.*?)</form>", template.read_text(encoding="utf-8"), re.DOTALL):
            method = re.search(r'\bmethod="([^"]+)"', attributes)
            if method is None or method.group(1).lower() != "post":
                continue
            action = re.search(r'\baction="([^"]*)"', attributes)
            enctype = re.search(r'\benctype="([^"]*)"', attributes)
            fields = tuple(re.findall(r'\bname="([^"]+)"', body))
            forms.append((action.group(1) if action else "", enctype.group(1) if enctype else "", fields))
        if forms:
            actual[template.name] = forms
    assert actual == expected


def test_technical_and_history_disclosures_default_closed(project_root: Path) -> None:
    for template in (project_root / "app/templates").glob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert not re.search(r'<details[^>]*class="[^"]*(?:technical-disclosure|history-disclosure)[^"]*"[^>]*\sopen(?:\s|=|>)', text), template


def test_progressive_navigation_and_case_sections_default_closed(project_root: Path) -> None:
    for template in (project_root / "app/templates").glob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert not re.search(
            r'<details[^>]*class="[^"]*(?:workflow-drawer|requirement-jump|case-section-disclosure)[^"]*"[^>]*\sopen(?:\s|=|>)',
            text,
        ), template


def test_ui_review_uses_visible_page_titles_and_records_source_freshness() -> None:
    head, digest = source_freshness()
    artifact = render_artifact(
        [("stale label", "<!doctype html><html><body><h1>Current <span>Page</span> Title</h1></body></html>")],
        1920,
        1080,
    )
    assert "01 · Current Page Title" in artifact
    assert "stale label" not in artifact
    assert f"Source HEAD <code>{head}</code>" in artifact
    assert f"template/CSS SHA-256 <code>{digest}</code>" in artifact
    assert "1 synthetic, de-identified 1920×1080 review surfaces" in artifact


def test_ui_review_source_freshness_uses_valid_public_export_manifest(tmp_path: Path, monkeypatch) -> None:
    expected_commit, expected_digest = write_public_export_fixture(tmp_path)
    monkeypatch.setattr(build_ui_review, "PROJECT_ROOT", tmp_path)

    assert build_ui_review.source_freshness() == (expected_commit, expected_digest)


def test_ui_review_source_freshness_rejects_missing_and_malformed_export_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(build_ui_review, "PROJECT_ROOT", tmp_path)
    (tmp_path / "app/templates").mkdir(parents=True)
    (tmp_path / "app/templates/base.html").write_text("<title>Base</title>\n", encoding="utf-8")
    (tmp_path / "app/static/css").mkdir(parents=True)
    (tmp_path / "app/static/css/app.css").write_text("body {}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="public_export_manifest_missing"):
        build_ui_review.source_freshness()

    (tmp_path / "public-export-manifest.json").write_text("{invalid", encoding="utf-8")
    with pytest.raises(RuntimeError, match="public_export_manifest_invalid"):
        build_ui_review.source_freshness()


def test_ui_review_source_freshness_rejects_invalid_contract_and_stale_inventory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    write_public_export_fixture(tmp_path)
    monkeypatch.setattr(build_ui_review, "PROJECT_ROOT", tmp_path)
    manifest_path = tmp_path / "public-export-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["package_kind"] = "untrusted_package"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="public_export_manifest_contract_invalid"):
        build_ui_review.source_freshness()

    write_public_export_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["review_date"] = "2026-02-30"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="public_export_manifest_contract_invalid"):
        build_ui_review.source_freshness()

    write_public_export_fixture(tmp_path)
    (tmp_path / "app/templates/base.html").write_text("<title>Changed</title>\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="public_export_inventory_mismatch:app/templates/base.html"):
        build_ui_review.source_freshness()


def test_delivery_lifecycle_cases_link_back_to_delivery_workspace(project_root: Path) -> None:
    account = (project_root / "app/templates/account.html").read_text(encoding="utf-8")
    for state in ("DELIVERED", "UNDER_ACCEPTANCE", "REMEDIATION", "REINSPECTION", "ACCEPTED", "DISPUTED"):
        assert state in account
    assert '/cases/{{ case.id }}/delivery' in account


def test_workspace_uses_delivery_workflow_state_for_both_parties(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    case_id = context["case_id"]
    delivery_path = f'href="/en/cases/{case_id}/delivery"'

    def assert_workspace_state(expected_state: str, expected_label: str, expected_stage: str) -> None:
        workflow = current_workflow(context["instance_root"], case_id)
        assert workflow["state"] == expected_state
        for party in (context["owner"], context["contractor"]):
            response = party.get("/en/account")
            assert response.status_code == 200
            html = response.get_data(as_text=True)
            match = re.search(
                rf'<a class="case-card" href="/en/cases/{re.escape(str(case_id))}/delivery">(.*?)</a>',
                html,
                re.DOTALL,
            )
            assert match is not None
            assert delivery_path in html
            assert f'<span class="case-stage">{expected_stage}</span>' in match.group(1)
            assert f"<span>{expected_label}</span>" in match.group(1)
            delivery_html = party.get(f"/en/cases/{case_id}/delivery").get_data(as_text=True)
            assert f'data-ui="status">{expected_stage} · {expected_label}' in delivery_html
            assert_workflow_stage(delivery_html, expected_stage)
            assert f'<p class="eyebrow">{expected_stage} · Review together</p>' in delivery_html

    assert context["contractor"].post(
        f"/en/cases/{case_id}/deliveries",
        data=delivery_payload(context["contractor"], context["criterion_id"]),
    ).status_code == 302
    assert_workspace_state("DELIVERED", "Delivery submitted", "Inspect")

    workflow = current_workflow(context["instance_root"], case_id)
    assert context["owner"].post(
        f"/en/cases/{case_id}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    assert_workspace_state("UNDER_ACCEPTANCE", "Client review in progress", "Inspect")

    workflow = current_workflow(context["instance_root"], case_id)
    assert context["owner"].post(
        f"/en/cases/{case_id}/deliveries/{workflow['current_delivery_id']}/inspections",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": str(workflow["revision"]),
            "idempotency_key": str(uuid.uuid4()),
            f"result_{context['criterion_id']}": "RETURNED",
            f"reason_{context['criterion_id']}": "The contact form is missing from the delivered scope.",
        },
    ).status_code == 302
    assert_workspace_state("REMEDIATION", "Remediation requested", "Fix")

    assert context["contractor"].post(
        f"/en/cases/{case_id}/deliveries",
        data=delivery_payload(
            context["contractor"], context["criterion_id"], summary="Contact form remediation"
        ),
    ).status_code == 302
    assert_workspace_state("REINSPECTION", "Reinspection in progress", "Inspect")

    workflow = current_workflow(context["instance_root"], case_id)
    assert context["owner"].post(
        f"/en/cases/{case_id}/deliveries/{workflow['current_delivery_id']}/inspections",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": str(workflow["revision"]),
            "idempotency_key": str(uuid.uuid4()),
            f"result_{context['criterion_id']}": "PASS",
            f"reason_{context['criterion_id']}": "",
            "explicit_acceptance": "yes",
        },
    ).status_code == 302
    assert_workspace_state("ACCEPTED", "Delivery accepted", "Evidence")

    disputed = create_formal_context(demo_runtime)
    disputed_case_id = disputed["case_id"]
    assert disputed["contractor"].post(
        f"/en/cases/{disputed_case_id}/deliveries",
        data=delivery_payload(disputed["contractor"], disputed["criterion_id"]),
    ).status_code == 302
    workflow = current_workflow(disputed["instance_root"], disputed_case_id)
    assert disputed["owner"].post(
        f"/en/cases/{disputed_case_id}/disputes",
        data={
            "csrf_token": csrf(disputed["owner"]),
            "expected_revision": str(workflow["revision"]),
            "reason": "The parties disagree about the delivered contact form.",
        },
    ).status_code == 302
    assert current_workflow(disputed["instance_root"], disputed_case_id)["state"] == "DISPUTED"
    disputed_path = f'href="/en/cases/{disputed_case_id}/delivery"'
    for party in (disputed["owner"], disputed["contractor"]):
        response = party.get("/en/account")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        match = re.search(
            rf'<a class="case-card" href="/en/cases/{re.escape(str(disputed_case_id))}/delivery">(.*?)</a>',
            html,
            re.DOTALL,
        )
        assert match is not None
        assert disputed_path in html
        assert '<span class="case-stage">Evidence</span>' in match.group(1)
        assert "<span>Records preserved for resolution</span>" in match.group(1)
        delivery_html = party.get(f"/en/cases/{disputed_case_id}/delivery").get_data(as_text=True)
        assert 'data-ui="status">Evidence · Records preserved for resolution' in delivery_html
        assert_workflow_stage(delivery_html, "Evidence")
        assert '<p class="eyebrow">Evidence · Review together</p>' in delivery_html


def test_contractor_authoring_summary_is_visible_and_raw_editor_is_closed(project_root: Path) -> None:
    draft = (project_root / "app/templates/json_b_draft.html").read_text(encoding="utf-8")
    summary_at = draft.index("Current proposal summary")
    disclosure_at = draft.index('<details class="technical-disclosure">')
    textarea_at = draft.index('name="draft_json"')
    handoff_at = draft.index("Review handoff")
    assert summary_at < disclosure_at < textarea_at < handoff_at
    assert "Open structured proposal editor" not in draft

    handoff = (project_root / "app/templates/json_b_import.html").read_text(encoding="utf-8")
    assert handoff.index("Step 1") < handoff.index("Step 2")
    assert "Download your saved proposal" in handoff
    assert "Upload the same file for review" in handoff
    assert "Reimport the exact proposal file" not in handoff
    assert "Quote · Proposal handoff" in handoff
    assert "Confirm · Proposal handoff" not in handoff

    room = (project_root / "app/templates/case_room.html").read_text(encoding="utf-8")
    action_at = room.index('class="action-panel stack"')
    discussion_at = room.index('id="discussion-heading"')
    issues_at = room.index('id="issues-heading"')
    assert action_at < discussion_at < issues_at


def test_delivery_states_expose_at_most_one_primary_action_per_role(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    case_id = context["case_id"]

    def primary_count(client) -> int:
        response = client.get(f"/en/cases/{case_id}/delivery")
        assert response.status_code == 200
        return response.get_data(as_text=True).count('data-ui="primary-action"')

    assert primary_count(context["contractor"]) == 1
    assert primary_count(context["owner"]) == 0
    delivered = context["contractor"].post(
        f"/en/cases/{case_id}/deliveries",
        data=delivery_payload(context["contractor"], context["criterion_id"]),
    )
    assert delivered.status_code == 302
    assert primary_count(context["owner"]) == 1
    assert primary_count(context["contractor"]) == 0

    workflow = current_workflow(context["instance_root"], case_id)
    started = context["owner"].post(
        f"/en/cases/{case_id}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    )
    assert started.status_code == 302
    assert primary_count(context["owner"]) == 1
    assert primary_count(context["contractor"]) == 0
    workflow = current_workflow(context["instance_root"], case_id)
    returned = context["owner"].post(
        f"/en/cases/{case_id}/deliveries/{workflow['current_delivery_id']}/inspections",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": str(workflow["revision"]),
            "idempotency_key": str(uuid.uuid4()),
            f"result_{context['criterion_id']}": "RETURNED",
            f"reason_{context['criterion_id']}": "Needs the agreed marker",
        },
    )
    assert returned.status_code == 302
    assert primary_count(context["contractor"]) == 1
    assert primary_count(context["owner"]) == 0

    reinspected = context["contractor"].post(
        f"/en/cases/{case_id}/deliveries",
        data=delivery_payload(context["contractor"], context["criterion_id"], summary="Remediated delivery"),
    )
    assert reinspected.status_code == 302
    assert primary_count(context["owner"]) == 1
    assert primary_count(context["contractor"]) == 0
    workflow = current_workflow(context["instance_root"], case_id)
    accepted = context["owner"].post(
        f"/en/cases/{case_id}/deliveries/{workflow['current_delivery_id']}/inspections",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": str(workflow["revision"]),
            "idempotency_key": str(uuid.uuid4()),
            f"result_{context['criterion_id']}": "PASS",
            f"reason_{context['criterion_id']}": "",
            "explicit_acceptance": "yes",
        },
    )
    assert accepted.status_code == 302
    assert primary_count(context["owner"]) == 1
    assert primary_count(context["contractor"]) == 1
    accepted_html = context["owner"].get(f"/en/cases/{case_id}/delivery").get_data(as_text=True)
    assert "Download Evidence Package (.zip)" in accepted_html
    assert "confirmed agreement, delivery versions, inspection decisions, and remediation history" in accepted_html
    assert "verification manifest" not in accepted_html.split("Verify this evidence package", 1)[0]
    assert accepted_html.index("Download Evidence Package (.zip)") < accepted_html.index("Delivery History")
    assert context["owner"].post(
        f"/en/cases/{case_id}/evidence-exports",
        data={"csrf_token": csrf(context["owner"])},
    ).status_code == 302
    package_html = context["owner"].get(f"/en/cases/{case_id}/delivery").get_data(as_text=True)
    assert re.search(r'<form method="post" action="/en/cases/[^/]+/evidence-exports\?download=1">', package_html)
    assert '<button data-ui="primary-action">Download Evidence Package (.zip)</button>' in package_html
    assert '<details class="technical-disclosure"><summary>Verify this evidence package</summary>' in package_html
    assert package_html.count('data-ui="primary-action"') == 1


def test_dispute_position_then_export_preserves_one_primary_action(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    case_id = context["case_id"]
    assert context["contractor"].post(
        f"/en/cases/{case_id}/deliveries",
        data=delivery_payload(context["contractor"], context["criterion_id"]),
    ).status_code == 302
    workflow = current_workflow(context["instance_root"], case_id)
    assert context["owner"].post(
        f"/en/cases/{case_id}/disputes",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"]), "reason": "Scope interpretation differs"},
    ).status_code == 302

    def html(client) -> str:
        response = client.get(f"/en/cases/{case_id}/delivery")
        assert response.status_code == 200
        return response.get_data(as_text=True)

    owner_html = html(context["owner"])
    contractor_html = html(context["contractor"])
    assert owner_html.count('data-ui="primary-action"') == 1
    assert contractor_html.count('data-ui="primary-action"') == 1
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        dispute_id = connection.execute("SELECT id FROM disputes WHERE case_id = ?", (case_id,)).fetchone()[0]
    finally:
        connection.close()
    assert context["owner"].post(
        f"/en/cases/{case_id}/disputes/{dispute_id}/positions",
        data={"csrf_token": csrf(context["owner"]), "position_text": "Owner position"},
    ).status_code == 302
    after_position = html(context["owner"])
    assert after_position.count('data-ui="primary-action"') == 1
    assert "Download Evidence Package (.zip)" in after_position
