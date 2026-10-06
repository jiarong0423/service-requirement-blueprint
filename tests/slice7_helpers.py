from __future__ import annotations

import uuid

from app.db import connect_database
from tests.helpers import csrf
from tests.slice4_helpers import valid_business_draft
from tests.slice5_helpers import create_json_b_review
from tests.slice6_helpers import acknowledge, confirm


def create_formal_context(demo_runtime) -> dict[str, object]:
    draft = valid_business_draft()
    criterion_id = draft["acceptance"]["criteria"][0]["criterion_id"]
    context = create_json_b_review(demo_runtime, draft=draft)
    response = context["contractor"].post(
        f"/en/cases/{context['case_id']}/candidates",
        data={"csrf_token": csrf(context["contractor"])},
    )
    candidate_id = response.location.rsplit("/", 1)[1]
    for party in (context["owner"], context["contractor"]):
        assert acknowledge(party, context["case_id"], candidate_id).status_code == 302
        assert confirm(party, context["case_id"], candidate_id).status_code == 302
    context.update({"candidate_id": candidate_id, "criterion_id": criterion_id})
    return context


def delivery_payload(client, criterion_id: str, *, summary: str = "Completed fixed delivery") -> dict[str, str]:
    return {
        "csrf_token": csrf(client),
        "idempotency_key": str(uuid.uuid4()),
        "summary": summary,
        f"note_{criterion_id}": "Implemented the agreed criterion",
        f"evidence_{criterion_id}": "local-test-report",
        f"test_outcome_{criterion_id}": "PASS",
        f"observation_{criterion_id}": "Documented test passed in the local environment",
    }


def current_workflow(instance_root, case_id: str):
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        return connection.execute("SELECT * FROM delivery_workflows WHERE case_id = ?", (case_id,)).fetchone()
    finally:
        connection.close()
