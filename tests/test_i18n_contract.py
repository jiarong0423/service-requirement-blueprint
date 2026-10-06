from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.config import SUPPORTED_LOCALES
from app.db import connect_database
from app.gate0 import Gate0Builder, Gate0Error, _load_json
from app.i18n import CatalogError, load_catalogs, translate
from app.services.blueprint import (
    MAX_LINES,
    MAX_TEXT_WIDTH,
    NODE_WIDTH,
    NODE_X,
    blueprint_data,
    estimate_svg_text_width,
    render_svg,
    wrap_svg_label,
)
from tests.helpers import complete_requirement, csrf, login


CATALOG_PATH = Path("app/i18n/catalog.en.json")


def load_catalog(root: Path) -> dict[str, str]:
    loaded = _load_json(root / CATALOG_PATH)
    assert isinstance(loaded, dict)
    return loaded


def replace_catalog(root: Path, catalog: dict[str, str]) -> None:
    path = root / CATALOG_PATH
    path.unlink()
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_only_english_catalog_is_shipped_and_nonempty(project_root: Path) -> None:
    assert SUPPORTED_LOCALES == ("en",)
    catalogs = sorted(path.name for path in (project_root / "app/i18n").glob("catalog.*.json"))
    assert catalogs == ["catalog.en.json"]
    english = load_catalog(project_root)
    assert english
    assert all(key and isinstance(value, str) and value.strip() and value != key for key, value in english.items())


def test_public_planning_delivery_is_english_only(project_root: Path) -> None:
    public_files = [
        project_root / "README.md",
        project_root / "devpost/scope.md",
        project_root / "devpost/prd.md",
        project_root / "devpost/spec.md",
        project_root / "devpost/checklist.md",
        project_root / "devpost/scope.html",
        project_root / "devpost/prd.html",
        project_root / "devpost/spec.html",
    ]
    cjk = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
    for path in public_files:
        text = path.read_text(encoding="utf-8")
        assert not cjk.search(text), path
        assert "Traditional Chinese" not in text
        assert "NT$" not in text and "TWD" not in text
    removed_companions = [
        project_root / "README.zh-TW.md",
        project_root / "app/i18n/catalog.zh-TW.json",
        project_root / "devpost/scope.zh-TW.md",
        project_root / "devpost/prd.zh-TW.md",
        project_root / "devpost/spec.zh-TW.md",
        project_root / "devpost/checklist.zh-TW.md",
    ]
    assert all(not path.exists() for path in removed_companions)


def test_public_demo_currency_is_test_only_and_ui_has_no_regional_default(project_root: Path) -> None:
    sample = json.loads((project_root / "app/preflight/sample.json").read_text(encoding="utf-8"))
    template = (project_root / "app/templates/requirement_section.html").read_text(encoding="utf-8")
    assert sample["currency_code"] == "XTS"  # ISO 4217 code reserved for testing.
    assert 'placeholder="3-letter ISO 4217 code"' in template
    assert "USD" not in template and "TWD" not in template


def test_requirement_blueprint_wraps_long_user_text_inside_three_nodes() -> None:
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    labels = (
        "Independent neighborhood studio clients with detailed project questions",
        "W" * 60,
        "界" * 60 + " <script>alert(1)</script><foreignObject>bad</foreignObject>",
    )
    requirements = {"project": {"target_audience": labels[0], "primary_purpose": labels[1], "primary_visitor_action": labels[2]}}
    root = ET.fromstring(render_svg(blueprint_data(requirements)))
    texts = root.findall("svg:text", namespace)
    rectangles = [element for element in root.findall("svg:rect", namespace) if element.attrib.get("rx") == "12"]
    assert len(texts) == len(rectangles) == 3
    assert all(NODE_X[index] + NODE_WIDTH <= NODE_X[index + 1] for index in range(2))
    for index, text in enumerate(texts):
        lines = text.findall("svg:tspan", namespace)
        assert 1 <= len(lines) <= 4
        assert all(line.text and estimate_svg_text_width(line.text) <= MAX_TEXT_WIDTH for line in lines)
        assert len({line.attrib["x"] for line in lines}) == 1
        assert text.attrib["clip-path"] == f"url(#node-clip-{index})"
    element_names = {element.tag.rsplit("}", 1)[-1].lower() for element in root.iter()}
    assert "script" not in element_names and "foreignobject" not in element_names


@pytest.mark.parametrize(
    "value",
    [
        "m" * 60,
        "w" * 60,
        "واجهةالمستخدم" * 8,
        "界" * 60,
        "😀" * 60,
        "e\u0301" * 60,
    ],
)
def test_requirement_blueprint_conservatively_wraps_wide_unicode(value: str) -> None:
    lines = wrap_svg_label(value)
    assert 2 <= len(lines) <= MAX_LINES
    assert all(estimate_svg_text_width(line.removesuffix("…")) <= MAX_TEXT_WIDTH for line in lines)
    if estimate_svg_text_width(value) > MAX_TEXT_WIDTH * MAX_LINES:
        assert lines[-1].endswith("…")


def test_catalog_loader_rejects_empty_value(isolated_project: Path) -> None:
    catalog = load_catalog(isolated_project)
    catalog[sorted(catalog)[0]] = ""
    replace_catalog(isolated_project, catalog)
    with pytest.raises(CatalogError, match="catalog_value_invalid"):
        load_catalogs(isolated_project / "app/i18n")


def test_catalog_loader_rejects_duplicate_key(isolated_project: Path) -> None:
    path = isolated_project / CATALOG_PATH
    path.write_text('{"app.name":"One","app.name":"Two"}\n', encoding="utf-8")
    with pytest.raises(CatalogError, match="duplicate_catalog_key"):
        load_catalogs(isolated_project / "app/i18n")


def test_catalog_loader_rejects_unenabled_catalog(isolated_project: Path) -> None:
    (isolated_project / "app/i18n/catalog.fr.json").write_text('{"app.name":"Nom"}\n', encoding="utf-8")
    with pytest.raises(CatalogError, match="catalog_set_invalid"):
        load_catalogs(isolated_project / "app/i18n")


def test_catalog_loader_rejects_bom_and_invalid_utf8(isolated_project: Path) -> None:
    path = isolated_project / CATALOG_PATH
    path.write_bytes(b"\xef\xbb\xbf{}")
    with pytest.raises(CatalogError, match="catalog_bom_forbidden"):
        load_catalogs(isolated_project / "app/i18n")
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(CatalogError, match="catalog_invalid"):
        load_catalogs(isolated_project / "app/i18n")


def test_missing_translation_has_no_fallback(project_root: Path) -> None:
    catalogs = load_catalogs(project_root / "app/i18n")
    with pytest.raises(CatalogError, match="translation_missing"):
        translate(catalogs, "en", "missing.key")
    builder = Gate0Builder(project_root)
    builder._load_and_validate_inputs()
    with pytest.raises(Gate0Error, match="translation_missing"):
        builder.translate("en", "missing.key")


def test_document_label_map_is_complete_and_catalog_backed(project_root: Path) -> None:
    mapping = _load_json(project_root / "app/preflight/document-labels.json")
    catalog = load_catalog(project_root)
    assert isinstance(mapping, dict)
    labels = list(mapping["pdf"]) + list(mapping["svg"])
    assert len(labels) == len(set(labels))
    assert set(labels) <= set(catalog)


def test_changed_document_label_map_fails_closed(isolated_project: Path, tmp_path: Path) -> None:
    path = isolated_project / "app/preflight/document-labels.json"
    mapping = _load_json(path)
    assert isinstance(mapping, dict)
    mapping["pdf"] = list(mapping["pdf"])[1:]
    path.unlink()
    path.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(Gate0Error, match="document_label_map_mismatch"):
        Gate0Builder(isolated_project, output_root=tmp_path / "preflight").build()


def test_only_english_route_family_is_registered(demo_runtime) -> None:
    app, _instance_root, _passwords = demo_runtime
    product_rules = {rule.rule for rule in app.url_map.iter_rules() if not rule.rule.startswith("/static/")}
    assert any(rule.startswith("/en/") for rule in product_rules)
    assert not any(rule.startswith("/zh-TW/") for rule in product_rules)
    with app.test_client() as client:
        root = client.get("/")
        assert root.status_code == 302 and root.location.endswith("/en/")
        assert client.get("/zh-TW/").status_code == 404
        assert client.get("/fr/").status_code == 404
        assert client.get("/owner/guide").status_code == 404


def test_templates_reference_only_english_catalog_keys(project_root: Path) -> None:
    english = load_catalog(project_root)
    key_pattern = re.compile(r"tr\(['\"]([^'\"]+)['\"]")
    referenced: set[str] = set()
    for template in (project_root / "app/templates").glob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert "switch_language" not in text
        referenced.update(key_pattern.findall(text))
    assert referenced
    fixed_references = {key for key in referenced if not key.endswith(".")}
    assert fixed_references <= set(english)


def test_delivery_dynamic_machine_codes_have_catalog_labels(project_root: Path) -> None:
    english = load_catalog(project_root)
    required = {
        *(f"status.{value}" for value in ("formal", "delivered", "under_acceptance", "remediation", "reinspection", "accepted", "disputed")),
        *(f"kind.{value}" for value in ("initial", "remediation", "reinspection")),
        *(f"result.{value}" for value in ("pass", "fail", "not_run", "returned", "accepted")),
        *(f"event.{value}" for value in (
            "delivery_submitted", "review_deadline_set", "inspection_started", "inspection_returned",
            "delivery_accepted", "remediation_submitted", "dispute_opened", "dispute_position_added",
            "evidence_exported",
        )),
    }
    assert required <= set(english)


def test_english_route_preserves_original_unicode_user_content(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    with app.test_client() as client:
        assert login(client, "en", "owner@example.test", passwords["owner"]).status_code == 302
        case_id = complete_requirement(client, instance_root)
        review = client.get(f"/en/owner/requirements/{case_id}/review")
        assert review.status_code == 200
        assert "Preserve Unicode exactly: café, résumé, naïve — unchanged.".encode() in review.data
        assert b'<html lang="en">' in review.data


def test_complete_slice_2_get_and_post_outcomes_in_english(demo_runtime) -> None:
    app, instance_root, passwords = demo_runtime
    connection = connect_database(instance_root / "app.sqlite3")
    try:
        contractor_id = connection.execute(
            "SELECT id FROM users WHERE email_normalized = 'contractor@example.test'"
        ).fetchone()[0]
    finally:
        connection.close()

    with app.test_client() as owner:
        html_pages = [owner.get("/en/"), owner.get("/en/login")]
        catalog = owner.get("/en/i18n/client.json")
        assert catalog.status_code == 200 and catalog.headers["Cache-Control"] == "no-store"
        assert login(owner, "en", "owner@example.test", passwords["owner"]).status_code == 302
        html_pages.extend((owner.get("/en/account"), owner.get("/en/owner/guide")))
        assert owner.post("/en/owner/requirements", data={"title": "missing csrf"}).status_code == 400
        case_id = complete_requirement(owner, instance_root)
        for number in range(1, 9):
            html_pages.append(owner.get(f"/en/owner/requirements/{case_id}/sections/{number}"))
        html_pages.append(owner.get(f"/en/owner/requirements/{case_id}/review"))
        blueprint = owner.get(f"/en/owner/requirements/{case_id}/blueprint.svg")
        assert blueprint.status_code == 200 and blueprint.mimetype == "image/svg+xml"
        connection = connect_database(instance_root / "app.sqlite3")
        try:
            revision = connection.execute(
                "SELECT content_revision FROM cases WHERE id = ?", (case_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        generated = owner.post(
            f"/en/owner/requirements/{case_id}/json-a",
            data={"csrf_token": csrf(owner), "expected_revision": revision, "review_confirmed": "yes"},
        )
        assert generated.status_code == 302
        connection = connect_database(instance_root / "app.sqlite3")
        try:
            artifact_id = connection.execute(
                "SELECT current_json_a_artifact_id FROM cases WHERE id = ?", (case_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        download = owner.get(f"/en/owner/requirements/{case_id}/json-a/{artifact_id}/download")
        assert download.status_code == 200 and download.mimetype == "application/json"
        for page in html_pages:
            assert page.status_code == 200
            body = page.get_data(as_text=True)
            assert '<html lang="en">' in body
            assert "{{" not in body and "tr(" not in body
        assert owner.post("/en/logout", data={"csrf_token": csrf(owner)}).status_code == 302

    with app.test_client() as admin:
        result = login(admin, "en", "admin@example.test", passwords["admin"])
        assert result.status_code == 302 and result.location.endswith("/en/admin/accounts")
        for path in ("/en/account", "/en/admin/accounts", "/en/admin/service-health"):
            page = admin.get(path)
            assert page.status_code == 200 and '<html lang="en">' in page.get_data(as_text=True)
        disabled = admin.post(
            f"/en/admin/accounts/{contractor_id}/status",
            data={"csrf_token": csrf(admin), "status": "disabled"},
        )
        assert disabled.status_code == 302
        enabled = admin.post(
            f"/en/admin/accounts/{contractor_id}/status",
            data={"csrf_token": csrf(admin), "status": "active"},
        )
        assert enabled.status_code == 302
        assert admin.post("/en/logout", data={"csrf_token": csrf(admin)}).status_code == 302
