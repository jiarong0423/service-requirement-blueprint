from __future__ import annotations

import hmac
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db import immediate_transaction
from app.services.artifact_store import (
    ArtifactStoreError,
    install_json_artifact,
    read_verified,
    remove_new_artifact,
)
from app.services.case_workflow import utc_now
from app.services.document_integrity import DocumentAssets, ensure_supported_text
from app.services.json_contracts import (
    JsonContractError,
    decode_strict_json,
    encode_json_once,
    sha256_bytes,
    validate_json_b,
)


ACTIVE_CASE_STATES = ("CASE_ACTIVE", "JSON_B_PRIVATE_DRAFT", "JSON_B_REVIEW")
BUSINESS_FIELDS = (
    "requirement_understanding",
    "included_scope",
    "excluded_scope",
    "pages",
    "functions",
    "data_and_permissions",
    "third_party_services",
    "milestones",
    "effort",
    "fee_items",
    "responsibilities",
    "acceptance",
    "remediation",
    "maintenance",
    "quote_expires_at",
)
OBJECTIONABLE_ROOTS = frozenset(BUSINESS_FIELDS)


class JsonBWorkflowError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExportResult:
    artifact_id: str
    draft_revision: int
    created: bool


@dataclass(frozen=True)
class ReviewResult:
    artifact_id: str
    version_no: int
    created: bool


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise JsonBWorkflowError("quote_expiry_invalid") from error
    if parsed.tzinfo is None:
        raise JsonBWorkflowError("quote_expiry_invalid")
    return parsed.astimezone(timezone.utc)


def _contractor_case(connection: sqlite3.Connection, case_id: str, actor_id: str) -> sqlite3.Row:
    actor = connection.execute(
        "SELECT status, system_role FROM users WHERE id = ?", (actor_id,)
    ).fetchone()
    if actor is None or actor["status"] != "active" or actor["system_role"] != "member":
        raise JsonBWorkflowError("case_unavailable")
    placeholders = ",".join("?" for _ in ACTIVE_CASE_STATES)
    row = connection.execute(
        "SELECT c.* FROM cases AS c "
        f"WHERE c.id = ? AND c.contractor_id = ? AND c.state IN ({placeholders})",
        (case_id, actor_id, *ACTIVE_CASE_STATES),
    ).fetchone()
    if row is None:
        raise JsonBWorkflowError("case_unavailable")
    return row


def _json_a_source(connection: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT a.id AS artifact_id, a.version_no, a.schema_version, a.sha256, a.byte_length, "
        "a.relative_path, v.document_id FROM cases AS c "
        "JOIN artifacts AS a ON a.id = c.current_json_a_artifact_id "
        "JOIN json_a_source_verifications AS v ON v.case_id = c.id AND v.artifact_id = a.id "
        "AND v.actor_role = 'contractor' "
        "WHERE c.id = ? AND a.kind = 'json_a' AND a.visibility = 'party_only' "
        "AND a.lifecycle = 'active'",
        (case_id,),
    ).fetchone()
    if row is None:
        raise JsonBWorkflowError("source_unavailable")
    return row


def _validate_source_file(data_root: Path, source: sqlite3.Row) -> None:
    try:
        raw = read_verified(data_root, source["relative_path"], source["sha256"])
    except (ArtifactStoreError, OSError, ValueError) as error:
        raise JsonBWorkflowError("source_unavailable") from error
    if len(raw) != source["byte_length"]:
        raise JsonBWorkflowError("source_unavailable")


def decode_business_draft(raw: bytes) -> dict[str, Any]:
    try:
        document = decode_strict_json(raw)
    except JsonContractError as error:
        raise JsonBWorkflowError("draft_invalid") from error
    if not isinstance(document, dict) or set(document) != set(BUSINESS_FIELDS):
        raise JsonBWorkflowError("draft_invalid")
    return document


def compute_fee_totals(fee_items: object) -> list[dict[str, object]]:
    if not isinstance(fee_items, list) or len(fee_items) > 200:
        raise JsonBWorkflowError("fee_items_invalid")
    totals: dict[tuple[str, str, str, str | None, bool], int] = {}
    for item in fee_items:
        if not isinstance(item, dict):
            raise JsonBWorkflowError("fee_items_invalid")
        billing_kind = item.get("billing_kind")
        cost_source = item.get("cost_source")
        currency_code = item.get("currency_code")
        amount = item.get("amount")
        billing_period = item.get("billing_period")
        included = item.get("included_in_quote")
        if billing_kind not in {"one_time", "recurring"}:
            raise JsonBWorkflowError("fee_items_invalid")
        if cost_source not in {"contractor", "third_party"}:
            raise JsonBWorkflowError("fee_items_invalid")
        if not isinstance(currency_code, str) or len(currency_code) != 3 or not currency_code.isascii() or not currency_code.isupper() or not currency_code.isalpha():
            raise JsonBWorkflowError("fee_items_invalid")
        if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= 9_999_999_999:
            raise JsonBWorkflowError("fee_items_invalid")
        if not isinstance(included, bool):
            raise JsonBWorkflowError("fee_items_invalid")
        if billing_kind == "one_time" and billing_period is not None:
            raise JsonBWorkflowError("fee_items_invalid")
        if billing_kind == "recurring" and billing_period not in {"monthly", "quarterly", "yearly"}:
            raise JsonBWorkflowError("fee_items_invalid")
        key = (billing_kind, cost_source, currency_code, billing_period, included)
        totals[key] = totals.get(key, 0) + amount
        if totals[key] > 9_999_999_999:
            raise JsonBWorkflowError("fee_items_invalid")
    return [
        {
            "billing_kind": key[0],
            "cost_source": key[1],
            "currency_code": key[2],
            "billing_period": key[3],
            "included_in_quote": key[4],
            "amount": amount,
        }
        for key, amount in sorted(
            totals.items(),
            key=lambda entry: (
                entry[0][0],
                entry[0][1],
                entry[0][2],
                entry[0][3] or "",
                entry[0][4],
            ),
        )
    ]


def _source_object(source: sqlite3.Row) -> dict[str, object]:
    return {
        "artifact_id": source["artifact_id"],
        "document_id": source["document_id"],
        "version": source["version_no"],
        "schema_version": source["schema_version"],
        "sha256": source["sha256"],
        "byte_length": source["byte_length"],
    }


def _build_document(
    case_id: str,
    draft: sqlite3.Row,
    source: sqlite3.Row,
    generated_at: str,
) -> dict[str, Any]:
    business = json.loads(draft["data_json"])
    document: dict[str, Any] = {
        "schema_version": "json-b/1.0",
        "document_type": "contractor_proposal",
        "document_id": draft["document_id"],
        "version": draft["target_version"],
        "generated_at": generated_at,
        "case_id": case_id,
        "source_json_a": _source_object(source),
    }
    document.update(business)
    document["computed_fee_totals"] = compute_fee_totals(document["fee_items"])
    return document


def _validate_document(document: dict[str, Any], schema_path: Path, *, now: datetime | None = None) -> None:
    try:
        validate_json_b(document, schema_path)
    except JsonContractError as error:
        raise JsonBWorkflowError("json_b_invalid") from error
    computed = compute_fee_totals(document.get("fee_items"))
    if document.get("computed_fee_totals") != computed:
        raise JsonBWorkflowError("totals_mismatch")
    generated = _parse_utc(str(document["generated_at"]))
    expiry = _parse_utc(str(document["quote_expires_at"]))
    if expiry <= generated:
        raise JsonBWorkflowError("quote_expiry_invalid")
    if now is not None and expiry <= now.astimezone(timezone.utc):
        raise JsonBWorkflowError("quote_expired")


def save_private_draft(
    connection: sqlite3.Connection,
    data_root: Path,
    schema_path: Path,
    case_id: str,
    contractor_id: str,
    expected_revision: int,
    raw_business_draft: bytes,
) -> int:
    business = decode_business_draft(raw_business_draft)
    with immediate_transaction(connection):
        case = _contractor_case(connection, case_id, contractor_id)
        source = _json_a_source(connection, case_id)
        _validate_source_file(data_root, source)
        current = connection.execute(
            "SELECT * FROM json_b_drafts WHERE case_id = ? AND contractor_id = ?",
            (case_id, contractor_id),
        ).fetchone()
        current_revision = 0 if current is None else current["content_revision"]
        if expected_revision != current_revision:
            raise JsonBWorkflowError("draft_conflict")
        target_version = connection.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM json_b_versions WHERE case_id = ?",
            (case_id,),
        ).fetchone()[0]
        document_id = str(uuid.uuid4()) if current is None else current["document_id"]
        draft_like = {
            "data_json": json.dumps(business, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            "document_id": document_id,
            "target_version": target_version,
        }
        preview = {
            "schema_version": "json-b/1.0",
            "document_type": "contractor_proposal",
            "document_id": document_id,
            "version": target_version,
            "generated_at": utc_now(),
            "case_id": case_id,
            "source_json_a": _source_object(source),
            **business,
            "computed_fee_totals": compute_fee_totals(business["fee_items"]),
        }
        _validate_document(preview, schema_path)
        next_revision = current_revision + 1
        now_text = utc_now()
        if current is None:
            connection.execute(
                "INSERT INTO json_b_drafts(case_id, contractor_id, document_id, target_version, "
                "base_review_artifact_id, source_artifact_id, source_document_id, "
                "source_version_no, source_schema_version, source_sha256, "
                "source_byte_length, data_json, content_revision, current_export_artifact_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)",
                (
                    case_id,
                    contractor_id,
                    document_id,
                    target_version,
                    case["current_json_b_artifact_id"],
                    source["artifact_id"],
                    source["document_id"],
                    source["version_no"],
                    source["schema_version"],
                    source["sha256"],
                    source["byte_length"],
                    draft_like["data_json"],
                    now_text,
                    now_text,
                ),
            )
        else:
            if current["current_export_artifact_id"] is not None:
                connection.execute(
                    "UPDATE json_b_private_exports SET state = 'INVALIDATED', invalidated_at = ? "
                    "WHERE artifact_id = ? AND state = 'ACTIVE'",
                    (now_text, current["current_export_artifact_id"]),
                )
                connection.execute(
                    "UPDATE artifacts SET lifecycle = 'invalidated' WHERE id = ? AND visibility = 'private'",
                    (current["current_export_artifact_id"],),
                )
            connection.execute(
                "UPDATE json_b_drafts SET target_version = ?, base_review_artifact_id = ?, "
                "source_artifact_id = ?, source_document_id = ?, "
                "source_version_no = ?, source_schema_version = ?, source_sha256 = ?, "
                "source_byte_length = ?, data_json = ?, content_revision = ?, "
                "current_export_artifact_id = NULL, updated_at = ? WHERE case_id = ? AND contractor_id = ?",
                (
                    target_version,
                    case["current_json_b_artifact_id"],
                    source["artifact_id"],
                    source["document_id"],
                    source["version_no"],
                    source["schema_version"],
                    source["sha256"],
                    source["byte_length"],
                    draft_like["data_json"],
                    next_revision,
                    now_text,
                    case_id,
                    contractor_id,
                ),
            )
        connection.execute(
            "UPDATE cases SET state = 'JSON_B_PRIVATE_DRAFT', updated_at = ? WHERE id = ?",
            (now_text, case_id),
        )
    return next_revision


def export_private_draft(
    connection: sqlite3.Connection,
    data_root: Path,
    schema_path: Path,
    case_id: str,
    contractor_id: str,
    expected_revision: int,
    document_assets: DocumentAssets | None = None,
) -> ExportResult:
    relative_path: str | None = None
    artifact_id = str(uuid.uuid4())
    try:
        with immediate_transaction(connection):
            _contractor_case(connection, case_id, contractor_id)
            draft = connection.execute(
                "SELECT * FROM json_b_drafts WHERE case_id = ? AND contractor_id = ?",
                (case_id, contractor_id),
            ).fetchone()
            if draft is None or draft["content_revision"] != expected_revision:
                raise JsonBWorkflowError("draft_conflict")
            if draft["current_export_artifact_id"] is not None:
                existing = connection.execute(
                    "SELECT e.artifact_id, a.relative_path, a.sha256, a.byte_length "
                    "FROM json_b_private_exports AS e JOIN artifacts AS a ON a.id = e.artifact_id "
                    "WHERE e.artifact_id = ? AND e.draft_revision = ? AND e.state = 'ACTIVE' "
                    "AND a.visibility = 'private' AND a.lifecycle = 'active'",
                    (draft["current_export_artifact_id"], expected_revision),
                ).fetchone()
                if existing is not None:
                    try:
                        stored = read_verified(data_root, existing["relative_path"], existing["sha256"])
                    except (ArtifactStoreError, OSError, ValueError) as error:
                        raise JsonBWorkflowError("export_unavailable") from error
                    if len(stored) != existing["byte_length"]:
                        raise JsonBWorkflowError("export_unavailable")
                    return ExportResult(existing["artifact_id"], expected_revision, False)
            source = _json_a_source(connection, case_id)
            _validate_source_file(data_root, source)
            for column, value in (
                ("source_artifact_id", source["artifact_id"]),
                ("source_document_id", source["document_id"]),
                ("source_version_no", source["version_no"]),
                ("source_schema_version", source["schema_version"]),
                ("source_sha256", source["sha256"]),
                ("source_byte_length", source["byte_length"]),
            ):
                if draft[column] != value:
                    raise JsonBWorkflowError("source_mismatch")
            document = _build_document(case_id, draft, source, utc_now())
            if document_assets is not None:
                ensure_supported_text(json.dumps(document, ensure_ascii=False), document_assets)
            _validate_document(document, schema_path)
            raw = encode_json_once(document)
            digest = sha256_bytes(raw)
            relative_path = install_json_artifact(data_root, case_id, artifact_id, raw)
            artifact_version = connection.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM artifacts "
                "WHERE case_id = ? AND kind = 'json_b_private_export'",
                (case_id,),
            ).fetchone()[0]
            now_text = utc_now()
            connection.execute(
                "INSERT INTO artifacts(id, case_id, author_id, kind, version_no, visibility, lifecycle, "
                "relative_path, sha256, byte_length, schema_version, created_at) "
                "VALUES (?, ?, ?, 'json_b_private_export', ?, 'private', 'active', ?, ?, ?, 'json-b/1.0', ?)",
                (
                    artifact_id,
                    case_id,
                    contractor_id,
                    artifact_version,
                    relative_path,
                    digest,
                    len(raw),
                    now_text,
                ),
            )
            connection.execute(
                "INSERT INTO json_b_private_exports(artifact_id, case_id, contractor_id, draft_revision, "
                "target_version, source_artifact_id, source_document_id, source_version_no, "
                "source_schema_version, source_sha256, source_byte_length, state, created_at, "
                "submitted_at, invalidated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, NULL, NULL)",
                (
                    artifact_id,
                    case_id,
                    contractor_id,
                    expected_revision,
                    draft["target_version"],
                    source["artifact_id"],
                    source["document_id"],
                    source["version_no"],
                    source["schema_version"],
                    source["sha256"],
                    source["byte_length"],
                    now_text,
                ),
            )
            connection.execute(
                "UPDATE json_b_drafts SET current_export_artifact_id = ?, updated_at = ? "
                "WHERE case_id = ? AND contractor_id = ? AND content_revision = ?",
                (artifact_id, now_text, case_id, contractor_id, expected_revision),
            )
        return ExportResult(artifact_id, expected_revision, True)
    except BaseException:
        if relative_path is not None:
            remove_new_artifact(data_root, relative_path)
        raise


def _source_matches_document(source: sqlite3.Row, document: dict[str, Any]) -> bool:
    return document.get("source_json_a") == _source_object(source)


def submit_private_export(
    connection: sqlite3.Connection,
    data_root: Path,
    schema_path: Path,
    case_id: str,
    contractor_id: str,
    artifact_id: str,
    uploaded_raw: bytes,
    *,
    now: datetime | None = None,
) -> ReviewResult:
    _contractor_case(connection, case_id, contractor_id)
    owned_export = connection.execute(
        "SELECT state FROM json_b_private_exports WHERE artifact_id = ? AND case_id = ? "
        "AND contractor_id = ?",
        (artifact_id, case_id, contractor_id),
    ).fetchone()
    if owned_export is None:
        raise JsonBWorkflowError("export_unavailable")
    parsed: dict[str, Any] | None = None
    if owned_export["state"] == "ACTIVE":
        try:
            candidate = decode_strict_json(uploaded_raw)
        except JsonContractError as error:
            raise JsonBWorkflowError("json_b_invalid") from error
        if not isinstance(candidate, dict):
            raise JsonBWorkflowError("json_b_invalid")
        _validate_document(candidate, schema_path)
        parsed = candidate
    with immediate_transaction(connection):
        case = _contractor_case(connection, case_id, contractor_id)
        export = connection.execute(
            "SELECT e.*, a.relative_path, a.sha256, a.byte_length, a.visibility, a.lifecycle "
            "FROM json_b_private_exports AS e JOIN artifacts AS a ON a.id = e.artifact_id "
            "WHERE e.artifact_id = ? AND e.case_id = ? AND e.contractor_id = ?",
            (artifact_id, case_id, contractor_id),
        ).fetchone()
        if export is None:
            raise JsonBWorkflowError("export_unavailable")
        if export["state"] == "SUBMITTED":
            review = connection.execute(
                "SELECT version_no FROM json_b_versions WHERE artifact_id = ? AND case_id = ?",
                (artifact_id, case_id),
            ).fetchone()
            if review is None:
                raise JsonBWorkflowError("export_unavailable")
            try:
                stored = read_verified(data_root, export["relative_path"], export["sha256"])
            except (ArtifactStoreError, OSError, ValueError) as error:
                raise JsonBWorkflowError("export_unavailable") from error
            if (
                len(uploaded_raw) != export["byte_length"]
                or sha256_bytes(uploaded_raw) != export["sha256"]
                or not hmac.compare_digest(stored, uploaded_raw)
            ):
                raise JsonBWorkflowError("exact_bytes_required")
            return ReviewResult(artifact_id, review["version_no"], False)
        if export["state"] != "ACTIVE" or export["visibility"] != "private" or export["lifecycle"] != "active":
            raise JsonBWorkflowError("export_unavailable")
        if parsed is None:
            try:
                candidate = decode_strict_json(uploaded_raw)
            except JsonContractError as error:
                raise JsonBWorkflowError("json_b_invalid") from error
            if not isinstance(candidate, dict):
                raise JsonBWorkflowError("json_b_invalid")
            parsed = candidate
        _validate_document(parsed, schema_path, now=now or datetime.now(timezone.utc))
        draft = connection.execute(
            "SELECT * FROM json_b_drafts WHERE case_id = ? AND contractor_id = ?",
            (case_id, contractor_id),
        ).fetchone()
        if (
            draft is None
            or draft["content_revision"] != export["draft_revision"]
            or draft["current_export_artifact_id"] != artifact_id
        ):
            raise JsonBWorkflowError("draft_conflict")
        try:
            stored = read_verified(data_root, export["relative_path"], export["sha256"])
        except (ArtifactStoreError, OSError, ValueError) as error:
            raise JsonBWorkflowError("export_unavailable") from error
        if len(uploaded_raw) != export["byte_length"] or sha256_bytes(uploaded_raw) != export["sha256"]:
            raise JsonBWorkflowError("exact_bytes_required")
        if not hmac.compare_digest(stored, uploaded_raw):
            raise JsonBWorkflowError("exact_bytes_required")
        source = _json_a_source(connection, case_id)
        _validate_source_file(data_root, source)
        if not _source_matches_document(source, parsed):
            raise JsonBWorkflowError("source_mismatch")
        if parsed.get("case_id") != case_id:
            raise JsonBWorkflowError("source_mismatch")
        if parsed.get("document_id") != draft["document_id"] or parsed.get("version") != export["target_version"]:
            raise JsonBWorkflowError("source_mismatch")
        now_text = utc_now()
        old = connection.execute(
            "SELECT artifact_id FROM json_b_versions WHERE case_id = ? AND state = 'CURRENT'",
            (case_id,),
        ).fetchone()
        if old is not None:
            connection.execute(
                "UPDATE json_b_versions SET state = 'SUPERSEDED', superseded_at = ? "
                "WHERE artifact_id = ? AND state = 'CURRENT'",
                (now_text, old["artifact_id"]),
            )
            connection.execute(
                "UPDATE artifacts SET lifecycle = 'superseded' WHERE id = ? AND lifecycle = 'active'",
                (old["artifact_id"],),
            )
        connection.execute(
            "UPDATE artifacts SET kind = 'json_b', version_no = ?, visibility = 'party_only' "
            "WHERE id = ? AND kind = 'json_b_private_export' AND visibility = 'private' AND lifecycle = 'active'",
            (export["target_version"], artifact_id),
        )
        connection.execute(
            "INSERT INTO json_b_versions(artifact_id, case_id, contractor_id, document_id, version_no, "
            "source_artifact_id, source_document_id, source_version_no, "
            "source_schema_version, source_sha256, source_byte_length, "
            "state, quote_expires_at, created_at, superseded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CURRENT', ?, ?, NULL)",
            (
                artifact_id,
                case_id,
                contractor_id,
                parsed["document_id"],
                parsed["version"],
                source["artifact_id"],
                source["document_id"],
                source["version_no"],
                source["schema_version"],
                source["sha256"],
                source["byte_length"],
                parsed["quote_expires_at"],
                now_text,
            ),
        )
        connection.execute(
            "UPDATE json_b_private_exports SET state = 'SUBMITTED', submitted_at = ? "
            "WHERE artifact_id = ? AND state = 'ACTIVE'",
            (now_text, artifact_id),
        )
        connection.execute(
            "UPDATE cases SET state = 'JSON_B_REVIEW', current_json_b_artifact_id = ?, updated_at = ? "
            "WHERE id = ? AND contractor_id = ?",
            (artifact_id, now_text, case_id, contractor_id),
        )
    return ReviewResult(artifact_id, int(parsed["version"]), True)


def load_private_draft(connection: sqlite3.Connection, case_id: str, contractor_id: str) -> sqlite3.Row | None:
    _contractor_case(connection, case_id, contractor_id)
    return connection.execute(
        "SELECT * FROM json_b_drafts WHERE case_id = ? AND contractor_id = ?",
        (case_id, contractor_id),
    ).fetchone()


def load_private_export(
    connection: sqlite3.Connection,
    case_id: str,
    contractor_id: str,
    artifact_id: str,
) -> sqlite3.Row:
    _contractor_case(connection, case_id, contractor_id)
    row = connection.execute(
        "SELECT a.*, e.state AS export_state, e.draft_revision FROM artifacts AS a "
        "JOIN json_b_private_exports AS e ON e.artifact_id = a.id "
        "WHERE a.id = ? AND a.case_id = ? AND e.contractor_id = ? AND e.state = 'ACTIVE' "
        "AND a.visibility = 'private' AND a.lifecycle = 'active'",
        (artifact_id, case_id, contractor_id),
    ).fetchone()
    if row is None:
        raise JsonBWorkflowError("export_unavailable")
    return row


def load_review(
    connection: sqlite3.Connection,
    case_id: str,
    actor_id: str,
    version_no: int,
) -> sqlite3.Row:
    actor = connection.execute(
        "SELECT status, system_role FROM users WHERE id = ?", (actor_id,)
    ).fetchone()
    if actor is None or actor["status"] != "active" or actor["system_role"] != "member":
        raise JsonBWorkflowError("review_unavailable")
    readable_states = (*ACTIVE_CASE_STATES, "CANDIDATE_REVIEW")
    placeholders = ",".join("?" for _ in readable_states)
    row = connection.execute(
        "SELECT v.*, a.relative_path, a.sha256, a.byte_length, a.lifecycle "
        "FROM json_b_versions AS v JOIN artifacts AS a ON a.id = v.artifact_id "
        "JOIN cases AS c ON c.id = v.case_id "
        f"WHERE v.case_id = ? AND v.version_no = ? AND c.state IN ({placeholders}) "
        "AND (c.owner_id = ? OR c.contractor_id = ?) AND a.visibility = 'party_only'",
        (case_id, version_no, *readable_states, actor_id, actor_id),
    ).fetchone()
    if row is None:
        raise JsonBWorkflowError("review_unavailable")
    return row


def read_review_bytes(data_root: Path, row: sqlite3.Row) -> tuple[dict[str, Any], bytes]:
    try:
        raw = read_verified(data_root, row["relative_path"], row["sha256"])
        document = decode_strict_json(raw)
    except (ArtifactStoreError, JsonContractError, OSError, ValueError) as error:
        raise JsonBWorkflowError("review_unavailable") from error
    if not isinstance(document, dict) or len(raw) != row["byte_length"]:
        raise JsonBWorkflowError("review_unavailable")
    return document, raw


def validate_objection_pointer(document: dict[str, Any], pointer: str) -> None:
    if not pointer.startswith("/") or len(pointer) > 500:
        raise JsonBWorkflowError("field_pointer_invalid")
    segments = pointer[1:].split("/")
    decoded: list[str] = []
    for segment in segments:
        if re.search(r"~(?![01])", segment):
            raise JsonBWorkflowError("field_pointer_invalid")
        decoded.append(segment.replace("~1", "/").replace("~0", "~"))
    if not decoded or decoded[0] not in OBJECTIONABLE_ROOTS:
        raise JsonBWorkflowError("field_pointer_invalid")
    current: object = document
    for segment in decoded:
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", segment) and int(segment) < len(current):
            current = current[int(segment)]
        else:
            raise JsonBWorkflowError("field_pointer_invalid")
