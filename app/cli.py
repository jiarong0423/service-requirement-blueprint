from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import secrets
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

from app.db import apply_migrations, connect_database, immediate_transaction
from app.gate0 import Gate0Builder, Gate0Error
from app.security.auth import hash_password
from app.services.case_workflow import utc_now


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("gate0", help="Run the English PDF and SVG preflight.")
    subcommands.add_parser("init-demo", help="Create the local secret, database, and three demo accounts.")
    subcommands.add_parser("migrate", help="Back up and migrate an existing local demonstration database.")
    return parser


def initialize_demo(project_root: Path) -> dict[str, object]:
    variables = ("DEMO_OWNER_PASSWORD", "DEMO_CONTRACTOR_PASSWORD", "DEMO_ADMIN_PASSWORD")
    passwords = {name: os.environ.get(name, "") for name in variables}
    if any(not value for value in passwords.values()):
        raise RuntimeError("demo_password_missing")
    instance_root = Path(os.environ.get("POC_INSTANCE_PATH", project_root / "instance")).resolve()
    secret_path = instance_root / "secret.key"
    database_path = instance_root / "app.sqlite3"
    if secret_path.exists() or database_path.exists():
        raise RuntimeError("demo_already_initialized")
    instance_was_created = not instance_root.exists()
    created_secret = False
    created_data_root = False
    try:
        instance_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(instance_root, 0o700)
        descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created_secret = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(secrets.token_bytes(32))
            stream.flush()
            os.fsync(stream.fileno())
        connection = connect_database(database_path)
        try:
            apply_migrations(connection, project_root / "migrations")
            accounts = (
                ("owner@example.test", "Demo Client", "member", passwords["DEMO_OWNER_PASSWORD"]),
                ("contractor@example.test", "Demo Contractor", "member", passwords["DEMO_CONTRACTOR_PASSWORD"]),
                ("admin@example.test", "Demo Administrator", "admin", passwords["DEMO_ADMIN_PASSWORD"]),
            )
            now = utc_now()
            with immediate_transaction(connection):
                for email, display_name, role, password in accounts:
                    connection.execute(
                        "INSERT INTO users(id, email_normalized, display_name, password_hash, system_role, status, session_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?)",
                        (str(uuid.uuid4()), email, display_name, hash_password(password), role, now, now),
                    )
        finally:
            connection.close()
        data_root = instance_root / "data"
        data_root.mkdir(mode=0o700)
        created_data_root = True
        os.chmod(data_root, 0o700)
        return {"status": "initialized", "accounts": 3}
    except BaseException:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(database_path) + suffix)
            if candidate.exists():
                candidate.unlink()
        if created_secret and secret_path.exists():
            secret_path.unlink()
        data_root = instance_root / "data"
        if created_data_root and data_root.exists():
            shutil.rmtree(data_root)
        if instance_was_created and instance_root.exists() and not any(instance_root.iterdir()):
            instance_root.rmdir()
        raise


def _create_locked_database_backup(database_path: Path, backup_path: Path) -> None:
    descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        source_uri = f"{database_path.as_uri()}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, autocommit=True, timeout=5.0)
        destination = sqlite3.connect(backup_path, autocommit=True, timeout=5.0)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        verification = sqlite3.connect(backup_path, autocommit=True, timeout=5.0)
        try:
            if verification.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("migration_backup_invalid")
            if verification.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("migration_backup_invalid")
        finally:
            verification.close()
        backup_descriptor = os.open(backup_path, os.O_RDONLY)
        try:
            os.fsync(backup_descriptor)
        finally:
            os.close(backup_descriptor)
        parent_descriptor = os.open(backup_path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except BaseException:
        if backup_path.exists():
            backup_path.unlink()
        raise


def migrate_demo(project_root: Path) -> dict[str, object]:
    instance_root = Path(os.environ.get("POC_INSTANCE_PATH", project_root / "instance")).resolve()
    database_path = instance_root / "app.sqlite3"
    if not database_path.is_file():
        raise RuntimeError("database_missing")
    expected = {path.stem for path in (project_root / "migrations").glob("*.sql")}
    inspection = connect_database(database_path)
    try:
        applied = {
            row[0]
            for row in inspection.execute("SELECT version FROM schema_migrations")
        }
    finally:
        inspection.close()
    if not applied.issubset(expected):
        raise RuntimeError("migration_version_unknown")
    if applied == expected:
        return {
            "status": "already_current",
            "backup_file": None,
            "versions": sorted(applied),
        }
    backup_root = instance_root / "backups"
    backup_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(backup_root, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_root / f"app-before-migrate-{stamp}-{uuid.uuid4().hex}.sqlite3"

    connection = connect_database(database_path)
    try:
        with immediate_transaction(connection):
            _create_locked_database_backup(database_path, backup_path)
            apply_migrations(
                connection,
                project_root / "migrations",
                manage_transactions=False,
            )
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("migration_integrity_failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("migration_foreign_key_failed")
            versions = [
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
            ]
    finally:
        connection.close()
    return {
        "status": "migrated",
        "backup_file": backup_path.name,
        "versions": versions,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    if arguments.command == "init-demo":
        try:
            result = initialize_demo(project_root)
        except Exception as error:
            allowed = {"demo_password_missing", "demo_already_initialized", "password_service_busy"}
            candidate = str(error)
            error_code = candidate if candidate in allowed else "initialization_failed"
            print(json.dumps({"status": "failed", "error_code": error_code}, sort_keys=True), file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0
    if arguments.command == "migrate":
        try:
            result = migrate_demo(project_root)
        except Exception as error:
            allowed = {
                "database_missing",
                "migration_integrity_failed",
                "migration_foreign_key_failed",
                "migration_backup_invalid",
                "migration_version_unknown",
            }
            candidate = str(error)
            error_code = candidate if candidate in allowed else "migration_failed"
            print(json.dumps({"status": "failed", "error_code": error_code}, sort_keys=True), file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0
    if arguments.command != "gate0":
        return 2
    try:
        result = Gate0Builder(project_root=project_root).build()
    except Gate0Error as error:
        print(json.dumps({"status": "failed", "error_code": error.code}, sort_keys=True), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "unexpected_failure"}, sort_keys=True), file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
