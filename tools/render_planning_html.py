from __future__ import annotations

import argparse
import html
import re
from pathlib import Path


STYLE = """
:root{color-scheme:light;--ink:#173b2e;--paper:#fbfaf5;--card:#fff;--line:#cad8cf;--accent:#315848}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:#17221d;font:16px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.layout{display:grid;grid-template-columns:minmax(220px,280px) minmax(0,900px);gap:32px;max-width:1240px;margin:auto;padding:32px}
nav{position:sticky;top:24px;align-self:start;max-height:calc(100vh - 48px);overflow:auto;border:1px solid var(--line);border-radius:16px;background:var(--card);padding:20px}
nav strong{color:var(--ink)}nav a{display:block;color:var(--accent);text-decoration:none;padding:5px 0}nav a.sub{padding-left:14px;font-size:.92rem}
article{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:clamp(24px,5vw,56px);box-shadow:0 16px 50px rgba(31,61,48,.08)}
h1,h2,h3{color:var(--ink);line-height:1.25;scroll-margin-top:24px}h2{margin-top:2.2em;border-top:1px solid var(--line);padding-top:1em}
code{background:#eef4ef;border-radius:5px;padding:.1em .35em}pre{overflow:auto;background:#10251d;color:#eaf3ed;padding:18px;border-radius:12px}pre code{background:transparent;padding:0}
table{width:100%;border-collapse:collapse}.table-wrap{overflow:auto}th,td{border:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}th{background:#edf3ee}
.status{display:inline-block;margin-bottom:18px;border-radius:999px;background:#e5eee7;color:var(--ink);padding:6px 12px;font-weight:700}
@media(max-width:820px){.layout{display:block;padding:14px}nav{position:static;margin-bottom:16px}article{padding:22px}}
""".strip()


def slug(value: str) -> str:
    plain = re.sub(r"[`*_]", "", value).lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
    return normalized or "section"


def inline(value: str) -> str:
    escaped = html.escape(value, quote=False)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" rel="noreferrer">\1</a>',
        escaped,
    )
    return escaped


def split_frontmatter(text: str) -> tuple[dict[str, str], list[str]]:
    lines = text.splitlines()
    metadata: dict[str, str] = {}
    if not lines or lines[0] != "---":
        return metadata, lines
    try:
        end = lines.index("---", 1)
    except ValueError:
        return metadata, lines
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip()
    return metadata, lines[end + 1 :]


def is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def render_markdown(text: str) -> tuple[str, list[tuple[int, str, str]], dict[str, str]]:
    metadata, lines = split_frontmatter(text)
    output: list[str] = []
    headings: list[tuple[int, str, str]] = []
    used_slugs: dict[str, int] = {}
    paragraph: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            output.append(f"<p>{inline(' '.join(part.strip() for part in paragraph))}</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            flush_paragraph()
            language = line[3:].strip()
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and not lines[index].startswith("```"):
                code_lines.append(lines[index])
                index += 1
            class_name = f' class="language-{html.escape(language)}"' if language else ""
            output.append(f"<pre><code{class_name}>{html.escape(chr(10).join(code_lines))}</code></pre>")
            index += 1
            continue
        heading_match = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            base = slug(title)
            count = used_slugs.get(base, 0) + 1
            used_slugs[base] = count
            identifier = base if count == 1 else f"{base}-{count}"
            headings.append((level, title, identifier))
            output.append(f"<h{level} id=\"{identifier}\">{inline(title)}</h{level}>")
            index += 1
            continue
        if line.startswith("|") and index + 1 < len(lines) and is_table_separator(lines[index + 1]):
            flush_paragraph()
            headers = table_cells(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].startswith("|"):
                rows.append(table_cells(lines[index]))
                index += 1
            header_html = "".join(f"<th>{inline(cell)}</th>" for cell in headers)
            row_html = "".join(
                "<tr>" + "".join(f"<td>{inline(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            output.append(f'<div class="table-wrap"><table><thead><tr>{header_html}</tr></thead><tbody>{row_html}</tbody></table></div>')
            continue
        unordered = re.match(r"^\s*-\s+(.+)$", line)
        ordered = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if unordered or ordered:
            flush_paragraph()
            tag = "ul" if unordered else "ol"
            items: list[str] = []
            pattern = r"^\s*-\s+(.+)$" if tag == "ul" else r"^\s*\d+\.\s+(.+)$"
            while index < len(lines):
                match = re.match(pattern, lines[index])
                if match is None:
                    break
                items.append(f"<li>{inline(match.group(1).strip())}</li>")
                index += 1
            output.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        if not line.strip():
            flush_paragraph()
            index += 1
            continue
        paragraph.append(line)
        index += 1
    flush_paragraph()
    return "\n".join(output), headings, metadata


def render_document(source: Path, destination: Path) -> None:
    body, headings, metadata = render_markdown(source.read_text(encoding="utf-8"))
    title = next((heading[1] for heading in headings if heading[0] == 1), source.stem)
    navigation = "".join(
        f'<a class="{"sub" if level == 3 else ""}" href="#{identifier}">{inline(label)}</a>'
        for level, label, identifier in headings
        if level <= 3
    )
    status = metadata.get("status", "draft").title()
    document = (
        "<!doctype html>\n<html lang=\"en\">\n<head>"
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>\n"
        f'<body><div class="layout"><nav><strong>English Review</strong>{navigation}</nav><article>'
        f'<span class="status">{html.escape(status)} canonical document · synchronized from {html.escape(source.name)}</span>'
        f"{body}</article></div></body>\n</html>\n"
    )
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    render_document(arguments.source, arguments.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
