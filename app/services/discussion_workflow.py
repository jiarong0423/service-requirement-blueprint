from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from app.db import immediate_transaction
from app.services.case_workflow import utc_now


ACTIVE_CASE_STATES = ("CASE_ACTIVE", "JSON_B_PRIVATE_DRAFT", "JSON_B_REVIEW")


class DiscussionWorkflowError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MessageResult:
    message_id: str
    created: bool


def _party(
    connection: sqlite3.Connection,
    case_id: str,
    actor_id: str,
    *,
    allowed_states: tuple[str, ...] = ACTIVE_CASE_STATES,
) -> tuple[sqlite3.Row, str]:
    actor = connection.execute(
        "SELECT status, system_role FROM users WHERE id = ?", (actor_id,)
    ).fetchone()
    if actor is None or actor["status"] != "active" or actor["system_role"] != "member":
        raise DiscussionWorkflowError("case_unavailable")
    placeholders = ",".join("?" for _ in allowed_states)
    row = connection.execute(
        "SELECT c.* FROM cases AS c "
        f"WHERE c.id = ? AND c.state IN ({placeholders}) "
        "AND (c.owner_id = ? OR c.contractor_id = ?)",
        (case_id, *allowed_states, actor_id, actor_id),
    ).fetchone()
    if row is None:
        raise DiscussionWorkflowError("case_unavailable")
    role = "client" if row["owner_id"] == actor_id else "contractor"
    return row, role


def _validated_uuid(value: str, code: str) -> str:
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError) as error:
        raise DiscussionWorkflowError(code) from error
    if parsed != value:
        raise DiscussionWorkflowError(code)
    return value


def post_message(
    connection: sqlite3.Connection,
    case_id: str,
    actor_id: str,
    body: str,
    idempotency_key: str,
    *,
    correction_of_id: str | None = None,
) -> MessageResult:
    if not body.strip() or len(body) > 4_000:
        raise DiscussionWorkflowError("message_invalid")
    _validated_uuid(idempotency_key, "message_invalid")
    if correction_of_id is not None:
        _validated_uuid(correction_of_id, "message_invalid")
    with immediate_transaction(connection):
        _case, role = _party(connection, case_id, actor_id)
        existing = connection.execute(
            "SELECT id, body, correction_of_id FROM case_messages "
            "WHERE case_id = ? AND author_id = ? AND idempotency_key = ?",
            (case_id, actor_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["body"] != body or existing["correction_of_id"] != correction_of_id:
                raise DiscussionWorkflowError("idempotency_conflict")
            return MessageResult(existing["id"], False)
        if correction_of_id is not None:
            original = connection.execute(
                "SELECT id FROM case_messages WHERE id = ? AND case_id = ? AND author_id = ?",
                (correction_of_id, case_id, actor_id),
            ).fetchone()
            if original is None:
                raise DiscussionWorkflowError("message_invalid")
        message_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO case_messages(id, case_id, author_id, author_role, issue_id, "
            "correction_of_id, idempotency_key, body, server_created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                message_id,
                case_id,
                actor_id,
                role,
                correction_of_id,
                idempotency_key,
                body,
                utc_now(),
            ),
        )
    return MessageResult(message_id, True)


def create_issue(
    connection: sqlite3.Connection,
    case_id: str,
    actor_id: str,
    summary: str,
    *,
    kind: str = "CLARIFICATION",
    source_json_b_artifact_id: str | None = None,
    field_pointer: str | None = None,
) -> str:
    if kind not in {"CLARIFICATION", "JSON_B_OBJECTION"}:
        raise DiscussionWorkflowError("issue_invalid")
    if not summary.strip() or len(summary) > 4_000:
        raise DiscussionWorkflowError("issue_invalid")
    if kind == "JSON_B_OBJECTION" and (source_json_b_artifact_id is None or field_pointer is None):
        raise DiscussionWorkflowError("issue_invalid")
    now = utc_now()
    issue_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    with immediate_transaction(connection):
        case, role = _party(connection, case_id, actor_id)
        if kind == "JSON_B_OBJECTION":
            if role != "client":
                raise DiscussionWorkflowError("issue_unavailable")
            current = connection.execute(
                "SELECT 1 FROM json_b_versions WHERE artifact_id = ? AND case_id = ? "
                "AND state = 'CURRENT'",
                (source_json_b_artifact_id, case_id),
            ).fetchone()
            if current is None:
                raise DiscussionWorkflowError("issue_unavailable")
        connection.execute(
            "INSERT INTO case_issues(id, case_id, kind, source_json_b_artifact_id, field_pointer, "
            "summary, opened_by_id, opened_by_role, state, proposal_role, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL, 1, ?, ?)",
            (
                issue_id,
                case_id,
                kind,
                source_json_b_artifact_id,
                field_pointer,
                summary,
                actor_id,
                role,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO issue_events(id, issue_id, actor_id, actor_role, action, from_state, "
            "to_state, note, related_json_b_artifact_id, issue_revision, server_created_at) "
            "VALUES (?, ?, ?, ?, 'OPENED', NULL, 'OPEN', ?, ?, 1, ?)",
            (event_id, issue_id, actor_id, role, summary, source_json_b_artifact_id, now),
        )
    return issue_id


def transition_issue(
    connection: sqlite3.Connection,
    case_id: str,
    issue_id: str,
    actor_id: str,
    action: str,
    expected_revision: int,
    note: str,
) -> int:
    transitions = {
        ("OPEN", "PROPOSE"): ("PROPOSED", True, False),
        ("REVISION_REQUESTED", "PROPOSE"): ("PROPOSED", True, False),
        ("PROPOSED", "REQUEST_REVISION"): ("REVISION_REQUESTED", False, True),
        ("OUT_OF_SCOPE_PROPOSED", "REQUEST_REVISION"): (
            "REVISION_REQUESTED",
            False,
            True,
        ),
        ("PROPOSED", "ACCEPT"): ("RESOLVED", False, True),
        ("OPEN", "PROPOSE_OUT_OF_SCOPE"): ("OUT_OF_SCOPE_PROPOSED", True, False),
        ("REVISION_REQUESTED", "PROPOSE_OUT_OF_SCOPE"): (
            "OUT_OF_SCOPE_PROPOSED",
            True,
            False,
        ),
        ("OUT_OF_SCOPE_PROPOSED", "ACCEPT_OUT_OF_SCOPE"): (
            "OUT_OF_SCOPE",
            False,
            True,
        ),
    }
    if len(note) > 4_000 or expected_revision < 1:
        raise DiscussionWorkflowError("issue_invalid")
    with immediate_transaction(connection):
        _case, role = _party(connection, case_id, actor_id)
        issue = connection.execute(
            "SELECT * FROM case_issues WHERE id = ? AND case_id = ?",
            (issue_id, case_id),
        ).fetchone()
        if issue is None:
            raise DiscussionWorkflowError("issue_unavailable")
        if issue["revision"] != expected_revision:
            raise DiscussionWorkflowError("issue_conflict")
        transition = transitions.get((issue["state"], action))
        if transition is None:
            raise DiscussionWorkflowError("issue_invalid_transition")
        next_state, records_proposer, requires_other_role = transition
        if requires_other_role and issue["proposal_role"] == role:
            raise DiscussionWorkflowError("issue_requires_other_role")
        next_revision = expected_revision + 1
        proposal_role = role if records_proposer else issue["proposal_role"]
        if next_state == "REVISION_REQUESTED":
            proposal_role = None
        updated = connection.execute(
            "UPDATE case_issues SET state = ?, proposal_role = ?, revision = ?, updated_at = ? "
            "WHERE id = ? AND case_id = ? AND revision = ?",
            (next_state, proposal_role, next_revision, utc_now(), issue_id, case_id, expected_revision),
        )
        if updated.rowcount != 1:
            raise DiscussionWorkflowError("issue_conflict")
        connection.execute(
            "INSERT INTO issue_events(id, issue_id, actor_id, actor_role, action, from_state, "
            "to_state, note, related_json_b_artifact_id, issue_revision, server_created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                str(uuid.uuid4()),
                issue_id,
                actor_id,
                role,
                next_state,
                issue["state"],
                next_state,
                note,
                next_revision,
                utc_now(),
            ),
        )
    return next_revision


def list_collaboration(connection: sqlite3.Connection, case_id: str, actor_id: str) -> dict[str, list[sqlite3.Row]]:
    _party(
        connection,
        case_id,
        actor_id,
        allowed_states=(*ACTIVE_CASE_STATES, "CANDIDATE_REVIEW", "FORMAL"),
    )
    messages = connection.execute(
        "SELECT m.id, m.author_role, m.correction_of_id, m.body, m.server_created_at, "
        "u.display_name AS author_display_name FROM case_messages AS m "
        "JOIN users AS u ON u.id = m.author_id WHERE m.case_id = ? ORDER BY m.server_created_at, m.id",
        (case_id,),
    ).fetchall()
    issues = connection.execute(
        "SELECT id, kind, source_json_b_artifact_id, field_pointer, summary, opened_by_role, "
        "state, proposal_role, revision, created_at, updated_at FROM case_issues "
        "WHERE case_id = ? ORDER BY created_at, id",
        (case_id,),
    ).fetchall()
    issue_events = connection.execute(
        "SELECT e.id, e.issue_id, e.actor_role, e.action, e.from_state, e.to_state, e.note, "
        "e.issue_revision, e.server_created_at, u.display_name AS actor_display_name "
        "FROM issue_events AS e JOIN case_issues AS i ON i.id = e.issue_id "
        "JOIN users AS u ON u.id = e.actor_id WHERE i.case_id = ? "
        "ORDER BY i.created_at, i.id, e.issue_revision",
        (case_id,),
    ).fetchall()
    return {"messages": messages, "issues": issues, "issue_events": issue_events}
