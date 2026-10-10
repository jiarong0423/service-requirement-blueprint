from __future__ import annotations

from pathlib import Path

import pytest
from flask import render_template_string

from app.i18n import load_catalogs
from app.presentation import (
    INVITATION_STATE_LABEL_KEYS,
    INVITATION_TASK_LABEL_KEYS,
    ISSUE_KIND_LABEL_KEYS,
    ISSUE_STATE_LABEL_KEYS,
    SCREEN_COPY_KEYS,
    STAGE_LABEL_KEYS,
    STATE_LABEL_KEYS,
    VALUE_LABEL_KEYS,
    PresentationError,
    invitation_state_label,
    invitation_task,
    issue_kind_label,
    issue_state_label,
    screen_copy,
    stage_label,
    state_label,
)


def test_executable_presentation_map_references_existing_catalog_keys(project_root: Path) -> None:
    catalogs = load_catalogs(project_root / "app/i18n")
    catalog = catalogs["en"]
    mapped_keys = set(STATE_LABEL_KEYS.values())
    mapped_keys.update(STAGE_LABEL_KEYS.values())
    mapped_keys.update(ISSUE_STATE_LABEL_KEYS.values())
    mapped_keys.update(ISSUE_KIND_LABEL_KEYS.values())
    for field_values in VALUE_LABEL_KEYS.values():
        mapped_keys.update(field_values.values())
    mapped_keys.update(INVITATION_STATE_LABEL_KEYS.values())
    mapped_keys.update(INVITATION_TASK_LABEL_KEYS.values())
    for screen in SCREEN_COPY_KEYS.values():
        mapped_keys.update(screen.values())
    mapped_keys.update(
        {
            "ui.status.unavailable",
            "ui.stage.unavailable",
            "ui.issue_kind.unavailable",
            "ui.invitation.task_unavailable",
        }
    )
    assert mapped_keys <= set(catalog)


def test_presentation_state_maps_fail_closed_and_screen_slots_are_explicit(project_root: Path) -> None:
    catalogs = load_catalogs(project_root / "app/i18n")
    assert state_label(catalogs, "en", "CASE_ACTIVE") == "Private case active"
    assert state_label(catalogs, "en", "JSON_B_PRIVATE_DRAFT") == "Contractor preparing proposal"
    assert state_label(catalogs, "en", "CANDIDATE_REVIEW") == "Waiting for confirmation"
    assert state_label(catalogs, "en", "UNKNOWN") == "Status unavailable"
    assert stage_label(catalogs, "en", "CANDIDATE_REVIEW") == "Confirm"
    assert stage_label(catalogs, "en", "UNKNOWN") == "Stage unavailable"
    assert issue_state_label(catalogs, "en", "OUT_OF_SCOPE_PROPOSED") == "Response proposed"
    assert issue_kind_label(catalogs, "en", "CLARIFICATION") == "Scope question"
    assert issue_kind_label(catalogs, "en", "UNKNOWN") == "Issue type unavailable"
    assert invitation_state_label(catalogs, "en", "PENDING") == "Invitation Pending"
    assert invitation_state_label(catalogs, "en", "UNKNOWN") == "Status unavailable"
    invitation_states = {"PENDING", "ACCEPTED", "REJECTED", "REVOKED", "EXPIRED"}
    assert set(INVITATION_STATE_LABEL_KEYS) == invitation_states
    assert set(INVITATION_TASK_LABEL_KEYS) == {
        (state, role) for state in invitation_states for role in ("client", "contractor")
    }
    assert invitation_task(catalogs, "en", "PENDING", "contractor") == "Review and respond to this invitation"
    assert invitation_task(catalogs, "en", "REJECTED", "client") == "Verify the saved requirement file"
    assert invitation_task(catalogs, "en", "REJECTED", "contractor") == "No action available for this invitation"
    assert invitation_task(catalogs, "en", "UNKNOWN", "contractor") == "No action available for this invitation"
    assert screen_copy(catalogs, "en", "confirmation", "title", version_no=2) == "Confirmation Version 2"
    with pytest.raises(PresentationError, match="presentation_slot_missing"):
        screen_copy(catalogs, "en", "confirmation", "missing")


def test_workflow_context_fails_closed_for_unknown_state(demo_runtime) -> None:
    app, _instance_root, _passwords = demo_runtime
    with app.test_request_context("/en/"):
        rendered = render_template_string(
            "{% from '_ui.html' import workflow_context_bar with context %}"
            "{{ workflow_context_bar('Member', 'UNKNOWN', 'Status unavailable', 'Review this page') }}"
        )
    assert 'data-ui="status">Stage unavailable · Status unavailable' in rendered
    assert 'aria-current="step"' not in rendered
    assert "workflow-current" not in rendered


def test_workflow_stage_label_agrees_with_context_and_drawer_for_every_known_state(demo_runtime) -> None:
    expected_by_state = {
        "REQUIREMENT_DRAFT": "Clarify",
        "INVITATION_PENDING": "Clarify",
        "WAITING_JSON_A_IMPORT": "Clarify",
        "CASE_ACTIVE": "Quote",
        "JSON_B_PRIVATE_DRAFT": "Quote",
        "JSON_B_REVIEW": "Review",
        "CANDIDATE_REVIEW": "Confirm",
        "FORMAL": "Deliver",
        "DELIVERED": "Inspect",
        "UNDER_ACCEPTANCE": "Inspect",
        "REINSPECTION": "Inspect",
        "REMEDIATION": "Fix",
        "ACCEPTED": "Evidence",
        "DISPUTED": "Evidence",
    }
    app, _instance_root, _passwords = demo_runtime
    with app.test_request_context("/en/"):
        for state, expected_stage in expected_by_state.items():
            rendered = render_template_string(
                "{% from '_ui.html' import workflow_context_bar, workflow_stage_label with context %}"
                '<span data-test-stage>{{ workflow_stage_label(state) }}</span>'
                "{{ workflow_context_bar('Member', state, 'Granular status', 'Current task') }}",
                state=state,
            )
            assert f"<span data-test-stage>{expected_stage}</span>" in rendered
            assert f'data-ui="status">{expected_stage} · Granular status' in rendered
            assert rendered.count('aria-current="step"') == 1
            assert (
                f'<li class="workflow-current" aria-current="step">\n        '
                f"<strong>{expected_stage}</strong>"
            ) in rendered


def test_core_workflow_templates_use_the_executable_map(project_root: Path) -> None:
    required_calls = {
        "case_room.html": "ui('case_room'",
        "json_b_review.html": "ui('proposal_review'",
        "candidate_review.html": "ui('confirmation'",
        "formal_verification.html": "ui('confirmed'",
        "delivery_dashboard.html": "ui('delivery'",
        "invitation_review.html": "ui_invitation_state(invitation_state)",
        "publish_invitation.html": "ui_invitation_task(invitation_state, 'client')",
        "_ui.html": "workflow_stage_label(state)",
    }
    for filename, marker in required_calls.items():
        source = (project_root / "app/templates" / filename).read_text(encoding="utf-8")
        assert marker in source


def test_human_value_preserves_user_acronyms_and_case(demo_runtime) -> None:
    app, _instance_root, _passwords = demo_runtime
    with app.test_request_context("/en/"):
        rendered = render_template_string(
            "{% from '_ui.html' import human_value with context %}{{ human_value(value) }}",
            value="ACME API URL /BrandPath",
        )
    assert "ACME API URL /BrandPath" in rendered


def test_human_value_maps_schema_enums_without_changing_user_text(demo_runtime) -> None:
    app, _instance_root, _passwords = demo_runtime
    with app.test_request_context("/en/"):
        rendered = render_template_string(
            "{% from '_ui.html' import human_value with context %}{{ human_value(value) }}",
            value={
                "billing_kind": "one_time",
                "cost_source": "third_party",
                "role_code": "client_admin",
                "status": "contractor_proposal",
                "notes": "client third_party one_time client_admin contractor_proposal keep_Case",
            },
        )
    assert "One-time" in rendered
    assert "Third party" in rendered
    assert "Client Admin" in rendered
    assert "Contractor to propose" in rendered
    assert "client third_party one_time client_admin contractor_proposal keep_Case" in rendered
    assert rendered.count("one_time") == 1
    assert rendered.count("third_party") == 1
    assert "keep_Case" in rendered


def test_proposal_presentation_preserves_contractor_authored_codes(demo_runtime) -> None:
    app, _instance_root, _passwords = demo_runtime
    with app.test_request_context("/en/"):
        rendered = render_template_string(
            "{% from '_ui.html' import human_value with context %}"
            "{{ human_value(value, none, true) }}",
            value={
                "role_code": "iOS app-store_v2",
                "service_code": "iOS app-store_v2",
                "billing_kind": "one_time",
            },
        )
    assert rendered.count("iOS app-store_v2") == 2
    assert "Ios App Store V2" not in rendered
    assert "One-time" in rendered


def test_invitation_technical_version_stays_in_closed_disclosure(project_root: Path) -> None:
    for filename in ("invitation_review.html", "publish_invitation.html"):
        source = (project_root / "app/templates" / filename).read_text(encoding="utf-8")
        disclosure_start = source.index('<details class="technical-disclosure">')
        version_label = source.index("tr('field.json_a_version')")
        assert version_label > disclosure_start
        assert "replace('_', ' ')|title" not in source
