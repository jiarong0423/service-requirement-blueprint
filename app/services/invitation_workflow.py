from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.db import deferred_read_transaction, immediate_transaction
from app.services.artifact_store import ArtifactStoreError, MAX_JSON_ARTIFACT_BYTES, read_verified
from app.services.json_contracts import JsonContractError, decode_strict_json, sha256_bytes, validate_json_a


INVITATION_TTL = timedelta(days=7)
_TOKEN_BYTES = 32
_TOKEN_MIN_LENGTH = 40
_TOKEN_MAX_LENGTH = 128
_TOKEN_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class InvitationWorkflowError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class InvitationIssue:
    invitation_id: str
    code: str
    expires_at: str
    source_artifact_id: str
    source_sha256: str


@dataclass(frozen=True)
class InvitationPreview:
    invitation_id: str
    case_id: str
    case_title: str
    owner_display_name: str
    source_version_no: int
    source_sha256: str
    expires_at: str


@dataclass(frozen=True)
class ImportResult:
    invitation_id: str
    case_id: str
    artifact_id: str
    sha256: str
    already_verified: bool


def _utc_now(now: datetime | None) -> datetime:
    value = now if now is not None else datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("server_time_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise InvitationWorkflowError("stored_time_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvitationWorkflowError("stored_time_invalid")
    return parsed.astimezone(timezone.utc)


def _token_hash(code: str) -> str:
    if (
        not isinstance(code, str)
        or not _TOKEN_MIN_LENGTH <= len(code) <= _TOKEN_MAX_LENGTH
        or any(character not in _TOKEN_ALPHABET for character in code)
    ):
        raise InvitationWorkflowError("invitation_unavailable")
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _new_token(connection: sqlite3.Connection) -> tuple[str, str]:
    for _attempt in range(4):
        code = secrets.token_urlsafe(_TOKEN_BYTES)
        digest = _token_hash(code)
        if connection.execute("SELECT 1 FROM invitations WHERE token_hash = ?", (digest,)).fetchone() is None:
            return code, digest
    raise InvitationWorkflowError("token_generation_failed")


def _validated_document(raw: bytes, schema_path: Path) -> dict[str, Any]:
    try:
        document = decode_strict_json(raw)
        if not isinstance(document, dict):
            raise JsonContractError("schema_validation_failed")
        validate_json_a(document, schema_path)
    except JsonContractError as error:
        raise InvitationWorkflowError("json_a_invalid") from error
    return document


def _current_source(connection: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT c.id AS case_id, c.owner_id, c.contractor_id, c.title, c.state, "
        "c.content_revision, c.current_json_a_artifact_id, "
        "a.id AS artifact_id, a.author_id, a.kind, a.version_no, a.visibility, a.lifecycle, "
        "a.relative_path, a.sha256, a.byte_length, a.schema_version "
        "FROM cases AS c LEFT JOIN artifacts AS a ON a.id = c.current_json_a_artifact_id "
        "WHERE c.id = ?",
        (case_id,),
    ).fetchone()
    if (
        row is None
        or row["artifact_id"] is None
        or row["kind"] != "json_a"
        or row["lifecycle"] != "active"
        or row["author_id"] != row["owner_id"]
    ):
        raise InvitationWorkflowError("source_unavailable")
    return row


def _verify_exact_source(
    source: sqlite3.Row,
    data_root: Path,
    schema_path: Path,
    raw: bytes,
    document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checked_document = document if document is not None else _validated_document(raw, schema_path)
    try:
        stored = read_verified(
            data_root, source["relative_path"], source["sha256"], source["byte_length"],
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
        )
    except (ArtifactStoreError, OSError, ValueError) as error:
        raise InvitationWorkflowError("source_artifact_invalid") from error
    supplied_digest = sha256_bytes(raw)
    if (
        len(stored) != source["byte_length"]
        or len(raw) != source["byte_length"]
        or not hmac.compare_digest(supplied_digest, source["sha256"])
        or not hmac.compare_digest(raw, stored)
        or checked_document.get("schema_version") != source["schema_version"]
        or checked_document.get("document_id") != source["artifact_id"]
        or checked_document.get("version") != source["version_no"]
    ):
        raise InvitationWorkflowError("json_a_mismatch")
    return checked_document


def _active_member(connection: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, display_name FROM users "
        "WHERE id = ? AND status = 'active' AND system_role = 'member'",
        (user_id,),
    ).fetchone()


def _source_matches_invitation(source: sqlite3.Row, invitation: sqlite3.Row) -> bool:
    return bool(
        source["artifact_id"] == invitation["source_artifact_id"]
        and source["version_no"] == invitation["source_version_no"]
        and hmac.compare_digest(source["sha256"], invitation["source_sha256"])
        and source["byte_length"] == invitation["source_byte_length"]
        and source["schema_version"] == invitation["source_schema_version"]
        and source["content_revision"] == invitation["source_content_revision"]
    )


def _invalidate_stale_workflow_for_reissue(
    connection: sqlite3.Connection,
    case: sqlite3.Row,
    now_text: str,
) -> sqlite3.Row:
    if case["state"] == "INVITATION_PENDING":
        pending = connection.execute(
            "SELECT * FROM invitations WHERE case_id = ? AND state = 'pending' ORDER BY created_at DESC LIMIT 1",
            (case["case_id"],),
        ).fetchone()
        if pending is None:
            raise InvitationWorkflowError("workflow_state_invalid")
        if _source_matches_invitation(case, pending):
            if _parse_utc(pending["expires_at"]) <= _parse_utc(now_text):
                _mark_expired(connection, pending, now_text)
                return _current_source(connection, case["case_id"])
            raise InvitationWorkflowError("invitation_exists")
        connection.execute(
            "UPDATE invitations SET state = 'revoked', revoked_at = ?, updated_at = ?, "
            "source_invalidated_at = ?, source_invalidation_reason = 'source_replaced' "
            "WHERE id = ? AND state = 'pending'",
            (now_text, now_text, now_text, pending["id"]),
        )
        connection.execute(
            "UPDATE cases SET state = 'REQUIREMENT_DRAFT', contractor_id = NULL, updated_at = ? WHERE id = ?",
            (now_text, case["case_id"]),
        )
        return _current_source(connection, case["case_id"])
    if case["state"] == "WAITING_JSON_A_IMPORT":
        accepted = connection.execute(
            "SELECT * FROM invitations WHERE case_id = ? AND state = 'accepted' "
            "AND source_invalidated_at IS NULL ORDER BY accepted_at DESC LIMIT 1",
            (case["case_id"],),
        ).fetchone()
        if accepted is None or _source_matches_invitation(case, accepted):
            raise InvitationWorkflowError("workflow_state_invalid")
        verified = connection.execute(
            "SELECT 1 FROM json_a_source_verifications "
            "WHERE invitation_id = ? AND actor_role = 'contractor'",
            (accepted["id"],),
        ).fetchone()
        if verified is not None:
            raise InvitationWorkflowError("workflow_state_invalid")
        connection.execute(
            "UPDATE invitations SET source_invalidated_at = ?, source_invalidation_reason = 'source_replaced', "
            "updated_at = ? WHERE id = ? AND source_invalidated_at IS NULL",
            (now_text, now_text, accepted["id"]),
        )
        connection.execute(
            "UPDATE cases SET state = 'REQUIREMENT_DRAFT', contractor_id = NULL, updated_at = ? WHERE id = ?",
            (now_text, case["case_id"]),
        )
        return _current_source(connection, case["case_id"])
    return case


def create_invitation(
    connection: sqlite3.Connection,
    data_root: Path,
    schema_path: Path,
    case_id: str,
    owner_id: str,
    designated_user_id: str,
    uploaded_json_a: bytes,
    *,
    now: datetime | None = None,
) -> InvitationIssue:
    document = _validated_document(uploaded_json_a, schema_path)
    current_time = _utc_now(now)
    now_text = _utc_text(current_time)
    expires_at = _utc_text(current_time + INVITATION_TTL)
    result: InvitationIssue | None = None
    with immediate_transaction(connection):
        if _active_member(connection, owner_id) is None:
            raise InvitationWorkflowError("case_unavailable")
        if designated_user_id == owner_id or _active_member(connection, designated_user_id) is None:
            raise InvitationWorkflowError("designated_account_unavailable")
        source = _current_source(connection, case_id)
        if source["owner_id"] != owner_id or source["state"] == "CASE_ACTIVE":
            raise InvitationWorkflowError("case_unavailable")
        source = _invalidate_stale_workflow_for_reissue(connection, source, now_text)
        if source["state"] != "REQUIREMENT_DRAFT" or source["contractor_id"] is not None:
            raise InvitationWorkflowError("workflow_state_invalid")
        _verify_exact_source(source, data_root, schema_path, uploaded_json_a, document)
        code, digest = _new_token(connection)
        invitation_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO invitations("
            "id, case_id, owner_id, designated_user_id, source_artifact_id, source_version_no, "
            "source_sha256, source_byte_length, source_schema_version, source_document_id, "
            "source_content_revision, token_hash, state, created_at, expires_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                invitation_id,
                case_id,
                owner_id,
                designated_user_id,
                source["artifact_id"],
                source["version_no"],
                source["sha256"],
                source["byte_length"],
                source["schema_version"],
                document["document_id"],
                source["content_revision"],
                digest,
                now_text,
                expires_at,
                now_text,
            ),
        )
        connection.execute(
            "INSERT INTO json_a_source_verifications("
            "id, invitation_id, case_id, actor_id, actor_role, artifact_id, supplied_sha256, "
            "supplied_byte_length, schema_version, document_id, document_version, verified_at"
            ") VALUES (?, ?, ?, ?, 'owner', ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                invitation_id,
                case_id,
                owner_id,
                source["artifact_id"],
                sha256_bytes(uploaded_json_a),
                len(uploaded_json_a),
                document["schema_version"],
                document["document_id"],
                document["version"],
                now_text,
            ),
        )
        updated = connection.execute(
            "UPDATE cases SET state = 'INVITATION_PENDING', updated_at = ? "
            "WHERE id = ? AND owner_id = ? AND state = 'REQUIREMENT_DRAFT' AND contractor_id IS NULL",
            (now_text, case_id, owner_id),
        )
        if updated.rowcount != 1:
            raise InvitationWorkflowError("workflow_state_invalid")
        result = InvitationIssue(invitation_id, code, expires_at, source["artifact_id"], source["sha256"])
    if result is None:
        raise InvitationWorkflowError("workflow_state_invalid")
    return result


def _token_invitation(
    connection: sqlite3.Connection,
    invitation_id: str,
    code: str,
    designated_user_id: str,
) -> sqlite3.Row | None:
    try:
        digest = _token_hash(code)
    except InvitationWorkflowError:
        return None
    return connection.execute(
        "SELECT i.*, c.title AS case_title, c.state AS case_state, c.contractor_id, "
        "c.current_json_a_artifact_id, c.content_revision, owner.display_name AS owner_display_name "
        "FROM invitations AS i "
        "JOIN cases AS c ON c.id = i.case_id "
        "JOIN users AS designated ON designated.id = i.designated_user_id "
        "JOIN users AS owner ON owner.id = i.owner_id "
        "WHERE i.id = ? AND i.token_hash = ? AND i.designated_user_id = ? "
        "AND designated.status = 'active' AND designated.system_role = 'member' "
        "AND owner.status = 'active' AND owner.system_role = 'member'",
        (invitation_id, digest, designated_user_id),
    ).fetchone()


def preview_invitation(
    connection: sqlite3.Connection,
    invitation_id: str,
    code: str,
    designated_user_id: str,
    *,
    now: datetime | None = None,
) -> InvitationPreview:
    current_time = _utc_now(now)
    invitation = _token_invitation(connection, invitation_id, code, designated_user_id)
    if (
        invitation is None
        or invitation["state"] != "pending"
        or invitation["source_invalidated_at"] is not None
        or current_time >= _parse_utc(invitation["expires_at"])
        or invitation["case_state"] != "INVITATION_PENDING"
        or invitation["contractor_id"] is not None
        or invitation["current_json_a_artifact_id"] != invitation["source_artifact_id"]
        or invitation["content_revision"] != invitation["source_content_revision"]
    ):
        raise InvitationWorkflowError("invitation_unavailable")
    return InvitationPreview(
        invitation["id"],
        invitation["case_id"],
        invitation["case_title"],
        invitation["owner_display_name"],
        invitation["source_version_no"],
        invitation["source_sha256"],
        invitation["expires_at"],
    )


def _mark_expired(connection: sqlite3.Connection, invitation: sqlite3.Row, now_text: str) -> None:
    connection.execute(
        "UPDATE invitations SET state = 'expired', expired_at = ?, updated_at = ? "
        "WHERE id = ? AND state = 'pending'",
        (now_text, now_text, invitation["id"]),
    )
    connection.execute(
        "UPDATE cases SET state = 'REQUIREMENT_DRAFT', contractor_id = NULL, updated_at = ? "
        "WHERE id = ? AND state = 'INVITATION_PENDING'",
        (now_text, invitation["case_id"]),
    )


def _mark_stale_pending(connection: sqlite3.Connection, invitation: sqlite3.Row, now_text: str) -> None:
    connection.execute(
        "UPDATE invitations SET state = 'revoked', revoked_at = ?, updated_at = ?, "
        "source_invalidated_at = ?, source_invalidation_reason = 'source_replaced' "
        "WHERE id = ? AND state = 'pending'",
        (now_text, now_text, now_text, invitation["id"]),
    )
    connection.execute(
        "UPDATE cases SET state = 'REQUIREMENT_DRAFT', contractor_id = NULL, updated_at = ? "
        "WHERE id = ? AND state = 'INVITATION_PENDING'",
        (now_text, invitation["case_id"]),
    )


def accept_invitation(
    connection: sqlite3.Connection,
    data_root: Path,
    invitation_id: str,
    code: str,
    designated_user_id: str,
    *,
    now: datetime | None = None,
) -> InvitationPreview:
    current_time = _utc_now(now)
    now_text = _utc_text(current_time)
    outcome: str | None = None
    preview: InvitationPreview | None = None
    with deferred_read_transaction(connection):
        invitation = _token_invitation(connection, invitation_id, code, designated_user_id)
        if invitation is None or invitation["state"] != "pending" or invitation["source_invalidated_at"] is not None:
            raise InvitationWorkflowError("invitation_unavailable")
        invitation_token = tuple(invitation)
        source = None
        source_token = None
        if current_time < _parse_utc(invitation["expires_at"]):
            source = _current_source(connection, invitation["case_id"])
            source_token = tuple(source)
            if (
                invitation["case_state"] == "INVITATION_PENDING"
                and invitation["contractor_id"] is None
                and _source_matches_invitation(source, invitation)
            ):
                try:
                    read_verified(
                        data_root, source["relative_path"], source["sha256"], source["byte_length"],
                        max_bytes=MAX_JSON_ARTIFACT_BYTES,
                    )
                except (ArtifactStoreError, OSError, ValueError) as error:
                    raise InvitationWorkflowError("source_artifact_invalid") from error
    with immediate_transaction(connection):
        locked_invitation = _token_invitation(connection, invitation_id, code, designated_user_id)
        if (
            locked_invitation is None
            or tuple(locked_invitation) != invitation_token
            or locked_invitation["state"] != "pending"
            or locked_invitation["source_invalidated_at"] is not None
        ):
            raise InvitationWorkflowError("invitation_unavailable")
        if current_time >= _parse_utc(locked_invitation["expires_at"]):
            _mark_expired(connection, locked_invitation, now_text)
            outcome = "invitation_expired"
        else:
            locked_source = _current_source(connection, locked_invitation["case_id"])
            if (
                source_token is None
                or tuple(locked_source) != source_token
                or locked_invitation["case_state"] != "INVITATION_PENDING"
                or locked_invitation["contractor_id"] is not None
                or not _source_matches_invitation(locked_source, locked_invitation)
            ):
                _mark_stale_pending(connection, locked_invitation, now_text)
                outcome = "invitation_unavailable"
            else:
                accepted = connection.execute(
                    "UPDATE invitations SET state = 'accepted', accepted_at = ?, updated_at = ? "
                    "WHERE id = ? AND state = 'pending'",
                    (now_text, now_text, invitation_id),
                )
                bound = connection.execute(
                    "UPDATE cases SET contractor_id = ?, state = 'WAITING_JSON_A_IMPORT', updated_at = ? "
                    "WHERE id = ? AND state = 'INVITATION_PENDING' AND contractor_id IS NULL",
                    (designated_user_id, now_text, locked_invitation["case_id"]),
                )
                if accepted.rowcount != 1 or bound.rowcount != 1:
                    raise InvitationWorkflowError("invitation_unavailable")
                preview = InvitationPreview(
                    locked_invitation["id"],
                    locked_invitation["case_id"],
                    locked_invitation["case_title"],
                    locked_invitation["owner_display_name"],
                    locked_invitation["source_version_no"],
                    locked_invitation["source_sha256"],
                    locked_invitation["expires_at"],
                )
    if outcome is not None:
        raise InvitationWorkflowError(outcome)
    if preview is None:
        raise InvitationWorkflowError("invitation_unavailable")
    return preview


def reject_invitation(
    connection: sqlite3.Connection,
    invitation_id: str,
    code: str,
    designated_user_id: str,
    *,
    now: datetime | None = None,
) -> None:
    current_time = _utc_now(now)
    now_text = _utc_text(current_time)
    outcome: str | None = None
    with immediate_transaction(connection):
        invitation = _token_invitation(connection, invitation_id, code, designated_user_id)
        if invitation is None or invitation["state"] != "pending" or invitation["source_invalidated_at"] is not None:
            raise InvitationWorkflowError("invitation_unavailable")
        if current_time >= _parse_utc(invitation["expires_at"]):
            _mark_expired(connection, invitation, now_text)
            outcome = "invitation_expired"
        elif invitation["case_state"] != "INVITATION_PENDING" or invitation["contractor_id"] is not None:
            raise InvitationWorkflowError("invitation_unavailable")
        else:
            connection.execute(
                "UPDATE invitations SET state = 'rejected', rejected_at = ?, updated_at = ? "
                "WHERE id = ? AND state = 'pending'",
                (now_text, now_text, invitation_id),
            )
            connection.execute(
                "UPDATE cases SET state = 'REQUIREMENT_DRAFT', updated_at = ? "
                "WHERE id = ? AND state = 'INVITATION_PENDING' AND contractor_id IS NULL",
                (now_text, invitation["case_id"]),
            )
    if outcome is not None:
        raise InvitationWorkflowError(outcome)


def revoke_invitation(
    connection: sqlite3.Connection,
    case_id: str,
    invitation_id: str,
    owner_id: str,
    *,
    now: datetime | None = None,
) -> None:
    current_time = _utc_now(now)
    now_text = _utc_text(current_time)
    outcome: str | None = None
    with immediate_transaction(connection):
        invitation = connection.execute(
            "SELECT i.*, c.state AS case_state, c.contractor_id "
            "FROM invitations AS i JOIN cases AS c ON c.id = i.case_id "
            "JOIN users AS owner ON owner.id = i.owner_id "
            "WHERE i.id = ? AND i.case_id = ? AND i.owner_id = ? "
            "AND owner.status = 'active' AND owner.system_role = 'member'",
            (invitation_id, case_id, owner_id),
        ).fetchone()
        if invitation is None or invitation["state"] != "pending":
            raise InvitationWorkflowError("invitation_unavailable")
        if current_time >= _parse_utc(invitation["expires_at"]):
            _mark_expired(connection, invitation, now_text)
            outcome = "invitation_expired"
        elif invitation["case_state"] != "INVITATION_PENDING" or invitation["contractor_id"] is not None:
            raise InvitationWorkflowError("invitation_unavailable")
        else:
            connection.execute(
                "UPDATE invitations SET state = 'revoked', revoked_at = ?, updated_at = ? "
                "WHERE id = ? AND state = 'pending'",
                (now_text, now_text, invitation_id),
            )
            connection.execute(
                "UPDATE cases SET state = 'REQUIREMENT_DRAFT', updated_at = ? "
                "WHERE id = ? AND state = 'INVITATION_PENDING' AND contractor_id IS NULL",
                (now_text, invitation["case_id"]),
            )
    if outcome is not None:
        raise InvitationWorkflowError(outcome)


def cancel_accepted_invitation_before_import(
    connection: sqlite3.Connection,
    case_id: str,
    invitation_id: str,
    owner_id: str,
    *,
    now: datetime | None = None,
) -> None:
    now_text = _utc_text(_utc_now(now))
    with immediate_transaction(connection):
        invitation = connection.execute(
            "SELECT i.*, c.state AS case_state, c.contractor_id, "
            "c.current_json_a_artifact_id, c.content_revision "
            "FROM invitations AS i JOIN cases AS c ON c.id = i.case_id "
            "JOIN users AS owner ON owner.id = i.owner_id "
            "WHERE i.id = ? AND i.case_id = ? AND i.owner_id = ? "
            "AND owner.status = 'active' AND owner.system_role = 'member'",
            (invitation_id, case_id, owner_id),
        ).fetchone()
        if (
            invitation is None
            or invitation["state"] != "accepted"
            or invitation["source_invalidated_at"] is not None
            or invitation["case_state"] != "WAITING_JSON_A_IMPORT"
            or invitation["contractor_id"] != invitation["designated_user_id"]
            or invitation["current_json_a_artifact_id"] != invitation["source_artifact_id"]
            or invitation["content_revision"] != invitation["source_content_revision"]
            or connection.execute(
                "SELECT 1 FROM json_a_source_verifications "
                "WHERE invitation_id = ? AND actor_role = 'contractor'",
                (invitation_id,),
            ).fetchone() is not None
        ):
            raise InvitationWorkflowError("invitation_unavailable")
        invalidated = connection.execute(
            "UPDATE invitations SET source_invalidated_at = ?, "
            "source_invalidation_reason = 'owner_cancelled_before_import', updated_at = ? "
            "WHERE id = ? AND case_id = ? AND owner_id = ? "
            "AND state = 'accepted' AND source_invalidated_at IS NULL",
            (now_text, now_text, invitation_id, case_id, owner_id),
        )
        released = connection.execute(
            "UPDATE cases SET state = 'REQUIREMENT_DRAFT', contractor_id = NULL, updated_at = ? "
            "WHERE id = ? AND owner_id = ? AND state = 'WAITING_JSON_A_IMPORT' "
            "AND contractor_id = ? AND current_json_a_artifact_id = ? AND content_revision = ?",
            (
                now_text,
                case_id,
                owner_id,
                invitation["designated_user_id"],
                invitation["source_artifact_id"],
                invitation["source_content_revision"],
            ),
        )
        if invalidated.rowcount != 1 or released.rowcount != 1:
            raise InvitationWorkflowError("workflow_state_invalid")


def import_json_a(
    connection: sqlite3.Connection,
    data_root: Path,
    schema_path: Path,
    invitation_id: str,
    contractor_id: str,
    uploaded_json_a: bytes,
    *,
    now: datetime | None = None,
) -> ImportResult:
    document = _validated_document(uploaded_json_a, schema_path)
    now_text = _utc_text(_utc_now(now))
    result: ImportResult | None = None
    stale_source = False
    with immediate_transaction(connection):
        invitation = connection.execute(
            "SELECT i.*, c.state AS case_state, c.contractor_id, c.current_json_a_artifact_id, "
            "c.content_revision, designated.status AS designated_status, "
            "designated.system_role AS designated_role "
            "FROM invitations AS i JOIN cases AS c ON c.id = i.case_id "
            "JOIN users AS designated ON designated.id = i.designated_user_id "
            "WHERE i.id = ? AND i.designated_user_id = ?",
            (invitation_id, contractor_id),
        ).fetchone()
        if (
            invitation is None
            or invitation["state"] != "accepted"
            or invitation["source_invalidated_at"] is not None
            or invitation["designated_status"] != "active"
            or invitation["designated_role"] != "member"
            or invitation["contractor_id"] != contractor_id
        ):
            raise InvitationWorkflowError("case_unavailable")
        source = _current_source(connection, invitation["case_id"])
        if not _source_matches_invitation(source, invitation):
            connection.execute(
                "UPDATE invitations SET source_invalidated_at = ?, source_invalidation_reason = 'source_replaced', "
                "updated_at = ? WHERE id = ? AND source_invalidated_at IS NULL",
                (now_text, now_text, invitation_id),
            )
            connection.execute(
                "UPDATE cases SET contractor_id = NULL, state = 'REQUIREMENT_DRAFT', updated_at = ? "
                "WHERE id = ? AND contractor_id = ? AND state = 'WAITING_JSON_A_IMPORT'",
                (now_text, invitation["case_id"], contractor_id),
            )
            stale_source = True
        else:
            checked = _verify_exact_source(source, data_root, schema_path, uploaded_json_a, document)
            existing = connection.execute(
                "SELECT id FROM json_a_source_verifications "
                "WHERE invitation_id = ? AND actor_role = 'contractor'",
                (invitation_id,),
            ).fetchone()
            if existing is not None:
                if invitation["case_state"] != "CASE_ACTIVE" or source["visibility"] != "party_only":
                    raise InvitationWorkflowError("workflow_state_invalid")
                result = ImportResult(invitation_id, invitation["case_id"], source["artifact_id"], source["sha256"], True)
            else:
                if invitation["case_state"] != "WAITING_JSON_A_IMPORT":
                    raise InvitationWorkflowError("case_unavailable")
                connection.execute(
                    "INSERT INTO json_a_source_verifications("
                    "id, invitation_id, case_id, actor_id, actor_role, artifact_id, supplied_sha256, "
                    "supplied_byte_length, schema_version, document_id, document_version, verified_at"
                    ") VALUES (?, ?, ?, ?, 'contractor', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        invitation_id,
                        invitation["case_id"],
                        contractor_id,
                        source["artifact_id"],
                        sha256_bytes(uploaded_json_a),
                        len(uploaded_json_a),
                        checked["schema_version"],
                        checked["document_id"],
                        checked["version"],
                        now_text,
                    ),
                )
                artifact_update = connection.execute(
                    "UPDATE artifacts SET visibility = 'party_only' "
                    "WHERE id = ? AND case_id = ? AND kind = 'json_a' AND lifecycle = 'active' "
                    "AND sha256 = ? AND byte_length = ?",
                    (
                        source["artifact_id"],
                        invitation["case_id"],
                        source["sha256"],
                        source["byte_length"],
                    ),
                )
                case_update = connection.execute(
                    "UPDATE cases SET state = 'CASE_ACTIVE', updated_at = ? "
                    "WHERE id = ? AND contractor_id = ? AND state = 'WAITING_JSON_A_IMPORT' "
                    "AND current_json_a_artifact_id = ? AND content_revision = ?",
                    (
                        now_text,
                        invitation["case_id"],
                        contractor_id,
                        source["artifact_id"],
                        source["content_revision"],
                    ),
                )
                if artifact_update.rowcount != 1 or case_update.rowcount != 1:
                    raise InvitationWorkflowError("workflow_state_invalid")
                result = ImportResult(invitation_id, invitation["case_id"], source["artifact_id"], source["sha256"], False)
    if stale_source:
        raise InvitationWorkflowError("source_replaced")
    if result is None:
        raise InvitationWorkflowError("case_unavailable")
    return result
