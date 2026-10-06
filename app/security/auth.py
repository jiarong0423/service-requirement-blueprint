from __future__ import annotations

import threading
from typing import Callable

from flask import g, session
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_db


KDF_CONCURRENCY_LIMIT = 2
_KDF_SLOTS = threading.BoundedSemaphore(KDF_CONCURRENCY_LIMIT)
DUMMY_PASSWORD_HASH = generate_password_hash("not-a-real-account-password", method="scrypt")


def hash_password(password: str) -> str:
    if not _KDF_SLOTS.acquire(blocking=False):
        raise RuntimeError("password_service_busy")
    try:
        return generate_password_hash(password, method="scrypt")
    finally:
        _KDF_SLOTS.release()


def verify_password(stored_hash: str | None, candidate: str) -> bool:
    target = stored_hash if stored_hash else DUMMY_PASSWORD_HASH
    if not _KDF_SLOTS.acquire(blocking=False):
        return False
    try:
        verified = check_password_hash(target, candidate)
    except (ValueError, MemoryError):
        return False
    finally:
        _KDF_SLOTS.release()
    return bool(stored_hash) and verified


def load_current_user() -> None:
    g.user = None
    user_id = session.get("user_id")
    session_version = session.get("session_version")
    if not isinstance(user_id, str) or not isinstance(session_version, int):
        return
    user = get_db().execute(
        "SELECT id, email_normalized, display_name, system_role, status, session_version "
        "FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if user is None or user["status"] != "active" or user["session_version"] != session_version:
        session.clear()
        return
    g.user = user


def current_user_required(handler: Callable[..., object]) -> Callable[..., object]:
    from functools import wraps
    from flask import abort

    @wraps(handler)
    def wrapped(*args: object, **kwargs: object) -> object:
        if g.user is None:
            abort(401, description="error.session_expired")
        return handler(*args, **kwargs)

    return wrapped
