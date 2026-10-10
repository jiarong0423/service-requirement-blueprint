from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db import immediate_transaction
from app.services.artifact_store import install_json_artifact, remove_new_artifact
from app.services.blueprint import blueprint_data
from app.services.document_integrity import DocumentAssets, ensure_supported_text
from app.services.json_contracts import encode_json_once, sha256_bytes, validate_json_a


SECTION_KEYS = (
    "project",
    "users_and_scenarios",
    "pages_and_content",
    "features",
    "data_and_permissions",
    "external_services_and_assets",
    "visual_preferences",
    "delivery_and_constraints",
)


def section_is_complete(section_no: int, values: dict[str, Any]) -> bool:
    if section_no == 1:
        required = ("working_name", "target_audience", "primary_purpose", "primary_visitor_action")
        return (
            all(str(values.get(key, "")).strip() for key in required)
            and len(str(values.get("working_name", ""))) <= 200
            and all(len(str(values.get(key, ""))) <= 240 for key in required[1:])
            and len(str(values.get("supplemental_notes", ""))) <= 10_000
        )
    if section_no == 2:
        return all(values.get(key) for key in ("user_groups", "primary_devices", "core_actions"))
    if section_no == 3:
        return bool(values.get("pages"))
    if section_no == 4:
        return len(values.get("items", [])) == 8
    if section_no == 5:
        return all(str(values.get(key, "")).strip() for key in ("retention_expectations", "deletion_expectations"))
    if section_no == 6:
        return len(values.get("services", [])) == 7 and len(values.get("assets", [])) == 4
    if section_no == 7:
        return bool(values.get("brand_style") or values.get("excluded_directions"))
    required_values_present = all(
        values.get(key)
        for key in ("deliverables", "source_code_ownership", "account_ownership", "deployment_expectation", "maintenance_expectation", "acceptance_expectations")
    )
    currency = values.get("budget_currency")
    minimum = values.get("budget_min_amount")
    maximum = values.get("budget_max_amount")
    budget_order_valid = minimum is None or maximum is None or minimum <= maximum
    currency_valid = currency is None or (
        isinstance(currency, str) and len(currency) == 3 and currency.isascii() and currency.isalpha() and currency.isupper()
    )
    amounts_have_currency = currency is not None or (minimum is None and maximum is None)
    return required_values_present and budget_order_valid and currency_valid and amounts_have_currency


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_case(connection, owner_id: str, title: str) -> str:
    case_id = str(uuid.uuid4())
    now = utc_now()
    initial = {key: {} for key in SECTION_KEYS}
    initial["project"] = {"working_name": title}
    with immediate_transaction(connection):
        connection.execute(
            "INSERT INTO cases(id, owner_id, title, state, content_revision, created_at, updated_at) "
            "VALUES (?, ?, ?, 'REQUIREMENT_DRAFT', 1, ?, ?)",
            (case_id, owner_id, title, now, now),
        )
        connection.execute(
            "INSERT INTO requirement_drafts(case_id, data_json, completed_sections_json, updated_at) VALUES (?, ?, '[]', ?)",
            (case_id, json.dumps(initial, ensure_ascii=False), now),
        )
    return case_id


def load_draft(connection, case_id: str) -> tuple[dict[str, Any], set[int]]:
    row = connection.execute(
        "SELECT data_json, completed_sections_json FROM requirement_drafts WHERE case_id = ?", (case_id,)
    ).fetchone()
    if row is None:
        raise LookupError("draft_missing")
    return json.loads(row["data_json"]), set(json.loads(row["completed_sections_json"]))


def save_section(connection, case_id: str, section_no: int, values: dict[str, Any], expected_revision: int) -> int:
    if not section_is_complete(section_no, values):
        raise ValueError("section_incomplete")
    with immediate_transaction(connection):
        case = connection.execute(
            "SELECT content_revision, state FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        if (
            case is None
            or case["state"] != "REQUIREMENT_DRAFT"
            or case["content_revision"] != expected_revision
        ):
            raise RuntimeError("revision_conflict")
        data, completed = load_draft(connection, case_id)
        data[SECTION_KEYS[section_no - 1]] = values
        completed.add(section_no)
        now = utc_now()
        # A saved JSON A describes the draft at one revision only. Editing any
        # section makes that version historical until the owner saves a new one.
        connection.execute(
            "UPDATE artifacts SET lifecycle = 'superseded' "
            "WHERE case_id = ? AND kind = 'json_a' AND lifecycle = 'active'",
            (case_id,),
        )
        connection.execute(
            "UPDATE requirement_drafts SET data_json = ?, completed_sections_json = ?, updated_at = ? WHERE case_id = ?",
            (json.dumps(data, ensure_ascii=False), json.dumps(sorted(completed)), now, case_id),
        )
        new_revision = expected_revision + 1
        connection.execute(
            "UPDATE cases SET title = ?, content_revision = ?, current_json_a_artifact_id = NULL, updated_at = ? WHERE id = ?",
            (str(data.get("project", {}).get("working_name", "Untitled")), new_revision, now, case_id),
        )
    return new_revision


def collect_open_items(data: dict[str, Any]) -> list[dict[str, str]]:
    """Derive one review and JSON A list from the guided choices."""
    items: list[dict[str, str]] = []

    def add(section: str, pointer: str, resolution: str, label_key: str) -> None:
        items.append(
            {
                "section_code": section,
                "field_pointer": pointer,
                "resolution": resolution,
                "note": "",
                "label_key": label_key,
            }
        )

    for index, feature in enumerate(data.get("features", {}).get("items", [])):
        if feature.get("requirement_level") == "undecided":
            add("features", f"/items/{index}/requirement_level", "undecided", f"feature.{feature['feature_code']}")

    external = data.get("external_services_and_assets", {})
    for collection, label_prefix in (("services", "service"), ("assets", "asset")):
        for index, entry in enumerate(external.get(collection, [])):
            status = entry.get("status")
            if status in ("to_select", "contractor_proposal"):
                resolution = "undecided" if status == "to_select" else "contractor_proposal_requested"
                add(
                    "external_services_and_assets",
                    f"/{collection}/{index}/status",
                    resolution,
                    f"{label_prefix}.{entry[f'{label_prefix}_code']}",
                )

    budget_status = data.get("delivery_and_constraints", {}).get("budget_status")
    if budget_status in ("undecided", "contractor_proposal_requested"):
        add("delivery_and_constraints", "/budget_status", budget_status, "field.budget_status")
    return items


def build_json_a_document(data: dict[str, Any], document_id: str, version: int) -> dict[str, Any]:
    project = data.get("project", {})
    open_items = [
        {key: value for key, value in item.items() if key != "label_key"}
        for item in collect_open_items(data)
    ]
    return {
        "schema_version": "json-a/1.1",
        "document_type": "requirement_request",
        "document_id": document_id,
        "version": version,
        "generated_at": utc_now(),
        "project": {
            "working_name": str(project.get("working_name", "")),
            "target_audience": str(project.get("target_audience", "")),
            "primary_purpose": str(project.get("primary_purpose", "")),
            "primary_visitor_action": str(project.get("primary_visitor_action", "")),
            "supplemental_notes": str(project.get("supplemental_notes", "")),
        },
        "users_and_scenarios": data.get("users_and_scenarios", {}),
        "pages_and_content": data.get("pages_and_content", {}),
        "features": data.get("features", {}),
        "data_and_permissions": data.get("data_and_permissions", {}),
        "external_services_and_assets": data.get("external_services_and_assets", {}),
        "visual_preferences": data.get("visual_preferences", {}),
        "delivery_and_constraints": data.get("delivery_and_constraints", {}),
        "blueprint": blueprint_data(data),
        "open_items": open_items,
    }


def generate_json_a(
    connection,
    data_root: Path,
    schema_path: Path,
    case_id: str,
    author_id: str,
    expected_revision: int,
    document_assets: DocumentAssets,
):
    case = connection.execute(
        "SELECT content_revision, current_json_a_artifact_id, state FROM cases WHERE id = ? AND owner_id = ?",
        (case_id, author_id),
    ).fetchone()
    if case is None:
        raise LookupError("case_missing")
    if case["state"] != "REQUIREMENT_DRAFT" or case["content_revision"] != expected_revision:
        raise RuntimeError("revision_conflict")
    data, completed = load_draft(connection, case_id)
    if completed != set(range(1, 9)):
        raise ValueError("sections_incomplete")
    if not all(section_is_complete(number, data.get(SECTION_KEYS[number - 1], {})) for number in range(1, 9)):
        raise ValueError("requirements_contradictory")
    artifact_id = str(uuid.uuid4())
    version = connection.execute(
        "SELECT COALESCE(MAX(version_no), 0) + 1 FROM artifacts WHERE case_id = ? AND kind = 'json_a'", (case_id,)
    ).fetchone()[0]
    document = build_json_a_document(data, artifact_id, version)
    ensure_supported_text(json.dumps(document, ensure_ascii=False), document_assets)
    validate_json_a(document, schema_path)
    raw = encode_json_once(document)
    digest = sha256_bytes(raw)
    relative_path = install_json_artifact(data_root, case_id, artifact_id, raw)
    now = utc_now()
    try:
        with immediate_transaction(connection):
            locked = connection.execute(
                "SELECT content_revision, state FROM cases WHERE id = ? AND owner_id = ?", (case_id, author_id)
            ).fetchone()
            if (
                locked is None
                or locked["state"] != "REQUIREMENT_DRAFT"
                or locked["content_revision"] != expected_revision
            ):
                raise RuntimeError("revision_conflict")
            connection.execute(
                "UPDATE artifacts SET lifecycle = 'superseded' WHERE case_id = ? AND kind = 'json_a' AND lifecycle = 'active'",
                (case_id,),
            )
            connection.execute(
                "INSERT INTO artifacts(id, case_id, author_id, kind, version_no, visibility, lifecycle, relative_path, sha256, byte_length, schema_version, created_at) "
                "VALUES (?, ?, ?, 'json_a', ?, 'private', 'active', ?, ?, ?, 'json-a/1.1', ?)",
                (artifact_id, case_id, author_id, version, relative_path, digest, len(raw), now),
            )
            connection.execute(
                "UPDATE cases SET current_json_a_artifact_id = ?, content_revision = content_revision + 1, updated_at = ? WHERE id = ?",
                (artifact_id, now, case_id),
            )
    except BaseException:
        remove_new_artifact(data_root, relative_path)
        raise
    return artifact_id, digest, raw
