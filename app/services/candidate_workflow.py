from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from app.db import deferred_read_transaction, immediate_transaction
from app.services.artifact_store import (
    ArtifactStoreError,
    MAX_CANDIDATE_BINARY_BYTES,
    MAX_CANDIDATE_MANIFEST_BYTES,
    MAX_JSON_ARTIFACT_BYTES,
    _ensure_private_directory,
    _fsync_directory,
    _rename_directory_no_replace,
    install_candidate_bundle,
    quarantine_candidate_bundle,
    read_verified,
    verify_candidate_bundle,
)
from app.services.candidate_documents import (
    CandidateBundle,
    CandidateSnapshot,
    SourceSnapshot,
    TEMPLATE_VERSION,
    build_candidate_bundle,
)
from app.services.document_integrity import DocumentAssets, DocumentIntegrityError
from app.services.json_contracts import (
    JsonContractError,
    decode_strict_json,
    validate_json_a,
    validate_json_b,
)


MANIFEST_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas/candidate-manifest.v1.schema.json"


class CandidateWorkflowError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CandidateResult:
    candidate_id: str
    version: int
    created: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CandidateWorkflowError("quote_expiry_invalid") from error
    if parsed.tzinfo is None:
        raise CandidateWorkflowError("quote_expiry_invalid")
    return parsed.astimezone(timezone.utc)


def _read_document(
    data_root: Path,
    row: sqlite3.Row,
    schema_path: Path,
    validator,
) -> tuple[dict[str, object], bytes]:
    try:
        raw = read_verified(
            data_root,
            row["relative_path"],
            row["sha256"],
            row["byte_length"],
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
        )
        document = decode_strict_json(raw)
        if not isinstance(document, dict):
            raise JsonContractError("schema_validation_failed")
        validator(document, schema_path)
    except (ArtifactStoreError, JsonContractError, OSError, ValueError) as error:
        raise CandidateWorkflowError("source_unavailable") from error
    return document, raw


def _current_source_rows(
    connection: sqlite3.Connection,
    case_id: str,
    actor_id: str,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    actor = connection.execute(
        "SELECT status, system_role FROM users WHERE id = ?", (actor_id,)
    ).fetchone()
    if actor is None or actor["status"] != "active" or actor["system_role"] != "member":
        raise CandidateWorkflowError("case_unavailable")
    case = connection.execute(
        "SELECT * FROM cases WHERE id = ? AND contractor_id = ? AND owner_id <> contractor_id "
        "AND state = 'JSON_B_REVIEW'",
        (case_id, actor_id),
    ).fetchone()
    if case is None or not case["current_json_a_artifact_id"] or not case["current_json_b_artifact_id"]:
        raise CandidateWorkflowError("case_unavailable")
    owner = connection.execute(
        "SELECT status, system_role FROM users WHERE id = ?", (case["owner_id"],)
    ).fetchone()
    if owner is None or owner["status"] != "active" or owner["system_role"] != "member":
        raise CandidateWorkflowError("case_unavailable")
    blocking = connection.execute(
        "SELECT 1 FROM case_issues WHERE case_id = ? AND state NOT IN ('RESOLVED', 'OUT_OF_SCOPE') LIMIT 1",
        (case_id,),
    ).fetchone()
    if blocking is not None:
        raise CandidateWorkflowError("blocking_issue")

    json_a_row = connection.execute(
        "SELECT a.*, v.document_id FROM artifacts AS a "
        "JOIN json_a_source_verifications AS v ON v.case_id = a.case_id AND v.artifact_id = a.id "
        "AND v.actor_role = 'contractor' "
        "WHERE a.id = ? AND a.case_id = ? AND a.kind = 'json_a' "
        "AND a.visibility = 'party_only' AND a.lifecycle = 'active'",
        (case["current_json_a_artifact_id"], case_id),
    ).fetchone()
    json_b_row = connection.execute(
        "SELECT a.*, v.document_id, v.source_artifact_id, v.source_document_id, "
        "v.source_version_no, v.source_schema_version, v.source_sha256, v.source_byte_length, "
        "v.quote_expires_at, v.state AS version_state FROM artifacts AS a "
        "JOIN json_b_versions AS v ON v.artifact_id = a.id "
        "WHERE a.id = ? AND a.case_id = ? AND a.kind = 'json_b' "
        "AND a.visibility = 'party_only' AND a.lifecycle = 'active' AND v.state = 'CURRENT'",
        (case["current_json_b_artifact_id"], case_id),
    ).fetchone()
    if json_a_row is None or json_b_row is None:
        raise CandidateWorkflowError("source_unavailable")
    if (
        json_b_row["source_artifact_id"] != json_a_row["id"]
        or json_b_row["source_document_id"] != json_a_row["document_id"]
        or json_b_row["source_version_no"] != json_a_row["version_no"]
        or json_b_row["source_schema_version"] != json_a_row["schema_version"]
        or not hmac.compare_digest(json_b_row["source_sha256"], json_a_row["sha256"])
        or json_b_row["source_byte_length"] != json_a_row["byte_length"]
    ):
        raise CandidateWorkflowError("source_mismatch")
    if _parse_time(json_b_row["quote_expires_at"]) <= datetime.now(timezone.utc):
        raise CandidateWorkflowError("quote_expired")
    return case, json_a_row, json_b_row


def _current_sources(
    connection: sqlite3.Connection,
    data_root: Path,
    json_a_schema: Path,
    json_b_schema: Path,
    case_id: str,
    actor_id: str,
) -> tuple[sqlite3.Row, SourceSnapshot, SourceSnapshot]:
    case, json_a_row, json_b_row = _current_source_rows(connection, case_id, actor_id)

    json_a_document, json_a_raw = _read_document(data_root, json_a_row, json_a_schema, validate_json_a)
    json_b_document, json_b_raw = _read_document(data_root, json_b_row, json_b_schema, validate_json_b)
    expected_source = {
        "artifact_id": json_a_row["id"],
        "document_id": json_a_row["document_id"],
        "version": json_a_row["version_no"],
        "schema_version": json_a_row["schema_version"],
        "sha256": json_a_row["sha256"],
        "byte_length": json_a_row["byte_length"],
    }
    if json_b_document.get("case_id") != case_id or json_b_document.get("source_json_a") != expected_source:
        raise CandidateWorkflowError("source_mismatch")
    return (
        case,
        SourceSnapshot(
            artifact_id=json_a_row["id"], document_id=json_a_row["document_id"], version=json_a_row["version_no"],
            schema_version=json_a_row["schema_version"], sha256=json_a_row["sha256"], byte_length=json_a_row["byte_length"],
            raw=json_a_raw, document=json_a_document,
        ),
        SourceSnapshot(
            artifact_id=json_b_row["id"], document_id=json_b_row["document_id"], version=json_b_row["version_no"],
            schema_version=json_b_row["schema_version"], sha256=json_b_row["sha256"], byte_length=json_b_row["byte_length"],
            raw=json_b_raw, document=json_b_document,
        ),
    )


def _assert_current_sources_unchanged(
    connection: sqlite3.Connection,
    case_id: str,
    actor_id: str,
    expected_case: sqlite3.Row,
    expected_json_a: SourceSnapshot,
    expected_json_b: SourceSnapshot,
) -> None:
    current_case, current_a, current_b = _current_source_rows(connection, case_id, actor_id)
    if (
        current_case["content_revision"] != expected_case["content_revision"]
        or current_case["current_json_a_artifact_id"] != expected_json_a.artifact_id
        or current_case["current_json_b_artifact_id"] != expected_json_b.artifact_id
        or (current_a["id"], current_a["document_id"], current_a["version_no"], current_a["schema_version"], current_a["sha256"], current_a["byte_length"])
        != (expected_json_a.artifact_id, expected_json_a.document_id, expected_json_a.version, expected_json_a.schema_version, expected_json_a.sha256, expected_json_a.byte_length)
        or (current_b["id"], current_b["document_id"], current_b["version_no"], current_b["schema_version"], current_b["sha256"], current_b["byte_length"])
        != (expected_json_b.artifact_id, expected_json_b.document_id, expected_json_b.version, expected_json_b.schema_version, expected_json_b.sha256, expected_json_b.byte_length)
    ):
        raise CandidateWorkflowError("candidate_source_changed")


def _validate_manifest(raw: bytes, schema_path: Path) -> dict[str, object]:
    try:
        document = json.loads(raw.decode("utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as error:
        raise CandidateWorkflowError("manifest_invalid") from error
    if not isinstance(document, dict):
        raise CandidateWorkflowError("manifest_invalid")
    return document


def _asset_identity(assets: DocumentAssets) -> tuple[str, str]:
    return assets.catalog_record.sha256, assets.label_record.sha256


def _file_contract(raw: bytes) -> tuple[str, int]:
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _expected_asset_record(record) -> dict[str, object]:
    return {
        "filename": record.filename,
        "media_type": record.media_type,
        "sha256": record.sha256,
        "byte_length": record.byte_length,
    }


def _verify_rendered_manifest(
    manifest: dict[str, object],
    snapshot: CandidateSnapshot,
    assets: DocumentAssets,
    bundle: CandidateBundle,
) -> None:
    expected_sources = {
        "json_a": {
            "artifact_id": snapshot.json_a.artifact_id,
            "document_id": snapshot.json_a.document_id,
            "version": snapshot.json_a.version,
            "schema_version": snapshot.json_a.schema_version,
            "sha256": snapshot.json_a.sha256,
            "byte_length": snapshot.json_a.byte_length,
        },
        "json_b": {
            "artifact_id": snapshot.json_b.artifact_id,
            "document_id": snapshot.json_b.document_id,
            "version": snapshot.json_b.version,
            "schema_version": snapshot.json_b.schema_version,
            "sha256": snapshot.json_b.sha256,
            "byte_length": snapshot.json_b.byte_length,
        },
    }
    expected_catalog = {"locale": "en", **_expected_asset_record(assets.catalog_record)}
    expected_labels = {
        "schema_version": "candidate-label-map/1.0",
        **_expected_asset_record(assets.label_record),
    }
    expected_fonts = {
        key: _expected_asset_record(record) for key, record in assets.font_records.items()
    }
    expected_artifacts = {
        "pdf": {
            "filename": "document.pdf", "media_type": "application/pdf",
            "sha256": _file_contract(bundle.pdf)[0], "byte_length": len(bundle.pdf),
        },
        "svg": {
            "filename": "diagram.svg", "media_type": "image/svg+xml",
            "sha256": _file_contract(bundle.svg)[0], "byte_length": len(bundle.svg),
        },
    }
    if (
        manifest.get("candidate_id") != snapshot.candidate_id
        or manifest.get("case_id") != snapshot.case_id
        or manifest.get("version") != snapshot.version
        or manifest.get("created_at") != snapshot.created_at
        or manifest.get("template_version") != TEMPLATE_VERSION
        or manifest.get("sources") != expected_sources
        or manifest.get("catalog") != expected_catalog
        or manifest.get("label_map") != expected_labels
        or manifest.get("fonts") != expected_fonts
        or manifest.get("artifacts") != expected_artifacts
    ):
        raise CandidateWorkflowError("manifest_binding_invalid")


def _existing_current(
    connection: sqlite3.Connection,
    case_id: str,
    json_a: SourceSnapshot,
    json_b: SourceSnapshot,
    assets: DocumentAssets,
) -> sqlite3.Row | None:
    catalog_digest, label_digest = _asset_identity(assets)
    return connection.execute(
        "SELECT * FROM candidate_versions WHERE case_id = ? AND state = 'CURRENT' "
        "AND json_a_artifact_id = ? AND json_a_sha256 = ? AND json_a_byte_length = ? "
        "AND json_b_artifact_id = ? AND json_b_sha256 = ? AND json_b_byte_length = ? "
        "AND template_version = ? AND catalog_sha256 = ? AND label_map_sha256 = ?",
        (
            case_id, json_a.artifact_id, json_a.sha256, json_a.byte_length,
            json_b.artifact_id, json_b.sha256, json_b.byte_length,
            TEMPLATE_VERSION, catalog_digest, label_digest,
        ),
    ).fetchone()


def _active_candidate_for_contractor(
    connection: sqlite3.Connection,
    case_id: str,
    actor_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT cv.* FROM cases AS c JOIN candidate_versions AS cv ON cv.id = c.active_candidate_id "
        "JOIN users AS contractor ON contractor.id = c.contractor_id "
        "JOIN users AS owner ON owner.id = c.owner_id "
        "WHERE c.id = ? AND c.contractor_id = ? AND c.state = 'CANDIDATE_REVIEW' "
        "AND cv.state = 'CURRENT' AND contractor.status = 'active' AND contractor.system_role = 'member' "
        "AND owner.status = 'active' AND owner.system_role = 'member'",
        (case_id, actor_id),
    ).fetchone()


def create_candidate(
    connection: sqlite3.Connection,
    data_root: Path,
    json_a_schema: Path,
    json_b_schema: Path,
    manifest_schema: Path,
    assets: DocumentAssets,
    case_id: str,
    actor_id: str,
) -> CandidateResult:
    already_active = _active_candidate_for_contractor(connection, case_id, actor_id)
    if already_active is not None:
        _load_verified_bundle(connection, data_root, case_id, already_active["id"], assets=assets)
        return CandidateResult(already_active["id"], already_active["version_no"], False)
    with deferred_read_transaction(connection):
        case, json_a, json_b = _current_sources(
            connection, data_root, json_a_schema, json_b_schema, case_id, actor_id
        )
    existing = _existing_current(connection, case_id, json_a, json_b, assets)
    if existing is not None:
        _load_verified_bundle(connection, data_root, case_id, existing["id"], assets=assets)
        return CandidateResult(existing["id"], existing["version_no"], False)
    version = connection.execute(
        "SELECT COALESCE(MAX(version_no), 0) + 1 FROM candidate_versions WHERE case_id = ?", (case_id,)
    ).fetchone()[0]
    candidate_id = str(uuid.uuid4())
    snapshot = CandidateSnapshot(
        candidate_id=candidate_id,
        case_id=case_id,
        version=version,
        created_at=_now(),
        json_a=json_a,
        json_b=json_b,
    )
    try:
        bundle = build_candidate_bundle(snapshot, assets)
        manifest = _validate_manifest(bundle.manifest, manifest_schema)
        _verify_rendered_manifest(manifest, snapshot, assets, bundle)
        relative_directory = install_candidate_bundle(data_root, case_id, candidate_id, bundle.files)
    except (CandidateWorkflowError, DocumentIntegrityError, ArtifactStoreError, OSError) as error:
        raise CandidateWorkflowError("candidate_build_failed") from error

    artifact_ids = {name: str(uuid.uuid4()) for name in ("manifest", "pdf", "svg")}
    artifact_contracts = {
        "manifest": (hashlib.sha256(bundle.manifest).hexdigest(), len(bundle.manifest)),
        "pdf": (hashlib.sha256(bundle.pdf).hexdigest(), len(bundle.pdf)),
        "svg": (hashlib.sha256(bundle.svg).hexdigest(), len(bundle.svg)),
    }
    created = False
    try:
        with immediate_transaction(connection):
            concurrent = _active_candidate_for_contractor(connection, case_id, actor_id)
            if concurrent is not None:
                if (
                    concurrent["json_a_artifact_id"] != json_a.artifact_id
                    or concurrent["json_a_sha256"] != json_a.sha256
                    or concurrent["json_b_artifact_id"] != json_b.artifact_id
                    or concurrent["json_b_sha256"] != json_b.sha256
                    or concurrent["template_version"] != TEMPLATE_VERSION
                    or concurrent["catalog_sha256"] != assets.catalog_record.sha256
                    or concurrent["label_map_sha256"] != assets.label_record.sha256
                ):
                    raise CandidateWorkflowError("candidate_source_changed")
                result = CandidateResult(concurrent["id"], concurrent["version_no"], False)
            else:
                _assert_current_sources_unchanged(
                    connection, case_id, actor_id, case, json_a, json_b
                )
                now = snapshot.created_at
                rows = (
                    (artifact_ids["manifest"], "candidate_manifest", "manifest.json", "candidate-manifest/1.0", artifact_contracts["manifest"]),
                    (artifact_ids["pdf"], "candidate_pdf", "document.pdf", TEMPLATE_VERSION, artifact_contracts["pdf"]),
                    (artifact_ids["svg"], "candidate_svg", "diagram.svg", TEMPLATE_VERSION, artifact_contracts["svg"]),
                )
                for artifact_id, kind, filename, schema_version, contract in rows:
                    connection.execute(
                        "INSERT INTO artifacts(id, case_id, author_id, kind, version_no, visibility, lifecycle, "
                        "relative_path, sha256, byte_length, schema_version, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 'party_only', 'active', ?, ?, ?, ?, ?)",
                        (artifact_id, case_id, actor_id, kind, version, f"{relative_directory}/{filename}", contract[0], contract[1], schema_version, now),
                    )
                connection.execute(
                    "INSERT INTO candidate_versions(id, case_id, version_no, created_by_id, state, "
                    "json_a_artifact_id, json_a_sha256, json_a_byte_length, json_b_artifact_id, json_b_sha256, json_b_byte_length, "
                    "manifest_artifact_id, pdf_artifact_id, svg_artifact_id, manifest_schema_version, template_version, "
                    "catalog_sha256, label_map_sha256, created_at, superseded_at) "
                    "VALUES (?, ?, ?, ?, 'CURRENT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        candidate_id, case_id, version, actor_id,
                        json_a.artifact_id, json_a.sha256, json_a.byte_length,
                        json_b.artifact_id, json_b.sha256, json_b.byte_length,
                        artifact_ids["manifest"], artifact_ids["pdf"], artifact_ids["svg"],
                        str(manifest["schema_version"]), TEMPLATE_VERSION,
                        assets.catalog_record.sha256, assets.label_record.sha256, now,
                    ),
                )
                connection.execute(
                    "INSERT INTO candidate_review_rounds(id, case_id, candidate_id, revision, state, "
                    "manifest_sha256, pdf_sha256, svg_sha256, json_a_sha256, json_b_sha256, created_at) "
                    "VALUES (?, ?, ?, 1, 'OPEN', ?, ?, ?, ?, ?, ?)",
                    (
                        candidate_id,
                        case_id,
                        candidate_id,
                        artifact_contracts["manifest"][0],
                        artifact_contracts["pdf"][0],
                        artifact_contracts["svg"][0],
                        json_a.sha256,
                        json_b.sha256,
                        now,
                    ),
                )
                updated = connection.execute(
                    "UPDATE cases SET active_candidate_id = ?, state = 'CANDIDATE_REVIEW', updated_at = ? "
                    "WHERE id = ? AND state = 'JSON_B_REVIEW' AND active_candidate_id IS NULL "
                    "AND current_json_a_artifact_id = ? AND current_json_b_artifact_id = ?",
                    (candidate_id, now, case_id, json_a.artifact_id, json_b.artifact_id),
                )
                if updated.rowcount != 1:
                    raise CandidateWorkflowError("candidate_source_changed")
                result = CandidateResult(candidate_id, version, True)
                created = True
    except BaseException as error:
        try:
            quarantine_candidate_bundle(data_root, case_id, candidate_id)
        except (ArtifactStoreError, OSError) as quarantine_error:
            raise CandidateWorkflowError("candidate_quarantine_failed") from quarantine_error
        if isinstance(error, CandidateWorkflowError):
            raise
        raise CandidateWorkflowError("candidate_commit_failed") from error
    if not created:
        try:
            quarantine_candidate_bundle(data_root, case_id, candidate_id)
        except (ArtifactStoreError, OSError) as error:
            raise CandidateWorkflowError("candidate_quarantine_failed") from error
    return result


def _validate_candidate_phase(row: sqlite3.Row) -> None:
    expected_lifecycle = {
        "CURRENT": "active",
        "INVALIDATED": "invalidated",
        "FORMAL": "formal",
    }.get(row["state"])
    phase_valid = (
        row["state"] == "CURRENT"
        and row["case_state"] == "CANDIDATE_REVIEW"
        and row["active_candidate_id"] == row["id"]
        and row["formal_candidate_id"] is None
    ) or (
        row["state"] == "FORMAL"
        and row["case_state"] == "FORMAL"
        and row["active_candidate_id"] is None
        and row["formal_candidate_id"] == row["id"]
    ) or (
        row["state"] == "INVALIDATED"
        and row["active_candidate_id"] != row["id"]
        and row["formal_candidate_id"] != row["id"]
    )
    if expected_lifecycle is None or not phase_valid or any(
        row[column] != expected_lifecycle
        for column in ("manifest_lifecycle", "pdf_lifecycle", "svg_lifecycle")
    ):
        raise CandidateWorkflowError("candidate_unavailable")


def _load_candidate_row(
    connection: sqlite3.Connection,
    case_id: str,
    candidate_id: str,
    actor_id: str,
) -> sqlite3.Row:
    actor = connection.execute("SELECT status, system_role FROM users WHERE id = ?", (actor_id,)).fetchone()
    if actor is None or actor["status"] != "active" or actor["system_role"] != "member":
        raise CandidateWorkflowError("candidate_unavailable")
    row = connection.execute(
        "SELECT cv.*, c.owner_id, c.contractor_id, c.state AS case_state, "
        "c.active_candidate_id, c.formal_candidate_id, "
        "ma.relative_path AS manifest_path, ma.sha256 AS manifest_sha256, "
        "ma.byte_length AS manifest_length, ma.byte_length AS manifest_byte_length, "
        "ma.lifecycle AS manifest_lifecycle, "
        "pa.relative_path AS pdf_path, pa.sha256 AS pdf_sha256, "
        "pa.byte_length AS pdf_length, pa.byte_length AS pdf_byte_length, "
        "pa.lifecycle AS pdf_lifecycle, "
        "sa.relative_path AS svg_path, sa.sha256 AS svg_sha256, "
        "sa.byte_length AS svg_length, sa.byte_length AS svg_byte_length "
        ", sa.lifecycle AS svg_lifecycle "
        "FROM candidate_versions AS cv JOIN cases AS c ON c.id = cv.case_id "
        "JOIN artifacts AS ma ON ma.id = cv.manifest_artifact_id "
        "JOIN artifacts AS pa ON pa.id = cv.pdf_artifact_id "
        "JOIN artifacts AS sa ON sa.id = cv.svg_artifact_id "
        "WHERE cv.id = ? AND cv.case_id = ? AND cv.state IN ('CURRENT', 'INVALIDATED', 'FORMAL') "
        "AND (c.owner_id = ? OR c.contractor_id = ?) "
        "AND cv.created_by_id = c.contractor_id "
        "AND ma.case_id = cv.case_id AND pa.case_id = cv.case_id AND sa.case_id = cv.case_id "
        "AND ma.author_id = cv.created_by_id AND pa.author_id = cv.created_by_id AND sa.author_id = cv.created_by_id "
        "AND ma.version_no = cv.version_no AND pa.version_no = cv.version_no AND sa.version_no = cv.version_no "
        "AND ma.schema_version = cv.manifest_schema_version "
        "AND pa.schema_version = cv.template_version AND sa.schema_version = cv.template_version "
        "AND ma.kind = 'candidate_manifest' AND pa.kind = 'candidate_pdf' AND sa.kind = 'candidate_svg' "
        "AND ma.visibility = 'party_only' AND pa.visibility = 'party_only' AND sa.visibility = 'party_only'",
        (candidate_id, case_id, actor_id, actor_id),
    ).fetchone()
    if row is None:
        raise CandidateWorkflowError("candidate_unavailable")
    _validate_candidate_phase(row)
    return row


def _load_verified_bundle(
    connection: sqlite3.Connection,
    data_root: Path,
    case_id: str,
    candidate_id: str,
    actor_id: str | None = None,
    assets: DocumentAssets | None = None,
) -> tuple[sqlite3.Row, dict[str, bytes]]:
    if actor_id is None:
        row = connection.execute(
            "SELECT cv.*, c.state AS case_state, c.active_candidate_id, c.formal_candidate_id, "
            "ma.relative_path AS manifest_path, ma.sha256 AS manifest_sha256, "
            "ma.byte_length AS manifest_length, ma.byte_length AS manifest_byte_length, "
            "ma.lifecycle AS manifest_lifecycle, "
            "pa.relative_path AS pdf_path, pa.sha256 AS pdf_sha256, "
            "pa.byte_length AS pdf_length, pa.byte_length AS pdf_byte_length, "
            "pa.lifecycle AS pdf_lifecycle, "
            "sa.relative_path AS svg_path, sa.sha256 AS svg_sha256, "
            "sa.byte_length AS svg_length, sa.byte_length AS svg_byte_length, "
            "sa.lifecycle AS svg_lifecycle "
            "FROM candidate_versions AS cv JOIN cases AS c ON c.id = cv.case_id "
            "JOIN artifacts AS ma ON ma.id = cv.manifest_artifact_id "
            "JOIN artifacts AS pa ON pa.id = cv.pdf_artifact_id JOIN artifacts AS sa ON sa.id = cv.svg_artifact_id "
            "WHERE cv.id = ? AND cv.case_id = ? AND cv.state IN ('CURRENT', 'INVALIDATED', 'FORMAL') "
            "AND cv.created_by_id = c.contractor_id "
            "AND ma.case_id = cv.case_id AND pa.case_id = cv.case_id AND sa.case_id = cv.case_id "
            "AND ma.author_id = cv.created_by_id AND pa.author_id = cv.created_by_id AND sa.author_id = cv.created_by_id "
            "AND ma.version_no = cv.version_no AND pa.version_no = cv.version_no AND sa.version_no = cv.version_no "
            "AND ma.schema_version = cv.manifest_schema_version "
            "AND pa.schema_version = cv.template_version AND sa.schema_version = cv.template_version "
            "AND ma.kind = 'candidate_manifest' AND pa.kind = 'candidate_pdf' AND sa.kind = 'candidate_svg' "
            "AND ma.visibility = 'party_only' AND pa.visibility = 'party_only' AND sa.visibility = 'party_only'",
            (candidate_id, case_id),
        ).fetchone()
        if row is None:
            raise CandidateWorkflowError("candidate_unavailable")
        _validate_candidate_phase(row)
    else:
        row = _load_candidate_row(connection, case_id, candidate_id, actor_id)
    directory = f"cases/{case_id}/candidates/{candidate_id}"
    expected_paths = {
        "manifest_path": f"{directory}/manifest.json",
        "pdf_path": f"{directory}/document.pdf",
        "svg_path": f"{directory}/diagram.svg",
    }
    if any(row[column] != expected for column, expected in expected_paths.items()):
        raise CandidateWorkflowError("candidate_integrity_invalid")
    try:
        files = verify_candidate_bundle(
            data_root,
            directory,
            {
                "manifest.json": (row["manifest_sha256"], row["manifest_length"], MAX_CANDIDATE_MANIFEST_BYTES),
                "document.pdf": (row["pdf_sha256"], row["pdf_length"], MAX_CANDIDATE_BINARY_BYTES),
                "diagram.svg": (row["svg_sha256"], row["svg_length"], MAX_CANDIDATE_BINARY_BYTES),
            },
        )
        manifest = _validate_manifest(files["manifest.json"], MANIFEST_SCHEMA_PATH)
    except (ArtifactStoreError, CandidateWorkflowError, OSError, UnicodeError) as error:
        raise CandidateWorkflowError("candidate_integrity_invalid") from error
    source_a = connection.execute(
        "SELECT a.id AS artifact_id, a.version_no, a.schema_version, a.sha256, a.byte_length, "
        "a.lifecycle, v.document_id "
        "FROM artifacts AS a JOIN json_a_source_verifications AS v ON v.artifact_id = a.id "
        "AND v.case_id = a.case_id AND v.actor_role = 'contractor' "
        "WHERE a.id = ? AND a.case_id = ? AND a.kind = 'json_a' "
        "AND a.visibility = 'party_only' AND a.lifecycle IN ('active', 'superseded', 'formal')",
        (row["json_a_artifact_id"], case_id),
    ).fetchone()
    source_b = connection.execute(
        "SELECT a.id AS artifact_id, a.version_no, a.schema_version, a.sha256, a.byte_length, "
        "a.lifecycle, v.document_id, v.state AS version_state "
        "FROM artifacts AS a JOIN json_b_versions AS v ON v.artifact_id = a.id "
        "WHERE a.id = ? AND a.case_id = ? AND a.kind = 'json_b' "
        "AND a.visibility = 'party_only' AND a.lifecycle IN ('active', 'superseded', 'formal') "
        "AND v.state IN ('CURRENT', 'SUPERSEDED')",
        (row["json_b_artifact_id"], case_id),
    ).fetchone()
    expected_sources = None
    if source_a is not None and source_b is not None:
        expected_sources = {
            "json_a": {
                "artifact_id": source_a["artifact_id"], "document_id": source_a["document_id"],
                "version": source_a["version_no"], "schema_version": source_a["schema_version"],
                "sha256": source_a["sha256"], "byte_length": source_a["byte_length"],
            },
            "json_b": {
                "artifact_id": source_b["artifact_id"], "document_id": source_b["document_id"],
                "version": source_b["version_no"], "schema_version": source_b["schema_version"],
                "sha256": source_b["sha256"], "byte_length": source_b["byte_length"],
            },
        }
    artifact_contract = {
        "pdf": {"filename": "document.pdf", "media_type": "application/pdf", "sha256": row["pdf_sha256"], "byte_length": row["pdf_length"]},
        "svg": {"filename": "diagram.svg", "media_type": "image/svg+xml", "sha256": row["svg_sha256"], "byte_length": row["svg_length"]},
    }
    if (
        source_a is None
        or source_b is None
        or (
            row["state"] == "CURRENT"
            and (source_a["lifecycle"] != "active" or source_b["lifecycle"] != "active" or source_b["version_state"] != "CURRENT")
        )
        or (
            row["state"] == "FORMAL"
            and (source_a["lifecycle"] != "formal" or source_b["lifecycle"] != "formal" or source_b["version_state"] != "CURRENT")
        )
        or row["json_a_sha256"] != source_a["sha256"]
        or row["json_a_byte_length"] != source_a["byte_length"]
        or row["json_b_sha256"] != source_b["sha256"]
        or row["json_b_byte_length"] != source_b["byte_length"]
        or manifest.get("schema_version") != row["manifest_schema_version"]
        or manifest.get("status") != "candidate"
        or manifest.get("label_mode") != "english_only"
        or manifest.get("candidate_id") != candidate_id
        or manifest.get("case_id") != case_id
        or manifest.get("version") != row["version_no"]
        or manifest.get("created_at") != row["created_at"]
        or manifest.get("sources") != expected_sources
        or manifest.get("artifacts") != artifact_contract
        or manifest.get("template_version") != row["template_version"]
        or manifest.get("catalog", {}).get("sha256") != row["catalog_sha256"]
        or manifest.get("label_map", {}).get("sha256") != row["label_map_sha256"]
    ):
        raise CandidateWorkflowError("candidate_integrity_invalid")
    return row, files


def load_candidate_for_party(
    connection: sqlite3.Connection,
    data_root: Path,
    case_id: str,
    candidate_id: str,
    actor_id: str,
    assets: DocumentAssets | None = None,
) -> sqlite3.Row:
    row, _files = _load_verified_bundle(connection, data_root, case_id, candidate_id, actor_id, assets)
    return row


def read_candidate_component(
    connection: sqlite3.Connection,
    data_root: Path,
    case_id: str,
    candidate_id: str,
    actor_id: str,
    filename: str,
    assets: DocumentAssets | None = None,
) -> bytes:
    if filename not in {"manifest.json", "document.pdf", "diagram.svg"}:
        raise CandidateWorkflowError("candidate_unavailable")
    _row, files = _load_verified_bundle(connection, data_root, case_id, candidate_id, actor_id, assets)
    return files[filename]


def reconcile_candidate_storage(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets | None = None,
) -> None:
    root = data_root.resolve() / "cases"
    candidate_rows = {
        (row["case_id"], row["id"]): row["state"]
        for row in connection.execute(
            "SELECT case_id, id, state FROM candidate_versions "
            "WHERE state IN ('CURRENT', 'INVALIDATED', 'FORMAL', 'SUPERSEDED')"
        )
    }
    pointer_cases = {
        row["id"]: (row["state"], row["active_candidate_id"], row["formal_candidate_id"])
        for row in connection.execute(
            "SELECT id, state, active_candidate_id, formal_candidate_id FROM cases "
            "WHERE state IN ('CANDIDATE_REVIEW', 'FORMAL') "
            "OR active_candidate_id IS NOT NULL OR formal_candidate_id IS NOT NULL"
        )
    }
    pointed_current = {
        (case_id, active)
        for case_id, (state, active, formal) in pointer_cases.items()
        if state == "CANDIDATE_REVIEW" and active is not None and formal is None
    }
    pointed_formal = {
        (case_id, formal)
        for case_id, (state, active, formal) in pointer_cases.items()
        if state == "FORMAL" and active is None and formal is not None
    }
    current_rows = {key for key, state in candidate_rows.items() if state == "CURRENT"}
    formal_rows = {key for key, state in candidate_rows.items() if state == "FORMAL"}
    if pointed_current != current_rows or pointed_formal != formal_rows or any(
        not (
            (state == "CANDIDATE_REVIEW" and active is not None and formal is None)
            or (state == "FORMAL" and active is None and formal is not None)
        )
        for state, active, formal in pointer_cases.values()
    ):
        raise CandidateWorkflowError("candidate_database_invalid")
    if not root.exists():
        if candidate_rows:
            raise CandidateWorkflowError("candidate_storage_missing")
        return
    known = set(candidate_rows)
    for case_directory in root.iterdir():
        if not case_directory.is_dir() or case_directory.is_symlink():
            continue
        case_id = case_directory.name
        staging = case_directory / "staging"
        quarantine = case_directory / "quarantine"
        _ensure_private_directory(quarantine)
        if staging.is_dir():
            for entry in list(staging.iterdir()):
                target = quarantine / f"staging-{entry.name}-{uuid.uuid4()}"
                _rename_directory_no_replace(entry, target)
                _fsync_directory(staging)
                _fsync_directory(quarantine)
        candidates = case_directory / "candidates"
        if candidates.is_dir():
            for entry in list(candidates.iterdir()):
                if (case_id, entry.name) not in known:
                    _rename_directory_no_replace(
                        entry, quarantine / f"{entry.name}-{uuid.uuid4()}"
                    )
                    _fsync_directory(candidates)
                    _fsync_directory(quarantine)
    for (case_id, candidate_id), state in candidate_rows.items():
        if state == "SUPERSEDED":
            candidate = connection.execute(
                "SELECT created_by_id, version_no, manifest_artifact_id, pdf_artifact_id, svg_artifact_id "
                "FROM candidate_versions WHERE case_id = ? AND id = ? AND state = 'SUPERSEDED'",
                (case_id, candidate_id),
            ).fetchone()
            if candidate is None:
                raise CandidateWorkflowError("candidate_database_invalid")
            directory = f"cases/{case_id}/candidates/{candidate_id}"
            expected = {}
            for filename, column, kind, limit in (
                ("manifest.json", "manifest_artifact_id", "candidate_manifest", MAX_CANDIDATE_MANIFEST_BYTES),
                ("document.pdf", "pdf_artifact_id", "candidate_pdf", MAX_CANDIDATE_BINARY_BYTES),
                ("diagram.svg", "svg_artifact_id", "candidate_svg", MAX_CANDIDATE_BINARY_BYTES),
            ):
                artifact = connection.execute(
                    "SELECT case_id, author_id, version_no, kind, visibility, relative_path, sha256, byte_length "
                    "FROM artifacts WHERE id = ?",
                    (candidate[column],),
                ).fetchone()
                if (
                    artifact is None
                    or artifact["case_id"] != case_id
                    or artifact["author_id"] != candidate["created_by_id"]
                    or artifact["version_no"] != candidate["version_no"]
                    or artifact["kind"] != kind
                    or artifact["visibility"] != "party_only"
                    or artifact["relative_path"] != f"{directory}/{filename}"
                ):
                    raise CandidateWorkflowError("candidate_database_invalid")
                expected[filename] = (artifact["sha256"], artifact["byte_length"], limit)
            try:
                verify_candidate_bundle(data_root, directory, expected)
            except (ArtifactStoreError, OSError) as error:
                raise CandidateWorkflowError("candidate_integrity_invalid") from error
        else:
            _load_verified_bundle(connection, data_root, case_id, candidate_id, assets=assets)
