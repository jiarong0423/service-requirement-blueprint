from __future__ import annotations

import hashlib

import pytest

from app import create_app
from app.db import connect_database
from app.services.json_contracts import JsonContractError, decode_strict_json, validate_json_a
from tests.helpers import complete_requirement, csrf, login


@pytest.mark.parametrize("raw,code", [(b'{"a":1,"a":2}', "duplicate_key"), (b"\xef\xbb\xbf{}", "bom_forbidden"), (b"\xff", "invalid_utf8"), (b"", "file_size_invalid")])
def test_strict_json_decoder_rejects_unsafe_bytes(raw: bytes, code: str) -> None:
    with pytest.raises(JsonContractError, match=code):
        decode_strict_json(raw)


def test_strict_json_decoder_enforces_exact_one_mebibyte_limit() -> None:
    with pytest.raises(JsonContractError, match="file_size_invalid"):
        decode_strict_json(b" " * 1_048_577)


def test_schema_rejects_unknown_fields(project_root) -> None:
    document = {"unexpected": True}
    with pytest.raises(JsonContractError, match="schema_validation_failed"):
        validate_json_a(document, project_root / "app/schemas/json-a.v1.schema.json")


def test_json_a_is_immutable_exact_bytes_across_downloads_and_restart(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    with app.test_client() as client:
        login(client, "en", "owner@example.test", passwords["owner"])
        case_id = complete_requirement(client, instance_root)
        connection = connect_database(instance_root / "app.sqlite3")
        try:
            revision = connection.execute("SELECT content_revision FROM cases WHERE id = ?", (case_id,)).fetchone()[0]
        finally:
            connection.close()
        response = client.post(f"/en/owner/requirements/{case_id}/json-a", data={"csrf_token": csrf(client), "expected_revision": revision, "review_confirmed": "yes"})
        assert response.status_code == 302
        connection = connect_database(instance_root / "app.sqlite3")
        try:
            artifact = connection.execute("SELECT id, sha256, byte_length, relative_path FROM artifacts WHERE case_id = ? AND kind = 'json_a'", (case_id,)).fetchone()
        finally:
            connection.close()
        english = client.get(f"/en/owner/requirements/{case_id}/json-a/{artifact['id']}/download")
        repeated = client.get(f"/en/owner/requirements/{case_id}/json-a/{artifact['id']}/download")
        assert english.status_code == repeated.status_code == 200
        assert english.data == repeated.data
        assert len(english.data) == artifact["byte_length"]
        assert hashlib.sha256(english.data).hexdigest() == artifact["sha256"]
        document = decode_strict_json(english.data)
        assert document["project"]["supplemental_notes"] == "Preserve Unicode exactly: café, résumé, naïve — unchanged."
        assert "owner_id" not in document and "role" not in document and "state" not in document
        stored = (instance_root / "data" / artifact["relative_path"]).read_bytes()
        assert stored == english.data
    restarted = create_app({"TESTING": True, "SECRET_KEY": (instance_root / "secret.key").read_bytes(), "INSTANCE_ROOT": instance_root, "DATABASE": instance_root / "app.sqlite3", "DATA_ROOT": instance_root / "data"})
    with restarted.test_client() as client:
        assert login(client, "en", "owner@example.test", passwords["owner"]).status_code == 302
        after_restart = client.get(f"/en/owner/requirements/{case_id}/json-a/{artifact['id']}/download")
        assert after_restart.status_code == 200
        assert after_restart.data == stored


def test_changed_stored_bytes_fail_download(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    with app.test_client() as client:
        login(client, "en", "owner@example.test", passwords["owner"])
        case_id = complete_requirement(client, instance_root)
        connection = connect_database(instance_root / "app.sqlite3")
        revision = connection.execute("SELECT content_revision FROM cases WHERE id = ?", (case_id,)).fetchone()[0]
        connection.close()
        client.post(f"/en/owner/requirements/{case_id}/json-a", data={"csrf_token": csrf(client), "expected_revision": revision, "review_confirmed": "yes"})
        connection = connect_database(instance_root / "app.sqlite3")
        artifact = connection.execute("SELECT id, relative_path FROM artifacts WHERE case_id = ?", (case_id,)).fetchone()
        connection.close()
        path = instance_root / "data" / artifact["relative_path"]
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b" ")
        path.chmod(0o400)
        assert client.get(f"/en/owner/requirements/{case_id}/json-a/{artifact['id']}/download").status_code == 409


def test_new_json_a_version_does_not_overwrite_prior_bytes(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    with app.test_client() as client:
        login(client, "en", "owner@example.test", passwords["owner"])
        case_id = complete_requirement(client, instance_root)
        for _attempt in range(2):
            connection = connect_database(instance_root / "app.sqlite3")
            revision = connection.execute("SELECT content_revision FROM cases WHERE id = ?", (case_id,)).fetchone()[0]
            connection.close()
            result = client.post(f"/en/owner/requirements/{case_id}/json-a", data={"csrf_token": csrf(client), "expected_revision": revision, "review_confirmed": "yes"})
            assert result.status_code == 302
        connection = connect_database(instance_root / "app.sqlite3")
        rows = connection.execute("SELECT version_no, relative_path, lifecycle FROM artifacts WHERE case_id = ? ORDER BY version_no", (case_id,)).fetchall()
        connection.close()
        assert [(row["version_no"], row["lifecycle"]) for row in rows] == [(1, "superseded"), (2, "active")]
        assert rows[0]["relative_path"] != rows[1]["relative_path"]
        assert all((instance_root / "data" / row["relative_path"]).is_file() for row in rows)


def test_budget_contradiction_is_rejected_without_state_or_file_change(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    with app.test_client() as client:
        login(client, "en", "owner@example.test", passwords["owner"])
        case_id = complete_requirement(client, instance_root)
        connection = connect_database(instance_root / "app.sqlite3")
        before_revision = connection.execute("SELECT content_revision FROM cases WHERE id = ?", (case_id,)).fetchone()[0]
        before_artifacts = connection.execute("SELECT COUNT(*) FROM artifacts WHERE case_id = ?", (case_id,)).fetchone()[0]
        connection.close()
        rejected = client.post(
            f"/en/owner/requirements/{case_id}/sections/8",
            data={
                "csrf_token": csrf(client), "expected_revision": before_revision,
                "deliverables": "source", "source_code_ownership": "client", "account_ownership": "client",
                "deployment_expectation": "local", "maintenance_expectation": "quoted separately",
                "launch_conditions": "tests pass", "required_deadline": "2026-12-31", "budget_status": "range",
                "budget_currency": "XTS", "budget_min_amount": "9000", "budget_max_amount": "1000", "acceptance_expectations": "criteria", "constraints": "offline",
            },
        )
        assert rejected.status_code == 422
        connection = connect_database(instance_root / "app.sqlite3")
        assert connection.execute("SELECT content_revision FROM cases WHERE id = ?", (case_id,)).fetchone()[0] == before_revision
        assert connection.execute("SELECT COUNT(*) FROM artifacts WHERE case_id = ?", (case_id,)).fetchone()[0] == before_artifacts
        connection.close()
