from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from app.db import apply_migrations, connect_database
from tests.test_slice5_migration import _insert_slice4_rows


MIGRATIONS_THROUGH_SLICE6 = (
    "001_initial.sql",
    "002_slice3_invitations.sql",
    "003_slice4_discussion_json_b.sql",
    "004_slice5_candidates.sql",
    "005_slice6_formalization.sql",
)


def _migration_root(project_root: Path, destination: Path, *, slice7: bool, broken: bool = False) -> Path:
    destination.mkdir()
    for name in MIGRATIONS_THROUGH_SLICE6:
        shutil.copy2(project_root / "migrations" / name, destination / name)
    if slice7:
        source = (project_root / "migrations/006_slice7_delivery.sql").read_text(encoding="utf-8")
        if broken:
            source += "\nINSERT INTO deliberately_missing_table(value) VALUES (1);\n"
        (destination / "006_slice7_delivery.sql").write_text(source, encoding="utf-8")
    return destination


def test_populated_slice6_database_upgrades_without_mutating_existing_rows(tmp_path, project_root) -> None:
    slice6 = _migration_root(project_root, tmp_path / "slice6", slice7=False)
    slice7 = _migration_root(project_root, tmp_path / "slice7", slice7=True)
    connection = connect_database(tmp_path / "populated.sqlite3")
    try:
        apply_migrations(connection, slice6)
        ids = _insert_slice4_rows(connection)
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("users", "cases", "artifacts", "case_messages", "json_b_drafts", "json_b_versions")
        }
        case_before = tuple(connection.execute("SELECT * FROM cases WHERE id = ?", (ids["case"],)).fetchone())
        apply_migrations(connection, slice7)
        apply_migrations(connection, slice7)
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        assert after == before
        assert tuple(connection.execute("SELECT * FROM cases WHERE id = ?", (ids["case"],)).fetchone()) == case_before
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = '006_slice7_delivery'").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_failed_slice7_migration_rolls_back_every_schema_change(tmp_path, project_root) -> None:
    slice6 = _migration_root(project_root, tmp_path / "slice6", slice7=False)
    broken = _migration_root(project_root, tmp_path / "broken", slice7=True, broken=True)
    connection = connect_database(tmp_path / "rollback.sqlite3")
    try:
        apply_migrations(connection, slice6)
        ids = _insert_slice4_rows(connection)
        before_case = tuple(connection.execute("SELECT * FROM cases WHERE id = ?", (ids["case"],)).fetchone())
        with pytest.raises(sqlite3.OperationalError, match="deliberately_missing_table"):
            apply_migrations(connection, broken)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "delivery_workflows" not in tables
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = '006_slice7_delivery'").fetchone()[0] == 0
        assert tuple(connection.execute("SELECT * FROM cases WHERE id = ?", (ids["case"],)).fetchone()) == before_case
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
