from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from app.db import deferred_read_transaction, immediate_transaction
from app.services.artifact_store import (
    ArtifactStoreError,
    MAX_DELIVERY_ARTIFACT_BYTES,
    MAX_JSON_ARTIFACT_BYTES,
    install_fixed_blob,
    quarantine_fixed_collection_directory,
    read_verified,
)
from app.services.document_integrity import DocumentAssets
from app.services.formalization_workflow import (
    FormalizationWorkflowError,
    load_formal_version,
    revalidate_formal_snapshot,
)


class DeliveryWorkflowError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


MAX_DELIVERY_VERSIONS = 20


@dataclass(frozen=True)
class DeliveryResult:
    delivery_id: str
    version_no: int
    state: str


@dataclass(frozen=True)
class InspectionResult:
    inspection_id: str
    decision: str
    state: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _future(now: str, days: int) -> str:
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return (parsed + timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _role(formal: Mapping[str, object], actor_id: str) -> str:
    case = formal["case"]
    if case["owner_id"] == actor_id:
        return "client"
    if case["contractor_id"] == actor_id:
        return "contractor"
    raise DeliveryWorkflowError("case_unavailable")


def _formal_json_b(
    connection: sqlite3.Connection,
    data_root: Path,
    formal: Mapping[str, object],
) -> dict[str, object]:
    candidate = formal["candidate"]
    artifact = connection.execute(
        "SELECT relative_path, sha256, byte_length FROM artifacts WHERE id = ? AND kind = 'json_b' AND lifecycle = 'formal'",
        (candidate["json_b_artifact_id"],),
    ).fetchone()
    if artifact is None:
        raise DeliveryWorkflowError("formal_integrity_invalid")
    try:
        raw = read_verified(
            data_root, artifact["relative_path"], artifact["sha256"], artifact["byte_length"],
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
        )
        document = json.loads(raw.decode("utf-8"))
    except (ArtifactStoreError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeliveryWorkflowError("formal_integrity_invalid") from error
    if not isinstance(document, dict):
        raise DeliveryWorkflowError("formal_integrity_invalid")
    return document


def _criteria(document: Mapping[str, object]) -> list[dict[str, str]]:
    acceptance = document.get("acceptance")
    values = acceptance.get("criteria") if isinstance(acceptance, dict) else None
    if not isinstance(values, list) or not values:
        raise DeliveryWorkflowError("criteria_invalid")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            raise DeliveryWorkflowError("criteria_invalid")
        criterion_id = value.get("criterion_id")
        description = value.get("description")
        method = value.get("verification_method")
        if not all(isinstance(item, str) and item for item in (criterion_id, description, method)):
            raise DeliveryWorkflowError("criteria_invalid")
        if criterion_id in seen:
            raise DeliveryWorkflowError("criteria_invalid")
        seen.add(criterion_id)
        result.append({"criterion_id": criterion_id, "description": description, "verification_method": method})
    return result


def _workflow(connection: sqlite3.Connection, case_id: str):
    return connection.execute("SELECT * FROM delivery_workflows WHERE case_id = ?", (case_id,)).fetchone()


def _workflow_token(row: sqlite3.Row | None) -> tuple[object, ...] | None:
    if row is None:
        return None
    return (
        row["case_id"], row["formal_candidate_id"], row["state"],
        row["current_delivery_id"], row["current_inspection_id"],
        row["active_dispute_id"], row["revision"],
    )


def _quarantine_provisional_blob(
    data_root: Path,
    case_id: str,
    collection: str,
    record_id: str,
) -> None:
    try:
        quarantine_fixed_collection_directory(data_root, case_id, collection, record_id)
    except (ArtifactStoreError, OSError) as error:
        raise DeliveryWorkflowError("artifact_quarantine_failed") from error


def _event(
    connection: sqlite3.Connection,
    case_id: str,
    code: str,
    entity_type: str,
    entity_id: str,
    actor_id: str | None,
    role: str | None,
    now: str,
    effective_at: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO case_timeline_events(id, case_id, event_code, entity_type, entity_id, actor_id, actor_role, occurred_at, effective_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), case_id, code, entity_type, entity_id, actor_id, role, now, effective_at),
    )


def submit_delivery(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    actor_id: str,
    summary: str,
    item_inputs: Mapping[str, tuple[str, str]],
    test_inputs: Mapping[str, tuple[str, str]],
    idempotency_key: str,
) -> DeliveryResult:
    summary = summary.strip()
    if not summary or len(summary) > 4000 or not 16 <= len(idempotency_key) <= 200:
        raise DeliveryWorkflowError("delivery_invalid")
    with deferred_read_transaction(connection):
        try:
            formal = load_formal_version(connection, data_root, assets, case_id, actor_id)
        except FormalizationWorkflowError as error:
            raise DeliveryWorkflowError("case_unavailable") from error
        if _role(formal, actor_id) != "contractor":
            raise DeliveryWorkflowError("case_unavailable")
        workflow = _workflow(connection, case_id)
        workflow_token = _workflow_token(workflow)
        state = workflow["state"] if workflow is not None else "FORMAL"
        document = _formal_json_b(connection, data_root, formal)
        criteria = _criteria(document)
        expected = {item["criterion_id"] for item in criteria}
        if set(item_inputs) != expected or set(test_inputs) != expected:
            raise DeliveryWorkflowError("criteria_incomplete")
        normalized_items: list[dict[str, str]] = []
        normalized_tests: list[dict[str, str]] = []
        for criterion in criteria:
            criterion_id = criterion["criterion_id"]
            note, evidence = (part.strip() for part in item_inputs[criterion_id])
            outcome, observation = test_inputs[criterion_id]
            observation = observation.strip()
            if not note or not evidence or len(note) > 4000 or len(evidence) > 1000:
                raise DeliveryWorkflowError("delivery_invalid")
            if outcome not in {"PASS", "FAIL", "NOT_RUN"} or not observation or len(observation) > 4000:
                raise DeliveryWorkflowError("delivery_invalid")
            normalized_items.append({**criterion, "delivery_note": note, "evidence_reference": evidence})
            normalized_tests.append({"criterion_id": criterion_id, "outcome": outcome, "observation": observation})
        request_sha256 = _digest({
            "actor_id": actor_id,
            "case_id": case_id,
            "formal_candidate_id": formal["candidate"]["id"],
            "summary": summary,
            "items": normalized_items,
            "tests": normalized_tests,
        })
        existing = connection.execute(
            "SELECT id, version_no, request_sha256 FROM delivery_versions WHERE case_id = ? AND idempotency_key = ?",
            (case_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(existing["request_sha256"], request_sha256):
                raise DeliveryWorkflowError("idempotency_conflict")
            return DeliveryResult(existing["id"], existing["version_no"], state)
        if state not in {"FORMAL", "REMEDIATION"}:
            raise DeliveryWorkflowError("delivery_state_invalid")
        version_no = 1 if workflow is None else connection.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM delivery_versions WHERE case_id = ?", (case_id,)
        ).fetchone()[0]
        if version_no > MAX_DELIVERY_VERSIONS:
            raise DeliveryWorkflowError("delivery_version_limit")
        predecessor = workflow["current_delivery_id"] if workflow is not None else None
        remediated_inspection = workflow["current_inspection_id"] if workflow is not None else None
        delivery_id = str(uuid.uuid4())
        now = _now()
        review_days = int(document["acceptance"]["review_period_days"])
        due_at = _future(now, review_days)
        record = {
            "schema_version": "delivery-record/1.0", "delivery_id": delivery_id,
            "case_id": case_id, "formal_candidate_id": formal["candidate"]["id"],
            "version": version_no, "kind": "INITIAL" if workflow is None else "REMEDIATION",
            "predecessor_delivery_id": predecessor, "remediated_inspection_id": remediated_inspection,
            "submitted_by_id": actor_id, "submitted_by_role": "contractor", "summary": summary,
            "items": normalized_items, "test_results": normalized_tests,
            "submitted_at": now, "review_due_at": due_at,
        }
        raw = _canonical_json(record)
        digest = hashlib.sha256(raw).hexdigest()
        criteria_sha256 = _digest(sorted(expected))

    install_fixed_blob(data_root, case_id, "deliveries", delivery_id, "delivery.json", raw)
    created = False
    try:
        with immediate_transaction(connection):
            try:
                revalidate_formal_snapshot(connection, case_id, actor_id, formal)
            except FormalizationWorkflowError as error:
                raise DeliveryWorkflowError("case_unavailable") from error
            locked_workflow = _workflow(connection, case_id)
            existing = connection.execute(
                "SELECT id, version_no, request_sha256 FROM delivery_versions WHERE case_id = ? AND idempotency_key = ?",
                (case_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(existing["request_sha256"], request_sha256):
                    raise DeliveryWorkflowError("idempotency_conflict")
                result = DeliveryResult(existing["id"], existing["version_no"], locked_workflow["state"] if locked_workflow else "FORMAL")
            else:
                locked_version = 1 if locked_workflow is None else connection.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM delivery_versions WHERE case_id = ?", (case_id,)
                ).fetchone()[0]
                if _workflow_token(locked_workflow) != workflow_token or locked_version != version_no:
                    raise DeliveryWorkflowError("workflow_stale")
                installed_path = f"cases/{case_id}/deliveries/{delivery_id}/delivery.json"
                connection.execute(
                    "INSERT INTO delivery_versions(id, case_id, formal_candidate_id, version_no, kind, predecessor_delivery_id, "
                    "remediated_inspection_id, submitted_by_id, summary, relative_path, sha256, byte_length, item_count, criteria_sha256, "
                    "request_sha256, submitted_at, review_due_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (delivery_id, case_id, formal["candidate"]["id"], version_no, record["kind"], predecessor,
                     remediated_inspection, actor_id, summary, installed_path, digest, len(raw), len(criteria), criteria_sha256,
                     request_sha256, now, due_at, idempotency_key),
                )
                for item, test in zip(normalized_items, normalized_tests, strict=True):
                    connection.execute(
                        "INSERT INTO delivery_items(id, delivery_id, criterion_id, description, verification_method, delivery_note, evidence_reference) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (str(uuid.uuid4()), delivery_id, item["criterion_id"], item["description"], item["verification_method"], item["delivery_note"], item["evidence_reference"]),
                    )
                    connection.execute(
                        "INSERT INTO delivery_test_results(id, delivery_id, criterion_id, outcome, observation) VALUES (?, ?, ?, ?, ?)",
                        (str(uuid.uuid4()), delivery_id, test["criterion_id"], test["outcome"], test["observation"]),
                    )
                next_state = "DELIVERED" if workflow is None else "REINSPECTION"
                if workflow is None:
                    connection.execute(
                        "INSERT INTO delivery_workflows(case_id, formal_candidate_id, state, current_delivery_id, revision, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 1, ?, ?)",
                        (case_id, formal["candidate"]["id"], next_state, delivery_id, now, now),
                    )
                else:
                    connection.execute(
                        "UPDATE delivery_workflows SET state = ?, current_delivery_id = ?, current_inspection_id = NULL, "
                        "revision = revision + 1, updated_at = ? WHERE case_id = ? AND state = 'REMEDIATION'",
                        (next_state, delivery_id, now, case_id),
                    )
                _event(connection, case_id, "DELIVERY_SUBMITTED" if version_no == 1 else "REMEDIATION_SUBMITTED", "DELIVERY", delivery_id, actor_id, "contractor", now)
                _event(connection, case_id, "REVIEW_DEADLINE_SET", "DELIVERY", delivery_id, None, None, now, due_at)
                result = DeliveryResult(delivery_id, version_no, next_state)
                created = True
    except BaseException:
        _quarantine_provisional_blob(data_root, case_id, "deliveries", delivery_id)
        raise
    if not created:
        _quarantine_provisional_blob(data_root, case_id, "deliveries", delivery_id)
    return result


def begin_inspection(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    delivery_id: str,
    actor_id: str,
    expected_revision: int,
) -> None:
    with deferred_read_transaction(connection):
        try:
            formal = load_formal_version(connection, data_root, assets, case_id, actor_id)
        except FormalizationWorkflowError as error:
            raise DeliveryWorkflowError("case_unavailable") from error
        if _role(formal, actor_id) != "client":
            raise DeliveryWorkflowError("case_unavailable")
    with immediate_transaction(connection):
        try:
            revalidate_formal_snapshot(connection, case_id, actor_id, formal)
        except FormalizationWorkflowError as error:
            raise DeliveryWorkflowError("case_unavailable") from error
        workflow = _workflow(connection, case_id)
        if workflow is None or workflow["state"] != "DELIVERED" or workflow["current_delivery_id"] != delivery_id or workflow["revision"] != expected_revision:
            raise DeliveryWorkflowError("workflow_stale")
        now = _now()
        updated = connection.execute(
            "UPDATE delivery_workflows SET state = 'UNDER_ACCEPTANCE', revision = revision + 1, updated_at = ? "
            "WHERE case_id = ? AND state = 'DELIVERED' AND current_delivery_id = ? AND revision = ?",
            (now, case_id, delivery_id, expected_revision),
        )
        if updated.rowcount != 1:
            raise DeliveryWorkflowError("workflow_stale")
        _event(connection, case_id, "INSPECTION_STARTED", "DELIVERY", delivery_id, actor_id, "client", now)


def submit_inspection(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    delivery_id: str,
    actor_id: str,
    expected_revision: int,
    results: Mapping[str, tuple[str, str]],
    explicit_acceptance: bool,
    idempotency_key: str,
) -> InspectionResult:
    if not 16 <= len(idempotency_key) <= 200:
        raise DeliveryWorkflowError("inspection_invalid")
    with deferred_read_transaction(connection):
        try:
            formal = load_formal_version(connection, data_root, assets, case_id, actor_id)
        except FormalizationWorkflowError as error:
            raise DeliveryWorkflowError("case_unavailable") from error
        if _role(formal, actor_id) != "client":
            raise DeliveryWorkflowError("case_unavailable")
        workflow = _workflow(connection, case_id)
        items = connection.execute(
            "SELECT id, criterion_id FROM delivery_items WHERE delivery_id = ? ORDER BY criterion_id", (delivery_id,)
        ).fetchall()
        expected = {row["criterion_id"] for row in items}
        if not items or set(results) != expected:
            raise DeliveryWorkflowError("criteria_incomplete")
        normalized: list[tuple[sqlite3.Row, str, str | None]] = []
        for item in items:
            result, reason = results[item["criterion_id"]]
            reason = reason.strip()
            if result == "PASS":
                reason_value = None
            elif result == "RETURNED" and reason and len(reason) <= 4000:
                reason_value = reason
            else:
                raise DeliveryWorkflowError("inspection_invalid")
            normalized.append((item, result, reason_value))
        all_pass = all(value[1] == "PASS" for value in normalized)
        if all_pass != explicit_acceptance:
            raise DeliveryWorkflowError("explicit_acceptance_required")
        request_sha256 = _digest({
            "actor_id": actor_id,
            "case_id": case_id,
            "delivery_id": delivery_id,
            "results": [{"criterion_id": item["criterion_id"], "result": result, "reason": reason} for item, result, reason in normalized],
            "explicit_acceptance": explicit_acceptance,
        })
        criteria_sha256 = _digest(sorted(expected))
        workflow_token = _workflow_token(workflow)
        item_token = tuple((item["id"], item["criterion_id"]) for item in items)
        existing = connection.execute(
            "SELECT id, decision, request_sha256 FROM inspection_rounds WHERE case_id = ? AND idempotency_key = ?",
            (case_id, idempotency_key),
        ).fetchone()
        existing_token = None if existing is None else tuple(existing)
        inspection_id = str(uuid.uuid4())
        round_no = connection.execute(
            "SELECT COALESCE(MAX(round_no), 0) + 1 FROM inspection_rounds WHERE case_id = ?", (case_id,)
        ).fetchone()[0]
        now = _now()
        decision = "ACCEPTED" if all_pass else "RETURNED"
        next_state = "ACCEPTED" if all_pass else "REMEDIATION"
        result_rows = [
            (str(uuid.uuid4()), inspection_id, item["id"], item["criterion_id"], result, reason)
            for item, result, reason in normalized
        ]
        acceptance_id = str(uuid.uuid4()) if all_pass else None
        evidence_sha256 = None
        if all_pass:
            evidence_sha256 = _digest({
                "case_id": case_id,
                "formal_candidate_id": formal["candidate"]["id"],
                "delivery_id": delivery_id,
                "inspection_id": inspection_id,
                "criteria_sha256": criteria_sha256,
                "accepted_by_id": actor_id,
                "accepted_at": now,
            })
    with immediate_transaction(connection):
        try:
            revalidate_formal_snapshot(connection, case_id, actor_id, formal)
        except FormalizationWorkflowError as error:
            raise DeliveryWorkflowError("case_unavailable") from error
        locked_workflow = _workflow(connection, case_id)
        locked_items = connection.execute(
            "SELECT id, criterion_id FROM delivery_items WHERE delivery_id = ? ORDER BY criterion_id", (delivery_id,)
        ).fetchall()
        if (
            _workflow_token(locked_workflow) != workflow_token
            or tuple((item["id"], item["criterion_id"]) for item in locked_items) != item_token
        ):
            raise DeliveryWorkflowError("workflow_stale")
        locked_existing = connection.execute(
            "SELECT id, decision, request_sha256 FROM inspection_rounds WHERE case_id = ? AND idempotency_key = ?",
            (case_id, idempotency_key),
        ).fetchone()
        if locked_existing is not None:
            if not hmac.compare_digest(locked_existing["request_sha256"], request_sha256):
                raise DeliveryWorkflowError("idempotency_conflict")
            return InspectionResult(locked_existing["id"], locked_existing["decision"], locked_workflow["state"] if locked_workflow else "")
        if existing_token is not None:
            raise DeliveryWorkflowError("workflow_stale")
        if locked_workflow is None or locked_workflow["state"] not in {"UNDER_ACCEPTANCE", "REINSPECTION"} or locked_workflow["current_delivery_id"] != delivery_id or locked_workflow["revision"] != expected_revision:
            raise DeliveryWorkflowError("workflow_stale")
        locked_round_no = connection.execute(
            "SELECT COALESCE(MAX(round_no), 0) + 1 FROM inspection_rounds WHERE case_id = ?", (case_id,)
        ).fetchone()[0]
        if locked_round_no != round_no:
            raise DeliveryWorkflowError("workflow_stale")
        connection.execute(
            "INSERT INTO inspection_rounds(id, case_id, delivery_id, round_no, kind, decision, explicit_acceptance, inspected_by_id, inspected_at, "
            "result_count, criteria_sha256, request_sha256, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (inspection_id, case_id, delivery_id, round_no, "INITIAL" if round_no == 1 else "REINSPECTION", decision, int(all_pass), actor_id, now,
             len(items), criteria_sha256, request_sha256, idempotency_key),
        )
        for result_row in result_rows:
            connection.execute(
                "INSERT INTO inspection_item_results(id, inspection_id, delivery_item_id, criterion_id, result, reason) VALUES (?, ?, ?, ?, ?, ?)",
                result_row,
            )
        if all_pass:
            connection.execute(
                "INSERT INTO acceptance_events(id, case_id, formal_candidate_id, delivery_id, inspection_id, accepted_by_id, evidence_sha256, accepted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (acceptance_id, case_id, formal["candidate"]["id"], delivery_id, inspection_id, actor_id, evidence_sha256, now),
            )
        updated = connection.execute(
            "UPDATE delivery_workflows SET state = ?, current_inspection_id = ?, revision = revision + 1, updated_at = ? "
            "WHERE case_id = ? AND state = ? AND current_delivery_id = ? AND revision = ?",
            (next_state, inspection_id, now, case_id, locked_workflow["state"], delivery_id, expected_revision),
        )
        if updated.rowcount != 1:
            raise DeliveryWorkflowError("workflow_stale")
        _event(connection, case_id, "DELIVERY_ACCEPTED" if all_pass else "INSPECTION_RETURNED", "INSPECTION", inspection_id, actor_id, "client", now)
        return InspectionResult(inspection_id, decision, next_state)


def open_dispute(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    actor_id: str,
    expected_revision: int,
    reason: str,
) -> str:
    reason = reason.strip()
    if not reason or len(reason) > 4000:
        raise DeliveryWorkflowError("dispute_invalid")
    with deferred_read_transaction(connection):
        try:
            formal = load_formal_version(connection, data_root, assets, case_id, actor_id)
        except FormalizationWorkflowError as error:
            raise DeliveryWorkflowError("case_unavailable") from error
        role = _role(formal, actor_id)
    with immediate_transaction(connection):
        try:
            revalidate_formal_snapshot(connection, case_id, actor_id, formal)
        except FormalizationWorkflowError as error:
            raise DeliveryWorkflowError("case_unavailable") from error
        workflow = _workflow(connection, case_id)
        if workflow is None or workflow["state"] not in {"DELIVERED", "UNDER_ACCEPTANCE", "REMEDIATION", "REINSPECTION"} or workflow["revision"] != expected_revision:
            raise DeliveryWorkflowError("workflow_stale")
        dispute_id = str(uuid.uuid4())
        now = _now()
        connection.execute(
            "INSERT INTO disputes(id, case_id, formal_candidate_id, delivery_id, inspection_id, prior_state, opened_by_id, opened_by_role, reason, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dispute_id, case_id, formal["candidate"]["id"], workflow["current_delivery_id"], workflow["current_inspection_id"], workflow["state"], actor_id, role, reason, now),
        )
        updated = connection.execute(
            "UPDATE delivery_workflows SET state = 'DISPUTED', active_dispute_id = ?, revision = revision + 1, updated_at = ? "
            "WHERE case_id = ? AND state = ? AND revision = ?",
            (dispute_id, now, case_id, workflow["state"], expected_revision),
        )
        if updated.rowcount != 1:
            raise DeliveryWorkflowError("workflow_stale")
        _event(connection, case_id, "DISPUTE_OPENED", "DISPUTE", dispute_id, actor_id, role, now)
        return dispute_id


def add_dispute_position(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    dispute_id: str,
    actor_id: str,
    position_text: str,
) -> str:
    position_text = position_text.strip()
    if not position_text or len(position_text) > 4000:
        raise DeliveryWorkflowError("position_invalid")
    with deferred_read_transaction(connection):
        try:
            formal = load_formal_version(connection, data_root, assets, case_id, actor_id)
        except FormalizationWorkflowError as error:
            raise DeliveryWorkflowError("case_unavailable") from error
        role = _role(formal, actor_id)
    with immediate_transaction(connection):
        try:
            revalidate_formal_snapshot(connection, case_id, actor_id, formal)
        except FormalizationWorkflowError as error:
            raise DeliveryWorkflowError("case_unavailable") from error
        workflow = _workflow(connection, case_id)
        if workflow is None or workflow["state"] != "DISPUTED" or workflow["active_dispute_id"] != dispute_id:
            raise DeliveryWorkflowError("dispute_unavailable")
        position_id = str(uuid.uuid4())
        now = _now()
        try:
            connection.execute(
                "INSERT INTO dispute_positions(id, dispute_id, actor_id, actor_role, position_text, submitted_at) VALUES (?, ?, ?, ?, ?, ?)",
                (position_id, dispute_id, actor_id, role, position_text, now),
            )
        except sqlite3.IntegrityError as error:
            raise DeliveryWorkflowError("position_exists") from error
        _event(connection, case_id, "DISPUTE_POSITION_ADDED", "DISPUTE", dispute_id, actor_id, role, now)
        return position_id


def _verify_inspection_and_acceptance_integrity(connection: sqlite3.Connection, case_id: str) -> None:
    case = connection.execute(
        "SELECT owner_id, formal_candidate_id FROM cases WHERE id = ? AND state = 'FORMAL'",
        (case_id,),
    ).fetchone()
    workflow = _workflow(connection, case_id)
    if case is None or workflow is None:
        raise DeliveryWorkflowError("acceptance_integrity_invalid")
    inspections = connection.execute(
        "SELECT * FROM inspection_rounds WHERE case_id = ? ORDER BY round_no",
        (case_id,),
    ).fetchall()
    for inspection in inspections:
        delivery = connection.execute(
            "SELECT item_count, criteria_sha256 FROM delivery_versions WHERE id = ? AND case_id = ?",
            (inspection["delivery_id"], case_id),
        ).fetchone()
        results = connection.execute(
            "SELECT criterion_id, result FROM inspection_item_results WHERE inspection_id = ? ORDER BY criterion_id",
            (inspection["id"],),
        ).fetchall()
        delivery_criteria = [
            row[0]
            for row in connection.execute(
                "SELECT criterion_id FROM delivery_items WHERE delivery_id = ? ORDER BY criterion_id",
                (inspection["delivery_id"],),
            ).fetchall()
        ]
        result_criteria = [row["criterion_id"] for row in results]
        all_pass = bool(results) and all(row["result"] == "PASS" for row in results)
        expected_decision = "ACCEPTED" if all_pass else "RETURNED"
        if (
            delivery is None
            or len(results) != inspection["result_count"]
            or inspection["result_count"] != delivery["item_count"]
            or inspection["criteria_sha256"] != delivery["criteria_sha256"]
            or result_criteria != delivery_criteria
            or inspection["decision"] != expected_decision
            or bool(inspection["explicit_acceptance"]) != all_pass
        ):
            raise DeliveryWorkflowError("acceptance_integrity_invalid")
    acceptance = connection.execute(
        "SELECT * FROM acceptance_events WHERE case_id = ?",
        (case_id,),
    ).fetchone()
    if workflow["state"] == "ACCEPTED":
        if acceptance is None:
            raise DeliveryWorkflowError("acceptance_integrity_invalid")
    elif acceptance is not None:
        raise DeliveryWorkflowError("acceptance_integrity_invalid")
    if acceptance is None:
        return
    inspection = connection.execute(
        "SELECT inspected_at, criteria_sha256 FROM inspection_rounds WHERE id = ? AND case_id = ? AND delivery_id = ? "
        "AND decision = 'ACCEPTED' AND explicit_acceptance = 1",
        (acceptance["inspection_id"], case_id, acceptance["delivery_id"]),
    ).fetchone()
    expected_digest = _digest({
        "case_id": case_id,
        "formal_candidate_id": acceptance["formal_candidate_id"],
        "delivery_id": acceptance["delivery_id"],
        "inspection_id": acceptance["inspection_id"],
        "criteria_sha256": inspection["criteria_sha256"] if inspection is not None else "",
        "accepted_by_id": acceptance["accepted_by_id"],
        "accepted_at": acceptance["accepted_at"],
    })
    if (
        inspection is None
        or acceptance["formal_candidate_id"] != case["formal_candidate_id"]
        or acceptance["accepted_by_id"] != case["owner_id"]
        or acceptance["accepted_at"] != inspection["inspected_at"]
        or workflow["current_delivery_id"] != acceptance["delivery_id"]
        or workflow["current_inspection_id"] != acceptance["inspection_id"]
        or not hmac.compare_digest(acceptance["evidence_sha256"], expected_digest)
    ):
        raise DeliveryWorkflowError("acceptance_integrity_invalid")


def load_delivery_dashboard(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    actor_id: str,
) -> dict[str, object]:
    try:
        formal = load_formal_version(connection, data_root, assets, case_id, actor_id)
    except FormalizationWorkflowError as error:
        raise DeliveryWorkflowError("case_unavailable") from error
    role = _role(formal, actor_id)
    document = _formal_json_b(connection, data_root, formal)
    criteria = _criteria(document)
    workflow = _workflow(connection, case_id)
    if workflow is not None:
        _verify_inspection_and_acceptance_integrity(connection, case_id)
    deliveries = connection.execute(
        "SELECT * FROM delivery_versions WHERE case_id = ? ORDER BY version_no", (case_id,)
    ).fetchall()
    delivery_details: list[dict[str, object]] = []
    for delivery in deliveries:
        delivery_details.append({
            "row": delivery,
            "items": connection.execute("SELECT * FROM delivery_items WHERE delivery_id = ? ORDER BY criterion_id", (delivery["id"],)).fetchall(),
            "tests": connection.execute("SELECT * FROM delivery_test_results WHERE delivery_id = ? ORDER BY criterion_id", (delivery["id"],)).fetchall(),
        })
    inspections = connection.execute(
        "SELECT * FROM inspection_rounds WHERE case_id = ? ORDER BY round_no", (case_id,)
    ).fetchall()
    inspection_details = [{
        "row": inspection,
        "results": connection.execute("SELECT * FROM inspection_item_results WHERE inspection_id = ? ORDER BY criterion_id", (inspection["id"],)).fetchall(),
    } for inspection in inspections]
    dispute = connection.execute("SELECT * FROM disputes WHERE case_id = ?", (case_id,)).fetchone()
    positions = [] if dispute is None else connection.execute(
        "SELECT * FROM dispute_positions WHERE dispute_id = ? ORDER BY actor_role", (dispute["id"],)
    ).fetchall()
    own_position = next((position for position in positions if position["actor_role"] == role), None)
    exports = connection.execute(
        "SELECT id, version_no, source_fingerprint, sha256, byte_length, prepared_at "
        "FROM evidence_exports WHERE case_id = ? ORDER BY version_no",
        (case_id,),
    ).fetchall()
    events = connection.execute(
        "SELECT * FROM case_timeline_events WHERE case_id = ? ORDER BY occurred_at, id", (case_id,)
    ).fetchall()
    acceptance = connection.execute("SELECT * FROM acceptance_events WHERE case_id = ?", (case_id,)).fetchone()
    return {
        "formal": formal,
        "role": role,
        "workflow": workflow,
        "state": workflow["state"] if workflow is not None else "FORMAL",
        "revision": workflow["revision"] if workflow is not None else 0,
        "criteria": criteria,
        "criteria_by_id": {criterion["criterion_id"]: criterion for criterion in criteria},
        "delivery_count": len(deliveries),
        "delivery_limit_reached": len(deliveries) >= MAX_DELIVERY_VERSIONS,
        "deliveries": delivery_details,
        "inspections": inspection_details,
        "dispute": dispute,
        "positions": positions,
        "own_position": own_position,
        "exports": exports,
        "events": events,
        "acceptance": acceptance,
    }


def read_delivery_record(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    delivery_id: str,
    actor_id: str,
) -> tuple[sqlite3.Row, bytes]:
    load_delivery_dashboard(connection, data_root, assets, case_id, actor_id)
    row = connection.execute(
        "SELECT * FROM delivery_versions WHERE id = ? AND case_id = ?", (delivery_id, case_id)
    ).fetchone()
    if row is None:
        raise DeliveryWorkflowError("delivery_unavailable")
    if row["relative_path"] != f"cases/{case_id}/deliveries/{delivery_id}/delivery.json":
        raise DeliveryWorkflowError("delivery_integrity_invalid")
    try:
        raw = read_verified(
            data_root, row["relative_path"], row["sha256"], row["byte_length"],
            max_bytes=MAX_DELIVERY_ARTIFACT_BYTES,
        )
    except (ArtifactStoreError, OSError) as error:
        raise DeliveryWorkflowError("delivery_integrity_invalid") from error
    if len(raw) != row["byte_length"]:
        raise DeliveryWorkflowError("delivery_integrity_invalid")
    return row, raw


def reconcile_delivery_storage(connection: sqlite3.Connection, data_root: Path) -> None:
    referenced: dict[tuple[str, str], sqlite3.Row] = {}
    rows = connection.execute("SELECT * FROM delivery_versions ORDER BY case_id, version_no").fetchall()
    for row in rows:
        expected_path = f"cases/{row['case_id']}/deliveries/{row['id']}/delivery.json"
        if row["relative_path"] != expected_path:
            raise DeliveryWorkflowError("delivery_path_invalid")
        try:
            raw = read_verified(
                data_root, expected_path, row["sha256"], row["byte_length"],
                max_bytes=MAX_DELIVERY_ARTIFACT_BYTES,
            )
            record = json.loads(raw.decode("utf-8"))
        except (ArtifactStoreError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DeliveryWorkflowError("delivery_integrity_invalid") from error
        if len(raw) != row["byte_length"] or not isinstance(record, dict):
            raise DeliveryWorkflowError("delivery_integrity_invalid")
        items = connection.execute("SELECT * FROM delivery_items WHERE delivery_id = ? ORDER BY criterion_id", (row["id"],)).fetchall()
        tests = connection.execute("SELECT * FROM delivery_test_results WHERE delivery_id = ? ORDER BY criterion_id", (row["id"],)).fetchall()
        criteria = [{"criterion_id": item["criterion_id"], "description": item["description"], "verification_method": item["verification_method"]} for item in items]
        record_items = [{"criterion_id": item["criterion_id"], "description": item["description"], "verification_method": item["verification_method"], "delivery_note": item["delivery_note"], "evidence_reference": item["evidence_reference"]} for item in items]
        record_tests = [{"criterion_id": test["criterion_id"], "outcome": test["outcome"], "observation": test["observation"]} for test in tests]
        expected_record = {
            "schema_version": "delivery-record/1.0", "delivery_id": row["id"], "case_id": row["case_id"],
            "formal_candidate_id": row["formal_candidate_id"], "version": row["version_no"], "kind": row["kind"],
            "predecessor_delivery_id": row["predecessor_delivery_id"], "remediated_inspection_id": row["remediated_inspection_id"],
            "submitted_by_id": row["submitted_by_id"], "submitted_by_role": "contractor",
            "summary": row["summary"], "items": record_items, "test_results": record_tests,
            "submitted_at": row["submitted_at"], "review_due_at": row["review_due_at"],
        }
        record_without_lists = dict(record)
        record_items_actual = record_without_lists.pop("items", None)
        record_tests_actual = record_without_lists.pop("test_results", None)
        expected_without_lists = dict(expected_record)
        expected_without_lists.pop("items")
        expected_without_lists.pop("test_results")
        if (len(items) != row["item_count"] or len(tests) != row["item_count"]
                or _digest(sorted(item["criterion_id"] for item in criteria)) != row["criteria_sha256"]
                or record_without_lists != expected_without_lists
                or not isinstance(record_items_actual, list) or not isinstance(record_tests_actual, list)
                or not all(isinstance(item, dict) for item in record_items_actual)
                or not all(isinstance(item, dict) for item in record_tests_actual)
                or sorted(record_items_actual, key=lambda item: item.get("criterion_id", "")) != record_items
                or sorted(record_tests_actual, key=lambda item: item.get("criterion_id", "")) != record_tests):
            raise DeliveryWorkflowError("delivery_integrity_invalid")
        referenced[(row["case_id"], row["id"])] = row
    for workflow_row in connection.execute("SELECT case_id FROM delivery_workflows ORDER BY case_id").fetchall():
        _verify_inspection_and_acceptance_integrity(connection, workflow_row["case_id"])
    cases_root = data_root / "cases"
    if not cases_root.exists():
        return
    for case_dir in cases_root.iterdir():
        delivery_root = case_dir / "deliveries"
        if not delivery_root.is_dir() or delivery_root.is_symlink():
            continue
        for entry in list(delivery_root.iterdir()):
            if entry.is_dir() and not entry.is_symlink() and (case_dir.name, entry.name) not in referenced:
                quarantine_fixed_collection_directory(data_root, case_dir.name, "deliveries", entry.name)
