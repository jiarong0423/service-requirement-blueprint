from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from flask import current_app, g


def connect_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, autocommit=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 5000")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("foreign_keys_unavailable")
    if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
        connection.close()
        raise RuntimeError("wal_unavailable")
    if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
        connection.close()
        raise RuntimeError("full_sync_unavailable")
    if connection.execute("PRAGMA busy_timeout").fetchone()[0] != 5000:
        connection.close()
        raise RuntimeError("busy_timeout_unavailable")
    return connection


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect_database(Path(current_app.config["DATABASE"]))
    return g.db


def close_db(_error: object = None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        try:
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


@contextmanager
def deferred_read_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Hold one WAL read snapshot without reserving the database writer."""
    connection.execute("BEGIN")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        try:
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


def _migration_statements(script: str) -> Iterator[str]:
    """Yield complete SQLite statements without splitting trigger bodies."""
    buffer: list[str] = []
    for character in script:
        buffer.append(character)
        if character == ";":
            candidate = "".join(buffer)
            if not sqlite3.complete_statement(candidate):
                continue
            statement = candidate.strip()
            if statement:
                yield statement
            buffer.clear()
    trailing = "".join(buffer).strip()
    if trailing:
        yield trailing


def apply_migrations(
    connection: sqlite3.Connection,
    migration_root: Path,
    *,
    manage_transactions: bool = True,
) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for migration in sorted(migration_root.glob("*.sql")):
        version = migration.stem
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone():
            continue
        script = migration.read_text(encoding="utf-8")
        def apply_one() -> None:
            for statement in _migration_statements(script):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (version,),
            )
        if manage_transactions:
            with immediate_transaction(connection):
                apply_one()
        else:
            apply_one()
