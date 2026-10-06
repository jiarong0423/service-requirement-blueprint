from __future__ import annotations

import io
import hashlib
import json
import sqlite3
import socket
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from app.db import connect_database
from app import create_app
from app.security.auth import hash_password
from app.services.case_workflow import utc_now
from app.services.artifact_store import install_fixed_blob
from app.services.evidence_export import prepare_evidence_export
from tests.helpers import csrf, login
from tests.slice7_helpers import create_formal_context, current_workflow, delivery_payload


def _submit_delivery(context, *, summary="Completed fixed delivery"):
    response = context["contractor"].post(
        f"/en/cases/{context['case_id']}/deliveries",
        data=delivery_payload(context["contractor"], context["criterion_id"], summary=summary),
    )
    assert response.status_code == 302
    return current_workflow(context["instance_root"], context["case_id"])


def _inspect(context, result: str, *, reason: str = "", accept: bool = False):
    workflow = current_workflow(context["instance_root"], context["case_id"])
    payload = {
        "csrf_token": csrf(context["owner"]),
        "expected_revision": str(workflow["revision"]),
        "idempotency_key": str(uuid.uuid4()),
        f"result_{context['criterion_id']}": result,
        f"reason_{context['criterion_id']}": reason,
    }
    if accept:
        payload["explicit_acceptance"] = "yes"
    response = context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/inspections",
        data=payload,
    )
    assert response.status_code == 302
    return current_workflow(context["instance_root"], context["case_id"])


def test_delivery_history_uses_accessible_responsive_table_region(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    _submit_delivery(context)

    response = context["contractor"].get(f"/en/cases/{context['case_id']}/delivery")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'class="table-scroll"' in html
    assert 'role="region"' in html
    assert 'aria-label="Delivery History · Delivery Version 1"' in html
    assert 'tabindex="0"' in html
    assert 'class="delivery-table"' in html

    stylesheet = Path("app/static/css/app.css").read_text(encoding="utf-8")
    assert ".table-scroll{max-width:100%;overflow-x:auto;" in stylesheet
    assert ".delivery-table{min-width:56rem}" in stylesheet
    assert "word-break:normal" in stylesheet
    assert ".data-panel,.data-panel article{min-width:0}" in stylesheet


def test_return_remediation_reinspection_acceptance_and_export(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    assert context["contractor"].get(f"/en/cases/{context['case_id']}/delivery").status_code == 200
    workflow = _submit_delivery(context)
    assert workflow["state"] == "DELIVERED"

    started = context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    )
    assert started.status_code == 302
    assert current_workflow(context["instance_root"], context["case_id"])["state"] == "UNDER_ACCEPTANCE"

    workflow = _inspect(context, "RETURNED", reason="The stored record is missing the agreed validation marker.")
    assert workflow["state"] == "REMEDIATION"
    workflow = _submit_delivery(context, summary="Corrected validation marker")
    assert workflow["state"] == "REINSPECTION"
    workflow = _inspect(context, "PASS", accept=True)
    assert workflow["state"] == "ACCEPTED"
    acceptance_page = context["owner"].get(f"/en/cases/{context['case_id']}/delivery")
    assert acceptance_page.status_code == 200
    for identity in (workflow["current_delivery_id"], workflow["current_inspection_id"], context["owner_id"]):
        assert str(identity).encode() in acceptance_page.data

    private_sentinel = "PRIVATE-DRAFT-SENTINEL-DO-NOT-EXPORT"
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        connection.execute(
            "UPDATE requirement_drafts SET data_json = ? WHERE case_id = ?",
            (json.dumps({"private": private_sentinel}), context["case_id"]),
        )
        connection.execute(
            "UPDATE json_b_drafts SET data_json = ? WHERE case_id = ?",
            (json.dumps({"private": private_sentinel}), context["case_id"]),
        )
    finally:
        connection.close()

    prepared = context["owner"].post(
        f"/en/cases/{context['case_id']}/evidence-exports",
        data={"csrf_token": csrf(context["owner"])},
    )
    assert prepared.status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        export = connection.execute("SELECT * FROM evidence_exports WHERE case_id = ?", (context["case_id"],)).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM delivery_versions WHERE case_id = ?", (context["case_id"],)).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM inspection_rounds WHERE case_id = ?", (context["case_id"],)).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM case_timeline_events WHERE case_id = ? AND event_code = 'REVIEW_DEADLINE_SET'", (context["case_id"],)).fetchone()[0] == 2
    finally:
        connection.close()
    downloaded = context["contractor"].get(
        f"/en/cases/{context['case_id']}/evidence-exports/{export['id']}/download"
    )
    assert downloaded.status_code == 200
    with zipfile.ZipFile(io.BytesIO(downloaded.data)) as archive:
        names = set(archive.namelist())
        assert {"formal/manifest.json", "formal/document.pdf", "formal/diagram.svg", "formal/json-a.json", "formal/json-b.json", "export-manifest.json"} <= names
        manifest = json.loads(archive.read("export-manifest.json"))
        assert manifest["workflow_state"] == "ACCEPTED"
        deliveries = json.loads(archive.read("records/deliveries.json"))
        inspections = json.loads(archive.read("records/inspections.json"))
        acceptance = json.loads(archive.read("records/acceptance.json"))
        assert all(item["submitted_by_role"] == "contractor" and item["submitted_by_id"] for item in deliveries)
        assert all(item["inspected_by_role"] == "client" and item["inspected_by_id"] for item in inspections)
        assert acceptance["accepted_by_id"] == context["owner_id"]
        combined = b"".join(archive.read(name) for name in names)
        assert b"json_b_drafts" not in combined
        assert b"password_hash" not in combined
        assert private_sentinel.encode() not in combined


def test_silence_deadline_and_get_never_accept(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    for _ in range(3):
        assert context["owner"].get(f"/en/cases/{context['case_id']}/delivery").status_code == 200
    unchanged = current_workflow(context["instance_root"], context["case_id"])
    assert unchanged["state"] == "DELIVERED"
    assert unchanged["revision"] == workflow["revision"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM inspection_rounds").fetchone()[0] == 0
    finally:
        connection.close()


def test_inspection_requires_complete_results_reason_and_explicit_acceptance(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    workflow = current_workflow(context["instance_root"], context["case_id"])
    base = {"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"]), "idempotency_key": str(uuid.uuid4())}
    missing_reason = context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/inspections",
        data={**base, f"result_{context['criterion_id']}": "RETURNED", f"reason_{context['criterion_id']}": ""},
    )
    assert missing_reason.status_code == 422
    no_explicit_accept = context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/inspections",
        data={**base, "idempotency_key": str(uuid.uuid4()), f"result_{context['criterion_id']}": "PASS", f"reason_{context['criterion_id']}": ""},
    )
    assert no_explicit_accept.status_code == 422
    assert current_workflow(context["instance_root"], context["case_id"])["state"] == "UNDER_ACCEPTANCE"


def test_dispute_preserves_positions_without_outcome_and_exports(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    opened = context["contractor"].post(
        f"/en/cases/{context['case_id']}/disputes",
        data={"csrf_token": csrf(context["contractor"]), "expected_revision": str(workflow["revision"]), "reason": "The parties disagree about the submitted evidence."},
    )
    assert opened.status_code == 302
    workflow = current_workflow(context["instance_root"], context["case_id"])
    assert workflow["state"] == "DISPUTED"
    for client, text in ((context["owner"], "Client final position"), (context["contractor"], "Contractor final position")):
        assert client.post(
            f"/en/cases/{context['case_id']}/disputes/{workflow['active_dispute_id']}/positions",
            data={"csrf_token": csrf(client), "position_text": text},
        ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(disputes)")}
        assert not ({"winner", "liability", "damages", "refund", "resolution"} & columns)
        assert connection.execute("SELECT COUNT(*) FROM dispute_positions").fetchone()[0] == 2
    finally:
        connection.close()
    assert context["owner"].post(f"/en/cases/{context['case_id']}/evidence-exports", data={"csrf_token": csrf(context["owner"])}).status_code == 302


def test_admin_third_party_and_anonymous_cannot_access_delivery_or_export(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    _inspect(context, "PASS", accept=True)
    assert context["owner"].post(f"/en/cases/{context['case_id']}/evidence-exports", data={"csrf_token": csrf(context["owner"])}).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        delivery_id = connection.execute("SELECT current_delivery_id FROM delivery_workflows WHERE case_id = ?", (context["case_id"],)).fetchone()[0]
        export_id = connection.execute("SELECT id FROM evidence_exports WHERE case_id = ?", (context["case_id"],)).fetchone()[0]
        now = utc_now()
        connection.execute(
            "INSERT INTO users(id, email_normalized, display_name, password_hash, system_role, status, session_version, created_at, updated_at) "
            "VALUES (?, 'slice7-third@example.test', 'Third', ?, 'member', 'active', 1, ?, ?)",
            (str(uuid.uuid4()), hash_password("slice7-third-password"), now, now),
        )
    finally:
        connection.close()
    anonymous = context["app"].test_client()
    for path in (
        f"/en/cases/{context['case_id']}/delivery",
        f"/en/cases/{context['case_id']}/deliveries/{delivery_id}/download",
        f"/en/cases/{context['case_id']}/evidence-exports/{export_id}/download",
    ):
        assert anonymous.get(path).status_code == 401
    for email, password in (("admin@example.test", context["passwords"]["admin"]), ("slice7-third@example.test", "slice7-third-password")):
        outsider = context["app"].test_client()
        assert login(outsider, "en", email, password).status_code == 302
        for path in (
            f"/en/cases/{context['case_id']}/delivery",
            f"/en/cases/{context['case_id']}/deliveries/{delivery_id}/download",
            f"/en/cases/{context['case_id']}/evidence-exports/{export_id}/download",
        ):
            assert outsider.get(path).status_code == 404
        assert outsider.post(f"/en/cases/{context['case_id']}/evidence-exports", data={"csrf_token": csrf(outsider)}).status_code == 404
    assert context["owner"].get(f"/en/cases/{context['case_id']}/deliveries/{delivery_id}/download").status_code == 200
    assert context["contractor"].get(f"/en/cases/{context['case_id']}/evidence-exports/{export_id}/download").status_code == 200
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        connection.execute(
            "UPDATE users SET status = 'disabled', session_version = session_version + 1, updated_at = ? WHERE id = ?",
            (utc_now(), context["owner_id"]),
        )
    finally:
        connection.close()
    assert context["owner"].get(f"/en/cases/{context['case_id']}/delivery").status_code == 401
    assert context["owner"].get(f"/en/cases/{context['case_id']}/evidence-exports/{export_id}/download").status_code == 401


def test_delivery_evidence_rows_are_append_only(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="delivery_versions_append_only"):
            connection.execute("UPDATE delivery_versions SET summary = 'changed' WHERE id = ?", (workflow["current_delivery_id"],))
        with pytest.raises(sqlite3.IntegrityError, match="delivery_items_append_only"):
            connection.execute("DELETE FROM delivery_items WHERE delivery_id = ?", (workflow["current_delivery_id"],))
        item = connection.execute("SELECT * FROM delivery_items WHERE delivery_id = ?", (workflow["current_delivery_id"],)).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="delivery_children_closed"):
            connection.execute(
                "INSERT INTO delivery_items(id, delivery_id, criterion_id, description, verification_method, delivery_note, evidence_reference) VALUES (?, ?, ?, 'extra', 'extra', 'extra', 'extra')",
                (str(uuid.uuid4()), workflow["current_delivery_id"], str(uuid.uuid4())),
            )
        with pytest.raises(sqlite3.IntegrityError, match="delivery_children_closed"):
            connection.execute(
                "INSERT INTO delivery_test_results(id, delivery_id, criterion_id, outcome, observation) VALUES (?, ?, ?, 'PASS', 'extra')",
                (str(uuid.uuid4()), workflow["current_delivery_id"], str(uuid.uuid4())),
            )
    finally:
        connection.close()


def test_delivery_post_requires_csrf(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    payload = delivery_payload(context["contractor"], context["criterion_id"])
    payload.pop("csrf_token")
    assert context["contractor"].post(f"/en/cases/{context['case_id']}/deliveries", data=payload).status_code == 400


def test_export_is_idempotent_exact_bytes_and_restart_safe(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    _inspect(context, "PASS", accept=True)
    for _ in range(2):
        assert context["owner"].post(
            f"/en/cases/{context['case_id']}/evidence-exports", data={"csrf_token": csrf(context["owner"])}
        ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        rows = connection.execute("SELECT * FROM evidence_exports WHERE case_id = ?", (context["case_id"],)).fetchall()
        assert len(rows) == 1
        export = rows[0]
    finally:
        connection.close()
    before = context["owner"].get(f"/en/cases/{context['case_id']}/evidence-exports/{export['id']}/download").data
    restarted = create_app({
        "TESTING": False,
        "SECRET_KEY": (context["instance_root"] / "secret.key").read_bytes(),
        "INSTANCE_ROOT": context["instance_root"],
        "DATABASE": context["instance_root"] / "app.sqlite3",
        "DATA_ROOT": context["instance_root"] / "data",
        "SESSION_COOKIE_SECURE": False,
    })
    client = restarted.test_client()
    assert login(client, "en", "owner@example.test", context["passwords"]["owner"]).status_code == 302
    after = client.get(f"/en/cases/{context['case_id']}/evidence-exports/{export['id']}/download")
    assert after.status_code == 200
    assert after.data == before


def test_export_tamper_fails_download_and_restart(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    _inspect(context, "PASS", accept=True)
    assert context["owner"].post(f"/en/cases/{context['case_id']}/evidence-exports", data={"csrf_token": csrf(context["owner"])}).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        export = connection.execute("SELECT * FROM evidence_exports WHERE case_id = ?", (context["case_id"],)).fetchone()
    finally:
        connection.close()
    path = context["instance_root"] / "data" / export["relative_path"]
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"tamper")
    assert context["owner"].get(f"/en/cases/{context['case_id']}/evidence-exports/{export['id']}/download").status_code == 409
    with pytest.raises(RuntimeError, match="evidence_export_storage_invalid"):
        create_app({
            "TESTING": False,
            "SECRET_KEY": (context["instance_root"] / "secret.key").read_bytes(),
            "INSTANCE_ROOT": context["instance_root"],
            "DATABASE": context["instance_root"] / "app.sqlite3",
            "DATA_ROOT": context["instance_root"] / "data",
            "SESSION_COOKIE_SECURE": False,
        })


def test_full_delivery_path_has_no_outbound_network_dependency(demo_runtime, monkeypatch) -> None:
    def denied(*_args, **_kwargs):
        raise AssertionError("outbound network is forbidden")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    _inspect(context, "PASS", accept=True)
    assert context["owner"].post(f"/en/cases/{context['case_id']}/evidence-exports", data={"csrf_token": csrf(context["owner"])}).status_code == 302


def test_unexpected_error_response_and_log_exclude_private_content(demo_runtime, monkeypatch, caplog) -> None:
    context = create_formal_context(demo_runtime)
    private_sentinel = "PRIVATE-REQUEST-CONTENT-MUST-NOT-BE-LOGGED"

    def fail_safely(*_args, **_kwargs):
        raise RuntimeError(private_sentinel)

    monkeypatch.setattr("app.routes.localized.load_delivery_dashboard", fail_safely)
    caplog.set_level("ERROR")
    response = context["owner"].get(f"/en/cases/{context['case_id']}/delivery")
    assert response.status_code == 500
    body = response.get_data(as_text=True)
    log_text = caplog.text
    assert private_sentinel not in body
    assert private_sentinel not in log_text
    assert "/Users/" not in log_text
    assert "request_failed correlation_id=" in log_text


def test_delivery_tamper_blocks_download_and_restart(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        delivery = connection.execute("SELECT * FROM delivery_versions WHERE id = ?", (workflow["current_delivery_id"],)).fetchone()
    finally:
        connection.close()
    path = context["instance_root"] / "data" / delivery["relative_path"]
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"tamper")
    assert context["owner"].get(f"/en/cases/{context['case_id']}/deliveries/{delivery['id']}/download").status_code == 409
    with pytest.raises(RuntimeError, match="delivery_storage_invalid"):
        create_app({
            "TESTING": False, "SECRET_KEY": (context["instance_root"] / "secret.key").read_bytes(),
            "INSTANCE_ROOT": context["instance_root"], "DATABASE": context["instance_root"] / "app.sqlite3",
            "DATA_ROOT": context["instance_root"] / "data", "SESSION_COOKIE_SECURE": False,
        })


def test_schema_failure_leaves_no_export_row_or_file(demo_runtime, tmp_path) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    _inspect(context, "PASS", accept=True)
    invalid_contract = tmp_path / "invalid-export-schema.json"
    invalid_contract.write_text(json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": {"schema_version": {"const": "impossible"}}, "required": ["schema_version"]}), encoding="utf-8")
    context["app"].config["EVIDENCE_EXPORT_SCHEMA_PATH"] = invalid_contract
    response = context["owner"].post(f"/en/cases/{context['case_id']}/evidence-exports", data={"csrf_token": csrf(context["owner"])})
    assert response.status_code == 422
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM evidence_exports").fetchone()[0] == 0
    finally:
        connection.close()
    export_root = context["instance_root"] / "data" / "cases" / context["case_id"] / "exports"
    assert not export_root.exists() or list(export_root.iterdir()) == []


def test_orphan_delivery_directory_is_quarantined_on_restart(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    orphan_id = str(uuid.uuid4())
    relative = install_fixed_blob(context["instance_root"] / "data", context["case_id"], "deliveries", orphan_id, "delivery.json", b"{}\n")
    assert (context["instance_root"] / "data" / relative).is_file()
    create_app({
        "TESTING": False, "SECRET_KEY": (context["instance_root"] / "secret.key").read_bytes(),
        "INSTANCE_ROOT": context["instance_root"], "DATABASE": context["instance_root"] / "app.sqlite3",
        "DATA_ROOT": context["instance_root"] / "data", "SESSION_COOKIE_SECURE": False,
    })
    assert not (context["instance_root"] / "data" / relative).exists()
    quarantine = context["instance_root"] / "data" / "cases" / context["case_id"] / "quarantine"
    assert any(path.name.startswith(f"deliveries-{orphan_id}-") for path in quarantine.iterdir())


def test_database_rejects_acceptance_without_evidence_and_late_inspection_children(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE delivery_workflows SET state = 'ACCEPTED', revision = revision + 1, updated_at = ? WHERE case_id = ?",
                (utc_now(), context["case_id"]),
            )
    finally:
        connection.close()
    workflow = _inspect(context, "PASS", accept=True)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        inspection = connection.execute("SELECT * FROM inspection_rounds WHERE id = ?", (workflow["current_inspection_id"],)).fetchone()
        item = connection.execute("SELECT * FROM delivery_items WHERE delivery_id = ?", (workflow["current_delivery_id"],)).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="inspection_children_closed"):
            connection.execute(
                "INSERT INTO inspection_item_results(id, inspection_id, delivery_item_id, criterion_id, result, reason) VALUES (?, ?, ?, ?, 'PASS', NULL)",
                (str(uuid.uuid4()), inspection["id"], item["id"], item["criterion_id"]),
            )
        assert connection.execute("SELECT COUNT(*) FROM acceptance_events WHERE case_id = ?", (context["case_id"],)).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="acceptance_events_append_only"):
            connection.execute("DELETE FROM acceptance_events WHERE case_id = ?", (context["case_id"],))
    finally:
        connection.close()


def test_database_rejects_acceptance_when_any_child_result_is_returned(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        delivery = connection.execute("SELECT * FROM delivery_versions WHERE id = ?", (workflow["current_delivery_id"],)).fetchone()
        item = connection.execute("SELECT * FROM delivery_items WHERE delivery_id = ?", (delivery["id"],)).fetchone()
        inspection_id = str(uuid.uuid4())
        inspected_at = utc_now()
        connection.execute(
            "INSERT INTO inspection_rounds(id, case_id, delivery_id, round_no, kind, decision, explicit_acceptance, inspected_by_id, inspected_at, "
            "result_count, criteria_sha256, request_sha256, idempotency_key) VALUES (?, ?, ?, 1, 'INITIAL', 'ACCEPTED', 1, ?, ?, 1, ?, ?, ?)",
            (
                inspection_id,
                context["case_id"],
                delivery["id"],
                context["owner_id"],
                inspected_at,
                delivery["criteria_sha256"],
                "0" * 64,
                str(uuid.uuid4()),
            ),
        )
        connection.execute("DROP TRIGGER inspection_results_insert_guard")
        connection.execute(
            "INSERT INTO inspection_item_results(id, inspection_id, delivery_item_id, criterion_id, result, reason) "
            "VALUES (?, ?, ?, ?, 'RETURNED', 'Contradictory child evidence')",
            (str(uuid.uuid4()), inspection_id, item["id"], item["criterion_id"]),
        )
        with pytest.raises(sqlite3.IntegrityError, match="acceptance_binding_invalid"):
            connection.execute(
                "INSERT INTO acceptance_events(id, case_id, formal_candidate_id, delivery_id, inspection_id, accepted_by_id, evidence_sha256, accepted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    context["case_id"],
                    context["candidate_id"],
                    delivery["id"],
                    inspection_id,
                    context["owner_id"],
                    "1" * 64,
                    inspected_at,
                ),
            )
    finally:
        connection.close()


def test_acceptance_digest_tamper_blocks_protected_read_and_restart(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    _inspect(context, "PASS", accept=True)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        connection.execute("DROP TRIGGER acceptance_events_no_update")
        connection.execute(
            "UPDATE acceptance_events SET evidence_sha256 = ? WHERE case_id = ?",
            ("0" * 64, context["case_id"]),
        )
    finally:
        connection.close()
    assert context["owner"].get(f"/en/cases/{context['case_id']}/delivery").status_code == 409
    with pytest.raises(RuntimeError, match="delivery_storage_invalid"):
        create_app({
            "TESTING": False,
            "SECRET_KEY": (context["instance_root"] / "secret.key").read_bytes(),
            "INSTANCE_ROOT": context["instance_root"],
            "DATABASE": context["instance_root"] / "app.sqlite3",
            "DATA_ROOT": context["instance_root"] / "data",
            "SESSION_COOKIE_SECURE": False,
        })


def test_idempotency_key_is_bound_to_delivery_request_content(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    key = str(uuid.uuid4())
    first = delivery_payload(context["contractor"], context["criterion_id"], summary="First exact request")
    first["idempotency_key"] = key
    assert context["contractor"].post(f"/en/cases/{context['case_id']}/deliveries", data=first).status_code == 302
    replay = dict(first)
    replay["csrf_token"] = csrf(context["contractor"])
    assert context["contractor"].post(f"/en/cases/{context['case_id']}/deliveries", data=replay).status_code == 302
    conflict = dict(replay)
    conflict["summary"] = "Different request with the same key"
    assert context["contractor"].post(f"/en/cases/{context['case_id']}/deliveries", data=conflict).status_code == 409
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM delivery_versions WHERE case_id = ?", (context["case_id"],)).fetchone()[0] == 1
    finally:
        connection.close()


def test_inspection_idempotency_key_is_bound_to_exact_request(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    workflow = current_workflow(context["instance_root"], context["case_id"])
    key = str(uuid.uuid4())
    route = f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/inspections"
    exact = {
        "csrf_token": csrf(context["owner"]),
        "expected_revision": str(workflow["revision"]),
        "idempotency_key": key,
        f"result_{context['criterion_id']}": "RETURNED",
        f"reason_{context['criterion_id']}": "The validation record is incomplete.",
    }
    assert context["owner"].post(route, data=exact).status_code == 302
    replay = dict(exact)
    replay["csrf_token"] = csrf(context["owner"])
    assert context["owner"].post(route, data=replay).status_code == 302
    conflict = dict(replay)
    conflict[f"reason_{context['criterion_id']}"] = "A different reason under the same key."
    assert context["owner"].post(route, data=conflict).status_code == 409
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM inspection_rounds WHERE case_id = ?", (context["case_id"],)).fetchone()[0] == 1
    finally:
        connection.close()


def test_all_delivery_evidence_tables_reject_update_and_delete(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    _inspect(context, "PASS", accept=True)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/evidence-exports",
        data={"csrf_token": csrf(context["owner"])},
    ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        rows = {
            "delivery_versions": connection.execute("SELECT id FROM delivery_versions WHERE case_id = ?", (context["case_id"],)).fetchone()[0],
            "delivery_items": connection.execute("SELECT id FROM delivery_items").fetchone()[0],
            "delivery_test_results": connection.execute("SELECT id FROM delivery_test_results").fetchone()[0],
            "inspection_rounds": connection.execute("SELECT id FROM inspection_rounds WHERE case_id = ?", (context["case_id"],)).fetchone()[0],
            "inspection_item_results": connection.execute("SELECT id FROM inspection_item_results").fetchone()[0],
            "case_timeline_events": connection.execute("SELECT id FROM case_timeline_events WHERE case_id = ?", (context["case_id"],)).fetchone()[0],
            "acceptance_events": connection.execute("SELECT id FROM acceptance_events WHERE case_id = ?", (context["case_id"],)).fetchone()[0],
            "evidence_exports": connection.execute("SELECT id FROM evidence_exports WHERE case_id = ?", (context["case_id"],)).fetchone()[0],
        }
        for table, row_id in rows.items():
            with pytest.raises(sqlite3.IntegrityError, match="append_only"):
                connection.execute(f"UPDATE {table} SET id = id WHERE id = ?", (row_id,))
            with pytest.raises(sqlite3.IntegrityError, match="append_only"):
                connection.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    finally:
        connection.close()


def test_database_rejects_controlled_path_substitution(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        fake_delivery_id = str(uuid.uuid4())
        with pytest.raises(sqlite3.IntegrityError, match="delivery_path_invalid"):
            connection.execute(
                "INSERT INTO delivery_versions(id, case_id, formal_candidate_id, version_no, kind, predecessor_delivery_id, "
                "remediated_inspection_id, submitted_by_id, summary, relative_path, sha256, byte_length, item_count, criteria_sha256, "
                "request_sha256, submitted_at, review_due_at, idempotency_key) VALUES (?, ?, ?, 2, 'REMEDIATION', ?, ?, ?, 'fixed', ?, ?, 1, 1, ?, ?, ?, ?, ?)",
                (
                    fake_delivery_id,
                    context["case_id"],
                    context["candidate_id"],
                    workflow["current_delivery_id"],
                    str(uuid.uuid4()),
                    context["contractor_id"],
                    f"cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/delivery.json",
                    "0" * 64,
                    "1" * 64,
                    "2" * 64,
                    utc_now(),
                    utc_now(),
                    str(uuid.uuid4()),
                ),
            )
        fake_export_id = str(uuid.uuid4())
        with pytest.raises(sqlite3.IntegrityError, match="evidence_export_path_invalid"):
            connection.execute(
                "INSERT INTO evidence_exports(id, case_id, version_no, prepared_by_id, source_fingerprint, relative_path, sha256, byte_length, schema_version, prepared_at) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, 1, 'evidence-export/1.0', ?)",
                (
                    fake_export_id,
                    context["case_id"],
                    context["owner_id"],
                    "3" * 64,
                    f"cases/{context['case_id']}/exports/{uuid.uuid4()}/evidence.zip",
                    "4" * 64,
                    utc_now(),
                ),
            )
    finally:
        connection.close()


def test_dispute_and_positions_reject_update_and_delete(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/disputes",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": str(workflow["revision"]),
            "reason": "The parties need an external interpretation of the evidence.",
        },
    ).status_code == 302
    workflow = current_workflow(context["instance_root"], context["case_id"])
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/disputes/{workflow['active_dispute_id']}/positions",
        data={"csrf_token": csrf(context["owner"]), "position_text": "Client evidence position."},
    ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        position_id = connection.execute("SELECT id FROM dispute_positions WHERE dispute_id = ?", (workflow["active_dispute_id"],)).fetchone()[0]
        for table, row_id in (("disputes", workflow["active_dispute_id"]), ("dispute_positions", position_id)):
            with pytest.raises(sqlite3.IntegrityError, match="append_only"):
                connection.execute(f"UPDATE {table} SET id = id WHERE id = ?", (row_id,))
            with pytest.raises(sqlite3.IntegrityError, match="append_only"):
                connection.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    finally:
        connection.close()


def test_inner_export_tamper_fails_even_if_outer_digest_is_rewritten(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    _inspect(context, "PASS", accept=True)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/evidence-exports",
        data={"csrf_token": csrf(context["owner"])},
    ).status_code == 302
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        export = connection.execute("SELECT * FROM evidence_exports WHERE case_id = ?", (context["case_id"],)).fetchone()
    finally:
        connection.close()
    path = context["instance_root"] / "data" / export["relative_path"]
    with zipfile.ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries["records/timeline.json"] += b"tamper"
    manifest = json.loads(entries["export-manifest.json"])
    timeline_entry = next(item for item in manifest["entries"] if item["path"] == "records/timeline.json")
    timeline_entry["byte_length"] = len(entries["records/timeline.json"])
    timeline_entry["sha256"] = hashlib.sha256(entries["records/timeline.json"]).hexdigest()
    entries["export-manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100400 << 16
            archive.writestr(info, raw)
    tampered = buffer.getvalue()
    path.chmod(0o600)
    path.write_bytes(tampered)
    path.chmod(0o400)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        connection.execute("DROP TRIGGER evidence_exports_no_update")
        connection.execute(
            "UPDATE evidence_exports SET sha256 = ?, byte_length = ? WHERE id = ?",
            (hashlib.sha256(tampered).hexdigest(), len(tampered), export["id"]),
        )
    finally:
        connection.close()
    assert context["owner"].get(
        f"/en/cases/{context['case_id']}/evidence-exports/{export['id']}/download"
    ).status_code == 409
    with pytest.raises(RuntimeError, match="evidence_export_storage_invalid"):
        create_app({
            "TESTING": False,
            "SECRET_KEY": (context["instance_root"] / "secret.key").read_bytes(),
            "INSTANCE_ROOT": context["instance_root"],
            "DATABASE": context["instance_root"] / "app.sqlite3",
            "DATA_ROOT": context["instance_root"] / "data",
            "SESSION_COOKIE_SECURE": False,
        })


def test_concurrent_export_requests_converge_on_one_fixed_export(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    _inspect(context, "PASS", accept=True)
    barrier = Barrier(2)

    def prepare() -> str:
        connection = connect_database(context["instance_root"] / "app.sqlite3")
        try:
            barrier.wait(timeout=5)
            result = prepare_evidence_export(
                connection,
                context["instance_root"] / "data",
                context["app"].config["DOCUMENT_ASSETS"],
                context["case_id"],
                context["owner_id"],
                context["app"].config["EVIDENCE_EXPORT_SCHEMA_PATH"],
            )
            return result.export_id
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        export_ids = list(executor.map(lambda _index: prepare(), range(2)))
    assert export_ids[0] == export_ids[1]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM evidence_exports WHERE case_id = ?", (context["case_id"],)).fetchone()[0] == 1
    finally:
        connection.close()


def test_twentieth_delivery_succeeds_and_twenty_first_fails_without_state_change(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context, summary="Delivery version 1")
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    for version in range(2, 21):
        workflow = _inspect(context, "RETURNED", reason=f"Correction required before version {version}.")
        assert workflow["state"] == "REMEDIATION"
        workflow = _submit_delivery(context, summary=f"Delivery version {version}")
        assert workflow["state"] == "REINSPECTION"
    workflow = _inspect(context, "RETURNED", reason="The twentieth version still requires correction.")
    assert workflow["state"] == "REMEDIATION"
    contractor_page = context["contractor"].get(f"/en/cases/{context['case_id']}/delivery")
    assert contractor_page.status_code == 200
    assert b"The 20-version delivery limit has been reached." in contractor_page.data
    assert f'action="/en/cases/{context["case_id"]}/deliveries"'.encode() not in contractor_page.data
    before = dict(workflow)
    rejected = context["contractor"].post(
        f"/en/cases/{context['case_id']}/deliveries",
        data=delivery_payload(context["contractor"], context["criterion_id"], summary="Forbidden delivery version 21"),
    )
    assert rejected.status_code == 409
    after = current_workflow(context["instance_root"], context["case_id"])
    assert after["state"] == before["state"]
    assert after["revision"] == before["revision"]
    assert after["current_delivery_id"] == before["current_delivery_id"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM delivery_versions WHERE case_id = ?", (context["case_id"],)).fetchone()[0] == 20
    finally:
        connection.close()
