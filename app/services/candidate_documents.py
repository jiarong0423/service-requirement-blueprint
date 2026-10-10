from __future__ import annotations

import hashlib
import html
import io
import json
import re
from dataclasses import dataclass
from typing import Any

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

from app.services.document_integrity import (
    DocumentAssets,
    DocumentIntegrityError,
    ensure_supported_text,
    validate_candidate_pdf,
    validate_safe_svg,
)
from app.presentation_semantics import HIDDEN_PRESENTATION_FIELDS, human_scalar


MANIFEST_SCHEMA_VERSION = "candidate-manifest/1.0"
TEMPLATE_VERSION = "candidate-document/1.4"

MACHINE_ONLY_DOCUMENT_FIELDS = frozenset({
    "schema_version",
    "document_type",
    "document_id",
    "version",
    "generated_at",
    "case_id",
    "source_json_a",
})

@dataclass(frozen=True)
class SourceSnapshot:
    artifact_id: str
    document_id: str
    version: int
    schema_version: str
    sha256: str
    byte_length: int
    raw: bytes
    document: dict[str, Any]


@dataclass(frozen=True)
class CandidateSnapshot:
    candidate_id: str
    case_id: str
    version: int
    created_at: str
    json_a: SourceSnapshot
    json_b: SourceSnapshot


@dataclass(frozen=True)
class CandidateBundle:
    manifest: bytes
    pdf: bytes
    svg: bytes

    @property
    def files(self) -> dict[str, bytes]:
        return {"manifest.json": self.manifest, "document.pdf": self.pdf, "diagram.svg": self.svg}


@dataclass(frozen=True)
class DiagramNode:
    identifier: str
    label: str


@dataclass(frozen=True)
class DiagramEdge:
    source: str
    target: str
    label: str


@dataclass(frozen=True)
class DiagramModel:
    nodes: tuple[DiagramNode, ...]
    edges: tuple[DiagramEdge, ...]


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _record(filename: str, media_type: str, raw: bytes) -> dict[str, object]:
    return {"filename": filename, "media_type": media_type, "sha256": _sha256(raw), "byte_length": len(raw)}


def _source_record(source: SourceSnapshot) -> dict[str, object]:
    return {
        "artifact_id": source.artifact_id,
        "document_id": source.document_id,
        "version": source.version,
        "schema_version": source.schema_version,
        "sha256": source.sha256,
        "byte_length": source.byte_length,
    }


def _xml_text(value: object) -> str:
    return html.escape(str(value), quote=False)


def _wrap_to_width(
    value: str,
    *,
    font_name: str,
    font_size: float,
    max_width: float,
) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in value:
        if character == "\n":
            lines.append(current)
            current = ""
            continue
        candidate = current + character
        if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > max_width:
            lines.append(current)
            current = character
        else:
            current = candidate
    lines.append(current)
    return lines or [""]


def _field_label(key: str, assets: DocumentAssets) -> str:
    labels = assets.labels.get("fields")
    if not isinstance(labels, dict):
        raise DocumentIntegrityError("field_label_map_invalid")
    value = labels.get(key)
    if not isinstance(value, str) or not value:
        raise DocumentIntegrityError("field_label_missing")
    return value


def _diagram_model(snapshot: CandidateSnapshot) -> DiagramModel:
    blueprint = snapshot.json_a.document.get("blueprint")
    if not isinstance(blueprint, dict):
        raise DocumentIntegrityError("blueprint_invalid")
    nodes = blueprint.get("nodes")
    edges = blueprint.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list) or len(nodes) != 3 or len(edges) != 2:
        raise DocumentIntegrityError("blueprint_size_invalid")
    model_nodes: list[DiagramNode] = []
    identifiers: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not isinstance(node.get("label"), str):
            raise DocumentIntegrityError("blueprint_invalid")
        identifier = node["id"]
        if identifier in identifiers:
            raise DocumentIntegrityError("blueprint_invalid")
        identifiers.add(identifier)
        model_nodes.append(DiagramNode(identifier, node["label"]))
    model_edges: list[DiagramEdge] = []
    for edge in edges:
        if (
            not isinstance(edge, dict)
            or not isinstance(edge.get("source"), str)
            or not isinstance(edge.get("target"), str)
            or not isinstance(edge.get("label"), str)
            or edge["source"] not in identifiers
            or edge["target"] not in identifiers
        ):
            raise DocumentIntegrityError("blueprint_edge_invalid")
        model_edges.append(DiagramEdge(edge["source"], edge["target"], edge["label"]))
    return DiagramModel(tuple(model_nodes), tuple(model_edges))


def _render_candidate_svg(model: DiagramModel, assets: DocumentAssets) -> bytes:
    width = 900
    box_width = 760
    prepared_nodes = [
        (
            node,
            _wrap_to_width(
                node.label,
                font_name="CandidateNotoSans",
                font_size=14,
                max_width=box_width - 40,
            ),
        )
        for node in model.nodes
    ]
    prepared_edges = [
        (
            edge,
            _wrap_to_width(
                edge.label,
                font_name="CandidateNotoSans",
                font_size=11,
                max_width=box_width - 120,
            ),
        )
        for edge in model.edges
    ]
    box_heights = [max(70, 34 + len(lines) * 18) for _node, lines in prepared_nodes]
    edge_gaps = [max(82, 38 + len(lines) * 18) for _edge, lines in prepared_edges]
    height = 100 + sum(box_heights) + sum(edge_gaps) + 45
    labels = assets.labels["svg"]
    positions: dict[str, tuple[float, float, float]] = {}
    node_fragments: list[str] = []
    y = 82
    for index, (node, lines) in enumerate(prepared_nodes):
        box_height = box_heights[index]
        x = (width - box_width) / 2
        positions[node.identifier] = (x, y, box_height)
        tspans = "".join(
            f'<tspan x="{x + box_width / 2:.1f}" y="{y + 28 + line_index * 18:.1f}">{_xml_text(line)}</tspan>'
            for line_index, line in enumerate(lines)
        )
        node_fragments.append(
            f'<g><rect x="{x}" y="{y}" width="{box_width}" height="{box_height}" rx="10" ry="10" '
            'fill="#e0f2fe" stroke="#0284c7" stroke-width="2"/>'
            f'<text font-family="Noto Sans" font-size="14" fill="#0c4a6e" text-anchor="middle">{tspans}</text></g>'
        )
        if index < len(edge_gaps):
            y += box_height + edge_gaps[index]
    edge_fragments: list[str] = []
    for edge, lines in prepared_edges:
        source_x, source_y, source_height = positions[edge.source]
        target_x, target_y, _target_height = positions[edge.target]
        x1, y1 = source_x + box_width / 2, source_y + source_height
        x2, y2 = target_x + box_width / 2, target_y
        direction = 1 if y2 >= y1 else -1
        line_end = y2 - direction * 10
        middle_x = (x1 + x2) / 2
        middle_y = (y1 + y2) / 2 - (len(lines) - 1) * 9
        tspans = "".join(
            f'<tspan x="{middle_x + 18:.1f}" y="{middle_y + line_index * 18:.1f}">{_xml_text(line)}</tspan>'
            for line_index, line in enumerate(lines)
        )
        edge_fragments.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{line_end:.1f}" '
            'stroke="#64748b" stroke-width="2"/>'
            f'<path d="M {x2 - 7:.1f} {line_end - direction * 10:.1f} L {x2:.1f} {y2:.1f} '
            f'L {x2 + 7:.1f} {line_end - direction * 10:.1f} Z" fill="#64748b"/>'
            f'<text font-family="Noto Sans" font-size="11" fill="#475569" text-anchor="start">{tspans}</text>'
        )
    raw = (
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title description">'
        f'<title id="title">{_xml_text(labels["title"])}</title>'
        f'<desc id="description">{_xml_text(labels["description"])}</desc>'
        '<rect x="1" y="1" width="99.7%" height="99.5%" rx="12" ry="12" fill="#f8fafc" stroke="#cbd5e1"/>'
        f'<text x="35" y="45" font-family="Noto Sans" font-size="20" font-weight="700" fill="#0f172a">{_xml_text(labels["title"])}</text>'
        + "".join(edge_fragments)
        + "".join(node_fragments)
        + "</svg>"
    ).encode("utf-8")
    validate_safe_svg(raw, (str(labels["title"]), str(labels["description"])))
    return raw


def _blueprint_drawing(model: DiagramModel, assets: DocumentAssets) -> Drawing:
    box_width = 460
    prepared = [
        (
            node,
            _wrap_to_width(
                node.label,
                font_name="CandidateNotoSans",
                font_size=7,
                max_width=box_width - 20,
            ),
        )
        for node in model.nodes
    ]
    prepared_edges = [
        (
            edge,
            _wrap_to_width(
                edge.label,
                font_name="CandidateNotoSans",
                font_size=6,
                max_width=box_width - 80,
            ),
        )
        for edge in model.edges
    ]
    box_heights = [max(54, 25 + len(lines) * 9) for _node, lines in prepared]
    edge_gaps = [max(46, 24 + len(lines) * 9) for _edge, lines in prepared_edges]
    drawing_height = 50 + sum(box_heights) + sum(edge_gaps) + 24
    drawing = Drawing(500, drawing_height)
    drawing.add(Rect(0, 0, 500, drawing_height, fillColor=colors.HexColor("#F8FAFC"), strokeColor=colors.HexColor("#CBD5E1"), rx=10, ry=10))
    drawing.add(String(18, drawing_height - 28, str(assets.labels["svg"]["title"]), fontName="CandidateNotoSans-Bold", fontSize=10, fillColor=colors.HexColor("#0F172A")))
    positions: dict[str, tuple[float, float, float]] = {}
    y = drawing_height - 50 - box_heights[0]
    for index, (node, lines) in enumerate(prepared):
        x = 20
        box_height = box_heights[index]
        positions[node.identifier] = (x, y, box_height)
        drawing.add(Rect(x, y, box_width, box_height, fillColor=colors.HexColor("#E0F2FE"), strokeColor=colors.HexColor("#0284C7"), rx=7, ry=7))
        for line_index, line in enumerate(lines):
            drawing.add(String(x + 10, y + box_height - 18 - line_index * 9, line, fontName="CandidateNotoSans", fontSize=7, fillColor=colors.HexColor("#0C4A6E")))
        if index < len(edge_gaps):
            y -= edge_gaps[index] + box_heights[index + 1]
    for edge, lines in prepared_edges:
        source_x, source_y, source_height = positions[edge.source]
        target_x, target_y, target_height = positions[edge.target]
        x = source_x + box_width / 2
        source_anchor = source_y if source_y > target_y else source_y + source_height
        target_anchor = target_y + target_height if source_y > target_y else target_y
        direction = -1 if target_anchor < source_anchor else 1
        line_end = target_anchor - direction * 7
        drawing.add(Line(x, source_anchor, x, line_end, strokeColor=colors.HexColor("#64748B"), strokeWidth=1.2))
        drawing.add(Polygon([x - 4, line_end - direction * 7, x, target_anchor, x + 4, line_end - direction * 7], fillColor=colors.HexColor("#64748B"), strokeColor=colors.HexColor("#64748B")))
        middle_y = (source_anchor + target_anchor) / 2 + (len(lines) - 1) * 4.5
        for line_index, line in enumerate(lines):
            drawing.add(String(x + 10, middle_y - line_index * 9, line, fontName="CandidateNotoSans", fontSize=6, fillColor=colors.HexColor("#475569")))
    return drawing


def _human_document_lines(
    value: object,
    assets: DocumentAssets,
    depth: int = 0,
    field_key: str | None = None,
    *,
    contractor_authored: bool = False,
) -> list[str]:
    prefix = "  " * depth
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if (
                key.endswith("_id")
                or key in MACHINE_ONLY_DOCUMENT_FIELDS
                or key in HIDDEN_PRESENTATION_FIELDS
            ):
                continue
            labels = assets.labels.get("fields", {})
            mapped = labels.get(key) if isinstance(labels, dict) else None
            label = mapped if isinstance(mapped, str) and mapped else key.replace("_", " ").title()
            nested = _human_document_lines(
                item,
                assets,
                depth + 1,
                key,
                contractor_authored=contractor_authored,
            )
            if not nested:
                lines.append(f"{prefix}{label}: None")
            elif len(nested) == 1 and not isinstance(item, (dict, list)):
                lines.append(f"{prefix}{label}: {nested[0].strip()}")
            else:
                lines.append(f"{prefix}{label}")
                lines.extend(nested)
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix}None"]
        lines = []
        for item in value:
            nested = _human_document_lines(
                item,
                assets,
                depth + 1,
                field_key,
                contractor_authored=contractor_authored,
            )
            if len(nested) == 1:
                lines.append(f"{prefix}• {nested[0].strip()}")
            else:
                lines.append(f"{prefix}•")
                lines.extend(nested)
        return lines
    if value is True:
        return [f"{prefix}Yes"]
    if value is False:
        return [f"{prefix}No"]
    if value is None:
        return [f"{prefix}Not applicable"]
    return [
        f"{prefix}{human_scalar(field_key, value, contractor_authored=contractor_authored)}"
    ]


def _open_item_lines(document: dict[str, Any], assets: DocumentAssets) -> list[str]:
    items = document.get("open_items")
    if not isinstance(items, list):
        raise DocumentIntegrityError("open_items_invalid")
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"section_code", "field_pointer", "resolution", "note"}:
            raise DocumentIntegrityError("open_item_invalid")
        section = item["section_code"]
        pointer = item["field_pointer"]
        resolution = item["resolution"]
        note = item["note"]
        if (
            not isinstance(section, str)
            or not isinstance(pointer, str)
            or not isinstance(resolution, str)
            or not isinstance(note, str)
            or (section, pointer) in seen
        ):
            raise DocumentIntegrityError("open_item_invalid")
        seen.add((section, pointer))
        source = document.get(section)
        if not isinstance(source, dict):
            raise DocumentIntegrityError("open_item_pointer_invalid")
        if section == "delivery_and_constraints":
            if (
                pointer != "/budget_status"
                or source.get("budget_status") != resolution
                or resolution not in ("undecided", "contractor_proposal_requested")
            ):
                raise DocumentIntegrityError("open_item_pointer_mismatch")
            catalog_key = "field.budget_status"
        else:
            if section == "features":
                match = re.fullmatch(r"/items/(0|[1-9][0-9]*)/requirement_level", pointer)
                collection, code_key, label_prefix = "items", "feature_code", "feature"
            elif section == "external_services_and_assets":
                match = re.fullmatch(r"/(services|assets)/(0|[1-9][0-9]*)/status", pointer)
                collection = match.group(1) if match else ""
                code_key = "service_code" if collection == "services" else "asset_code"
                label_prefix = "service" if collection == "services" else "asset"
            else:
                raise DocumentIntegrityError("open_item_pointer_invalid")
            if match is None:
                raise DocumentIntegrityError("open_item_pointer_invalid")
            entries = source.get(collection)
            if not isinstance(entries, list) or not entries:
                raise DocumentIntegrityError("open_item_pointer_invalid")
            index_text = match.group(1) if section == "features" else match.group(2)
            maximum_text = str(len(entries) - 1)
            if len(index_text) > len(maximum_text) or (
                len(index_text) == len(maximum_text) and index_text > maximum_text
            ):
                raise DocumentIntegrityError("open_item_pointer_invalid")
            index = int(index_text)
            if not isinstance(entries[index], dict):
                raise DocumentIntegrityError("open_item_pointer_invalid")
            entry = entries[index]
            value = entry.get("requirement_level" if section == "features" else "status")
            if not isinstance(value, str):
                raise DocumentIntegrityError("open_item_pointer_mismatch")
            if section == "features":
                expected_resolution = "undecided" if value == "undecided" else None
            else:
                expected_resolution = {
                    "to_select": "undecided",
                    "contractor_proposal": "contractor_proposal_requested",
                }.get(value)
            if expected_resolution != resolution:
                raise DocumentIntegrityError("open_item_pointer_mismatch")
            code = entry.get(code_key)
            if not isinstance(code, str):
                raise DocumentIntegrityError("open_item_label_missing")
            catalog_key = f"{label_prefix}.{code}"
        label = assets.catalog.get(catalog_key)
        if not isinstance(label, str) or not label:
            raise DocumentIntegrityError("open_item_label_missing")
        lines.append(f"  • {label}: {human_scalar('resolution', resolution)}" + (f" — {note}" if note else ""))
    return lines or ["  None"]


def _render_candidate_pdf(
    snapshot: CandidateSnapshot,
    model: DiagramModel,
    assets: DocumentAssets,
) -> bytes:
    labels = assets.labels["pdf"]
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    title = ParagraphStyle("CandidateTitle", parent=styles["Title"], fontName="CandidateNotoSans-Bold", fontSize=18, leading=23, textColor=colors.HexColor("#0F172A"))
    heading = ParagraphStyle("CandidateHeading", parent=styles["Heading2"], fontName="CandidateNotoSans-Bold", fontSize=12, leading=16, textColor=colors.HexColor("#0C4A6E"), spaceBefore=8, spaceAfter=6)
    body = ParagraphStyle("CandidateBody", parent=styles["BodyText"], fontName="CandidateNotoSans", fontSize=8.5, leading=12)
    code = ParagraphStyle("CandidateCode", parent=body, fontName="CandidateNotoSans", fontSize=7, leading=9, leftIndent=6, borderColor=colors.HexColor("#CBD5E1"), borderWidth=0.5, borderPadding=6, splitLongWords=1, wordWrap="LTR")
    notice = ParagraphStyle("CandidateNotice", parent=body, alignment=TA_CENTER, backColor=colors.HexColor("#FEF3C7"), borderColor=colors.HexColor("#F59E0B"), borderWidth=0.5, borderPadding=7, spaceAfter=12)
    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm, title=str(labels["title"]), author="Service Requirement Blueprint")
    story: list[object] = [
        Paragraph(html.escape(str(labels["title"])), title),
        Paragraph(html.escape(str(labels["candidate_notice"])), notice),
        Paragraph("Shared Version", heading),
        Paragraph(f"Version {snapshot.version}<br/>Prepared {html.escape(snapshot.created_at)}", body),
        Paragraph(html.escape(str(labels["blueprint"])), heading),
        _blueprint_drawing(model, assets),
        PageBreak(),
        Paragraph(html.escape(str(labels["client_requirements"])), heading),
    ]
    for key, value in snapshot.json_a.document.items():
        if key in MACHINE_ONLY_DOCUMENT_FIELDS:
            continue
        story.append(Paragraph(html.escape(_field_label(key, assets)), heading))
        rendered = (
            _open_item_lines(snapshot.json_a.document, assets)
            if key == "open_items"
            else _human_document_lines(value, assets, field_key=key)
        )
        story.append(Paragraph("<br/>".join(html.escape(line) or "&#160;" for line in rendered), code))
    story.extend([PageBreak(), Paragraph(html.escape(str(labels["contractor_proposal"])), heading)])
    for key, value in snapshot.json_b.document.items():
        if key in MACHINE_ONLY_DOCUMENT_FIELDS:
            continue
        story.append(Paragraph(html.escape(_field_label(key, assets)), heading))
        rendered = _human_document_lines(
            value,
            assets,
            field_key=key,
            contractor_authored=True,
        )
        story.append(Paragraph("<br/>".join(html.escape(line) or "&#160;" for line in rendered), code))
    try:
        document.build(story)
    except Exception as error:
        raise DocumentIntegrityError("pdf_render_failed") from error
    raw = buffer.getvalue()
    required = (str(labels["title"]), str(labels["candidate_notice"]), f"Version {snapshot.version}")
    validate_candidate_pdf(raw, required)
    return raw


def _build_manifest(snapshot: CandidateSnapshot, assets: DocumentAssets, pdf: bytes, svg: bytes) -> bytes:
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": "candidate",
        "candidate_id": snapshot.candidate_id,
        "case_id": snapshot.case_id,
        "version": snapshot.version,
        "created_at": snapshot.created_at,
        "label_mode": "english_only",
        "template_version": TEMPLATE_VERSION,
        "sources": {"json_a": _source_record(snapshot.json_a), "json_b": _source_record(snapshot.json_b)},
        "catalog": {"locale": "en", **assets.catalog_record.__dict__},
        "label_map": {"schema_version": "candidate-label-map/1.0", **assets.label_record.__dict__},
        "fonts": {key: record.__dict__ for key, record in assets.font_records.items()},
        "artifacts": {"pdf": _record("document.pdf", "application/pdf", pdf), "svg": _record("diagram.svg", "image/svg+xml", svg)},
    }
    return (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def build_candidate_bundle(snapshot: CandidateSnapshot, assets: DocumentAssets) -> CandidateBundle:
    if snapshot.json_a.sha256 != _sha256(snapshot.json_a.raw) or snapshot.json_a.byte_length != len(snapshot.json_a.raw):
        raise DocumentIntegrityError("json_a_integrity_invalid")
    if snapshot.json_b.sha256 != _sha256(snapshot.json_b.raw) or snapshot.json_b.byte_length != len(snapshot.json_b.raw):
        raise DocumentIntegrityError("json_b_integrity_invalid")
    combined_text = json.dumps(snapshot.json_a.document, ensure_ascii=False) + json.dumps(snapshot.json_b.document, ensure_ascii=False)
    ensure_supported_text(combined_text, assets)
    model = _diagram_model(snapshot)
    svg = _render_candidate_svg(model, assets)
    pdf = _render_candidate_pdf(snapshot, model, assets)
    manifest = _build_manifest(snapshot, assets, pdf, svg)
    if len(manifest) > 1_048_576:
        raise DocumentIntegrityError("manifest_size_invalid")
    return CandidateBundle(manifest=manifest, pdf=pdf, svg=svg)
