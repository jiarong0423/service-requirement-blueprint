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

from app.db import immediate_transaction
from app.services.artifact_store import (
    ArtifactStoreError,
    install_fixed_blob,
    quarantine_fixed_collection_directory,
    read_verified,
    remove_new_artifact,
)
from app.services.delivery_workflow import DeliveryWorkflowError, _event, _now, load_delivery_dashboard
from app.services.document_integrity import DocumentAssets


class EvidenceExportError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EvidenceExportResult:
    export_id: str
    version_no: int


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _plain_rows(rows) -> list[dict[str, object]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


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
            raw = read_verified(data_root, row["relative_path"], row["sha256"])
        except (ArtifactStoreError, OSError) as error:
            raise EvidenceExportError("formal_integrity_invalid") from error
        if len(raw) != row["byte_length"]:
            raise EvidenceExportError("formal_integrity_invalid")
        result[name] = raw
    return result


def _evidence_entries(connection: sqlite3.Connection, data_root: Path, view: dict[str, object]) -> dict[str, bytes]:
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
            fixed = read_verified(data_root, row["relative_path"], row["sha256"])
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
    entries["records/deliveries.json"] = _json_bytes(deliveries)
    inspections = []
    for detail in view["inspections"]:
        row = detail["row"]
        inspections.append({
            "id": row["id"], "delivery_id": row["delivery_id"], "round": row["round_no"],
            "kind": row["kind"], "decision": row["decision"],
            "explicit_acceptance": bool(row["explicit_acceptance"]),
            "inspected_by_id": row["inspected_by_id"], "inspected_by_role": "client", "inspected_at": row["inspected_at"],
            "results": _plain_rows(detail["results"]),
        })
    entries["records/inspections.json"] = _json_bytes(inspections)
    timeline = [row for row in view["events"] if row["event_code"] != "EVIDENCE_EXPORTED"]
    entries["records/timeline.json"] = _json_bytes(_plain_rows(timeline))
    dispute = view["dispute"]
    dispute_record = None if dispute is None else {
        "id": dispute["id"], "delivery_id": dispute["delivery_id"], "inspection_id": dispute["inspection_id"],
        "prior_state": dispute["prior_state"], "opened_by_id": dispute["opened_by_id"], "opened_by_role": dispute["opened_by_role"],
        "reason": dispute["reason"], "opened_at": dispute["opened_at"],
        "positions": _plain_rows(view["positions"]),
    }
    entries["records/dispute.json"] = _json_bytes(dispute_record)
    acceptance = connection.execute("SELECT * FROM acceptance_events WHERE case_id = ?", (formal["case"]["id"],)).fetchone()
    entries["records/acceptance.json"] = _json_bytes(None if acceptance is None else dict(acceptance))
    return entries


def prepare_evidence_export(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    actor_id: str,
    schema_path: Path,
) -> EvidenceExportResult:
    installed_path: str | None = None
    try:
        with immediate_transaction(connection):
            try:
                view = load_delivery_dashboard(connection, data_root, assets, case_id, actor_id)
            except DeliveryWorkflowError as error:
                raise EvidenceExportError("case_unavailable") from error
            if view["state"] not in {"ACCEPTED", "DISPUTED"}:
                raise EvidenceExportError("export_state_invalid")
            entries = _evidence_entries(connection, data_root, view)
            source_manifest = [_entry(name, raw) for name, raw in sorted(entries.items())]
            fingerprint = hashlib.sha256(_json_bytes(source_manifest)).hexdigest()
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
                "schema_version": "evidence-export/1.0",
                "export_id": export_id,
                "case_id": case_id,
                "formal_candidate_id": view["formal"]["candidate"]["id"],
                "workflow_state": view["state"],
                "version": version_no,
                "prepared_at": now,
                "source_fingerprint": fingerprint,
                "entries": source_manifest,
                "exclusions": [
                    "private requirement drafts", "private JSON B drafts and exports", "credentials and sessions",
                    "invitation secrets", "administrator data", "logs", "unrelated cases",
                ],
            }
            _validate_manifest(manifest, schema_path)
            entries["export-manifest.json"] = _json_bytes(manifest)
            raw = _write_zip(entries)
            digest = hashlib.sha256(raw).hexdigest()
            installed_path = install_fixed_blob(data_root, case_id, "exports", export_id, "evidence.zip", raw)
            connection.execute(
                "INSERT INTO evidence_exports(id, case_id, version_no, prepared_by_id, source_fingerprint, relative_path, sha256, byte_length, schema_version, prepared_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'evidence-export/1.0', ?)",
                (export_id, case_id, version_no, actor_id, fingerprint, installed_path, digest, len(raw), now),
            )
            _event(connection, case_id, "EVIDENCE_EXPORTED", "EXPORT", export_id, actor_id, view["role"], now)
            return EvidenceExportResult(export_id, version_no)
    except BaseException:
        if installed_path is not None:
            remove_new_artifact(data_root, installed_path)
        raise


def read_evidence_export(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    export_id: str,
    actor_id: str,
    schema_path: Path,
) -> tuple[sqlite3.Row, bytes]:
    try:
        load_delivery_dashboard(connection, data_root, assets, case_id, actor_id)
    except DeliveryWorkflowError as error:
        raise EvidenceExportError("case_unavailable") from error
    row = connection.execute(
        "SELECT * FROM evidence_exports WHERE id = ? AND case_id = ?", (export_id, case_id)
    ).fetchone()
    if row is None:
        raise EvidenceExportError("export_unavailable")
    if row["relative_path"] != f"cases/{case_id}/exports/{export_id}/evidence.zip":
        raise EvidenceExportError("export_integrity_invalid")
    try:
        raw = read_verified(data_root, row["relative_path"], row["sha256"])
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
    rows = connection.execute("SELECT * FROM evidence_exports").fetchall()
    referenced: set[tuple[str, str]] = set()
    for row in rows:
        expected_path = f"cases/{row['case_id']}/exports/{row['id']}/evidence.zip"
        if row["relative_path"] != expected_path:
            raise EvidenceExportError("export_integrity_invalid")
        try:
            raw = read_verified(data_root, row["relative_path"], row["sha256"])
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
