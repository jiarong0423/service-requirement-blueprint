from __future__ import annotations

import io
import json
from urllib.parse import parse_qs, urlparse

from tests.helpers import csrf
from tests.slice4_helpers import activate_private_case, valid_business_draft


def create_json_b_review(demo_runtime, *, draft: dict[str, object] | None = None) -> dict[str, object]:
    context = activate_private_case(demo_runtime)
    case_id = str(context["case_id"])
    contractor = context["contractor"]
    business = draft or valid_business_draft()
    saved = contractor.post(
        f"/en/cases/{case_id}/json-b/draft",
        data={
            "csrf_token": csrf(contractor),
            "expected_revision": "0",
            "draft_json": json.dumps(business, ensure_ascii=False),
        },
    )
    assert saved.status_code == 302
    exported = contractor.post(
        f"/en/cases/{case_id}/json-b/draft/export",
        data={"csrf_token": csrf(contractor), "expected_revision": "1"},
    )
    assert exported.status_code == 302
    artifact_id = parse_qs(urlparse(exported.location).query)["artifact_id"][0]
    private = contractor.get(
        f"/en/cases/{case_id}/json-b/draft/exports/{artifact_id}/download"
    )
    assert private.status_code == 200
    submitted = contractor.post(
        f"/en/cases/{case_id}/json-b/review/import",
        data={
            "csrf_token": csrf(contractor),
            "artifact_id": artifact_id,
            "json_b_file": (io.BytesIO(private.data), "json-b.json"),
        },
    )
    assert submitted.status_code == 302
    context.update({"json_b_artifact_id": artifact_id, "json_b_raw": private.data})
    return context
