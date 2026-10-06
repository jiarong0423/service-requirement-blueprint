from __future__ import annotations

import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from fontTools.ttLib import TTFont as FontToolsTTFont
from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as ReportLabTTFont


class DocumentIntegrityError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FileRecord:
    filename: str
    media_type: str
    sha256: str
    byte_length: int


@dataclass(frozen=True)
class DocumentAssets:
    catalog: dict[str, str]
    labels: dict[str, object]
    catalog_record: FileRecord
    label_record: FileRecord
    font_records: dict[str, FileRecord]
    regular_font_path: Path
    bold_font_path: Path
    supported_codepoints: frozenset[int]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest_record(path: Path, media_type: str = "application/octet-stream") -> FileRecord:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise DocumentIntegrityError("asset_missing") from error
    if not raw:
        raise DocumentIntegrityError("asset_empty")
    return FileRecord(path.name, media_type, _digest(raw), len(raw))


def _load_json(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DocumentIntegrityError("asset_json_invalid") from error
    if not isinstance(value, dict):
        raise DocumentIntegrityError("asset_json_invalid")
    return value, raw


def load_and_validate_document_assets(project_root: Path) -> DocumentAssets:
    root = project_root.resolve()
    catalog_path = root / "app/i18n/catalog.en.json"
    label_path = root / "app/documents/candidate-labels.v1.json"
    regular = root / "assets/fonts/NotoSansTC-Regular.ttf"
    bold = root / "assets/fonts/NotoSansTC-Bold.ttf"
    license_path = root / "assets/fonts/OFL.txt"
    provenance_path = root / "assets/fonts/SOURCE.json"

    catalog_object, catalog_raw = _load_json(catalog_path)
    if not catalog_object or any(
        not isinstance(key, str) or not isinstance(value, str) or not value.strip()
        for key, value in catalog_object.items()
    ):
        raise DocumentIntegrityError("catalog_invalid")
    label_keys, label_raw = _load_json(label_path)
    if set(label_keys) != {"schema_version", "pdf", "svg", "fields"} or label_keys["schema_version"] != "candidate-label-map/1.0":
        raise DocumentIntegrityError("label_map_invalid")
    resolved_labels: dict[str, object] = {"schema_version": label_keys["schema_version"]}
    for group in ("pdf", "svg", "fields"):
        if not isinstance(label_keys[group], dict) or not label_keys[group] or any(
            not isinstance(key, str) or not isinstance(value, str) or not value.strip()
            for key, value in label_keys[group].items()
        ):
            raise DocumentIntegrityError("label_map_invalid")
        try:
            resolved_labels[group] = {
                key: catalog_object[value] for key, value in label_keys[group].items()
            }
        except KeyError as error:
            raise DocumentIntegrityError("label_translation_missing") from error

    source, _source_raw = _load_json(provenance_path)
    outputs = source.get("outputs")
    if source.get("schema_version") != "gate0-font-source/1.0" or not isinstance(outputs, dict):
        raise DocumentIntegrityError("font_provenance_invalid")
    records = {
        "regular": file_digest_record(regular, "font/ttf"),
        "bold": file_digest_record(bold, "font/ttf"),
        "license": file_digest_record(license_path, "text/plain"),
        "provenance": file_digest_record(provenance_path, "application/json"),
    }
    for key, path in (("regular", regular), ("bold", bold), ("license", license_path)):
        recorded = outputs.get(path.name)
        if not isinstance(recorded, dict) or recorded.get("bytes") != records[key].byte_length or recorded.get("sha256") != records[key].sha256:
            raise DocumentIntegrityError("font_digest_mismatch")
    try:
        if "SIL OPEN FONT LICENSE Version 1.1" not in license_path.read_text(encoding="utf-8"):
            raise DocumentIntegrityError("font_license_invalid")
    except (OSError, UnicodeError) as error:
        raise DocumentIntegrityError("font_license_invalid") from error

    supported: set[int] = set()
    for path, weight, postscript in (
        (regular, 400, "NotoSansTC-Regular"),
        (bold, 700, "NotoSansTC-Bold"),
    ):
        try:
            font = FontToolsTTFont(path)
            if "fvar" in font or font["OS/2"].usWeightClass != weight:
                raise DocumentIntegrityError("font_style_invalid")
            names = {entry.toUnicode() for entry in font["name"].names if entry.nameID == 6}
            if names != {postscript}:
                raise DocumentIntegrityError("font_style_invalid")
            supported.update(font.getBestCmap())
        except DocumentIntegrityError:
            raise
        except Exception as error:
            raise DocumentIntegrityError("font_invalid") from error
    try:
        pdfmetrics.registerFont(ReportLabTTFont("CandidateNotoSans", regular))
        pdfmetrics.registerFont(ReportLabTTFont("CandidateNotoSans-Bold", bold))
    except Exception as error:
        raise DocumentIntegrityError("font_registration_failed") from error

    return DocumentAssets(
        catalog={key: str(value) for key, value in catalog_object.items()},
        labels=resolved_labels,
        catalog_record=FileRecord(catalog_path.name, "application/json", _digest(catalog_raw), len(catalog_raw)),
        label_record=FileRecord(label_path.name, "application/json", _digest(label_raw), len(label_raw)),
        font_records=records,
        regular_font_path=regular,
        bold_font_path=bold,
        supported_codepoints=frozenset(supported),
    )


def ensure_supported_text(text: str, assets: DocumentAssets) -> None:
    unsupported = {
        ord(character)
        for character in text
        if not character.isspace() and ord(character) not in assets.supported_codepoints
    }
    if unsupported:
        raise DocumentIntegrityError("font_glyph_missing")


def validate_safe_svg(raw: bytes, required_text: tuple[str, ...] = ()) -> None:
    if not raw or len(raw) > 2_000_000:
        raise DocumentIntegrityError("svg_size_invalid")
    prefix = raw[:4096].lower()
    if any(token in prefix for token in (b"<!doctype", b"<!entity", b"<?xml-stylesheet")):
        raise DocumentIntegrityError("svg_unsafe_content")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise DocumentIntegrityError("svg_xml_invalid") from error
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise DocumentIntegrityError("svg_root_invalid")
    allowed_elements = {"svg", "title", "desc", "rect", "line", "path", "text", "tspan", "g"}
    allowed_attributes = {
        "xmlns", "version", "width", "height", "viewbox", "x", "y", "x1", "y1", "x2", "y2",
        "rx", "ry", "d", "fill", "stroke", "stroke-width", "font-family", "font-size", "font-weight",
        "text-anchor", "dominant-baseline", "transform", "role", "aria-labelledby", "id",
    }
    element_count = 0
    for element in root.iter():
        element_count += 1
        if element_count > 3000 or element.tag.rsplit("}", 1)[-1].lower() not in allowed_elements:
            raise DocumentIntegrityError("svg_unsafe_content")
        for name, value in element.attrib.items():
            local = name.rsplit("}", 1)[-1].lower()
            lowered = value.lower()
            if local.startswith("on") or local not in allowed_attributes:
                raise DocumentIntegrityError("svg_unsafe_content")
            if any(token in lowered for token in ("url(", "javascript:", "data:", "file:", "http:", "https:", "//")):
                raise DocumentIntegrityError("svg_external_reference")
    text = "".join(root.itertext())
    if not root.findall("{*}title") or not root.findall("{*}desc"):
        raise DocumentIntegrityError("svg_accessible_text_missing")
    if any(required not in text for required in required_text):
        raise DocumentIntegrityError("svg_required_text_missing")


def validate_candidate_pdf(raw: bytes, required_text: tuple[str, ...] = ()) -> None:
    if not raw or len(raw) > 12_000_000:
        raise DocumentIntegrityError("pdf_size_invalid")
    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 80:
            raise DocumentIntegrityError("pdf_structure_invalid")
        root = reader.trailer["/Root"]
        for forbidden in ("/OpenAction", "/AA", "/Names", "/AcroForm"):
            if forbidden in root:
                raise DocumentIntegrityError("pdf_active_content")
        extracted: list[str] = []
        embedded_fonts: set[str] = set()
        for page in reader.pages:
            if "/AA" in page or "/Annots" in page:
                raise DocumentIntegrityError("pdf_active_content")
            page_text = page.extract_text() or ""
            if not page_text.strip():
                raise DocumentIntegrityError("pdf_text_missing")
            extracted.append(page_text)
            resources = page.get("/Resources", {}).get_object()
            fonts = resources.get("/Font", {}).get_object()
            for reference in fonts.values():
                font = reference.get_object()
                candidates = [font]
                if "/DescendantFonts" in font:
                    candidates.extend(item.get_object() for item in font["/DescendantFonts"])
                for candidate in candidates:
                    descriptor = candidate.get("/FontDescriptor")
                    if descriptor is None:
                        continue
                    descriptor = descriptor.get_object()
                    if any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
                        embedded_fonts.add(str(candidate.get("/BaseFont", font.get("/BaseFont", ""))))
        text = "\n".join(extracted)
        if any(required not in text for required in required_text):
            raise DocumentIntegrityError("pdf_required_text_missing")
        if not any("NotoSans" in name for name in embedded_fonts):
            raise DocumentIntegrityError("pdf_font_not_embedded")
    except DocumentIntegrityError:
        raise
    except Exception as error:
        raise DocumentIntegrityError("pdf_parse_failed") from error

    try:
        document = pdfium.PdfDocument(raw)
        for index in range(len(document)):
            image = document[index].render(scale=1.0).to_pil().convert("RGB")
            pixels = image.get_flattened_data()
            nonwhite = sum(1 for red, green, blue in pixels if min(red, green, blue) < 248)
            if nonwhite / (image.width * image.height) < 0.001:
                raise DocumentIntegrityError("pdf_raster_blank")
    except DocumentIntegrityError:
        raise
    except Exception as error:
        raise DocumentIntegrityError("pdf_raster_failed") from error
