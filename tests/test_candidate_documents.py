from __future__ import annotations

import hashlib
import io
import json
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics

from app.services.candidate_documents import (
    CandidateSnapshot,
    SourceSnapshot,
    _wrap_to_width,
    build_candidate_bundle,
)
from app.services.candidate_workflow import create_candidate, read_candidate_component
from app.services.document_integrity import validate_candidate_pdf, validate_safe_svg
from app.services.json_contracts import JsonContractError, validate_json_a
from app.db import connect_database
from tests.slice5_helpers import create_json_b_review


def test_candidate_bundle_binds_sources_and_has_safe_readable_outputs(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    app = context["app"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        result = create_candidate(
            connection,
            context["instance_root"] / "data",
            app.config["SCHEMA_PATH"],
            app.config["JSON_B_SCHEMA_PATH"],
            app.config["CANDIDATE_MANIFEST_SCHEMA_PATH"],
            app.config["DOCUMENT_ASSETS"],
            context["case_id"],
            context["contractor_id"],
        )
        manifest_raw = read_candidate_component(
            connection, context["instance_root"] / "data", context["case_id"], result.candidate_id,
            context["owner_id"], "manifest.json"
        )
        pdf = read_candidate_component(
            connection, context["instance_root"] / "data", context["case_id"], result.candidate_id,
            context["contractor_id"], "document.pdf"
        )
        svg = read_candidate_component(
            connection, context["instance_root"] / "data", context["case_id"], result.candidate_id,
            context["owner_id"], "diagram.svg"
        )
        manifest = json.loads(manifest_raw)
        schema = json.loads(app.config["CANDIDATE_MANIFEST_SCHEMA_PATH"].read_text())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)
        assert manifest["sources"]["json_a"]["sha256"] == hashlib.sha256(context["json_a_raw"]).hexdigest()
        assert manifest["sources"]["json_b"]["sha256"] == hashlib.sha256(context["json_b_raw"]).hexdigest()
        assert manifest["artifacts"]["pdf"]["sha256"] == hashlib.sha256(pdf).hexdigest()
        assert manifest["artifacts"]["svg"]["sha256"] == hashlib.sha256(svg).hexdigest()
        assert "café" in context["json_b_raw"].decode("utf-8")
        validate_candidate_pdf(pdf, ("Fixed document", context["case_id"]))
        validate_safe_svg(svg, ("Final Requirement Blueprint",))
    finally:
        connection.close()


def test_candidate_retry_returns_same_saved_identity(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    app = context["app"]
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        args = (
            connection, context["instance_root"] / "data",
            app.config["SCHEMA_PATH"], app.config["JSON_B_SCHEMA_PATH"],
            app.config["CANDIDATE_MANIFEST_SCHEMA_PATH"], app.config["DOCUMENT_ASSETS"],
            context["case_id"], context["contractor_id"],
        )
        first = create_candidate(*args)
        before = read_candidate_component(
            connection, context["instance_root"] / "data", context["case_id"], first.candidate_id,
            context["contractor_id"], "document.pdf"
        )
        second = create_candidate(*args)
        after = read_candidate_component(
            connection, context["instance_root"] / "data", context["case_id"], second.candidate_id,
            context["owner_id"], "document.pdf"
        )
        assert first.created is True
        assert second.created is False
        assert second.candidate_id == first.candidate_id
        assert after == before
        assert connection.execute("SELECT COUNT(*) FROM candidate_versions").fetchone()[0] == 1
    finally:
        connection.close()


def test_candidate_preserves_complete_long_blueprint_and_body_text(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    app = context["app"]
    json_a = json.loads(context["json_a_raw"])
    json_b = json.loads(context["json_b_raw"])
    node_labels = ["Node 1 " + "漢" * 220, "Node 2 " + "B" * 220, "Node 3 " + "C" * 220]
    edge_labels = ["Edge 1 " + "界" * 220, "Edge 2 " + "Y" * 220]
    for node, label in zip(json_a["blueprint"]["nodes"], node_labels, strict=True):
        node["label"] = label
    for edge, label in zip(json_a["blueprint"]["edges"], edge_labels, strict=True):
        edge["label"] = label
    long_body = "LONGVALUE" + "Q" * 3500 + "ENDVALUE"
    json_b["requirement_understanding"] = long_body
    raw_a = (json.dumps(json_a, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    raw_b = (json.dumps(json_b, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    snapshot = replace(
        _snapshot_from_context(context),
        json_a=SourceSnapshot(
            artifact_id=json_b["source_json_a"]["artifact_id"], document_id=json_a["document_id"],
            version=json_a["version"], schema_version=json_a["schema_version"],
            sha256=hashlib.sha256(raw_a).hexdigest(), byte_length=len(raw_a), raw=raw_a, document=json_a,
        ),
        json_b=SourceSnapshot(
            artifact_id=context["json_b_artifact_id"], document_id=json_b["document_id"],
            version=json_b["version"], schema_version=json_b["schema_version"],
            sha256=hashlib.sha256(raw_b).hexdigest(), byte_length=len(raw_b), raw=raw_b, document=json_b,
        ),
    )
    bundle = build_candidate_bundle(snapshot, app.config["DOCUMENT_ASSETS"])
    svg_text = "".join(ET.fromstring(bundle.svg).itertext())
    for label in (*node_labels, *edge_labels):
        assert label in svg_text
    extracted = "".join(
        (page.extract_text() or "") for page in PdfReader(io.BytesIO(bundle.pdf)).pages
    )
    compact = "".join(extracted.split())
    assert "".join(long_body.split()) in compact
    for label in (*node_labels, *edge_labels):
        assert "".join(label.split()) in compact
    pdf_node_lines = _wrap_to_width(
        node_labels[0], font_name="CandidateNotoSans", font_size=7, max_width=440
    )
    pdf_edge_lines = _wrap_to_width(
        edge_labels[0], font_name="CandidateNotoSans", font_size=6, max_width=380
    )
    svg_node_lines = _wrap_to_width(
        node_labels[0], font_name="CandidateNotoSans", font_size=14, max_width=720
    )
    assert all(pdfmetrics.stringWidth(line, "CandidateNotoSans", 7) <= 440 for line in pdf_node_lines)
    assert all(pdfmetrics.stringWidth(line, "CandidateNotoSans", 6) <= 380 for line in pdf_edge_lines)
    assert all(pdfmetrics.stringWidth(line, "CandidateNotoSans", 14) <= 720 for line in svg_node_lines)


def test_json_a_rejects_blueprint_edge_labels_over_240_characters(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    document = json.loads(context["json_a_raw"])
    document["blueprint"]["edges"][0]["label"] = "E" * 241
    with pytest.raises(JsonContractError, match="schema_validation_failed"):
        validate_json_a(document, context["app"].config["SCHEMA_PATH"])


def _snapshot_from_context(context):
    json_a = json.loads(context["json_a_raw"])
    json_b = json.loads(context["json_b_raw"])
    return CandidateSnapshot(
        candidate_id="00000000-0000-4000-8000-000000000010",
        case_id=context["case_id"],
        version=1,
        created_at="2026-10-06T00:00:00+00:00",
        json_a=SourceSnapshot(
            artifact_id=json_b["source_json_a"]["artifact_id"], document_id=json_a["document_id"],
            version=json_a["version"], schema_version=json_a["schema_version"],
            sha256=hashlib.sha256(context["json_a_raw"]).hexdigest(), byte_length=len(context["json_a_raw"]),
            raw=context["json_a_raw"], document=json_a,
        ),
        json_b=SourceSnapshot(
            artifact_id=context["json_b_artifact_id"], document_id=json_b["document_id"],
            version=json_b["version"], schema_version=json_b["schema_version"],
            sha256=hashlib.sha256(context["json_b_raw"]).hexdigest(), byte_length=len(context["json_b_raw"]),
            raw=context["json_b_raw"], document=json_b,
        ),
    )
