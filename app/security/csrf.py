from __future__ import annotations

import hmac
import secrets

from flask import abort, request, session


def new_nonce() -> str:
    return secrets.token_urlsafe(32)


def ensure_nonce() -> str:
    value = session.get("csrf_nonce")
    if not isinstance(value, str) or len(value) < 40:
        value = new_nonce()
        session["csrf_nonce"] = value
    return value


def validate_csrf() -> None:
    expected = session.get("csrf_nonce")
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not isinstance(expected, str) or not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
        abort(400, description="error.csrf_invalid")
