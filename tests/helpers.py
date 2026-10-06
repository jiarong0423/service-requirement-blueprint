from __future__ import annotations

from typing import Any

from app.db import connect_database


def csrf(client) -> str:
    with client.session_transaction() as state:
        return state["csrf_nonce"]


def login(client, locale: str, email: str, password: str):
    client.get(f"/{locale}/login")
    return client.post(
        f"/{locale}/login",
        data={"csrf_token": csrf(client), "email": email, "password": password},
    )


def complete_requirement(client, instance_root, locale: str = "en") -> str:
    created = client.post(
        f"/{locale}/owner/requirements",
        data={"csrf_token": csrf(client), "title": "Community Studio Website"},
    )
    case_id = created.location.split("/")[4]
    sections: list[dict[str, Any] | None] = [
        {"working_name": "Community Studio Website", "target_audience": "Local visitors", "primary_purpose": "Explain available services", "primary_visitor_action": "Request an appointment", "supplemental_notes": "Preserve Unicode exactly: café, résumé, naïve — unchanged."},
        {"user_groups": "Visitors\nReturning clients", "primary_devices": "mobile", "entry_paths": "social", "core_actions": "request an appointment"},
        {"pages": "Home|Services and appointments|text,image", "language_requirements": "English"},
        None,
        {"data_categories": "booking", "contains_customer_data": "yes", "read_rights": "booking", "write_rights": "booking", "retention_expectations": "30 days", "deletion_expectations": "on request"},
        None,
        {"brand_style": "warm", "colors": "green", "reference_sites": "https://example.com|clear", "mobile_requirements": "responsive", "accessibility_requirements": "keyboard", "excluded_directions": "copied design"},
        {"deliverables": "source", "source_code_ownership": "client", "account_ownership": "client", "deployment_expectation": "local", "maintenance_expectation": "quoted separately", "launch_conditions": "tests pass", "required_deadline": "2026-12-31", "budget_status": "range", "budget_currency": "XTS", "budget_min_amount": "1000", "budget_max_amount": "5000", "acceptance_expectations": "listed criteria", "constraints": "offline"},
    ]
    for number in range(1, 9):
        connection = connect_database(instance_root / "app.sqlite3")
        try:
            revision = connection.execute("SELECT content_revision FROM cases WHERE id = ?", (case_id,)).fetchone()[0]
        finally:
            connection.close()
        payload: dict[str, Any] = {"csrf_token": csrf(client), "expected_revision": str(revision)}
        payload.update(sections[number - 1] or {})
        if number == 4:
            for code in ("booking", "form", "membership", "search", "cart", "admin", "notification", "other"):
                payload[f"feature_{code}"] = "required" if code == "booking" else "excluded"
                payload[f"rules_{code}"] = ""
        if number == 6:
            for code in ("domain", "hosting", "payment", "maps", "social", "email", "api"):
                payload[f"service_{code}"] = "contractor_proposal"
                payload[f"service_{code}_notes"] = ""
            for code in ("existing_site", "logo", "brand_materials", "database"):
                payload[f"asset_{code}"] = "not_needed"
                payload[f"asset_{code}_notes"] = ""
        response = client.post(f"/{locale}/owner/requirements/{case_id}/sections/{number}", data=payload)
        assert response.status_code == 302
    return case_id
