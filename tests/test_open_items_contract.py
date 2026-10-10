from __future__ import annotations

import json
import re

from app.db import connect_database
from app.services.case_workflow import build_json_a_document, collect_open_items, load_draft, save_section
from tests.helpers import complete_requirement, csrf, login


def test_nested_open_items_match_review_and_saved_json_a(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    with app.test_client() as client:
        assert login(client, "en", "owner@example.test", passwords["owner"]).status_code == 302
        case_id = complete_requirement(client, instance_root)
        connection = connect_database(instance_root / "app.sqlite3")
        try:
            draft, _ = load_draft(connection, case_id)
            draft["features"]["items"][1]["requirement_level"] = "undecided"
            draft["external_services_and_assets"]["services"][0]["status"] = "to_select"
            draft["external_services_and_assets"]["assets"][0]["status"] = "contractor_proposal"
            draft["delivery_and_constraints"]["budget_status"] = "contractor_proposal_requested"
            for section_no, section_name in (
                (4, "features"),
                (6, "external_services_and_assets"),
                (8, "delivery_and_constraints"),
            ):
                revision = connection.execute("SELECT content_revision FROM cases WHERE id = ?", (case_id,)).fetchone()[0]
                save_section(connection, case_id, section_no, draft[section_name], revision)
            expected = collect_open_items(draft)
            expected_document = build_json_a_document(draft, "test-document", 1)
            assert len(expected) == 10
            assert len(expected_document["open_items"]) == len(expected)
            assert {item["field_pointer"] for item in expected_document["open_items"]} >= {
                "/items/1/requirement_level",
                "/services/0/status",
                "/assets/0/status",
                "/budget_status",
            }
            assert all(item["field_pointer"].startswith("/") for item in expected_document["open_items"])
        finally:
            connection.close()

        review = client.get(f"/en/owner/requirements/{case_id}/review")
        assert review.status_code == 200
        visible_list = re.search(rb"<h4>Items to resolve</h4>.*?</ul>", review.data, re.DOTALL)
        assert visible_list is not None
        assert visible_list.group().count(b"<li>") == len(expected)

        connection = connect_database(instance_root / "app.sqlite3")
        try:
            revision = connection.execute("SELECT content_revision FROM cases WHERE id = ?", (case_id,)).fetchone()[0]
        finally:
            connection.close()
        saved = client.post(
            f"/en/owner/requirements/{case_id}/json-a",
            data={"csrf_token": csrf(client), "expected_revision": str(revision), "review_confirmed": "yes"},
        )
        assert saved.status_code == 302
        connection = connect_database(instance_root / "app.sqlite3")
        try:
            artifact_id = connection.execute(
                "SELECT current_json_a_artifact_id FROM cases WHERE id = ?", (case_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        downloaded = client.get(f"/en/owner/requirements/{case_id}/json-a/{artifact_id}/download")
        assert downloaded.status_code == 200
        actual = json.loads(downloaded.data)
        assert actual["open_items"] == expected_document["open_items"]
