from __future__ import annotations

import hmac
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.db import deferred_read_transaction, immediate_transaction
from app.services.artifact_store import (
    ArtifactStoreError,
    MAX_JSON_ARTIFACT_BYTES,
    read_verified,
)
from app.services.candidate_workflow import (
    CandidateWorkflowError,
    _load_candidate_row,
    _load_verified_bundle,
)
from app.services.case_workflow import SECTION_KEYS
from app.services.document_integrity import DocumentAssets


class FormalizationWorkflowError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ConfirmationResult:
    candidate_id: str
    formalized: bool
    created: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FormalizationWorkflowError("quote_expiry_invalid") from error
    if parsed.tzinfo is None:
        raise FormalizationWorkflowError("quote_expiry_invalid")
    return parsed.astimezone(timezone.utc)


def _audit(
    connection: sqlite3.Connection,
    actor_id: str,
    case_id: str,
    entity_id: str,
    action: str,
    result: str = "success",
) -> None:
    connection.execute(
        "INSERT INTO audit_events(id, actor_id, case_id, entity_id, action, result, "
        "server_created_at, request_correlation_id, locale_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'en')",
        (str(uuid.uuid4()), actor_id, case_id, entity_id, action, result, _now(), str(uuid.uuid4())),
    )


def _actor(connection: sqlite3.Connection, actor_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT id, status, system_role FROM users WHERE id = ?", (actor_id,)
    ).fetchone()
    if row is None or row["status"] != "active" or row["system_role"] != "member":
        raise FormalizationWorkflowError("candidate_unavailable")
    return row


def _active_context(
    connection: sqlite3.Connection,
    case_id: str,
    candidate_id: str,
    actor_id: str,
    *,
    require_consent_ready: bool,
) -> tuple[sqlite3.Row, str]:
    _actor(connection, actor_id)
    row = connection.execute(
        "SELECT c.id AS case_id, c.owner_id, c.contractor_id, c.state AS case_state, "
        "c.content_revision, c.current_json_a_artifact_id, c.current_json_b_artifact_id, "
        "c.active_candidate_id, c.formal_candidate_id, "
        "candidate.*, round.id AS round_id, round.revision AS round_revision, "
        "round.state AS round_state, round.manifest_sha256 AS round_manifest_sha256, "
        "round.pdf_sha256 AS round_pdf_sha256, round.svg_sha256 AS round_svg_sha256, "
        "round.json_a_sha256 AS round_json_a_sha256, round.json_b_sha256 AS round_json_b_sha256, "
        "manifest.sha256 AS manifest_sha256, manifest.byte_length AS manifest_byte_length, "
        "pdf.sha256 AS pdf_sha256, pdf.byte_length AS pdf_byte_length, "
        "svg.sha256 AS svg_sha256, svg.byte_length AS svg_byte_length, "
        "proposal.quote_expires_at, proposal.state AS proposal_state, "
        "owner.status AS owner_status, owner.system_role AS owner_system_role, "
        "contractor.status AS contractor_status, contractor.system_role AS contractor_system_role "
        "FROM cases AS c "
        "JOIN users AS owner ON owner.id = c.owner_id "
        "JOIN users AS contractor ON contractor.id = c.contractor_id "
        "JOIN candidate_versions AS candidate ON candidate.id = c.active_candidate_id "
        "JOIN candidate_review_rounds AS round ON round.candidate_id = candidate.id "
        "JOIN artifacts AS manifest ON manifest.id = candidate.manifest_artifact_id "
        "JOIN artifacts AS pdf ON pdf.id = candidate.pdf_artifact_id "
        "JOIN artifacts AS svg ON svg.id = candidate.svg_artifact_id "
        "JOIN json_b_versions AS proposal ON proposal.artifact_id = candidate.json_b_artifact_id "
        "WHERE c.id = ? AND candidate.id = ? AND (c.owner_id = ? OR c.contractor_id = ?)",
        (case_id, candidate_id, actor_id, actor_id),
    ).fetchone()
    if row is None:
        raise FormalizationWorkflowError("candidate_unavailable")
    role = "client" if row["owner_id"] == actor_id else "contractor"
    if (
        row["owner_id"] == row["contractor_id"]
        or row["owner_status"] != "active"
        or row["contractor_status"] != "active"
        or row["owner_system_role"] != "member"
        or row["contractor_system_role"] != "member"
        or row["case_state"] != "CANDIDATE_REVIEW"
        or row["active_candidate_id"] != candidate_id
        or row["formal_candidate_id"] is not None
        or row["state"] != "CURRENT"
        or row["round_id"] != candidate_id
        or row["round_state"] != "OPEN"
        or row["round_revision"] != row["review_revision"]
        or row["current_json_a_artifact_id"] != row["json_a_artifact_id"]
        or row["current_json_b_artifact_id"] != row["json_b_artifact_id"]
        or not hmac.compare_digest(row["round_manifest_sha256"], row["manifest_sha256"])
        or not hmac.compare_digest(row["round_pdf_sha256"], row["pdf_sha256"])
        or not hmac.compare_digest(row["round_svg_sha256"], row["svg_sha256"])
        or not hmac.compare_digest(row["round_json_a_sha256"], row["json_a_sha256"])
        or not hmac.compare_digest(row["round_json_b_sha256"], row["json_b_sha256"])
    ):
        raise FormalizationWorkflowError("candidate_stale")
    if require_consent_ready:
        if row["proposal_state"] != "CURRENT" or _parse_time(row["quote_expires_at"]) <= datetime.now(timezone.utc):
            raise FormalizationWorkflowError("quote_expired")
        blocking = connection.execute(
            "SELECT 1 FROM case_issues WHERE case_id = ? "
            "AND state NOT IN ('RESOLVED', 'OUT_OF_SCOPE') LIMIT 1",
            (case_id,),
        ).fetchone()
        if blocking is not None:
            raise FormalizationWorkflowError("blocking_issue")
    return row, role


def _digest_values(row: sqlite3.Row) -> tuple[str, str, str, str, str]:
    return (
        row["manifest_sha256"],
        row["pdf_sha256"],
        row["svg_sha256"],
        row["json_a_sha256"],
        row["json_b_sha256"],
    )


def _binding_values(row: sqlite3.Row) -> tuple[object, ...]:
    return (
        row["manifest_artifact_id"], row["manifest_sha256"], row["manifest_byte_length"],
        row["pdf_artifact_id"], row["pdf_sha256"], row["pdf_byte_length"],
        row["svg_artifact_id"], row["svg_sha256"], row["svg_byte_length"],
        row["json_a_artifact_id"], row["json_a_sha256"], row["json_a_byte_length"],
        row["json_b_artifact_id"], row["json_b_sha256"], row["json_b_byte_length"],
    )


def _active_context_token(row: sqlite3.Row, role: str) -> tuple[object, ...]:
    return (
        row["case_id"], row["owner_id"], row["contractor_id"], role,
        row["case_state"], row["content_revision"], row["active_candidate_id"],
        row["formal_candidate_id"], row["id"], row["state"], row["review_revision"],
        row["round_id"], row["round_revision"], row["round_state"],
        row["proposal_state"], row["quote_expires_at"], *_binding_values(row),
    )


def _verified_active_context(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    candidate_id: str,
    actor_id: str,
    *,
    require_consent_ready: bool,
) -> tuple[sqlite3.Row, str]:
    with deferred_read_transaction(connection):
        context, role = _active_context(
            connection,
            case_id,
            candidate_id,
            actor_id,
            require_consent_ready=require_consent_ready,
        )
        candidate, _files = _load_verified_bundle(
            connection, data_root, case_id, candidate_id, actor_id, assets
        )
        if (
            candidate["id"] != context["id"]
            or candidate["review_revision"] != context["round_revision"]
            or _binding_values(candidate) != _binding_values(context)
        ):
            raise FormalizationWorkflowError("candidate_stale")
        return context, role


def _record_matches(row: sqlite3.Row, context: sqlite3.Row, actor_id: str, role: str) -> bool:
    return (
        row["case_id"] == context["case_id"]
        and row["candidate_id"] == context["id"]
        and row["actor_id"] == actor_id
        and row["actor_role"] == role
        and _binding_values(row) == _binding_values(context)
    )


def _unrenewed_withdrawn_confirmation(
    connection: sqlite3.Connection,
    round_id: str,
    actor_id: str,
    role: str,
) -> sqlite3.Row | None:
    rows = connection.execute(
        "SELECT confirmation.id, confirmation.withdrawn_at "
        "FROM candidate_confirmations AS confirmation "
        "LEFT JOIN candidate_acknowledgement_renewals AS renewal "
        "ON renewal.withdrawn_confirmation_id = confirmation.id "
        "WHERE confirmation.round_id = ? AND confirmation.actor_id = ? "
        "AND confirmation.actor_role = ? AND confirmation.state = 'WITHDRAWN' "
        "AND renewal.id IS NULL LIMIT 2",
        (round_id, actor_id, role),
    ).fetchall()
    if len(rows) > 1:
        raise FormalizationWorkflowError("acknowledgement_conflict")
    return rows[0] if rows else None


def acknowledge_candidate(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    candidate_id: str,
    actor_id: str,
    expected_revision: int,
) -> bool:
    verified, verified_role = _verified_active_context(
        connection, data_root, assets, case_id, candidate_id, actor_id,
        require_consent_ready=True,
    )
    with immediate_transaction(connection):
        context, role = _active_context(
            connection, case_id, candidate_id, actor_id, require_consent_ready=True
        )
        if (
            role != verified_role
            or _active_context_token(context, role) != _active_context_token(verified, verified_role)
            or context["round_revision"] != expected_revision
        ):
            raise FormalizationWorkflowError("confirmation_stale")
        existing = connection.execute(
            "SELECT * FROM candidate_acknowledgements WHERE round_id = ? AND actor_role = ?",
            (context["round_id"], role),
        ).fetchone()
        if existing is not None:
            if not _record_matches(existing, context, actor_id, role):
                raise FormalizationWorkflowError("acknowledgement_conflict")
            withdrawn = _unrenewed_withdrawn_confirmation(
                connection, context["round_id"], actor_id, role
            )
            if withdrawn is None:
                return False
            connection.execute(
                "INSERT INTO candidate_acknowledgement_renewals("
                "id, acknowledgement_id, withdrawn_confirmation_id, renewal_kind, renewed_at) "
                "VALUES (?, ?, ?, 'EXPLICIT', ?)",
                (str(uuid.uuid4()), existing["id"], withdrawn["id"], _now()),
            )
            _audit(connection, actor_id, case_id, candidate_id, "candidate_reacknowledged")
            return True
        connection.execute(
            "INSERT INTO candidate_acknowledgements(id, round_id, case_id, candidate_id, actor_id, "
            "actor_role, manifest_artifact_id, manifest_sha256, manifest_byte_length, "
            "pdf_artifact_id, pdf_sha256, pdf_byte_length, svg_artifact_id, svg_sha256, svg_byte_length, "
            "json_a_artifact_id, json_a_sha256, json_a_byte_length, "
            "json_b_artifact_id, json_b_sha256, json_b_byte_length, acknowledged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), context["round_id"], case_id, candidate_id, actor_id, role,
                *_binding_values(context), _now(),
            ),
        )
        _audit(connection, actor_id, case_id, candidate_id, "candidate_acknowledged")
        return True


def confirm_candidate(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    candidate_id: str,
    actor_id: str,
    expected_revision: int,
) -> ConfirmationResult:
    verified, verified_role = _verified_active_context(
        connection, data_root, assets, case_id, candidate_id, actor_id,
        require_consent_ready=True,
    )
    with immediate_transaction(connection):
        context, role = _active_context(
            connection, case_id, candidate_id, actor_id, require_consent_ready=True
        )
        if (
            role != verified_role
            or _active_context_token(context, role) != _active_context_token(verified, verified_role)
            or context["round_revision"] != expected_revision
        ):
            raise FormalizationWorkflowError("confirmation_stale")
        acknowledgement = connection.execute(
            "SELECT * FROM candidate_acknowledgements WHERE round_id = ? AND actor_role = ?",
            (context["round_id"], role),
        ).fetchone()
        if acknowledgement is None or not _record_matches(
            acknowledgement, context, actor_id, role
        ):
            raise FormalizationWorkflowError("acknowledgement_required")
        withdrawn = _unrenewed_withdrawn_confirmation(
            connection, context["round_id"], actor_id, role
        )
        if withdrawn is not None:
            raise FormalizationWorkflowError("acknowledgement_required")
        existing = connection.execute(
            "SELECT * FROM candidate_confirmations WHERE round_id = ? AND actor_role = ? AND state = 'CURRENT'",
            (context["round_id"], role),
        ).fetchone()
        created = False
        if existing is None:
            connection.execute(
                "INSERT INTO candidate_confirmations(id, round_id, case_id, candidate_id, actor_id, "
                "actor_role, manifest_artifact_id, manifest_sha256, manifest_byte_length, "
                "pdf_artifact_id, pdf_sha256, pdf_byte_length, svg_artifact_id, svg_sha256, svg_byte_length, "
                "json_a_artifact_id, json_a_sha256, json_a_byte_length, "
                "json_b_artifact_id, json_b_sha256, json_b_byte_length, state, confirmed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CURRENT', ?)",
                (
                    str(uuid.uuid4()), context["round_id"], case_id, candidate_id, actor_id, role,
                    *_binding_values(context), _now(),
                ),
            )
            _audit(connection, actor_id, case_id, candidate_id, "candidate_confirmed")
            created = True
        elif not _record_matches(existing, context, actor_id, role):
            raise FormalizationWorkflowError("confirmation_conflict")

        confirmations = connection.execute(
            "SELECT * FROM candidate_confirmations WHERE round_id = ? AND state = 'CURRENT' ORDER BY actor_role",
            (context["round_id"],),
        ).fetchall()
        if len(confirmations) < 2:
            return ConfirmationResult(candidate_id, False, created)
        expected_parties = {"client": context["owner_id"], "contractor": context["contractor_id"]}
        if len(confirmations) != 2 or {
            row["actor_role"]: row["actor_id"] for row in confirmations
        } != expected_parties or any(
            not _record_matches(row, context, row["actor_id"], row["actor_role"])
            for row in confirmations
        ):
            raise FormalizationWorkflowError("formalization_conflict")

        formalized_at = _now()
        cleared = connection.execute(
            "UPDATE cases SET state = 'JSON_B_REVIEW', active_candidate_id = NULL, updated_at = ? "
            "WHERE id = ? AND state = 'CANDIDATE_REVIEW' AND active_candidate_id = ? "
            "AND formal_candidate_id IS NULL",
            (formalized_at, case_id, candidate_id),
        )
        if cleared.rowcount != 1:
            raise FormalizationWorkflowError("formalization_conflict")
        changed_candidate = connection.execute(
            "UPDATE candidate_versions SET state = 'FORMAL', formalized_at = ? "
            "WHERE id = ? AND case_id = ? AND state = 'CURRENT'",
            (formalized_at, candidate_id, case_id),
        )
        if changed_candidate.rowcount != 1:
            raise FormalizationWorkflowError("formalization_conflict")
        artifact_ids = (
            context["json_a_artifact_id"], context["json_b_artifact_id"],
            context["manifest_artifact_id"], context["pdf_artifact_id"], context["svg_artifact_id"],
        )
        changed_artifacts = connection.execute(
            "UPDATE artifacts SET lifecycle = 'formal' WHERE case_id = ? AND id IN (?, ?, ?, ?, ?) "
            "AND lifecycle = 'active'",
            (case_id, *artifact_ids),
        )
        if changed_artifacts.rowcount != 5:
            raise FormalizationWorkflowError("formalization_conflict")
        if connection.execute(
            "UPDATE candidate_confirmations SET state = 'FORMALIZED', formalized_at = ? "
            "WHERE round_id = ? AND state = 'CURRENT'",
            (formalized_at, context["round_id"]),
        ).rowcount != 2:
            raise FormalizationWorkflowError("formalization_conflict")
        if connection.execute(
            "UPDATE candidate_review_rounds SET state = 'FORMALIZED', formalized_at = ? "
            "WHERE id = ? AND state = 'OPEN'",
            (formalized_at, context["round_id"]),
        ).rowcount != 1:
            raise FormalizationWorkflowError("formalization_conflict")
        finalized = connection.execute(
            "UPDATE cases SET state = 'FORMAL', formal_candidate_id = ?, updated_at = ? "
            "WHERE id = ? AND state = 'JSON_B_REVIEW' AND active_candidate_id IS NULL "
            "AND formal_candidate_id IS NULL",
            (candidate_id, formalized_at, case_id),
        )
        if finalized.rowcount != 1:
            raise FormalizationWorkflowError("formalization_conflict")
        _audit(connection, actor_id, case_id, candidate_id, "candidate_formalized")
        return ConfirmationResult(candidate_id, True, created)


def withdraw_confirmation(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    candidate_id: str,
    actor_id: str,
    expected_revision: int,
) -> None:
    verified, verified_role = _verified_active_context(
        connection, data_root, assets, case_id, candidate_id, actor_id,
        require_consent_ready=False,
    )
    with immediate_transaction(connection):
        context, role = _active_context(
            connection, case_id, candidate_id, actor_id, require_consent_ready=False
        )
        if (
            role != verified_role
            or _active_context_token(context, role) != _active_context_token(verified, verified_role)
            or context["round_revision"] != expected_revision
        ):
            raise FormalizationWorkflowError("confirmation_stale")
        changed = connection.execute(
            "UPDATE candidate_confirmations SET state = 'WITHDRAWN', withdrawn_at = ? "
            "WHERE round_id = ? AND actor_id = ? AND actor_role = ? AND state = 'CURRENT'",
            (_now(), context["round_id"], actor_id, role),
        )
        if changed.rowcount != 1:
            raise FormalizationWorkflowError("confirmation_unavailable")
        _audit(connection, actor_id, case_id, candidate_id, "candidate_confirmation_withdrawn")


def request_candidate_revision(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    candidate_id: str,
    actor_id: str,
    expected_revision: int,
    reason: str,
) -> None:
    normalized_reason = reason.strip()
    if not 1 <= len(normalized_reason) <= 4000:
        raise FormalizationWorkflowError("revision_reason_required")
    verified, verified_role = _verified_active_context(
        connection, data_root, assets, case_id, candidate_id, actor_id,
        require_consent_ready=False,
    )
    with immediate_transaction(connection):
        context, role = _active_context(
            connection, case_id, candidate_id, actor_id, require_consent_ready=False
        )
        if (
            role != verified_role
            or _active_context_token(context, role) != _active_context_token(verified, verified_role)
            or context["round_revision"] != expected_revision
        ):
            raise FormalizationWorkflowError("confirmation_stale")
        now = _now()
        cleared = connection.execute(
            "UPDATE cases SET state = 'JSON_B_REVIEW', active_candidate_id = NULL, updated_at = ? "
            "WHERE id = ? AND state = 'CANDIDATE_REVIEW' AND active_candidate_id = ? "
            "AND formal_candidate_id IS NULL",
            (now, case_id, candidate_id),
        )
        if cleared.rowcount != 1:
            raise FormalizationWorkflowError("candidate_stale")
        connection.execute(
            "UPDATE candidate_confirmations SET state = 'INVALIDATED', invalidated_at = ? "
            "WHERE round_id = ? AND state = 'CURRENT'",
            (now, context["round_id"]),
        )
        if connection.execute(
            "UPDATE candidate_review_rounds SET state = 'INVALIDATED', invalidated_at = ?, "
            "invalidated_by_id = ?, revision_reason = ? WHERE id = ? AND state = 'OPEN'",
            (now, actor_id, normalized_reason, context["round_id"]),
        ).rowcount != 1:
            raise FormalizationWorkflowError("candidate_stale")
        if connection.execute(
            "UPDATE candidate_versions SET state = 'INVALIDATED', invalidated_at = ?, "
            "invalidation_reason = 'revision_requested' WHERE id = ? AND state = 'CURRENT'",
            (now, candidate_id),
        ).rowcount != 1:
            raise FormalizationWorkflowError("candidate_stale")
        if connection.execute(
            "UPDATE artifacts SET lifecycle = 'invalidated' WHERE case_id = ? "
            "AND id IN (?, ?, ?) AND lifecycle = 'active'",
            (
                case_id, context["manifest_artifact_id"],
                context["pdf_artifact_id"], context["svg_artifact_id"],
            ),
        ).rowcount != 3:
            raise FormalizationWorkflowError("candidate_stale")
        issue_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO case_issues(id, case_id, kind, summary, opened_by_id, "
            "opened_by_role, state, revision, created_at, updated_at) "
            "VALUES (?, ?, 'CLARIFICATION', ?, ?, ?, 'OPEN', 1, ?, ?)",
            (issue_id, case_id, normalized_reason, actor_id, role, now, now),
        )
        connection.execute(
            "INSERT INTO issue_events(id, issue_id, actor_id, actor_role, action, "
            "from_state, to_state, note, issue_revision, server_created_at) "
            "VALUES (?, ?, ?, ?, 'OPENED', NULL, 'OPEN', ?, 1, ?)",
            (str(uuid.uuid4()), issue_id, actor_id, role, normalized_reason, now),
        )
        _audit(connection, actor_id, case_id, candidate_id, "candidate_revision_requested")


def load_confirmation_view(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    candidate_id: str,
    actor_id: str,
) -> dict[str, object]:
    candidate, _files = _load_verified_bundle(
        connection, data_root, case_id, candidate_id, actor_id, assets
    )
    round_row = connection.execute(
        "SELECT * FROM candidate_review_rounds WHERE candidate_id = ? AND case_id = ?",
        (candidate_id, case_id),
    ).fetchone()
    if round_row is None:
        raise FormalizationWorkflowError("candidate_unavailable")
    acknowledgements = connection.execute(
        "SELECT actor_role, actor_id, acknowledged_at FROM candidate_acknowledgements "
        "WHERE round_id = ? ORDER BY actor_role",
        (round_row["id"],),
    ).fetchall()
    confirmations = connection.execute(
        "SELECT id, actor_role, actor_id, state, confirmed_at, withdrawn_at, invalidated_at, formalized_at "
        "FROM candidate_confirmations WHERE round_id = ? ORDER BY confirmed_at, id",
        (round_row["id"],),
    ).fetchall()
    acknowledgement_renewals = connection.execute(
        "SELECT renewal.id, renewal.acknowledgement_id, renewal.withdrawn_confirmation_id, "
        "renewal.renewal_kind, renewal.renewed_at, acknowledgement.actor_id, acknowledgement.actor_role "
        "FROM candidate_acknowledgement_renewals AS renewal "
        "JOIN candidate_acknowledgements AS acknowledgement "
        "ON acknowledgement.id = renewal.acknowledgement_id "
        "WHERE acknowledgement.round_id = ? ORDER BY renewal.renewed_at, renewal.id",
        (round_row["id"],),
    ).fetchall()
    unrenewed_confirmations = connection.execute(
        "SELECT confirmation.id, confirmation.actor_role, confirmation.actor_id, "
        "confirmation.state, confirmation.confirmed_at, confirmation.withdrawn_at, "
        "confirmation.invalidated_at, confirmation.formalized_at "
        "FROM candidate_confirmations AS confirmation "
        "LEFT JOIN candidate_acknowledgement_renewals AS renewal "
        "ON renewal.withdrawn_confirmation_id = confirmation.id "
        "WHERE confirmation.round_id = ? AND confirmation.state = 'WITHDRAWN' "
        "AND renewal.id IS NULL",
        (round_row["id"],),
    ).fetchall()
    return {
        "candidate": candidate,
        "round": round_row,
        "acknowledgements": acknowledgements,
        "acknowledgement_renewals": acknowledgement_renewals,
        "unrenewed_confirmations": unrenewed_confirmations,
        "confirmations": confirmations,
    }


def _load_formal_db_view(
    connection: sqlite3.Connection,
    case_id: str,
    actor_id: str,
) -> dict[str, object]:
    _actor(connection, actor_id)
    case = connection.execute(
        "SELECT id, owner_id, contractor_id, title, state, active_candidate_id, formal_candidate_id "
        "FROM cases WHERE id = ? AND (owner_id = ? OR contractor_id = ?)",
        (case_id, actor_id, actor_id),
    ).fetchone()
    if (
        case is None
        or case["state"] != "FORMAL"
        or case["active_candidate_id"] is not None
        or case["formal_candidate_id"] is None
        or case["owner_id"] == case["contractor_id"]
    ):
        raise FormalizationWorkflowError("formal_unavailable")
    try:
        candidate = _load_candidate_row(
            connection, case_id, case["formal_candidate_id"], actor_id
        )
    except CandidateWorkflowError as error:
        raise FormalizationWorkflowError("formal_integrity_invalid") from error
    round_row = connection.execute(
        "SELECT * FROM candidate_review_rounds WHERE candidate_id = ? AND state = 'FORMALIZED'",
        (candidate["id"],),
    ).fetchone()
    acknowledgements = connection.execute(
        "SELECT * FROM candidate_acknowledgements "
        "WHERE round_id = ? ORDER BY actor_role",
        (candidate["id"],),
    ).fetchall()
    confirmations = connection.execute(
        "SELECT * FROM candidate_confirmations "
        "WHERE round_id = ? AND state = 'FORMALIZED' ORDER BY actor_role",
        (candidate["id"],),
    ).fetchall()
    withdrawn_confirmations = connection.execute(
        "SELECT id, actor_id, actor_role FROM candidate_confirmations "
        "WHERE round_id = ? AND state = 'WITHDRAWN' ORDER BY confirmed_at, id",
        (candidate["id"],),
    ).fetchall()
    acknowledgement_renewals = connection.execute(
        "SELECT renewal.id, renewal.acknowledgement_id, renewal.withdrawn_confirmation_id, "
        "renewal.renewal_kind, renewal.renewed_at, acknowledgement.actor_id, acknowledgement.actor_role "
        "FROM candidate_acknowledgement_renewals AS renewal "
        "JOIN candidate_acknowledgements AS acknowledgement "
        "ON acknowledgement.id = renewal.acknowledgement_id "
        "WHERE acknowledgement.round_id = ? ORDER BY renewal.renewed_at, renewal.id",
        (candidate["id"],),
    ).fetchall()
    expected_parties = {"client": case["owner_id"], "contractor": case["contractor_id"]}
    round_matches = (
        round_row is not None
        and round_row["id"] == candidate["id"]
        and round_row["case_id"] == case_id
        and round_row["candidate_id"] == candidate["id"]
        and round_row["revision"] == candidate["review_revision"]
        and hmac.compare_digest(round_row["manifest_sha256"], candidate["manifest_sha256"])
        and hmac.compare_digest(round_row["pdf_sha256"], candidate["pdf_sha256"])
        and hmac.compare_digest(round_row["svg_sha256"], candidate["svg_sha256"])
        and hmac.compare_digest(round_row["json_a_sha256"], candidate["json_a_sha256"])
        and hmac.compare_digest(round_row["json_b_sha256"], candidate["json_b_sha256"])
    )
    acknowledgements_match = (
        len(acknowledgements) == 2
        and {row["actor_role"]: row["actor_id"] for row in acknowledgements} == expected_parties
        and all(
            _record_matches(row, candidate, row["actor_id"], row["actor_role"])
            for row in acknowledgements
        )
    )
    confirmations_match = (
        len(confirmations) == 2
        and {row["actor_role"]: row["actor_id"] for row in confirmations} == expected_parties
        and all(
            _record_matches(row, candidate, row["actor_id"], row["actor_role"])
            for row in confirmations
        )
    )
    acknowledgement_ids = {
        (row["actor_id"], row["actor_role"]): row["id"] for row in acknowledgements
    }
    withdrawn_parties = {
        row["id"]: (row["actor_id"], row["actor_role"])
        for row in withdrawn_confirmations
    }
    renewals_match = (
        len(acknowledgement_renewals) == len(withdrawn_confirmations)
        and {row["withdrawn_confirmation_id"] for row in acknowledgement_renewals}
        == set(withdrawn_parties)
        and all(
            acknowledgement_ids.get(withdrawn_parties[row["withdrawn_confirmation_id"]])
            == row["acknowledgement_id"]
            for row in acknowledgement_renewals
        )
    )
    if (
        not round_matches
        or not acknowledgements_match
        or not confirmations_match
        or not renewals_match
    ):
        raise FormalizationWorkflowError("formal_integrity_invalid")
    return {
        "case": case,
        "candidate": candidate,
        "round": round_row,
        "acknowledgements": acknowledgements,
        "acknowledgement_renewals": acknowledgement_renewals,
        "confirmations": confirmations,
    }


def _formal_snapshot_token(view: dict[str, object]) -> tuple[object, ...]:
    case = view["case"]
    candidate = view["candidate"]
    round_row = view["round"]
    acknowledgements = view["acknowledgements"]
    acknowledgement_renewals = view["acknowledgement_renewals"]
    confirmations = view["confirmations"]
    case_token = (
        case["id"], case["owner_id"], case["contractor_id"], case["title"],
        case["state"], case["active_candidate_id"], case["formal_candidate_id"],
    )
    candidate_token = (
        candidate["id"], candidate["case_id"], candidate["version_no"],
        candidate["state"], candidate["review_revision"], *_binding_values(candidate),
    )
    round_token = (
        round_row["id"], round_row["case_id"], round_row["candidate_id"],
        round_row["revision"], round_row["state"], round_row["manifest_sha256"],
        round_row["pdf_sha256"], round_row["svg_sha256"],
        round_row["json_a_sha256"], round_row["json_b_sha256"],
    )
    acknowledgement_token = tuple(
        (
            row["id"], row["actor_id"], row["actor_role"],
            *_binding_values(row), row["acknowledged_at"],
        )
        for row in acknowledgements
    )
    acknowledgement_renewal_token = tuple(
        (
            row["id"], row["acknowledgement_id"], row["withdrawn_confirmation_id"],
            row["renewal_kind"], row["actor_id"], row["actor_role"], row["renewed_at"],
        )
        for row in acknowledgement_renewals
    )
    confirmation_token = tuple(
        (
            row["id"], row["actor_id"], row["actor_role"], row["state"],
            *_binding_values(row), row["confirmed_at"], row["formalized_at"],
        )
        for row in confirmations
    )
    return (
        case_token,
        candidate_token,
        round_token,
        acknowledgement_token,
        acknowledgement_renewal_token,
        confirmation_token,
    )


def revalidate_formal_snapshot(
    connection: sqlite3.Connection,
    case_id: str,
    actor_id: str,
    expected: dict[str, object],
) -> dict[str, object]:
    current = _load_formal_db_view(connection, case_id, actor_id)
    if _formal_snapshot_token(current) != _formal_snapshot_token(expected):
        raise FormalizationWorkflowError("formal_integrity_invalid")
    return current


def load_formal_version(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    actor_id: str,
) -> dict[str, object]:
    view = _load_formal_db_view(connection, case_id, actor_id)
    candidate, files = _load_verified_bundle(
        connection, data_root, case_id, view["case"]["formal_candidate_id"], actor_id, assets
    )
    if _binding_values(candidate) != _binding_values(view["candidate"]):
        raise FormalizationWorkflowError("formal_integrity_invalid")
    return {**view, "candidate": candidate, "files": files}


def read_formal_source(
    connection: sqlite3.Connection,
    data_root: Path,
    assets: DocumentAssets,
    case_id: str,
    actor_id: str,
    source_kind: str,
) -> tuple[sqlite3.Row, bytes]:
    source_contracts = {
        "json_a": ("json_a_artifact_id", "json_a_sha256", "json_a_byte_length"),
        "json_b": ("json_b_artifact_id", "json_b_sha256", "json_b_byte_length"),
    }
    contract = source_contracts.get(source_kind)
    if contract is None:
        raise FormalizationWorkflowError("formal_unavailable")
    formal = load_formal_version(connection, data_root, assets, case_id, actor_id)
    candidate = formal["candidate"]
    artifact_id_column, sha_column, length_column = contract
    artifact = connection.execute(
        "SELECT id, relative_path, sha256, byte_length FROM artifacts "
        "WHERE id = ? AND case_id = ? AND kind = ? "
        "AND visibility = 'party_only' AND lifecycle = 'formal'",
        (candidate[artifact_id_column], case_id, source_kind),
    ).fetchone()
    if (
        artifact is None
        or artifact["sha256"] != candidate[sha_column]
        or artifact["byte_length"] != candidate[length_column]
    ):
        raise FormalizationWorkflowError("formal_integrity_invalid")
    try:
        raw = read_verified(
            data_root,
            artifact["relative_path"],
            artifact["sha256"],
            artifact["byte_length"],
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
        )
    except (ArtifactStoreError, OSError, ValueError) as error:
        raise FormalizationWorkflowError("formal_integrity_invalid") from error
    return artifact, raw


def create_separate_proposal(
    connection: sqlite3.Connection,
    parent_case_id: str,
    actor_id: str,
    idempotency_key: str,
) -> str:
    if not 16 <= len(idempotency_key) <= 200:
        raise FormalizationWorkflowError("idempotency_key_invalid")
    with immediate_transaction(connection):
        _actor(connection, actor_id)
        parent = connection.execute(
            "SELECT id, owner_id, title, state, formal_candidate_id FROM cases "
            "WHERE id = ? AND owner_id = ?",
            (parent_case_id, actor_id),
        ).fetchone()
        if parent is None or parent["state"] != "FORMAL" or parent["formal_candidate_id"] is None:
            raise FormalizationWorkflowError("formal_unavailable")
        existing = connection.execute(
            "SELECT child_case_id FROM separate_proposal_requests "
            "WHERE parent_case_id = ? AND idempotency_key = ?",
            (parent_case_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            return existing["child_case_id"]
        child_case_id = str(uuid.uuid4())
        now = _now()
        title = "Separate Proposal"
        initial = {key: {} for key in SECTION_KEYS}
        connection.execute(
            "INSERT INTO cases(id, owner_id, parent_case_id, title, state, content_revision, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'REQUIREMENT_DRAFT', 1, ?, ?)",
            (child_case_id, actor_id, parent_case_id, title, now, now),
        )
        connection.execute(
            "INSERT INTO requirement_drafts(case_id, data_json, completed_sections_json, updated_at) "
            "VALUES (?, ?, '[]', ?)",
            (child_case_id, json.dumps(initial, ensure_ascii=False), now),
        )
        connection.execute(
            "INSERT INTO separate_proposal_requests(id, parent_case_id, child_case_id, created_by_id, "
            "idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), parent_case_id, child_case_id, actor_id, idempotency_key, now),
        )
        _audit(connection, actor_id, parent_case_id, child_case_id, "separate_proposal_started")
        return child_case_id
