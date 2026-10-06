from __future__ import annotations

import re

from tests.helpers import csrf
from tests.slice5_helpers import create_json_b_review


def create_candidate_context(demo_runtime) -> dict[str, object]:
    context = create_json_b_review(demo_runtime)
    response = context["contractor"].post(
        f"/en/cases/{context['case_id']}/candidates",
        data={"csrf_token": csrf(context["contractor"])},
    )
    assert response.status_code == 302
    match = re.search(r"/candidates/([0-9a-f-]{36})$", response.location)
    assert match is not None
    context["candidate_id"] = match.group(1)
    context["candidate_url"] = response.location
    return context


def acknowledge(client, case_id: str, candidate_id: str, revision: int = 1):
    return client.post(
        f"/en/cases/{case_id}/candidates/{candidate_id}/acknowledgements",
        data={
            "csrf_token": csrf(client),
            "expected_revision": str(revision),
            "acknowledged": "yes",
        },
    )


def confirm(client, case_id: str, candidate_id: str, revision: int = 1):
    return client.post(
        f"/en/cases/{case_id}/candidates/{candidate_id}/confirmations",
        data={
            "csrf_token": csrf(client),
            "expected_revision": str(revision),
            "confirmed": "yes",
        },
    )
