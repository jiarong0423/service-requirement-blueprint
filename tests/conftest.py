from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

import pytest

from app import create_app
from app.cli import initialize_demo


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def isolated_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    for relative in (Path("app/i18n"), Path("app/preflight"), Path("assets/fonts")):
        source = PROJECT_ROOT / relative
        destination = root / relative
        destination.mkdir(parents=True, exist_ok=True)
        for entry in source.iterdir():
            if not entry.is_file():
                continue
            target = destination / entry.name
            if relative == Path("assets/fonts"):
                os.link(entry, target)
            else:
                shutil.copy2(entry, target)
    return root


@pytest.fixture
def demo_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    instance_root = tmp_path / "instance"
    passwords = {
        "owner": secrets.token_urlsafe(24),
        "contractor": secrets.token_urlsafe(24),
        "admin": secrets.token_urlsafe(24),
    }
    monkeypatch.setenv("POC_INSTANCE_PATH", str(instance_root))
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", passwords["owner"])
    monkeypatch.setenv("DEMO_CONTRACTOR_PASSWORD", passwords["contractor"])
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", passwords["admin"])
    initialize_demo(PROJECT_ROOT)
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": (instance_root / "secret.key").read_bytes(),
            "INSTANCE_ROOT": instance_root,
            "DATABASE": instance_root / "app.sqlite3",
            "DATA_ROOT": instance_root / "data",
            "MAX_CONTENT_LENGTH": 2_097_152,
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "SESSION_COOKIE_SECURE": False,
            "SESSION_REFRESH_EACH_REQUEST": False,
        }
    )
    return app, instance_root, passwords
