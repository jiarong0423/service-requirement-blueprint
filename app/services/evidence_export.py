from __future__ import annotations

import hashlib
import hmac
import io
import json
import sqlite3
import stat
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from app.db import deferred_read_transaction, immediate_transaction
from app.services.artifact_store import (
    ArtifactStoreError,
    MAX_DELIVERY_ARTIFACT_BYTES,
    MAX_EVIDENCE_EXPORT_BYTES,
    MAX_JSON_ARTIFACT_BYTES,
    install_fixed_blob,
    quarantine_fixed_collection_directory,
    read_verified,
)
from app.services.delivery_workflow import DeliveryWorkflowError, _event, _now, load_delivery_dashboard
from app.services.document_integrity import DocumentAssets
from app.services.formalization_workflow import FormalizationWorkflowError, revalidate_formal_snapshot


class EvidenceExportError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EvidenceExportResult:
    export_id: str
    version_no: int


INSPECTION_RESULT_FIELDS = (
    "id", "inspection_id", "delivery_item_id", "criterion_id", "result", "reason",
)
TIMELINE_FIELDS = (
    "id", "case_id", "event_code", "entity_type", "entity_id", "actor_id",
    "actor_role", "occurred_at", "effective_at",
)
DISPUTE_POSITION_FIELDS = (
    "id", "dispute_id", "actor_id", "actor_role", "position_text", "submitted_at",
)
ACCEPTANCE_FIELDS = (
    "id", "case_id", "formal_candidate_id", "delivery_id", "inspection_id",
    "accepted_by_id", "evidence_sha256", "accepted_at",
)
RECORD_SCHEMA_KEYS = {
    "records/deliveries.json": "deliveries",
    "records/inspections.json": "inspections",
    "records/timeline.json": "timeline",
    "records/dispute.json": "dispute",
    "records/acceptance.json": "acceptance",
}
REQUIRED_EVIDENCE_ENTRY_PATHS = frozenset({
    "formal/manifest.json",
    "formal/document.pdf",
    "formal/diagram.svg",
    "formal/json-a.json",
    "formal/json-b.json",
    *RECORD_SCHEMA_KEYS,
})


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _project_row(row: sqlite3.Row, fields: tuple[str, ...]) -> dict[str, object]:
    try:
        return {field: row[field] for field in fields}
    except (IndexError, KeyError) as error:
        raise EvidenceExportError("export_record_projection_invalid") from error


def _project_rows(rows, fields: tuple[str, ...]) -> list[dict[str, object]]:
    return [_project_row(row, fields) for row in rows]


def _entry(name: str, raw: bytes) -> dict[str, object]:
    return {
        "path": name,
        "byte_length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _write_zip(entries: dict[str, bytes]) -> bytes:
    if len(entries) > 32 or sum(len(value) for value in entries.values()) > 64 * 1024 * 1024:
        raise EvidenceExportError("export_limit_exceeded")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            if name.startswith("/") or ".." in name.split("/") or "\\" in name:
                raise EvidenceExportError("export_path_invalid")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100400 << 16
            archive.writestr(info, entries[name])
    return output.getvalue()


def _manifest_validator(schema_path: Path) -> Draft202012Validator:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if not isinstance(schema, dict):
            raise SchemaError("schema root must be an object")
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SchemaError) as error:
        raise EvidenceExportError("export_schema_invalid") from error
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_manifest(manifest: dict[str, object], schema_path: Path) -> None:
    errors = sorted(_manifest_validator(schema_path).iter_errors(manifest), key=lambda error: list(error.path))
    if errors:
        raise EvidenceExportError("export_manifest_invalid")


def _record_validators(manifest_schema_path: Path) -> dict[str, Draft202012Validator]:
    schema_path = manifest_schema_path.with_name("evidence-export-records.v1.schema.json")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        definitions = schema.get("$defs") if isinstance(schema, dict) else None
        if not isinstance(definitions, dict) or set(definitions) != set(RECORD_SCHEMA_KEYS.values()):
            raise SchemaError("record definitions mismatch")
        validators: dict[str, Draft202012Validator] = {}
        for key, definition in definitions.items():
            if not isinstance(definition, dict):
                raise SchemaError("record definition must be an object")
            Draft202012Validator.check_schema(definition)
            validators[key] = Draft202012Validator(definition, format_checker=FormatChecker())
        return validators
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SchemaError) as error:
        raise EvidenceExportError("export_record_schema_invalid") from error


def _validate_record_payloads(payloads: dict[str, object], manifest_schema_path: Path) -> None:
    if set(payloads) != set(RECORD_SCHEMA_KEYS):
        raise EvidenceExportError("export_record_set_invalid")
    validators = _record_validators(manifest_schema_path)
    for path, schema_key in RECORD_SCHEMA_KEYS.items():
        errors = sorted(
            validators[schema_key].iter_errors(payloads[path]),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            raise EvidenceExportError("export_record_invalid")


def _verify_zip(raw: bytes, row, schema_path: Path) -> dict[str, object]:
    if len(raw) > 64 * 1024 * 1024:
        raise EvidenceExportError("export_integrity_invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) > 33 or len(names) != len(set(names)) or "export-manifest.json" not in names:
                raise EvidenceExportError("export_integrity_invalid")
            if any(name.startswith("/") or ".." in name.split("/") or "\\" in name for name in names):
                raise EvidenceExportError("export_integrity_invalid")
            infos = {info.filename: info for info in archive.infolist()}
            if any(
                info.is_dir()
                or info.flag_bits & 0x1
                or not stat.S_ISREG(info.external_attr >> 16)
                for info in infos.values()
            ):
                raise EvidenceExportError("export_integrity_invalid")
            manifest_info = infos["export-manifest.json"]
            if manifest_info.file_size <= 0 or manifest_info.file_size > 1024 * 1024:
                raise EvidenceExportError("export_integrity_invalid")
            manifest = json.loads(archive.read("export-manifest.json").decode("utf-8"))
            if not isinstance(manifest, dict):
                raise EvidenceExportError("export_integrity_invalid")
            _validate_manifest(manifest, schema_path)
            listed = manifest["entries"]
            if listed != sorted(listed, key=lambda entry: entry["path"]):
                raise EvidenceExportError("export_integrity_invalid")
            expected_names = {entry["path"] for entry in listed} | {"export-manifest.json"}
            if {entry["path"] for entry in listed} != REQUIRED_EVIDENCE_ENTRY_PATHS:
                raise EvidenceExportError("export_integrity_invalid")
            if len(expected_names) != len(listed) + 1 or set(names) != expected_names:
                raise EvidenceExportError("export_integrity_invalid")
            if sum(entry["byte_length"] for entry in listed) > 64 * 1024 * 1024:
                raise EvidenceExportError("export_integrity_invalid")
            recomputed_fingerprint = hashlib.sha256(_json_bytes(listed)).hexdigest()
            if not hmac.compare_digest(recomputed_fingerprint, manifest["source_fingerprint"]):
                raise EvidenceExportError("export_integrity_invalid")
            for entry in listed:
                info = infos[entry["path"]]
                if info.file_size != entry["byte_length"]:
                    raise EvidenceExportError("export_integrity_invalid")
                content = archive.read(entry["path"])
                if len(content) != entry["byte_length"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    raise EvidenceExportError("export_integrity_invalid")
            record_payloads = {
                path: json.loads(archive.read(path).decode("utf-8"))
                for path in RECORD_SCHEMA_KEYS
            }
            try:
                _validate_record_payloads(record_payloads, schema_path)
            except EvidenceExportError as error:
                raise EvidenceExportError("export_integrity_invalid") from error
    except EvidenceExportError:
        raise
    except (zipfile.BadZipFile, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceExportError("export_integrity_invalid") from error
    if (manifest["export_id"] != row["id"] or manifest["case_id"] != row["case_id"]
            or manifest["version"] != row["version_no"]
            or not hmac.compare_digest(manifest["source_fingerprint"], row["source_fingerprint"])
            or manifest["schema_version"] != row["schema_version"]):
        raise EvidenceExportError("export_integrity_invalid")
    return manifest


def _formal_source_bytes(connection: sqlite3.Connection, data_root: Path, candidate) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for name, artifact_id in (
        ("formal/json-a.json", candidate["json_a_artifact_id"]),
        ("formal/json-b.json", candidate["json_b_artifact_id"]),
    ):
        row = connection.execute(
            "SELECT relative_path, sha256, byte_length FROM artifacts WHERE id = ? AND lifecycle = 'formal'",
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise EvidenceExportError("formal_integrity_invalid")
        try:
            raw = read_verified(
                data_root, row["relative_path"], row["sha256"], row["byte_length"],
                max_bytes=MAX_JSON_ARTIFACT_BYTES,
            )
        except (ArtifactStoreError, OSError) as error:
            raise EvidenceExportError("formal_integrity_invalid") from error
        if len(raw) != row["byte_length"]:
            raise EvidenceExportError("formal_integrity_invalid")
        result[name] = raw
    return result


def _evidence_entries(
    connection: sqlite3.Connection,
    data_root: Path,
    view: dict[str, object],
    manifest_schema_path: Path,
) -> dict[str, bytes]:
    formal = view["formal"]
    entries = {
        "formal/manifest.json": formal["files"]["manifest.json"],
        "formal/document.pdf": formal["files"]["document.pdf"],
        "formal/diagram.svg": formal["files"]["diagram.svg"],
    }
    entries.update(_formal_source_bytes(connection, data_root, formal["candidate"]))
    deliveries: list[dict[str, object]] = []
    for detail in view["deliveries"]:
        row = detail["row"]
        try:
            fixed = read_verified(
                data_root, row["relative_path"], row["sha256"], row["byte_length"],
                max_bytes=MAX_DELIVERY_ARTIFACT_BYTES,
            )
        except (ArtifactStoreError, OSError) as error:
            raise EvidenceExportError("delivery_integrity_invalid") from error
        if len(fixed) != row["byte_length"]:
            raise EvidenceExportError("delivery_integrity_invalid")
        try:
            fixed_record = json.loads(fixed.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvidenceExportError("delivery_integrity_invalid") from error
        deliveries.append({
            "id": row["id"], "version": row["version_no"], "kind": row["kind"],
            "predecessor_delivery_id": row["predecessor_delivery_id"],
            "remediated_inspection_id": row["remediated_inspection_id"],
            "sha256": row["sha256"], "byte_length": row["byte_length"],
            "submitted_by_id": row["submitted_by_id"], "submitted_by_role": "contractor",
            "submitted_at": row["submitted_at"], "review_due_at": row["review_due_at"],
            "fixed_record": fixed_record,
        })
    inspections = []
    for detail in view["inspections"]:
        row = detail["row"]
        inspections.append({
            "id": row["id"], "delivery_id": row["delivery_id"], "round": row["round_no"],
            "kind": row["kind"], "decision": row["decision"],
            "explicit_acceptance": bool(row["explicit_acceptance"]),
            "inspected_by_id": row["inspected_by_id"], "inspected_by_role": "client", "inspected_at": row["inspected_at"],
            "results": _project_rows(detail["results"], INSPECTION_RESULT_FIELDS),
        })
    timeline = [row for row in view["events"] if row["event_code"] != "EVIDENCE_EXPORTED"]
    dispute = view["dispute"]
    dispute_record = None if dispute is None else {
        "id": dispute["id"], "delivery_id": dispute["delivery_id"], "inspection_id": dispute["inspection_id"],
        "prior_state": dispute["prior_state"], "opened_by_id": dispute["opened_by_id"], "opened_by_role": dispute["opened_by_role"],
        "reason": dispute["reason"], "opened_at": dispute["opened_at"],
        "positions": _project_rows(view["positions"], DISPUTE_POSITION_FIELDS),
    }
    acceptance = connection.execute(
        "SELECT id, case_id, formal_candidate_id, delivery_id, inspection_id, accepted_by_id, "
        "evidence_sha256, accepted_at FROM acceptance_events WHERE case_id = ?",
        (formal["case"]["id"],),
    ).fetchone()
    record_payloads: dict[str, object] = {
        "records/deliveries.json": deliveries,
        "records/inspections.json": inspections,
        "records/timeline.json": _project_rows(timeline, TIMELINE_FIELDS),
        "records/dispute.json": dispute_record,
        "records/acceptance.json": None if acceptance is None else _project_row(acceptance, ACCEPTANCE_FIELDS),
    }
    _validate_record_payloads(record_payloads, manifest_schema_path)
    entries.update({path: _json_bytes(payload) for path, payload in record_payloads.items()})
    return entries


def _fingerprint_for_view(
    connection: sqlite3.Connection,
    data_root: Path,
    view: dict[str, object],
    manifest_schema_path: Path,
) -> tuple[dict[str, bytes], list[dict[str, object]], str]:
    entries = _evidence_entries(connection, data_root, view, manifest_schema_path)
    source_manifest = [_entry(name, raw) for name, raw in sorted(entries.items())]
    fingerprint = hashlib.sha256(_json_bytes(source_manifest)).hexdigest()
    return entries, source_manifest, fingerprint


def load_delivery_dashboard_with_export_status(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    actor_id: str,
    schema_path: Path,
) -> dict[str, object]:
    """Load the dashboard and mark exports against one current read snapshot."""
    with deferred_read_transaction(connection):
        view = load_delivery_dashboard(connection, data_root, assets, case_id, actor_id)
        current_fingerprint: str | None = None
        if view["exports"]:
            if view["state"] not in {"ACCEPTED", "DISPUTED"}:
                raise EvidenceExportError("export_integrity_invalid")
            _entries, _source_manifest, current_fingerprint = _fingerprint_for_view(
                connection, data_root, view, schema_path
            )
        view["exports"] = [
            {
                **dict(row),
                "is_current": current_fingerprint is not None
                and hmac.compare_digest(row["source_fingerprint"], current_fingerprint),
            }
            for row in view["exports"]
        ]
        return view


def _compact_evidence_cas_vector(
    connection: sqlite3.Connection,
    case_id: str,
    actor_id: str,
    formal_snapshot: dict[str, object],
) -> tuple[object, ...]:
    try:
        formal = revalidate_formal_snapshot(connection, case_id, actor_id, formal_snapshot)
    except FormalizationWorkflowError as error:
        raise EvidenceExportError("case_unavailable") from error
    workflow = connection.execute(
        "SELECT state, current_delivery_id, current_inspection_id, active_dispute_id, revision, updated_at "
        "FROM delivery_workflows WHERE case_id = ?", (case_id,)
    ).fetchone()
    workflow_token = None if workflow is None else tuple(workflow)
    count_queries = (
        ("SELECT COUNT(*) FROM delivery_versions WHERE case_id = ?", (case_id,)),
        (
            "SELECT COUNT(*) FROM delivery_items AS item JOIN delivery_versions AS delivery "
            "ON delivery.id = item.delivery_id WHERE delivery.case_id = ?",
            (case_id,),
        ),
        (
            "SELECT COUNT(*) FROM delivery_test_results AS test JOIN delivery_versions AS delivery "
            "ON delivery.id = test.delivery_id WHERE delivery.case_id = ?",
            (case_id,),
        ),
        ("SELECT COUNT(*) FROM inspection_rounds WHERE case_id = ?", (case_id,)),
        (
            "SELECT COUNT(*) FROM inspection_item_results AS result JOIN inspection_rounds AS inspection "
            "ON inspection.id = result.inspection_id WHERE inspection.case_id = ?",
            (case_id,),
        ),
        ("SELECT COUNT(*) FROM disputes WHERE case_id = ?", (case_id,)),
        (
            "SELECT COUNT(*) FROM dispute_positions AS position JOIN disputes AS dispute "
            "ON dispute.id = position.dispute_id WHERE dispute.case_id = ?",
            (case_id,),
        ),
        (
            "SELECT COUNT(*) FROM case_timeline_events WHERE case_id = ? AND event_code <> 'EVIDENCE_EXPORTED'",
            (case_id,),
        ),
        ("SELECT COUNT(*) FROM acceptance_events WHERE case_id = ?", (case_id,)),
    )
    counts = tuple(connection.execute(sql, parameters).fetchone()[0] for sql, parameters in count_queries)
    return (
        formal["case"]["state"],
        formal["case"]["formal_candidate_id"],
        formal["candidate"]["id"],
        workflow_token,
        *counts,
    )


def _quarantine_provisional_export(data_root: Path, case_id: str, export_id: str) -> None:
    try:
        quarantine_fixed_collection_directory(data_root, case_id, "exports", export_id)
    except (ArtifactStoreError, OSError) as error:
        raise EvidenceExportError("export_quarantine_failed") from error


def prepare_evidence_export(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    actor_id: str,
    schema_path: Path,
) -> EvidenceExportResult:
    with deferred_read_transaction(connection):
        try:
            view = load_delivery_dashboard(connection, data_root, assets, case_id, actor_id)
        except DeliveryWorkflowError as error:
            raise EvidenceExportError("case_unavailable") from error
        if view["state"] not in {"ACCEPTED", "DISPUTED"}:
            raise EvidenceExportError("export_state_invalid")
        entries, source_manifest, fingerprint = _fingerprint_for_view(
            connection, data_root, view, schema_path
        )
        cas_vector = _compact_evidence_cas_vector(
            connection, case_id, actor_id, view["formal"]
        )
        existing = connection.execute(
            "SELECT id, version_no FROM evidence_exports WHERE case_id = ? AND source_fingerprint = ?",
            (case_id, fingerprint),
        ).fetchone()
        if existing is not None:
            return EvidenceExportResult(existing["id"], existing["version_no"])
        export_id = str(uuid.uuid4())
        version_no = connection.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM evidence_exports WHERE case_id = ?", (case_id,)
        ).fetchone()[0]
        now = _now()
        manifest = {
            "schema_version": "evidence-export/1.0", "export_id": export_id,
            "case_id": case_id, "formal_candidate_id": view["formal"]["candidate"]["id"],
            "workflow_state": view["state"], "version": version_no, "prepared_at": now,
            "source_fingerprint": fingerprint, "entries": source_manifest,
            "exclusions": [
                "private requirement drafts", "private JSON B drafts and exports", "credentials and sessions",
                "invitation secrets", "administrator data", "logs", "unrelated cases",
            ],
        }
        _validate_manifest(manifest, schema_path)
    entries["export-manifest.json"] = _json_bytes(manifest)
    raw = _write_zip(entries)
    digest = hashlib.sha256(raw).hexdigest()
    install_fixed_blob(data_root, case_id, "exports", export_id, "evidence.zip", raw)
    created = False
    try:
        with immediate_transaction(connection):
            current_cas_vector = _compact_evidence_cas_vector(
                connection, case_id, actor_id, view["formal"]
            )
            if current_cas_vector != cas_vector:
                raise EvidenceExportError("export_source_changed")
            existing = connection.execute(
                "SELECT id, version_no FROM evidence_exports WHERE case_id = ? AND source_fingerprint = ?",
                (case_id, fingerprint),
            ).fetchone()
            if existing is not None:
                result = EvidenceExportResult(existing["id"], existing["version_no"])
            else:
                locked_version = connection.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM evidence_exports WHERE case_id = ?", (case_id,)
                ).fetchone()[0]
                if locked_version != version_no:
                    raise EvidenceExportError("export_source_changed")
                installed_path = f"cases/{case_id}/exports/{export_id}/evidence.zip"
                connection.execute(
                    "INSERT INTO evidence_exports(id, case_id, version_no, prepared_by_id, source_fingerprint, relative_path, sha256, byte_length, schema_version, prepared_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'evidence-export/1.0', ?)",
                    (export_id, case_id, version_no, actor_id, fingerprint, installed_path, digest, len(raw), now),
                )
                _event(connection, case_id, "EVIDENCE_EXPORTED", "EXPORT", export_id, actor_id, view["role"], now)
                result = EvidenceExportResult(export_id, version_no)
                created = True
    except BaseException:
        _quarantine_provisional_export(data_root, case_id, export_id)
        raise
    if not created:
        _quarantine_provisional_export(data_root, case_id, export_id)
    return result


def read_evidence_export(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    export_id: str,
    actor_id: str,
    schema_path: Path,
) -> tuple[sqlite3.Row, bytes]:
    with deferred_read_transaction(connection):
        try:
            view = load_delivery_dashboard(connection, data_root, assets, case_id, actor_id)
        except DeliveryWorkflowError as error:
            raise EvidenceExportError("case_unavailable") from error
        if view["state"] not in {"ACCEPTED", "DISPUTED"}:
            raise EvidenceExportError("export_state_invalid")
        row = connection.execute(
            "SELECT * FROM evidence_exports WHERE id = ? AND case_id = ?", (export_id, case_id)
        ).fetchone()
        if row is None:
            raise EvidenceExportError("export_unavailable")
        _entries, _source_manifest, current_fingerprint = _fingerprint_for_view(
            connection, data_root, view, schema_path
        )
        if not hmac.compare_digest(row["source_fingerprint"], current_fingerprint):
            raise EvidenceExportError("export_superseded")
        if row["relative_path"] != f"cases/{case_id}/exports/{export_id}/evidence.zip":
            raise EvidenceExportError("export_integrity_invalid")
        try:
            raw = read_verified(
                data_root, row["relative_path"], row["sha256"], row["byte_length"],
                max_bytes=MAX_EVIDENCE_EXPORT_BYTES,
            )
        except (ArtifactStoreError, OSError) as error:
            raise EvidenceExportError("export_integrity_invalid") from error
        if len(raw) != row["byte_length"]:
            raise EvidenceExportError("export_integrity_invalid")
        manifest = _verify_zip(raw, row, schema_path)
        case = connection.execute("SELECT formal_candidate_id FROM cases WHERE id = ?", (case_id,)).fetchone()
        if case is None or manifest["formal_candidate_id"] != case["formal_candidate_id"]:
            raise EvidenceExportError("export_integrity_invalid")
        return row, raw


def reconcile_evidence_export_storage(connection: sqlite3.Connection, data_root: Path, schema_path: Path) -> None:
    _manifest_validator(schema_path)
    _record_validators(schema_path)
    rows = connection.execute("SELECT * FROM evidence_exports").fetchall()
    referenced: set[tuple[str, str]] = set()
    for row in rows:
        expected_path = f"cases/{row['case_id']}/exports/{row['id']}/evidence.zip"
        if row["relative_path"] != expected_path:
            raise EvidenceExportError("export_integrity_invalid")
        try:
            raw = read_verified(
                data_root, row["relative_path"], row["sha256"], row["byte_length"],
                max_bytes=MAX_EVIDENCE_EXPORT_BYTES,
            )
        except (ArtifactStoreError, OSError) as error:
            raise EvidenceExportError("export_integrity_invalid") from error
        if len(raw) != row["byte_length"]:
            raise EvidenceExportError("export_integrity_invalid")
        manifest = _verify_zip(raw, row, schema_path)
        case = connection.execute("SELECT formal_candidate_id FROM cases WHERE id = ?", (row["case_id"],)).fetchone()
        if case is None or manifest["formal_candidate_id"] != case["formal_candidate_id"]:
            raise EvidenceExportError("export_integrity_invalid")
        referenced.add((row["case_id"], row["id"]))
    cases_root = data_root / "cases"
    if not cases_root.exists():
        return
    for case_dir in cases_root.iterdir():
        export_root = case_dir / "exports"
        if not export_root.is_dir() or export_root.is_symlink():
            continue
        for entry in list(export_root.iterdir()):
            if entry.is_dir() and not entry.is_symlink() and (case_dir.name, entry.name) not in referenced:
                quarantine_fixed_collection_directory(data_root, case_dir.name, "exports", entry.name)
