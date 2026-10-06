from __future__ import annotations

from html import escape
from unicodedata import combining


NODE_WIDTH = 260
NODE_HEIGHT = 126
NODE_TOP = 62
NODE_X = (10, 350, 690)
MAX_LINES = 4
TEXT_INSET = 16
MAX_TEXT_WIDTH = NODE_WIDTH - TEXT_INSET * 2


def estimate_svg_text_width(value: str) -> int:
    width = 0
    for character in value:
        if combining(character):
            continue
        if character.isspace():
            width += 7
        else:
            width += 20
    return width


def wrap_svg_label(value: str) -> list[str]:
    normalized = " ".join(value.split()) or "Not specified"
    lines: list[str] = []
    current = ""
    for token in normalized.split(" "):
        candidate = f"{current} {token}" if current else token
        if estimate_svg_text_width(candidate) <= MAX_TEXT_WIDTH:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        token_part = ""
        for character in token:
            candidate_part = token_part + character
            if token_part and estimate_svg_text_width(candidate_part) > MAX_TEXT_WIDTH:
                lines.append(token_part)
                token_part = character
            else:
                token_part = candidate_part
        current = token_part
    if current:
        lines.append(current)
    if len(lines) <= MAX_LINES:
        return lines
    visible = lines[:MAX_LINES]
    final_line = visible[-1].rstrip()
    while final_line and estimate_svg_text_width(final_line + "…") > MAX_TEXT_WIDTH:
        final_line = final_line[:-1].rstrip()
    visible[-1] = (final_line + "…") if final_line else "…"
    return visible


def blueprint_data(requirements: dict[str, object]) -> dict[str, list[dict[str, str]]]:
    project = requirements.get("project", {})
    if not isinstance(project, dict):
        project = {}
    purpose = str(project.get("primary_purpose", ""))
    action = str(project.get("primary_visitor_action", ""))
    return {
        "nodes": [
            {"id": "visitor", "label": str(project.get("target_audience", ""))},
            {"id": "website", "label": purpose},
            {"id": "action", "label": action},
        ],
        "edges": [
            {"source": "visitor", "target": "website", "label": "visits"},
            {"source": "website", "target": "action", "label": "supports"},
        ],
    }


def render_svg(blueprint: dict[str, list[dict[str, str]]]) -> str:
    nodes = blueprint["nodes"]
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="250" viewBox="0 0 960 250" preserveAspectRatio="xMidYMid meet" role="img">',
        '<rect width="960" height="250" fill="#f7f4ea"/>',
        '<path d="M270 125 H340 M610 125 H680" stroke="#315848" stroke-width="3"/>',
        "<defs>",
    ]
    for index, x in enumerate(NODE_X):
        parts.append(
            f'<clipPath id="node-clip-{index}"><rect x="{x + TEXT_INSET}" y="{NODE_TOP + 8}" width="{MAX_TEXT_WIDTH}" height="{NODE_HEIGHT - 16}"/></clipPath>'
        )
    parts.append("</defs>")
    for index, node in enumerate(nodes):
        x = NODE_X[index]
        center_x = x + NODE_WIDTH // 2
        lines = wrap_svg_label(str(node["label"]))
        line_height = 22
        first_baseline = NODE_TOP + (NODE_HEIGHT - line_height * len(lines)) // 2 + 17
        parts.append(
            f'<rect x="{x}" y="{NODE_TOP}" width="{NODE_WIDTH}" height="{NODE_HEIGHT}" rx="12" fill="#e5eee7" stroke="#315848"/>'
        )
        parts.append(
            f'<text x="{center_x}" y="{first_baseline}" text-anchor="middle" font-family="sans-serif" font-size="17" fill="#173b2e" clip-path="url(#node-clip-{index})">'
        )
        for line_index, line in enumerate(lines):
            dy = 0 if line_index == 0 else line_height
            parts.append(f'<tspan x="{center_x}" dy="{dy}">{escape(line)}</tspan>')
        parts.append("</text>")
    parts.append("</svg>")
    return "".join(parts)
