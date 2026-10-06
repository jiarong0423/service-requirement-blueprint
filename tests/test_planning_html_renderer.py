from __future__ import annotations

import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path


class TableShapeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._row.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def test_planning_html_companions_match_canonical_markdown(
    project_root: Path,
    tmp_path: Path,
) -> None:
    renderer = project_root / "tools/render_planning_html.py"
    for name in ("prd", "spec"):
        generated = tmp_path / f"{name}.html"
        result = subprocess.run(
            [sys.executable, str(renderer), str(project_root / "devpost" / f"{name}.md"), str(generated)],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert generated.read_bytes() == (project_root / "devpost" / f"{name}.html").read_bytes()
        parser = TableShapeParser()
        parser.feed(generated.read_text(encoding="utf-8"))
        expected_columns: int | None = None
        for row in parser.rows:
            if row and all(cell == "th" for cell in row):
                expected_columns = len(row)
            elif row and expected_columns is not None:
                assert len(row) == expected_columns
