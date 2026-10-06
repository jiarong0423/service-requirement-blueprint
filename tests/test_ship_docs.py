from __future__ import annotations

from html.parser import HTMLParser


def test_english_ship_documents_are_factual_and_record_authorized_publication(project_root) -> None:
    readme = (project_root / "README.md").read_text(encoding="utf-8")
    demo = (project_root / "devpost/demo-guide.md").read_text(encoding="utf-8")
    evidence = (project_root / "devpost/ship-evidence.md").read_text(encoding="utf-8")
    assert "return-remediation-reinspection-acceptance" in readme
    assert "Silence" in readme
    assert "private JSON B drafts" in readme
    assert "## Return, remediation, and explicit acceptance" in demo
    assert "no winner, liability, damages, refund, or payment decision" in demo
    public_repository = "https://github.com/jiarong0423/service-requirement-blueprint"
    assert public_repository in readme
    assert public_repository in evidence
    assert "sanitized 122-file source candidate" in evidence
    assert "No remote, public push, deployment" in evidence
    assert "production ready" not in (readme + demo + evidence).lower()
    assert "legal guarantee" not in (readme + demo + evidence).lower()


def test_readme_documents_current_commands_and_offline_boundary(project_root) -> None:
    readme = (project_root / "README.md").read_text(encoding="utf-8")
    for command in (
        ".venv/bin/python -m app.cli init-demo",
        ".venv/bin/python -m app.cli migrate",
        ".venv/bin/python -m pytest -q",
        ".venv/bin/python -m app.cli gate0",
    ):
        assert command in readme
    assert "work offline" in readme
    assert "/zh-TW" not in readme


class _AppMapParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.external_resources: list[str] = []
        self.script_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script":
            self.script_count += 1
        if tag in {"script", "link", "img"}:
            value = values.get("src") or values.get("href")
            if value and (value.startswith("http://") or value.startswith("https://")):
                self.external_resources.append(value)


def test_app_map_is_offline_and_references_current_code(project_root) -> None:
    path = project_root / "devpost/app-map.html"
    text = path.read_text(encoding="utf-8")
    parser = _AppMapParser()
    parser.feed(text)

    assert parser.script_count == 0
    assert parser.external_resources == []
    assert "Functional source: responsive implementation commit <code>e7b7306</code>" in text
    assert "this offline map was added in <code>c182197</code>" in text
    assert "Snapshot source: commit <code>63c71d2</code>" not in text
    assert "test_delivery_history_uses_accessible_responsive_table_region" in text
    for relative in (
        "app/routes/localized.py",
        "app/services/delivery_workflow.py",
        "app/services/evidence_export.py",
        "app/templates/delivery_dashboard.html",
        "app/static/css/app.css",
        "app/i18n/catalog.en.json",
        "app/schemas/evidence-export-manifest.v1.schema.json",
        "migrations/006_slice7_delivery.sql",
        "tests/test_slice7_delivery.py",
    ):
        assert relative in text
        assert (project_root / relative).is_file()
