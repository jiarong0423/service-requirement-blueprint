from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import pypdfium2 as pdfium
from fontTools.ttLib import TTFont as FontToolsTTFont
from pypdf import PdfReader
from reportlab.graphics import renderPDF, renderSVG
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as ReportLabTTFont
from reportlab.pdfgen import canvas

from app.config import SUPPORTED_LOCALES

MANIFEST_SCHEMA = "gate0-preflight-manifest/1.1"
CATALOG_PATHS = {locale: Path(f"app/i18n/catalog.{locale}.json") for locale in SUPPORTED_LOCALES}
MAPPING_PATH = Path("app/preflight/document-labels.json")
SAMPLE_PATH = Path("app/preflight/sample.json")
FONT_PATHS = {
    "regular": Path("assets/fonts/NotoSansTC-Regular.ttf"),
    "bold": Path("assets/fonts/NotoSansTC-Bold.ttf"),
    "license": Path("assets/fonts/OFL.txt"),
    "source": Path("assets/fonts/SOURCE.json"),
}
REQUIRED_PDF_KEYS = (
    "pdf.title",
    "pdf.scope",
    "pdf.fees",
    "pdf.responsibilities",
    "pdf.milestones",
    "pdf.acceptance",
    "pdf.maintenance",
    "pdf.blueprint",
    "pdf.integrity",
    "pdf.fixed_original_notice",
)
REQUIRED_SVG_KEYS = (
    "svg.requirement",
    "svg.proposal",
    "svg.candidate",
    "svg.fixed_snapshot",
)
PLACEHOLDER_PATTERN = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
RASTER_PROFILE = {
    "engine": "pypdfium2",
    "scale": 2.0,
    "expected_width": 1191,
    "expected_height": 1684,
    "minimum_nonwhite_ratio": 0.002,
}


class Gate0Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Gate0Error("duplicate_json_key")
        result[key] = value
    return result


def _load_json(path: Path) -> object:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise Gate0Error("required_file_missing") from error
    if len(raw) > 1024 * 1024:
        raise Gate0Error("json_file_too_large")
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as error:
        raise Gate0Error("invalid_utf8") from error
    except json.JSONDecodeError as error:
        raise Gate0Error("invalid_json") from error


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_record(project_root: Path, relative_path: Path) -> dict[str, object]:
    path = project_root / relative_path
    try:
        data = path.read_bytes()
    except OSError as error:
        raise Gate0Error("required_file_missing") from error
    return {
        "path": relative_path.as_posix(),
        "bytes": len(data),
        "sha256": _sha256_bytes(data),
    }


def _safe_fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise Gate0Error("directory_fsync_failed") from error
    finally:
        os.close(descriptor)


def _write_new_file(path: Path, data: bytes, mode: int) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except OSError as error:
        raise Gate0Error("artifact_install_failed") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as error:
        path.unlink(missing_ok=True)
        if isinstance(error, Gate0Error):
            raise
        raise Gate0Error("artifact_write_failed") from error


@dataclass(frozen=True)
class Artifact:
    kind: str
    filename: str
    data: bytes

    @property
    def sha256(self) -> str:
        return _sha256_bytes(self.data)


class ArtifactRegistry:
    _allowed = {"pdf", "svg", "manifest"}

    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}

    def add(self, artifact: Artifact) -> None:
        if artifact.kind not in self._allowed:
            raise Gate0Error("unsupported_artifact_kind")
        if artifact.kind in self._artifacts:
            raise Gate0Error("duplicate_artifact_kind")
        self._artifacts[artifact.kind] = artifact

    def require_complete(self) -> None:
        if set(self._artifacts) != self._allowed:
            raise Gate0Error("artifact_set_incomplete")

    def get(self, kind: str) -> Artifact:
        try:
            return self._artifacts[kind]
        except KeyError as error:
            raise Gate0Error("artifact_set_incomplete") from error


class Gate0Builder:
    def __init__(
        self,
        project_root: Path,
        output_root: Path | None = None,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.output_root = output_root or self.project_root / "instance/preflight"
        self.run_id_factory = run_id_factory or (lambda: secrets.token_hex(16))
        self.catalogs: dict[str, dict[str, str]] = {}
        self.labels: dict[str, tuple[str, ...]] = {}
        self.sample: dict[str, object] = {}

    def build(self) -> dict[str, object]:
        self._load_and_validate_inputs()
        drawing = self._create_drawing()
        svg_bytes = self._render_svg(drawing)
        pdf_bytes = self._render_pdf(drawing)
        self._validate_svg(svg_bytes)
        self._validate_pdf(pdf_bytes)

        run_id = self._allocate_run_id()
        registry = ArtifactRegistry()
        registry.add(Artifact("pdf", "document.pdf", pdf_bytes))
        registry.add(Artifact("svg", "diagram.svg", svg_bytes))
        manifest_bytes = self._build_manifest(run_id, registry)
        registry.add(Artifact("manifest", "manifest.json", manifest_bytes))
        registry.require_complete()
        return self._install(run_id, registry)

    def translate(self, locale: str, key: str) -> str:
        try:
            return self.catalogs[locale][key]
        except KeyError as error:
            raise Gate0Error("translation_missing") from error

    def _load_and_validate_inputs(self) -> None:
        catalogs: dict[str, dict[str, str]] = {}
        for locale, relative_path in CATALOG_PATHS.items():
            loaded = _load_json(self.project_root / relative_path)
            if not isinstance(loaded, dict) or not loaded:
                raise Gate0Error("catalog_invalid")
            catalog: dict[str, str] = {}
            for key, value in loaded.items():
                if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
                    raise Gate0Error("catalog_value_invalid")
                if key == value:
                    raise Gate0Error("catalog_raw_key_value")
                catalog[key] = value
            catalogs[locale] = catalog

        self.catalogs = catalogs

        mapping = _load_json(self.project_root / MAPPING_PATH)
        if not isinstance(mapping, dict) or set(mapping) != {"schema_version", "pdf", "svg"}:
            raise Gate0Error("document_label_map_invalid")
        if mapping["schema_version"] != "gate0-document-label-map/1.0":
            raise Gate0Error("document_label_map_version_invalid")
        if tuple(mapping["pdf"]) != REQUIRED_PDF_KEYS or tuple(mapping["svg"]) != REQUIRED_SVG_KEYS:
            raise Gate0Error("document_label_map_mismatch")
        all_labels = tuple(mapping["pdf"]) + tuple(mapping["svg"])
        if len(all_labels) != len(set(all_labels)) or not set(all_labels).issubset(catalogs["en"]):
            raise Gate0Error("document_label_map_invalid")
        self.labels = {"pdf": tuple(mapping["pdf"]), "svg": tuple(mapping["svg"])}

        sample = _load_json(self.project_root / SAMPLE_PATH)
        if not isinstance(sample, dict) or set(sample) != {
            "amount",
            "currency_code",
            "diagram",
            "project_name",
            "schema_version",
            "user_content",
        }:
            raise Gate0Error("sample_invalid")
        if sample["schema_version"] != "gate0-sample/1.1":
            raise Gate0Error("sample_version_invalid")
        if not isinstance(sample["amount"], int) or isinstance(sample["amount"], bool) or sample["amount"] < 0:
            raise Gate0Error("sample_amount_invalid")
        if not isinstance(sample["currency_code"], str) or not re.fullmatch(r"[A-Z]{3}", sample["currency_code"]):
            raise Gate0Error("sample_currency_invalid")
        if not isinstance(sample["project_name"], str) or not sample["project_name"].strip():
            raise Gate0Error("sample_project_name_invalid")
        if not isinstance(sample["user_content"], str) or not sample["user_content"].strip():
            raise Gate0Error("sample_user_content_invalid")
        diagram = sample["diagram"]
        if not isinstance(diagram, dict) or set(diagram) != {"nodes", "edges"}:
            raise Gate0Error("sample_diagram_invalid")
        if diagram["nodes"] != ["requirement", "proposal", "candidate"]:
            raise Gate0Error("sample_diagram_invalid")
        if diagram["edges"] != [["requirement", "proposal"], ["proposal", "candidate"]]:
            raise Gate0Error("sample_diagram_invalid")
        self.sample = sample
        self._validate_fonts()

    def _validate_fonts(self) -> None:
        source = _load_json(self.project_root / FONT_PATHS["source"])
        if not isinstance(source, dict) or source.get("schema_version") != "gate0-font-source/1.0":
            raise Gate0Error("font_source_metadata_invalid")
        outputs = source.get("outputs")
        if not isinstance(outputs, dict):
            raise Gate0Error("font_source_metadata_invalid")
        specifications = {
            "regular": (400, "NotoSansTC-Regular"),
            "bold": (700, "NotoSansTC-Bold"),
        }
        for kind, (weight, postscript_name) in specifications.items():
            relative_path = FONT_PATHS[kind]
            path = self.project_root / relative_path
            record = outputs.get(relative_path.name)
            if not isinstance(record, dict):
                raise Gate0Error("font_source_metadata_invalid")
            file_record = _file_record(self.project_root, relative_path)
            if record.get("bytes") != file_record["bytes"] or record.get("sha256") != file_record["sha256"]:
                raise Gate0Error("font_digest_mismatch")
            try:
                font = FontToolsTTFont(path)
            except Exception as error:
                raise Gate0Error("font_invalid") from error
            if "fvar" in font or font["OS/2"].usWeightClass != weight:
                raise Gate0Error("font_style_invalid")
            names = {entry.toUnicode() for entry in font["name"].names if entry.nameID == 6}
            if names != {postscript_name}:
                raise Gate0Error("font_style_invalid")

        license_path = self.project_root / FONT_PATHS["license"]
        license_record = _file_record(self.project_root, FONT_PATHS["license"])
        recorded_license = outputs.get("OFL.txt")
        if not isinstance(recorded_license, dict) or recorded_license.get("sha256") != license_record["sha256"]:
            raise Gate0Error("font_license_digest_mismatch")
        try:
            license_text = license_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise Gate0Error("font_license_invalid") from error
        if "SIL OPEN FONT LICENSE Version 1.1" not in license_text:
            raise Gate0Error("font_license_invalid")

        try:
            pdfmetrics.registerFont(
                ReportLabTTFont("Gate0NotoSansTC", self.project_root / FONT_PATHS["regular"])
            )
            pdfmetrics.registerFont(
                ReportLabTTFont("Gate0NotoSansTC-Bold", self.project_root / FONT_PATHS["bold"])
            )
        except Exception as error:
            raise Gate0Error("font_registration_failed") from error

    def _create_drawing(self) -> Drawing:
        drawing = Drawing(500, 178)
        drawing.add(Rect(0, 0, 500, 178, fillColor=HexColor("#F8FAFC"), strokeColor=HexColor("#CBD5E1"), rx=10, ry=10))
        drawing.add(String(18, 151, self.translate("en", "svg.fixed_snapshot"), fontName="Gate0NotoSansTC-Bold", fontSize=11, fillColor=HexColor("#0F172A")))
        positions = (20, 188, 356)
        node_keys = ("svg.requirement", "svg.proposal", "svg.candidate")
        for index, (x, key) in enumerate(zip(positions, node_keys, strict=True)):
            drawing.add(Rect(x, 72, 124, 50, fillColor=HexColor("#E0F2FE"), strokeColor=HexColor("#0284C7"), rx=8, ry=8))
            drawing.add(String(x + 10, 94, self.translate("en", key), fontName="Gate0NotoSansTC", fontSize=8.5, fillColor=HexColor("#0C4A6E")))
            if index < 2:
                drawing.add(Line(x + 124, 97, positions[index + 1], 97, strokeColor=HexColor("#64748B"), strokeWidth=1.5))
        drawing.add(String(18, 36, str(self.sample["user_content"]), fontName="Gate0NotoSansTC", fontSize=7.4, fillColor=HexColor("#334155")))
        return drawing

    def _render_svg(self, drawing: Drawing) -> bytes:
        try:
            rendered = renderSVG.drawToString(drawing)
        except Exception as error:
            raise Gate0Error("svg_render_failed") from error
        if isinstance(rendered, str):
            rendered = rendered.encode("utf-8")
        if not isinstance(rendered, bytes) or not rendered:
            raise Gate0Error("svg_render_failed")
        return rendered

    def _render_pdf(self, drawing: Drawing) -> bytes:
        buffer = io.BytesIO()
        try:
            document = canvas.Canvas(buffer, pagesize=A4, pageCompression=1, invariant=1)
            width, height = A4
            document.setTitle("Gate 0 English Document")
            document.setAuthor("Service Requirement Blueprint")
            document.setFont("Gate0NotoSansTC-Bold", 15)
            document.drawString(48, height - 58, self.translate("en", "pdf.title"))
            document.setFont("Gate0NotoSansTC", 9)
            document.drawString(48, height - 82, self.translate("en", "pdf.fixed_original_notice"))
            document.setStrokeColor(HexColor("#CBD5E1"))
            document.line(48, height - 94, width - 48, height - 94)

            y = height - 122
            rows = (
                ("field.project_name", str(self.sample["project_name"])),
                ("notice.user_content_original", self.translate("en", "notice.no_auto_translation")),
                ("pdf.fees", f"{self.sample['currency_code']} {self.sample['amount']:,}"),
                ("pdf.scope", self.translate("en", "document.blueprint")),
                ("pdf.responsibilities", self.translate("en", "case.party_only")),
                ("pdf.milestones", self.translate("en", "document.candidate")),
                ("pdf.acceptance", self.translate("en", "status.awaiting_confirmation")),
                ("pdf.maintenance", self.translate("en", "field.maintenance")),
                ("pdf.integrity", "SHA-256"),
            )
            for key, value in rows:
                document.setFont("Gate0NotoSansTC-Bold", 9)
                document.drawString(48, y, self.translate("en", key))
                document.setFont("Gate0NotoSansTC", 9)
                document.drawString(236, y, value)
                y -= 23

            document.setFont("Gate0NotoSansTC-Bold", 10)
            document.drawString(48, y - 4, self.translate("en", "pdf.blueprint"))
            renderPDF.draw(drawing, document, 48, y - 200)
            document.showPage()
            document.save()
        except Exception as error:
            raise Gate0Error("pdf_render_failed") from error
        data = buffer.getvalue()
        if not data:
            raise Gate0Error("pdf_render_failed")
        return data

    def _validate_svg(self, data: bytes) -> None:
        try:
            root = ET.fromstring(data)
        except ET.ParseError as error:
            raise Gate0Error("svg_xml_invalid") from error
        if not root.tag.endswith("svg"):
            raise Gate0Error("svg_root_invalid")
        text = "".join(root.itertext())
        for key in self.labels["svg"]:
            if self.translate("en", key) not in text:
                raise Gate0Error("svg_label_missing")
        if text.count(str(self.sample["user_content"])) != 1:
            raise Gate0Error("svg_user_content_invalid")
        for element in root.iter():
            local_name = element.tag.rsplit("}", 1)[-1].lower()
            if local_name in {"script", "foreignobject"}:
                raise Gate0Error("svg_unsafe_content")
            for name, value in element.attrib.items():
                if name.rsplit("}", 1)[-1].lower() in {"href", "src"} and re.match(r"^(?:https?:|//|data:)", value, re.IGNORECASE):
                    raise Gate0Error("svg_external_reference")

    def _validate_pdf(self, data: bytes) -> None:
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if len(reader.pages) != 1:
                raise Gate0Error("pdf_page_count_invalid")
            page = reader.pages[0]
            text = page.extract_text()
        except Gate0Error:
            raise
        except Exception as error:
            raise Gate0Error("pdf_parse_failed") from error
        for key in self.labels["pdf"]:
            if self.translate("en", key) not in text:
                raise Gate0Error("pdf_label_missing")
        if text.count(str(self.sample["user_content"])) != 1:
            raise Gate0Error("pdf_user_content_invalid")

        resources = page.get("/Resources", {}).get_object()
        fonts = resources.get("/Font", {}).get_object()
        embedded_names: set[str] = set()
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
                    embedded_names.add(str(candidate.get("/BaseFont", font.get("/BaseFont", ""))))
        if len(embedded_names) < 2 or not any("NotoSansTC" in name for name in embedded_names):
            raise Gate0Error("pdf_font_not_embedded")

        try:
            rendered = pdfium.PdfDocument(data)
            if len(rendered) != 1:
                raise Gate0Error("pdf_raster_page_count_invalid")
            bitmap = rendered[0].render(scale=float(RASTER_PROFILE["scale"]))
            image = bitmap.to_pil().convert("RGB")
        except Gate0Error:
            raise
        except Exception as error:
            raise Gate0Error("pdf_raster_failed") from error
        expected_size = (int(RASTER_PROFILE["expected_width"]), int(RASTER_PROFILE["expected_height"]))
        if image.size != expected_size:
            raise Gate0Error("pdf_raster_size_invalid")
        pixels = image.get_flattened_data()
        nonwhite = sum(1 for red, green, blue in pixels if min(red, green, blue) < 248)
        if nonwhite / (image.width * image.height) < float(RASTER_PROFILE["minimum_nonwhite_ratio"]):
            raise Gate0Error("pdf_raster_blank")

    def _allocate_run_id(self) -> str:
        for _ in range(5):
            run_id = self.run_id_factory()
            if not re.fullmatch(r"[a-f0-9]{32}", run_id):
                raise Gate0Error("run_id_invalid")
            if not (self.output_root / "runs" / run_id).exists() and not (self.output_root / ".staging" / run_id).exists():
                return run_id
        raise Gate0Error("run_id_collision")

    def _build_manifest(self, run_id: str, registry: ArtifactRegistry) -> bytes:
        pdf = registry.get("pdf")
        svg = registry.get("svg")
        source_paths = [SAMPLE_PATH, MAPPING_PATH, *CATALOG_PATHS.values(), *FONT_PATHS.values()]
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "status": "complete",
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "label_mode": "english_only",
            "raster_profile": RASTER_PROFILE,
            "sources": [_file_record(self.project_root, path) for path in source_paths],
            "artifacts": {
                "pdf": {
                    "path": f"runs/{run_id}/{pdf.filename}",
                    "bytes": len(pdf.data),
                    "sha256": pdf.sha256,
                },
                "svg": {
                    "path": f"runs/{run_id}/{svg.filename}",
                    "bytes": len(svg.data),
                    "sha256": svg.sha256,
                },
            },
        }
        return (json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def _install(self, run_id: str, registry: ArtifactRegistry) -> dict[str, object]:
        staging_root = self.output_root / ".staging"
        runs_root = self.output_root / "runs"
        quarantine_root = self.output_root / "quarantine"
        for directory in (self.output_root, staging_root, runs_root, quarantine_root):
            try:
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(directory, 0o700)
            except OSError as error:
                raise Gate0Error("output_directory_failed") from error

        staging = staging_root / run_id
        final = runs_root / run_id
        try:
            staging.mkdir(mode=0o700, exist_ok=False)
            for kind in ("pdf", "svg", "manifest"):
                artifact = registry.get(kind)
                _write_new_file(staging / artifact.filename, artifact.data, 0o600)
                os.chmod(staging / artifact.filename, 0o400)
            _safe_fsync_directory(staging)
            _safe_fsync_directory(staging_root)
            _safe_fsync_directory(runs_root)
            if final.exists():
                raise Gate0Error("run_id_collision")
            os.rename(staging, final)
            _safe_fsync_directory(runs_root)
        except Exception as error:
            failed_tree = final if final.exists() else staging if staging.exists() else None
            if failed_tree is not None:
                quarantine = quarantine_root / f"{run_id}-{uuid.uuid4().hex}"
                try:
                    source_parent = failed_tree.parent
                    os.rename(failed_tree, quarantine)
                    _safe_fsync_directory(source_parent)
                    _safe_fsync_directory(quarantine_root)
                except Exception as quarantine_error:
                    try:
                        if failed_tree.exists():
                            shutil.rmtree(failed_tree)
                            _safe_fsync_directory(failed_tree.parent)
                    except Exception as cleanup_error:
                        raise Gate0Error("quarantine_cleanup_failed") from cleanup_error
                    raise Gate0Error("quarantine_failed") from quarantine_error
            if isinstance(error, Gate0Error):
                raise
            raise Gate0Error("artifact_install_failed") from error

        result: dict[str, object] = {"status": "complete", "run_id": run_id, "artifacts": {}}
        result_artifacts = result["artifacts"]
        assert isinstance(result_artifacts, dict)
        for kind in ("pdf", "svg", "manifest"):
            artifact = registry.get(kind)
            result_artifacts[kind] = {
                "path": f"runs/{run_id}/{artifact.filename}",
                "bytes": len(artifact.data),
                "sha256": artifact.sha256,
            }
        return result
