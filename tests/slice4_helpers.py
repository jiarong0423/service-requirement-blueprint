from __future__ import annotations

import io
import re

from app.db import connect_database
from tests.helpers import complete_requirement, csrf, login


def activate_private_case(demo_runtime) -> dict[str, object]:
    app, instance_root, passwords = demo_runtime
    owner = app.test_client()
    contractor = app.test_client()
    assert login(owner, "en", "owner@example.test", passwords["owner"]).status_code == 302
    assert login(contractor, "en", "contractor@example.test", passwords["contractor"]).status_code == 302
    case_id = complete_requirement(owner, instance_root)
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        revision = connection.execute(
            "SELECT content_revision FROM cases WHERE id = ?", (case_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert owner.post(
        f"/en/owner/requirements/{case_id}/json-a",
        data={
            "csrf_token": csrf(owner),
            "expected_revision": str(revision),
            "review_confirmed": "yes",
        },
    ).status_code == 302
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        artifact = connection.execute(
            "SELECT id, relative_path FROM artifacts WHERE case_id = ? AND kind = 'json_a' "
            "AND lifecycle = 'active'",
            (case_id,),
        ).fetchone()
        revision = connection.execute(
            "SELECT content_revision FROM cases WHERE id = ?", (case_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    raw = (instance_root / "data" / artifact["relative_path"]).read_bytes()
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
    code_match = re.search(rb'class="code-panel code-value"><code>([^<]+)</code>', created.data)
    invitation_match = re.search(rb'/en/invitations/([0-9a-f-]{36})', created.data)
    assert code_match is not None and invitation_match is not None
    invitation_id = invitation_match.group(1).decode("ascii")
    code = code_match.group(1).decode("ascii")
    assert contractor.post(
        f"/en/invitations/{invitation_id}/accept",
        data={"csrf_token": csrf(contractor), "invitation_code": code},
    ).status_code == 302
    assert contractor.post(
        f"/en/cases/{case_id}/json-a/import",
        data={
            "csrf_token": csrf(contractor),
            "expected_revision": str(revision),
            "json_a_file": (io.BytesIO(raw), "requirements.json"),
        },
    ).status_code == 302
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        owner_id = connection.execute(
            "SELECT id FROM users WHERE email_normalized = 'owner@example.test'"
        ).fetchone()[0]
        contractor_id = connection.execute(
            "SELECT id FROM users WHERE email_normalized = 'contractor@example.test'"
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "app": app,
        "instance_root": instance_root,
        "passwords": passwords,
        "owner": owner,
        "contractor": contractor,
        "owner_id": owner_id,
        "contractor_id": contractor_id,
        "case_id": case_id,
        "json_a_raw": raw,
    }


def valid_business_draft() -> dict[str, object]:
    import uuid

    return {
        "requirement_understanding": "Build a private appointment website for café visitors.",
        "included_scope": [
            {"item_id": str(uuid.uuid4()), "description": "Responsive appointment form"}
        ],
        "excluded_scope": [
            {"item_id": str(uuid.uuid4()), "description": "Payment processing"}
        ],
        "pages": [
            {
                "page_id": str(uuid.uuid4()),
                "title": "Appointments",
                "deliverables": ["Responsive page", "Validation"],
            }
        ],
        "functions": [
            {
                "function_id": str(uuid.uuid4()),
                "name": "Appointment request",
                "rules": ["No automatic acceptance"],
            }
        ],
        "data_and_permissions": [
            {
                "role_code": "client_admin",
                "data_categories": ["appointment requests"],
                "permissions": ["read", "delete"],
            }
        ],
        "third_party_services": [
            {
                "service_code": "hosting",
                "responsible_party": "client",
                "notes": "Billed directly by provider",
            }
        ],
        "milestones": [
            {
                "milestone_id": str(uuid.uuid4()),
                "title": "Working form",
                "deliverables": ["Form and validation tests"],
                "estimated_working_days": 5,
            }
        ],
        "effort": {
            "estimated_hours": 24,
            "difficulty": "medium",
            "assumptions": ["Client supplies approved copy"],
        },
        "fee_items": [
            {
                "fee_id": str(uuid.uuid4()),
                "description": "Implementation",
                "billing_kind": "one_time",
                "cost_source": "contractor",
                "currency_code": "XTS",
                "amount": 1200,
                "billing_period": None,
                "included_in_quote": True,
            },
            {
                "fee_id": str(uuid.uuid4()),
                "description": "Hosting",
                "billing_kind": "recurring",
                "cost_source": "third_party",
                "currency_code": "XTS",
                "amount": 20,
                "billing_period": "monthly",
                "included_in_quote": False,
            },
        ],
        "responsibilities": {
            "client": ["Provide content"],
            "contractor": ["Implement and test"],
        },
        "acceptance": {
            "criteria": [
                {
                    "criterion_id": str(uuid.uuid4()),
                    "description": "Valid requests are stored",
                    "verification_method": "Run the documented acceptance test",
                }
            ],
            "review_period_days": 7,
            "silence_is_acceptance": False,
        },
        "remediation": {
            "included_corrections": ["Fix reproducible defects"],
            "excluded_changes": ["New features"],
            "reinspection_required": True,
        },
        "maintenance": {
            "warranty_days": 30,
            "included_support": ["Defect correction"],
            "excluded_support": ["New features"],
        },
        "quote_expires_at": "2099-12-31T23:59:59Z",
    }
