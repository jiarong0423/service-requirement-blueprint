from __future__ import annotations

from html.parser import HTMLParser
import re
import xml.etree.ElementTree as ET


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
    assert "Earlier repair checkpoint — historical evidence" in evidence
    assert "generated `public-export-manifest.json`" in evidence
    assert "prior 122-file package" in evidence
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


def test_main_proposal_copy_uses_plain_language(project_root) -> None:
    import json

    catalog = json.loads((project_root / "app/i18n/catalog.en.json").read_text(encoding="utf-8"))
    keys = (
        "help.json_b_business_data",
        "notice.json_b_draft_private",
        "notice.json_b_exact_bytes",
        "notice.json_b_server_fields",
    )
    combined = " ".join(catalog[key] for key in keys).lower()
    for forbidden in ("byte", "server owns", "binding", "candidate"):
        assert forbidden not in combined
    assert "share it with the client for review" in catalog["notice.json_b_exact_bytes"]


def test_current_demo_narration_captions_and_scene_contract(project_root) -> None:
    srt = (project_root / "devpost/demo-script.srt").read_text(encoding="utf-8")
    voiceover = (project_root / "devpost/demo-voiceover.txt").read_text(encoding="utf-8")
    shot_list = (project_root / "devpost/demo-shot-list.md").read_text(encoding="utf-8")
    production = (project_root / "devpost/demo-production-package.md").read_text(encoding="utf-8")
    guide = (project_root / "devpost/demo-capture-guide.md").read_text(encoding="utf-8")

    def milliseconds(value: str) -> int:
        hours, minutes, rest = value.split(":")
        seconds, millis = rest.split(",")
        return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)

    blocks = [block.strip().splitlines() for block in srt.strip().split("\n\n")]
    spoken: list[str] = []
    previous_end = 0
    for expected_index, block in enumerate(blocks, 1):
        assert int(block[0]) == expected_index
        start, end = [part.strip() for part in block[1].split("-->")]
        assert milliseconds(start) >= previous_end
        assert milliseconds(end) - milliseconds(start) >= 1_000
        assert len(block[2:]) <= 2
        assert all(len(line) <= 52 for line in block[2:])
        spoken.append(" ".join(block[2:]))
        previous_end = milliseconds(end)
    assert len(blocks) == 39
    assert previous_end == 161_800
    paragraphs = voiceover.strip().split("\n\n")
    assert len(paragraphs) == 23
    assert " ".join(" ".join(paragraph.split()) for paragraph in paragraphs) == " ".join(spoken)
    for text in (srt, voiceover, shot_list, production, guide):
        assert not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text)
        assert "US$199" not in text
        assert re.search(r"(?<!NT)\$199", text) is None
    rows = re.findall(r"\| ([0-9]{2}) · ([^|]+) \| ([0-9.]+)–([0-9.]+) \| (illustration|video|still) \|", shot_list)
    assert len(rows) == 23
    previous = 0.0
    for index, (_, _, start, end, _) in enumerate(rows, 1):
        assert int(rows[index - 1][0]) == index
        assert abs(float(start) - previous) < 0.002
        assert float(end) > float(start)
        previous = float(end)
    assert previous == 161.8
    assert [row[4] for row in rows].count("illustration") == 3
    assert [row[4] for row in rows].count("video") == 4
    assert [row[4] for row in rows].count("still") == 16
    assert "HISTORICAL / STALE EVIDENCE" in production
    assert "not a recorded download click" in production
    assert "inspected off camera" in guide
    assert "Continuous human playback" in guide
    assert "not proof of an independently tested" in guide
    assert "current editing or subtitle authority" in guide
    assert "Administrator" in shot_list
    assert "`/en/cases/<case_id>/json-b/reviews/<version_no>`" in shot_list
    assert "`/en/cases/<case_id>/delivery`" in shot_list
    ET.parse(project_root / "devpost/media/demo-end-card.svg")


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
    assert "earlier P1 repair checkpoint <code>f0cc808</code>" in text
    assert "public-export-manifest.json" in text
    assert "Recording readiness remains a separate browser gate" in text
    assert "app/presentation_semantics.py" in text
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
