from __future__ import annotations

import os
import stat
from datetime import timedelta
from pathlib import Path


SUPPORTED_LOCALES = ("en",)
MAX_JSON_BYTES = 1_048_576


def load_secret_key(path: Path) -> bytes:
    if not path.is_file():
        raise RuntimeError("secret_key_missing")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise RuntimeError("secret_key_permissions_invalid")
    value = path.read_bytes()
    if len(value) != 32:
        raise RuntimeError("secret_key_invalid")
    return value


def build_config(project_root: Path) -> dict[str, object]:
    instance_root = Path(os.environ.get("POC_INSTANCE_PATH", project_root / "instance")).resolve()
    local_http = os.environ.get("POC_LOCAL_HTTP") == "1"
    if not local_http:
        raise RuntimeError("secure_runtime_not_configured")
    return {
        "INSTANCE_ROOT": instance_root,
        "DATABASE": instance_root / "app.sqlite3",
        "DATA_ROOT": instance_root / "data",
        "SECRET_KEY": load_secret_key(instance_root / "secret.key"),
        "MAX_CONTENT_LENGTH": 2_097_152,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": False,
        "PERMANENT_SESSION_LIFETIME": timedelta(minutes=30),
        "SESSION_REFRESH_EACH_REQUEST": False,
        "TRUSTED_HOSTS": ["localhost", "127.0.0.1", "localhost:*", "127.0.0.1:*"],
    }
