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
    _human_document_lines,
    _open_item_lines,
    _wrap_to_width,
    build_candidate_bundle,
)
from app.services.candidate_workflow import create_candidate, read_candidate_component
from app.services.document_integrity import DocumentIntegrityError, validate_candidate_pdf, validate_safe_svg
from app.services.json_contracts import JsonContractError, validate_json_a
from app.db import connect_database
from tests.slice4_helpers import valid_business_draft
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
        assert manifest["template_version"] == "candidate-document/1.4"
        assert manifest["sources"]["json_a"]["sha256"] == hashlib.sha256(context["json_a_raw"]).hexdigest()
        assert manifest["sources"]["json_b"]["sha256"] == hashlib.sha256(context["json_b_raw"]).hexdigest()
        assert manifest["artifacts"]["pdf"]["sha256"] == hashlib.sha256(pdf).hexdigest()
        assert manifest["artifacts"]["svg"]["sha256"] == hashlib.sha256(svg).hexdigest()
        assert "café" in context["json_b_raw"].decode("utf-8")
        validate_candidate_pdf(pdf, ("Service Requirements and Delivery Confirmation", "Version 1"))
        reader = PdfReader(io.BytesIO(pdf), strict=True)
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert context["case_id"] not in pdf_text
        assert manifest["sources"]["json_a"]["sha256"] not in pdf_text
        assert manifest["sources"]["json_b"]["sha256"] not in pdf_text
        assert "SHA-256" not in pdf_text
        assert "one_time" not in pdf_text
        assert "third_party" not in pdf_text
        assert "JSON A" not in pdf_text
        assert "JSON B" not in pdf_text
        assert "Candidate" not in pdf_text
        assert "Formalized" not in pdf_text
        assert "client_admin" in pdf_text
        assert "contractor_proposal" not in pdf_text
        assert "Client Admin" not in pdf_text
        validate_safe_svg(svg, ("Final Requirement Blueprint",))
    finally:
        connection.close()


def test_candidate_document_humanizes_only_declared_enum_fields(demo_runtime) -> None:
    app, _instance_root, _passwords = demo_runtime
    assets = app.config["DOCUMENT_ASSETS"]
    rendered = _human_document_lines(
        {
            "responsible_party": "client",
            "billing_kind": "one_time",
            "cost_source": "third_party",
            "role_code": "client_admin",
            "status": "contractor_proposal",
            "notes": "client third_party one_time",
        },
        assets,
    )
    text = "\n".join(rendered)
    assert "Responsible Party: Client" in text
    assert "Billing Kind: One-time" in text
    assert "Cost Source: Third party" in text
    assert "Role Code: Client Admin" in text
    assert "Status: Contractor to propose" in text
    assert "Notes: client third_party one_time" in text


def test_candidate_pdf_names_distinct_open_items_with_same_section_and_resolution(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    document = json.loads(context["json_a_raw"])
    matching = [
        item for item in document["open_items"]
        if item["section_code"] == "external_services_and_assets"
        and item["resolution"] == "contractor_proposal_requested"
    ]
    assert {item["field_pointer"] for item in matching} >= {
        "/services/0/status", "/services/1/status"
    }
    bundle = build_candidate_bundle(
        _snapshot_from_context(context), context["app"].config["DOCUMENT_ASSETS"]
    )
    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(bundle.pdf)).pages)
    open_items = text.split("Open Items", 1)[1].split("Contractor Proposal", 1)[0]
    assert "Domain: Contractor proposal requested" in open_items
    assert "Hosting: Contractor proposal requested" in open_items
    assert "/services/0/status" not in open_items


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"field_pointer": "/services/999/status"}, "open_item_pointer_invalid"),
        ({"field_pointer": "/items/0/requirement_level"}, "open_item_pointer_invalid"),
        (
            {"section_code": "features", "field_pointer": f"/items/{'9' * 5000}/requirement_level"},
            "open_item_pointer_invalid",
        ),
        ({"resolution": "undecided"}, "open_item_pointer_mismatch"),
    ],
)
def test_open_item_pdf_presentation_rejects_pointer_or_resolution_mismatch(
    demo_runtime, change, error
) -> None:
    context = create_json_b_review(demo_runtime)
    document = json.loads(context["json_a_raw"])
    document["open_items"][0].update(change)
    with pytest.raises(DocumentIntegrityError, match=error):
        _open_item_lines(document, context["app"].config["DOCUMENT_ASSETS"])


def test_open_item_pdf_presentation_requires_catalog_label(demo_runtime) -> None:
    context = create_json_b_review(demo_runtime)
    assets = context["app"].config["DOCUMENT_ASSETS"]
    document = json.loads(context["json_a_raw"])
    without_domain = replace(
        assets,
        catalog={key: value for key, value in assets.catalog.items() if key != "service.domain"},
    )
    with pytest.raises(DocumentIntegrityError, match="open_item_label_missing"):
        _open_item_lines(document, without_domain)


def test_contractor_authored_role_and_service_codes_remain_verbatim_in_html_and_new_pdf(
    demo_runtime,
) -> None:
    draft = valid_business_draft()
    draft["data_and_permissions"][0]["role_code"] = "iOS app-store_v2"
    draft["third_party_services"][0]["service_code"] = "iOS app-store_v2"
    context = create_json_b_review(demo_runtime, draft=draft)

    proposal = context["owner"].get(
        f"/en/cases/{context['case_id']}/json-b/reviews/1"
    )
    assert proposal.status_code == 200
    assert proposal.data.count(b"iOS app-store_v2") == 2
    assert b"Ios App Store V2" not in proposal.data

    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        result = create_candidate(
            connection,
            context["instance_root"] / "data",
            context["app"].config["SCHEMA_PATH"],
            context["app"].config["JSON_B_SCHEMA_PATH"],
            context["app"].config["CANDIDATE_MANIFEST_SCHEMA_PATH"],
            context["app"].config["DOCUMENT_ASSETS"],
            context["case_id"],
            context["contractor_id"],
        )
        pdf = read_candidate_component(
            connection,
            context["instance_root"] / "data",
            context["case_id"],
            result.candidate_id,
            context["owner_id"],
            "document.pdf",
        )
    finally:
        connection.close()
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    assert pdf_text.count("iOS app-store_v2") == 2
    assert "Ios App Store V2" not in pdf_text
    assert "Site Administrator" in pdf_text
    assert "site_administrator" not in pdf_text


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
