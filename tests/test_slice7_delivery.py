from __future__ import annotations

import io
import hashlib
import json
import re
import sqlite3
import socket
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event, Thread

import pytest

import app.services.delivery_workflow as delivery_module
import app.services.evidence_export as evidence_module
from app.db import connect_database
from app import create_app
from app.security.auth import hash_password
from app.services.case_workflow import utc_now
from app.services.artifact_store import install_fixed_blob
from app.services.evidence_export import prepare_evidence_export
from app.services.delivery_workflow import submit_delivery
from app.services.evidence_export import EvidenceExportError
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


def test_inspection_digest_work_never_runs_inside_writer_transaction(
    demo_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    writer_active = False
    original_transaction = delivery_module.immediate_transaction
    original_digest = delivery_module._digest

    @contextmanager
    def tracked_transaction(connection):
        nonlocal writer_active
        with original_transaction(connection):
            writer_active = True
            try:
                yield
            finally:
                writer_active = False

    def guarded_digest(*args, **kwargs):
        assert writer_active is False
        return original_digest(*args, **kwargs)

    monkeypatch.setattr(delivery_module, "immediate_transaction", tracked_transaction)
    monkeypatch.setattr(delivery_module, "_digest", guarded_digest)
    assert _inspect(context, "PASS", accept=True)["state"] == "ACCEPTED"


def test_evidence_graph_serialization_never_runs_inside_writer_transaction(
    demo_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    assert _inspect(context, "PASS", accept=True)["state"] == "ACCEPTED"
    writer_active = False
    original_transaction = evidence_module.immediate_transaction

    @contextmanager
    def tracked_transaction(connection):
        nonlocal writer_active
        with original_transaction(connection):
            writer_active = True
            try:
                yield
            finally:
                writer_active = False

    monkeypatch.setattr(evidence_module, "immediate_transaction", tracked_transaction)
    for name in ("_evidence_entries", "_write_zip", "_json_bytes"):
        original = getattr(evidence_module, name)

        def outside_writer(*args, _original=original, _name=name, **kwargs):
            assert writer_active is False, f"{_name} ran inside writer transaction"
            return _original(*args, **kwargs)

        monkeypatch.setattr(evidence_module, name, outside_writer)
    response = context["owner"].post(
        f"/en/cases/{context['case_id']}/evidence-exports",
        data={"csrf_token": csrf(context["owner"])},
    )
    assert response.status_code == 302


def test_evidence_export_projection_ignores_new_database_columns(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    assert _inspect(context, "PASS", accept=True)["state"] == "ACCEPTED"

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assets = context["app"].config["DOCUMENT_ASSETS"]
        data_root = context["instance_root"] / "data"
        schema_path = Path(context["app"].config["EVIDENCE_EXPORT_SCHEMA_PATH"])
        before_view = delivery_module.load_delivery_dashboard(
            connection, data_root, assets, context["case_id"], context["owner_id"]
        )
        before = evidence_module._evidence_entries(connection, data_root, before_view, schema_path)

        for table in (
            "delivery_versions",
            "inspection_item_results",
            "case_timeline_events",
            "acceptance_events",
        ):
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN secret_canary TEXT NOT NULL "
                "DEFAULT 'SECRET-CANARY-MUST-NOT-EXPORT'"
            )

        after_view = delivery_module.load_delivery_dashboard(
            connection, data_root, assets, context["case_id"], context["owner_id"]
        )
        after = evidence_module._evidence_entries(connection, data_root, after_view, schema_path)
    finally:
        connection.close()

    assert after == before
    assert b"SECRET-CANARY-MUST-NOT-EXPORT" not in b"".join(after.values())


def test_evidence_record_schema_rejects_unknown_fields(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    assert _inspect(context, "PASS", accept=True)["state"] == "ACCEPTED"

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        data_root = context["instance_root"] / "data"
        schema_path = Path(context["app"].config["EVIDENCE_EXPORT_SCHEMA_PATH"])
        view = delivery_module.load_delivery_dashboard(
            connection,
            data_root,
            context["app"].config["DOCUMENT_ASSETS"],
            context["case_id"],
            context["owner_id"],
        )
        entries = evidence_module._evidence_entries(connection, data_root, view, schema_path)
    finally:
        connection.close()

    payloads = {
        path: json.loads(entries[path].decode("utf-8"))
        for path in evidence_module.RECORD_SCHEMA_KEYS
    }
    payloads["records/timeline.json"][0]["unexpected_secret"] = "must fail closed"
    with pytest.raises(EvidenceExportError, match="export_record_invalid"):
        evidence_module._validate_record_payloads(payloads, schema_path)


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
    assert re.search(r"\.table-scroll\s*\{[^}]*max-width:\s*100%;[^}]*overflow-x:\s*auto;", stylesheet)
    assert re.search(r"\.delivery-table\s*\{[^}]*min-width:\s*56rem;", stylesheet)
    assert re.search(r"\.delivery-table td\s*\{[^}]*word-break:\s*normal;", stylesheet)
    assert re.search(r"\.data-panel article\s*\{[^}]*min-width:\s*0;", stylesheet)


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
        f"/en/cases/{context['case_id']}/evidence-exports?download=1",
        data={"csrf_token": csrf(context["owner"])},
    )
    assert prepared.status_code == 200
    assert prepared.mimetype == "application/zip"
    assert prepared.headers["Content-Disposition"] == 'attachment; filename="evidence-1.zip"'
    assert prepared.headers["Cache-Control"] == "no-store, private"
    with zipfile.ZipFile(io.BytesIO(prepared.data)) as archive:
        assert "export-manifest.json" in archive.namelist()
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


def test_export_verifier_rejects_a_self_consistent_replaced_fixed_entry(demo_runtime) -> None:
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
        export = connection.execute(
            "SELECT * FROM evidence_exports WHERE case_id = ?", (context["case_id"],)
        ).fetchone()
    finally:
        connection.close()
    raw = (context["instance_root"] / "data" / export["relative_path"]).read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = {
            name: archive.read(name)
            for name in archive.namelist()
            if name != "export-manifest.json"
        }
        manifest = json.loads(archive.read("export-manifest.json"))

    removed = entries.pop("formal/json-b.json")
    entries["private/requirement-draft.json"] = removed
    listed = [evidence_module._entry(name, content) for name, content in sorted(entries.items())]
    manifest["entries"] = listed
    manifest["source_fingerprint"] = hashlib.sha256(evidence_module._json_bytes(listed)).hexdigest()
    entries["export-manifest.json"] = evidence_module._json_bytes(manifest)
    tampered = evidence_module._write_zip(entries)
    row = dict(export)
    row["source_fingerprint"] = manifest["source_fingerprint"]
    row["sha256"] = hashlib.sha256(tampered).hexdigest()
    row["byte_length"] = len(tampered)

    with pytest.raises(EvidenceExportError, match="export_integrity_invalid"):
        evidence_module._verify_zip(
            tampered,
            row,
            Path(context["app"].config["EVIDENCE_EXPORT_SCHEMA_PATH"]),
        )


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
    first_package = context["owner"].post(
        f"/en/cases/{context['case_id']}/evidence-exports?download=1",
        data={"csrf_token": csrf(context["owner"])},
    )
    assert first_package.status_code == 200
    with zipfile.ZipFile(io.BytesIO(first_package.data)) as archive:
        assert json.loads(archive.read("records/dispute.json"))["positions"] == []
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        first_export_id = connection.execute(
            "SELECT id FROM evidence_exports WHERE case_id = ? AND version_no = 1",
            (context["case_id"],),
        ).fetchone()[0]
    finally:
        connection.close()
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
    superseded = context["owner"].get(
        f"/en/cases/{context['case_id']}/evidence-exports/{first_export_id}/download"
    )
    assert superseded.status_code == 409
    assert context["contractor"].get(
        f"/en/cases/{context['case_id']}/evidence-exports/{first_export_id}/download"
    ).status_code == 409
    dashboard = context["owner"].get(f"/en/cases/{context['case_id']}/delivery")
    assert dashboard.status_code == 200
    assert b"Superseded by newer case evidence" in dashboard.data
    assert f"/evidence-exports/{first_export_id}/download".encode("ascii") not in dashboard.data
    current_package = context["owner"].post(
        f"/en/cases/{context['case_id']}/evidence-exports?download=1",
        data={"csrf_token": csrf(context["owner"])},
    )
    assert current_package.status_code == 200
    assert current_package.headers["Content-Disposition"] == 'attachment; filename="evidence-2.zip"'
    with zipfile.ZipFile(io.BytesIO(current_package.data)) as archive:
        assert len(json.loads(archive.read("records/dispute.json"))["positions"]) == 2
    current_dashboard = context["contractor"].get(
        f"/en/cases/{context['case_id']}/delivery"
    )
    assert current_dashboard.status_code == 200
    assert b"Current package" in current_dashboard.data
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        current_export_id = connection.execute(
            "SELECT id FROM evidence_exports WHERE case_id = ? AND version_no = 2",
            (context["case_id"],),
        ).fetchone()[0]
    finally:
        connection.close()
    reused = context["contractor"].post(
        f"/en/cases/{context['case_id']}/evidence-exports?download=1",
        data={"csrf_token": csrf(context["contractor"])},
    )
    assert reused.status_code == 200
    assert reused.data == current_package.data
    assert reused.headers["Content-Disposition"] == 'attachment; filename="evidence-2.zip"'
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        exports = connection.execute(
            "SELECT id, version_no FROM evidence_exports WHERE case_id = ? ORDER BY version_no",
            (context["case_id"],),
        ).fetchall()
        assert [tuple(row) for row in exports] == [
            (first_export_id, 1),
            (current_export_id, 2),
        ]
    finally:
        connection.close()


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


def test_case_b_delivery_mutation_completes_while_case_a_export_compresses(
    demo_runtime, monkeypatch
) -> None:
    import app.services.evidence_export as export_module

    case_a = create_formal_context(demo_runtime)
    workflow = _submit_delivery(case_a)
    assert case_a["owner"].post(
        f"/en/cases/{case_a['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(case_a["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    _inspect(case_a, "PASS", accept=True)
    case_b = create_formal_context(demo_runtime)

    entered_compression = Event()
    release_compression = Event()
    original_write_zip = export_module._write_zip
    errors: list[BaseException] = []

    def paused_write_zip(entries):
        entered_compression.set()
        if not release_compression.wait(timeout=10):
            raise AssertionError("compression release timed out")
        return original_write_zip(entries)

    def export_case_a() -> None:
        connection = connect_database(case_a["instance_root"] / "app.sqlite3")
        try:
            prepare_evidence_export(
                connection,
                case_a["instance_root"] / "data",
                case_a["app"].config["DOCUMENT_ASSETS"],
                case_a["case_id"],
                case_a["owner_id"],
                case_a["app"].config["EVIDENCE_EXPORT_SCHEMA_PATH"],
            )
        except BaseException as error:
            errors.append(error)
        finally:
            connection.close()

    monkeypatch.setattr(export_module, "_write_zip", paused_write_zip)
    export_thread = Thread(target=export_case_a)
    export_thread.start()
    assert entered_compression.wait(timeout=10)

    mutation_done = Event()

    def mutate_case_b() -> None:
        connection = connect_database(case_b["instance_root"] / "app.sqlite3")
        try:
            submit_delivery(
                connection,
                case_b["instance_root"] / "data",
                case_b["app"].config["DOCUMENT_ASSETS"],
                case_b["case_id"],
                case_b["contractor_id"],
                "Independent case delivery",
                {
                    case_b["criterion_id"]: (
                        "Implemented the fixed criterion",
                        "local-evidence-reference",
                    )
                },
                {case_b["criterion_id"]: ("PASS", "Local check passed")},
                str(uuid.uuid4()),
            )
            mutation_done.set()
        except BaseException as error:
            errors.append(error)
        finally:
            connection.close()

    mutation_thread = Thread(target=mutate_case_b)
    mutation_thread.start()
    assert mutation_done.wait(timeout=3), "case B waited for case A ZIP compression"
    release_compression.set()
    mutation_thread.join(timeout=10)
    export_thread.join(timeout=10)
    assert not mutation_thread.is_alive()
    assert not export_thread.is_alive()
    assert errors == []


def test_same_case_source_change_during_compression_fails_export_cas(
    demo_runtime, monkeypatch
) -> None:
    import app.services.evidence_export as export_module

    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/disputes",
        data={
            "csrf_token": csrf(context["owner"]),
            "expected_revision": str(workflow["revision"]),
            "reason": "Evidence interpretation differs.",
        },
    ).status_code == 302
    workflow = current_workflow(context["instance_root"], context["case_id"])

    entered_compression = Event()
    release_compression = Event()
    original_write_zip = export_module._write_zip
    outcome: list[str] = []

    def paused_write_zip(entries):
        entered_compression.set()
        if not release_compression.wait(timeout=10):
            raise AssertionError("compression release timed out")
        return original_write_zip(entries)

    def prepare() -> None:
        connection = connect_database(context["instance_root"] / "app.sqlite3")
        try:
            prepare_evidence_export(
                connection,
                context["instance_root"] / "data",
                context["app"].config["DOCUMENT_ASSETS"],
                context["case_id"],
                context["owner_id"],
                context["app"].config["EVIDENCE_EXPORT_SCHEMA_PATH"],
            )
            outcome.append("unexpected-success")
        except EvidenceExportError as error:
            outcome.append(error.code)
        finally:
            connection.close()

    monkeypatch.setattr(export_module, "_write_zip", paused_write_zip)
    thread = Thread(target=prepare)
    thread.start()
    assert entered_compression.wait(timeout=10)
    assert context["contractor"].post(
        f"/en/cases/{context['case_id']}/disputes/{workflow['active_dispute_id']}/positions",
        data={"csrf_token": csrf(context["contractor"]), "position_text": "Contractor position."},
    ).status_code == 302
    release_compression.set()
    thread.join(timeout=10)
    assert outcome == ["export_source_changed"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence_exports WHERE case_id = ?", (context["case_id"],)
        ).fetchone()[0] == 0
    finally:
        connection.close()
    quarantine = context["instance_root"] / "data" / "cases" / context["case_id"] / "quarantine"
    assert any(path.name.startswith("exports-") for path in quarantine.iterdir())


def test_unexpected_error_response_and_log_exclude_private_content(demo_runtime, monkeypatch, caplog) -> None:
    context = create_formal_context(demo_runtime)
    private_sentinel = "PRIVATE-REQUEST-CONTENT-MUST-NOT-BE-LOGGED"

    def fail_safely(*_args, **_kwargs):
        raise RuntimeError(private_sentinel)

    monkeypatch.setattr(
        "app.routes.localized.load_delivery_dashboard_with_export_status",
        fail_safely,
    )
    caplog.set_level("ERROR")
    response = context["owner"].get(f"/en/cases/{context['case_id']}/delivery")
    assert response.status_code == 500
    body = response.get_data(as_text=True)
    log_text = caplog.text
    assert private_sentinel not in body
    assert private_sentinel not in log_text
    assert "/" + "Users/" not in log_text
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
        assert connection.execute(
            "SELECT COUNT(*) FROM case_timeline_events WHERE case_id = ? AND event_code = 'EVIDENCE_EXPORTED'",
            (context["case_id"],),
        ).fetchone()[0] == 1
    finally:
        connection.close()
    case_root = context["instance_root"] / "data" / "cases" / context["case_id"]
    assert len(list((case_root / "exports").iterdir())) == 1
    assert list((case_root / "staging").iterdir()) == []
    assert len([path for path in (case_root / "quarantine").iterdir() if path.name.startswith("exports-")]) == 1


def test_export_db_failure_after_publish_rolls_back_and_quarantines(demo_runtime) -> None:
    context = create_formal_context(demo_runtime)
    workflow = _submit_delivery(context)
    assert context["owner"].post(
        f"/en/cases/{context['case_id']}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(context["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code == 302
    _inspect(context, "PASS", accept=True)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        connection.execute(
            "CREATE TRIGGER reject_evidence_export BEFORE INSERT ON evidence_exports "
            "BEGIN SELECT RAISE(ABORT, 'forced_export_failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="forced_export_failure"):
            prepare_evidence_export(
                connection,
                context["instance_root"] / "data",
                context["app"].config["DOCUMENT_ASSETS"],
                context["case_id"],
                context["owner_id"],
                context["app"].config["EVIDENCE_EXPORT_SCHEMA_PATH"],
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence_exports WHERE case_id = ?", (context["case_id"],)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM case_timeline_events WHERE case_id = ? AND event_code = 'EVIDENCE_EXPORTED'",
            (context["case_id"],),
        ).fetchone()[0] == 0
    finally:
        connection.close()
    case_root = context["instance_root"] / "data" / "cases" / context["case_id"]
    assert list((case_root / "exports").iterdir()) == []
    assert len([path for path in (case_root / "quarantine").iterdir() if path.name.startswith("exports-")]) == 1


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
    assert b"Delivery limit reached \xc2\xb7 Review the record or open a dispute" in contractor_page.data
    assert b"Contractor action \xc2\xb7 Submit remediation" not in contractor_page.data
    assert f'action="/en/cases/{context["case_id"]}/deliveries"'.encode() not in contractor_page.data
    owner_page = context["owner"].get(f"/en/cases/{context['case_id']}/delivery")
    assert owner_page.status_code == 200
    assert b"Delivery limit reached \xc2\xb7 Review the record or open a dispute" in owner_page.data
    assert b"Waiting for remediation" not in owner_page.data
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
