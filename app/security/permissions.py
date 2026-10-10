from __future__ import annotations

from functools import wraps
from collections.abc import Collection
from typing import Callable

from flask import abort, g

from app.db import get_db


def require_admin(handler: Callable[..., object]) -> Callable[..., object]:
    @wraps(handler)
    def wrapped(*args: object, **kwargs: object) -> object:
        if g.user is None or g.user["system_role"] != "admin":
            abort(404)
        return handler(*args, **kwargs)

    return wrapped


def require_member(handler: Callable[..., object]) -> Callable[..., object]:
    """Allow active non-administrator accounts into member-only workflows."""

    @wraps(handler)
    def wrapped(*args: object, **kwargs: object) -> object:
        if g.user is None or g.user["system_role"] != "member":
            abort(404)
        return handler(*args, **kwargs)

    wrapped.requires_authenticated_user = True
    return wrapped


def load_owner_case(case_id: str, allowed_states: Collection[str] | None = None):
    if g.user is None or g.user["system_role"] == "admin":
        abort(404)
    case = get_db().execute(
        "SELECT id, owner_id, contractor_id, title, state, content_revision, current_json_a_artifact_id, "
        "current_json_b_artifact_id, active_candidate_id, formal_candidate_id "
        "FROM cases WHERE id = ? AND owner_id = ?",
        (case_id, g.user["id"]),
    ).fetchone()
    if case is None or (allowed_states is not None and case["state"] not in allowed_states):
        abort(404)
    return case


def load_case_party(
    case_id: str,
    *,
    allowed_roles: Collection[str] = ("client", "contractor"),
    allowed_states: Collection[str] | None = None,
):
    """Return the case and current user's explicit case role.

    Administrator status is rejected before any case-table query. Case access is
    therefore granted only by the live owner/contractor binding in SQLite.
    """
    if g.user is None or g.user["system_role"] == "admin":
        abort(404)
    case = get_db().execute(
        "SELECT id, owner_id, contractor_id, title, state, content_revision, "
        "current_json_a_artifact_id, current_json_b_artifact_id, active_candidate_id, "
        "formal_candidate_id FROM cases "
        "WHERE id = ? AND (owner_id = ? OR contractor_id = ?)",
        (case_id, g.user["id"], g.user["id"]),
    ).fetchone()
    if case is None:
        abort(404)
    role = "client" if case["owner_id"] == g.user["id"] else "contractor"
    if role not in allowed_roles or (allowed_states is not None and case["state"] not in allowed_states):
        abort(404)
    return case, role
