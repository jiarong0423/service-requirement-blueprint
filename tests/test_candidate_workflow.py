from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import app.services.candidate_workflow as candidate_module
from app.db import connect_database
from app.services.candidate_workflow import (
    CandidateWorkflowError,
    create_candidate,
    read_candidate_component,
    reconcile_candidate_storage,
)
from app.services.candidate_documents import CandidateBundle
from app.services.document_integrity import DocumentIntegrityError
from tests.slice5_helpers import create_json_b_review


def _args(context, connection):
    app = context["app"]
    return (
        connection,
        context["instance_root"] / "data",
        app.config["SCHEMA_PATH"],
        app.config["JSON_B_SCHEMA_PATH"],
        app.config["CANDIDATE_MANIFEST_SCHEMA_PATH"],
        app.config["DOCUMENT_ASSETS"],
        context["case_id"],
        context["contractor_id"],
    )


def test_render_failure_leaves_no_candidate_files_or_rows(demo_runtime, monkeypatch) -> None:
    context = create_json_b_review(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        def fail_render(*_args, **_kwargs):
            raise DocumentIntegrityError("forced_render_failure")

        monkeypatch.setattr(candidate_module, "build_candidate_bundle", fail_render)
        with pytest.raises(CandidateWorkflowError, match="candidate_build_failed"):
            create_candidate(*_args(context, connection))
        assert connection.execute("SELECT COUNT(*) FROM candidate_versions").fetchone()[0] == 0
        assert connection.execute("SELECT state FROM cases WHERE id = ?", (context["case_id"],)).fetchone()[0] == "JSON_B_REVIEW"
        candidates = context["instance_root"] / "data" / "cases" / context["case_id"] / "candidates"
        assert not candidates.exists() or not any(candidates.iterdir())
    finally:
        connection.close()


def test_database_failure_rolls_back_and_quarantines_published_bundle(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        connection.execute(
            "CREATE TRIGGER reject_slice5_candidate BEFORE INSERT ON candidate_versions "
            "BEGIN SELECT RAISE(ABORT, 'forced_candidate_failure'); END"
        )
        with pytest.raises(CandidateWorkflowError, match="candidate_commit_failed"):
            create_candidate(*_args(context, connection))
        assert connection.execute("SELECT COUNT(*) FROM candidate_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts WHERE kind LIKE 'candidate_%'").fetchone()[0] == 0
        assert connection.execute("SELECT state, active_candidate_id FROM cases WHERE id = ?", (context["case_id"],)).fetchone()[:] == ("JSON_B_REVIEW", None)
        case_root = context["instance_root"] / "data" / "cases" / context["case_id"]
        assert not any((case_root / "candidates").iterdir())
        assert len(list((case_root / "quarantine").iterdir())) == 1
    finally:
        connection.close()


def test_tampered_component_makes_entire_candidate_unavailable(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        result = create_candidate(*_args(context, connection))
        row = connection.execute(
            "SELECT relative_path FROM artifacts WHERE id = (SELECT pdf_artifact_id FROM candidate_versions WHERE id = ?)",
            (result.candidate_id,),
        ).fetchone()
        target = context["instance_root"] / "data" / row["relative_path"]
        target.chmod(0o600)
        target.write_bytes(target.read_bytes() + b"tamper")
        target.chmod(0o400)
        for filename in ("manifest.json", "document.pdf", "diagram.svg"):
            with pytest.raises(CandidateWorkflowError, match="candidate_integrity_invalid"):
                read_candidate_component(
                    connection, context["instance_root"] / "data", context["case_id"], result.candidate_id,
                    context["owner_id"], filename,
                )
    finally:
        connection.close()


def test_reconciliation_quarantines_orphan_staging_and_final_directories(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    data_root = context["instance_root"] / "data"
    case_root = data_root / "cases" / context["case_id"]
    staging = case_root / "staging" / str(uuid.uuid4())
    orphan = case_root / "candidates" / str(uuid.uuid4())
    staging.mkdir(parents=True)
    orphan.mkdir(parents=True)
    (staging / "partial").write_text("partial")
    (orphan / "manifest.json").write_text("orphan")
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        reconcile_candidate_storage(connection, data_root)
    finally:
        connection.close()
    assert not any((case_root / "staging").iterdir())
    assert not any((case_root / "candidates").iterdir())
    quarantined = {entry.name for entry in (case_root / "quarantine").iterdir()}
    assert len(quarantined) == 2
    assert any(name.startswith("staging-") for name in quarantined)


def test_expired_quote_rejects_without_files(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        connection.execute(
            "UPDATE json_b_versions SET quote_expires_at = '2000-01-01T00:00:00Z' WHERE case_id = ?",
            (context["case_id"],),
        )
        with pytest.raises(CandidateWorkflowError, match="quote_expired"):
            create_candidate(*_args(context, connection))
        assert connection.execute("SELECT COUNT(*) FROM candidate_versions").fetchone()[0] == 0
    finally:
        connection.close()


def test_manifest_cannot_misstate_saved_artifact_digest(demo_runtime, monkeypatch) -> None:
    context = create_json_b_review(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    original = candidate_module.build_candidate_bundle

    def build_mismatched_manifest(*args, **kwargs):
        bundle = original(*args, **kwargs)
        manifest = json.loads(bundle.manifest)
        manifest["artifacts"]["pdf"]["sha256"] = "0" * 64
        raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        return CandidateBundle(manifest=raw, pdf=bundle.pdf, svg=bundle.svg)

    try:
        monkeypatch.setattr(candidate_module, "build_candidate_bundle", build_mismatched_manifest)
        with pytest.raises(CandidateWorkflowError, match="candidate_build_failed"):
            create_candidate(*_args(context, connection))
        assert connection.execute("SELECT COUNT(*) FROM candidate_versions").fetchone()[0] == 0
        candidate_root = context["instance_root"] / "data" / "cases" / context["case_id"] / "candidates"
        assert not candidate_root.exists() or not any(candidate_root.iterdir())
    finally:
        connection.close()


def test_json_b_must_bind_the_complete_json_a_source_object(demo_runtime, monkeypatch) -> None:
    context = create_json_b_review(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    original = candidate_module._read_document

    def read_with_mismatched_source(data_root, row, schema_path, validator):
        document, raw = original(data_root, row, schema_path, validator)
        if row["kind"] == "json_b":
            document = json.loads(json.dumps(document))
            document["source_json_a"]["document_id"] = str(uuid.uuid4())
        return document, raw

    try:
        monkeypatch.setattr(candidate_module, "_read_document", read_with_mismatched_source)
        with pytest.raises(CandidateWorkflowError, match="source_mismatch"):
            create_candidate(*_args(context, connection))
        assert connection.execute("SELECT COUNT(*) FROM candidate_versions").fetchone()[0] == 0
    finally:
        connection.close()


def test_reconciliation_rejects_active_pointer_mismatch(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        create_candidate(*_args(context, connection))
        connection.execute(
            "UPDATE cases SET state = 'JSON_B_REVIEW', active_candidate_id = NULL WHERE id = ?",
            (context["case_id"],),
        )
        with pytest.raises(CandidateWorkflowError, match="candidate_database_invalid"):
            reconcile_candidate_storage(
                connection,
                context["instance_root"] / "data",
                context["app"].config["DOCUMENT_ASSETS"],
            )
    finally:
        connection.close()


def test_reconciliation_rejects_missing_candidate_storage_root(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    data_root = context["instance_root"] / "data"
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        create_candidate(*_args(context, connection))
        (data_root / "cases").rename(data_root / "cases-withheld-for-test")
        with pytest.raises(CandidateWorkflowError, match="candidate_storage_missing"):
            reconcile_candidate_storage(
                connection,
                data_root,
                context["app"].config["DOCUMENT_ASSETS"],
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("column", "replacement"),
    (("kind", "candidate_pdf_wrong"), ("visibility", "private"), ("lifecycle", "superseded")),
)
def test_candidate_component_requires_expected_artifact_contract(
    demo_runtime,
    column: str,
    replacement: str,
) -> None:
    context = create_json_b_review(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        result = create_candidate(*_args(context, connection))
        artifact_id = connection.execute(
            "SELECT pdf_artifact_id FROM candidate_versions WHERE id = ?",
            (result.candidate_id,),
        ).fetchone()[0]
        connection.execute(f"UPDATE artifacts SET {column} = ? WHERE id = ?", (replacement, artifact_id))
        with pytest.raises(CandidateWorkflowError, match="candidate_unavailable"):
            read_candidate_component(
                connection,
                context["instance_root"] / "data",
                context["case_id"],
                result.candidate_id,
                context["owner_id"],
                "document.pdf",
                context["app"].config["DOCUMENT_ASSETS"],
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("column", "expected_error"),
    (
        ("case_id", "candidate_unavailable"),
        ("author_id", "candidate_unavailable"),
        ("version_no", "candidate_unavailable"),
        ("schema_version", "candidate_unavailable"),
        ("relative_path", "candidate_integrity_invalid"),
    ),
)
def test_candidate_reads_and_startup_require_complete_artifact_row_coherence(
    demo_runtime,
    column: str,
    expected_error: str,
) -> None:
    context = create_json_b_review(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        result = create_candidate(*_args(context, connection))
        artifact = connection.execute(
            "SELECT id, relative_path FROM artifacts WHERE id = "
            "(SELECT pdf_artifact_id FROM candidate_versions WHERE id = ?)",
            (result.candidate_id,),
        ).fetchone()
        if column == "case_id":
            replacement = str(uuid.uuid4())
            now = candidate_module._now()
            connection.execute(
                "INSERT INTO cases(id, owner_id, title, state, content_revision, created_at, updated_at) "
                "VALUES (?, ?, 'Coherence Test', 'REQUIREMENT_DRAFT', 1, ?, ?)",
                (replacement, context["owner_id"], now, now),
            )
        elif column == "author_id":
            replacement = context["owner_id"]
        elif column == "version_no":
            replacement = 99
        elif column == "schema_version":
            replacement = "candidate-document/wrong"
        else:
            replacement = artifact["relative_path"].replace("document.pdf", "misdirected.pdf")
        connection.execute(f"UPDATE artifacts SET {column} = ? WHERE id = ?", (replacement, artifact["id"]))

        with pytest.raises(CandidateWorkflowError, match=expected_error):
            read_candidate_component(
                connection,
                context["instance_root"] / "data",
                context["case_id"],
                result.candidate_id,
                context["owner_id"],
                "document.pdf",
                context["app"].config["DOCUMENT_ASSETS"],
            )
        with pytest.raises(CandidateWorkflowError):
            reconcile_candidate_storage(
                connection,
                context["instance_root"] / "data",
                context["app"].config["DOCUMENT_ASSETS"],
            )
    finally:
        connection.close()
