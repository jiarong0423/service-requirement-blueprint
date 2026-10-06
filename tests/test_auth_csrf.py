from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import app.cli as cli_module
from app.cli import initialize_demo
from app.config import build_config
from app.db import connect_database
from tests.helpers import csrf, login


def test_missing_password_creates_no_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_root: Path) -> None:
    instance_root = tmp_path / "missing"
    monkeypatch.setenv("POC_INSTANCE_PATH", str(instance_root))
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", "present")
    monkeypatch.delenv("DEMO_CONTRACTOR_PASSWORD", raising=False)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "present")
    with pytest.raises(RuntimeError, match="demo_password_missing"):
        initialize_demo(project_root)
    assert not instance_root.exists()


def test_initialization_failure_removes_new_secret_and_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_root: Path) -> None:
    instance_root = tmp_path / "failed"
    monkeypatch.setenv("POC_INSTANCE_PATH", str(instance_root))
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", "owner-password")
    monkeypatch.setenv("DEMO_CONTRACTOR_PASSWORD", "contractor-password")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "admin-password")
    calls = 0

    def failing_hash(_password: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected_hash_failure")
        return "scrypt:test"

    monkeypatch.setattr(cli_module, "hash_password", failing_hash)
    with pytest.raises(RuntimeError, match="injected_hash_failure"):
        initialize_demo(project_root)
    assert not (instance_root / "secret.key").exists()
    assert not (instance_root / "app.sqlite3").exists()


def test_connection_setup_failure_compensates_created_database_and_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_root: Path) -> None:
    instance_root = tmp_path / "connection-failed"
    monkeypatch.setenv("POC_INSTANCE_PATH", str(instance_root))
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", "owner-password")
    monkeypatch.setenv("DEMO_CONTRACTOR_PASSWORD", "contractor-password")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "admin-password")

    def failing_connect(path: Path):
        path.touch()
        Path(f"{path}-wal").touch()
        raise RuntimeError("injected_connection_failure")

    monkeypatch.setattr(cli_module, "connect_database", failing_connect)
    with pytest.raises(RuntimeError, match="injected_connection_failure"):
        initialize_demo(project_root)
    assert not instance_root.exists()


def test_unsafe_secret_permissions_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_root: Path) -> None:
    instance_root = tmp_path / "unsafe"
    instance_root.mkdir()
    secret = instance_root / "secret.key"
    secret.write_bytes(os.urandom(32))
    secret.chmod(0o644)
    monkeypatch.setenv("POC_INSTANCE_PATH", str(instance_root))
    monkeypatch.setenv("POC_LOCAL_HTTP", "1")
    with pytest.raises(RuntimeError, match="secret_key_permissions_invalid"):
        build_config(project_root)


def test_init_creates_scrypt_accounts_and_private_secret(demo_runtime) -> None:
    _app, instance_root, _passwords = demo_runtime
    assert stat.S_IMODE((instance_root / "secret.key").stat().st_mode) == 0o600
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        rows = connection.execute("SELECT password_hash FROM users ORDER BY email_normalized").fetchall()
        assert len(rows) == 3
        assert all(row[0].startswith("scrypt:") for row in rows)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_login_logout_rotate_identity_and_csrf(demo_runtime) -> None:
    app, _instance_root, passwords = demo_runtime
    with app.test_client() as client:
        client.get("/en/login")
        anonymous_nonce = csrf(client)
        response = client.post("/en/login", data={"csrf_token": anonymous_nonce, "email": "owner@example.test", "password": passwords["owner"]})
        assert response.status_code == 302
        with client.session_transaction() as state:
            authenticated_nonce = state["csrf_nonce"]
            assert authenticated_nonce != anonymous_nonce
            assert set(state) <= {"_permanent", "user_id", "session_version", "csrf_nonce"}
        rejected = client.post("/en/logout", data={"csrf_token": anonymous_nonce})
        assert rejected.status_code == 400
        response = client.post("/en/logout", data={"csrf_token": authenticated_nonce})
        assert response.status_code == 302
        with client.session_transaction() as state:
            assert set(state) == {"csrf_nonce"}
            assert state["csrf_nonce"] != authenticated_nonce


def test_login_errors_are_generic_and_mutation_requires_csrf(demo_runtime) -> None:
    app, _instance_root, passwords = demo_runtime
    with app.test_client() as client:
        client.get("/en/login")
        first = client.post("/en/login", data={"csrf_token": csrf(client), "email": "missing@example.test", "password": passwords["owner"]})
        client.get("/en/login")
        second = client.post("/en/login", data={"csrf_token": csrf(client), "email": "owner@example.test", "password": "wrong-password"})
        assert first.status_code == second.status_code == 401
        assert b"Invalid sign-in information" in first.data and b"Invalid sign-in information" in second.data
        assert client.post("/en/login", data={"email": "owner@example.test", "password": passwords["owner"]}).status_code == 400


def test_stale_session_version_is_rejected_on_next_request(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    with app.test_client() as client:
        assert login(client, "en", "owner@example.test", passwords["owner"]).status_code == 302
        connection = connect_database(instance_root / "app.sqlite3")
        try:
            connection.execute("UPDATE users SET session_version = session_version + 1 WHERE email_normalized = 'owner@example.test'")
        finally:
            connection.close()
        assert client.get("/en/account").status_code == 401
        with client.session_transaction() as state:
            assert "user_id" not in state
