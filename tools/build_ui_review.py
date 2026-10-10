from __future__ import annotations

import argparse
import base64
import hashlib
from html import escape, unescape
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess
import sys
import tempfile
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app
from app.cli import initialize_demo
from tests.helpers import complete_requirement, csrf, login
from tests.slice6_helpers import create_candidate_context
from tests.slice7_helpers import create_formal_context, current_workflow, delivery_payload
from tools.build_public_candidate import (
    PUBLIC_MANIFEST_NAME,
    PUBLIC_MANIFEST_SCHEMA,
    PUBLIC_PACKAGE_KIND,
    is_valid_review_date,
)


UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.IGNORECASE)
SHA256_PATTERN = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", re.IGNORECASE)
HEADING_PATTERN = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
TAG_PATTERN = re.compile(r"<[^>]+>")
GIT_OBJECT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def visible_title(page: str) -> str:
    match = HEADING_PATTERN.search(page)
    if match is None:
        raise RuntimeError("visual_page_title_unavailable")
    title = unescape(TAG_PATTERN.sub("", match.group(1)))
    normalized = " ".join(title.split())
    if not normalized:
        raise RuntimeError("visual_page_title_unavailable")
    return normalized


def _freshness_sources(project_root: Path) -> tuple[Path, ...]:
    templates = tuple(sorted((project_root / "app/templates").glob("*.html")))
    css = project_root / "app/static/css/app.css"
    if not templates or not css.is_file():
        raise RuntimeError("visual_source_unavailable")
    return (*templates, css)


def _source_digest(project_root: Path, sources: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for source in sources:
        relative_path = source.relative_to(project_root).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_source_commit(project_root: Path) -> str | None:
    if not (project_root / ".git").exists():
        return None
    try:
        repository_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if Path(repository_root).resolve() != project_root.resolve():
            return None
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        return None
    if GIT_OBJECT_PATTERN.fullmatch(head) is None:
        raise RuntimeError("visual_source_commit_invalid")
    return head


def _validated_export_source_commit(project_root: Path, sources: tuple[Path, ...]) -> str:
    manifest_path = project_root / PUBLIC_MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("public_export_manifest_missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("public_export_manifest_invalid") from error
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "package_kind",
        "review_date",
        "source_commit",
        "source_tree",
        "files",
    }:
        raise RuntimeError("public_export_manifest_contract_invalid")
    source_commit = manifest["source_commit"]
    source_tree = manifest["source_tree"]
    if (
        manifest["schema_version"] != PUBLIC_MANIFEST_SCHEMA
        or manifest["package_kind"] != PUBLIC_PACKAGE_KIND
        or not is_valid_review_date(manifest["review_date"])
        or not isinstance(source_commit, str)
        or GIT_OBJECT_PATTERN.fullmatch(source_commit) is None
        or not isinstance(source_tree, str)
        or GIT_OBJECT_PATTERN.fullmatch(source_tree) is None
        or not isinstance(manifest["files"], list)
    ):
        raise RuntimeError("public_export_manifest_contract_invalid")

    project_root_resolved = project_root.resolve(strict=True)
    records: dict[str, dict[str, object]] = {}
    ordered_paths: list[str] = []
    for record in manifest["files"]:
        if not isinstance(record, dict) or set(record) != {"path", "byte_length", "sha256"}:
            raise RuntimeError("public_export_inventory_invalid")
        relative = record["path"]
        byte_length = record["byte_length"]
        expected_digest = record["sha256"]
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or not isinstance(byte_length, int)
            or isinstance(byte_length, bool)
            or byte_length < 0
            or not isinstance(expected_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        ):
            raise RuntimeError("public_export_inventory_invalid")
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or relative_path.as_posix() != relative or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise RuntimeError("public_export_inventory_invalid")
        if relative in records:
            raise RuntimeError(f"public_export_inventory_duplicate:{relative}")
        target = project_root.joinpath(*relative_path.parts)
        current = project_root
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink():
                raise RuntimeError(f"public_export_inventory_unsafe:{relative}")
        try:
            resolved_target = target.resolve(strict=True)
        except OSError as error:
            raise RuntimeError(f"public_export_inventory_missing:{relative}") from error
        if not resolved_target.is_relative_to(project_root_resolved) or not resolved_target.is_file():
            raise RuntimeError(f"public_export_inventory_unsafe:{relative}")
        raw = resolved_target.read_bytes()
        if len(raw) != byte_length or hashlib.sha256(raw).hexdigest() != expected_digest:
            raise RuntimeError(f"public_export_inventory_mismatch:{relative}")
        records[relative] = record
        ordered_paths.append(relative)
    if ordered_paths != sorted(ordered_paths):
        raise RuntimeError("public_export_inventory_order_invalid")
    required_sources = {source.relative_to(project_root).as_posix() for source in sources}
    missing_sources = sorted(required_sources.difference(records))
    if missing_sources:
        raise RuntimeError(f"public_export_source_missing:{','.join(missing_sources)}")
    return source_commit


def source_freshness() -> tuple[str, str]:
    sources = _freshness_sources(PROJECT_ROOT)
    head = _git_source_commit(PROJECT_ROOT)
    if head is None:
        head = _validated_export_source_commit(PROJECT_ROOT, sources)
    return head, _source_digest(PROJECT_ROOT, sources)


def build_demo_runtime(instance_root: Path):
    passwords = {
        "owner": secrets.token_urlsafe(24),
        "contractor": secrets.token_urlsafe(24),
        "admin": secrets.token_urlsafe(24),
    }
    os.environ["POC_INSTANCE_PATH"] = str(instance_root)
    os.environ["DEMO_OWNER_PASSWORD"] = passwords["owner"]
    os.environ["DEMO_CONTRACTOR_PASSWORD"] = passwords["contractor"]
    os.environ["DEMO_ADMIN_PASSWORD"] = passwords["admin"]
    initialize_demo(PROJECT_ROOT)
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": (instance_root / "secret.key").read_bytes(),
            "INSTANCE_ROOT": instance_root,
            "DATABASE": instance_root / "app.sqlite3",
            "DATA_ROOT": instance_root / "data",
            "MAX_CONTENT_LENGTH": 2_097_152,
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "SESSION_COOKIE_SECURE": False,
            "SESSION_REFRESH_EACH_REQUEST": False,
        }
    )
    return app, instance_root, passwords


def inline_page(response, client, css: str) -> str:
    html = response.get_data(as_text=True)
    html = re.sub(r'<link rel="stylesheet"[^>]+>', f"<style>{css}</style>", html)
    html = re.sub(r'<script\b[^>]*>.*?</script>', "", html, flags=re.DOTALL)

    def replace_image(match: re.Match[str]) -> str:
        source = match.group(1)
        image_response = client.get(source)
        if image_response.status_code != 200:
            raise RuntimeError(f"visual_asset_unavailable:{source}")
        encoded = base64.b64encode(image_response.data).decode("ascii")
        return f'src="data:{image_response.mimetype};base64,{encoded}"'

    html = re.sub(r'src="(/[^"]+\.svg)"', replace_image, html)
    html = re.sub(
        r'(<input\b[^>]*\btype="hidden"[^>]*\bvalue=")[^"]*(")',
        r"\1[redacted]\2",
        html,
    )
    html = html.replace("owner@example.test", "client@example.test")
    html = html.replace("contractor@example.test", "contractor@example.test")
    html = html.replace("admin@example.test", "administrator@example.test")
    html = html.replace("https://example.com", "reference.example.invalid")
    html = UUID_PATTERN.sub("demo-record-id", html)
    html = SHA256_PATTERN.sub("redacted-sha256", html)
    return html


def collect_pages(runtime) -> list[tuple[str, str]]:
    app, instance_root, passwords = runtime
    css = (PROJECT_ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
    pages: list[tuple[str, str]] = []

    anonymous = app.test_client()
    pages.append(("01 · Start", inline_page(anonymous.get("/en/"), anonymous, css)))

    client = app.test_client()
    if login(client, "en", "owner@example.test", passwords["owner"]).status_code != 302:
        raise RuntimeError("visual_login_failed")
    case_id = complete_requirement(client, instance_root)
    pages.append(("02 · Requirement Step 8", inline_page(client.get(f"/en/owner/requirements/{case_id}/sections/8"), client, css)))
    pages.append(("03 · Requirement Review", inline_page(client.get(f"/en/owner/requirements/{case_id}/review"), client, css)))

    candidate = create_candidate_context(runtime)
    pages.append(("04 · Private Case", inline_page(candidate["owner"].get(f"/en/cases/{candidate['case_id']}"), candidate["owner"], css)))
    pages.append(("05 · Candidate Review", inline_page(candidate["owner"].get(candidate["candidate_url"]), candidate["owner"], css)))

    formal = create_formal_context(runtime)
    pages.append(("06 · Formal Version", inline_page(formal["owner"].get(f"/en/cases/{formal['case_id']}/formal"), formal["owner"], css)))
    case_id = formal["case_id"]
    if formal["contractor"].post(
        f"/en/cases/{case_id}/deliveries",
        data=delivery_payload(formal["contractor"], formal["criterion_id"]),
    ).status_code != 302:
        raise RuntimeError("visual_delivery_failed")
    workflow = current_workflow(instance_root, case_id)
    if formal["owner"].post(
        f"/en/cases/{case_id}/deliveries/{workflow['current_delivery_id']}/begin-inspection",
        data={"csrf_token": csrf(formal["owner"]), "expected_revision": str(workflow["revision"])},
    ).status_code != 302:
        raise RuntimeError("visual_inspection_start_failed")
    workflow = current_workflow(instance_root, case_id)
    if formal["owner"].post(
        f"/en/cases/{case_id}/deliveries/{workflow['current_delivery_id']}/inspections",
        data={
            "csrf_token": csrf(formal["owner"]),
            "expected_revision": str(workflow["revision"]),
            "idempotency_key": str(uuid.uuid4()),
            f"result_{formal['criterion_id']}": "PASS",
            f"reason_{formal['criterion_id']}": "",
            "explicit_acceptance": "yes",
        },
    ).status_code != 302:
        raise RuntimeError("visual_acceptance_failed")
    if formal["owner"].post(
        f"/en/cases/{case_id}/evidence-exports",
        data={"csrf_token": csrf(formal["owner"])},
    ).status_code != 302:
        raise RuntimeError("visual_export_failed")
    pages.append(("07 · Accepted Delivery and Evidence Package", inline_page(formal["owner"].get(f"/en/cases/{case_id}/delivery"), formal["owner"], css)))

    administrator = app.test_client()
    if login(administrator, "en", "admin@example.test", passwords["admin"]).status_code != 302:
        raise RuntimeError("visual_admin_login_failed")
    pages.append(("08 · Administration", inline_page(administrator.get("/en/admin/accounts"), administrator, css)))
    return pages


def render_artifact(pages: list[tuple[str, str]], width: int, height: int) -> str:
    preview_width = 1152
    scale = preview_width / width
    preview_height = round(height * scale)
    head, source_digest = source_freshness()
    sections = []
    for index, (_title, page) in enumerate(pages, start=1):
        title = f"{index:02d} · {visible_title(page)}"
        sections.append(
            f'<section><h2>{escape(title)}</h2><div class="viewport"><iframe title="{escape(title)}" srcdoc="{escape(page, quote=True)}"></iframe></div></section>'
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Service Requirement Blueprint · UI Review</title>
<style>
body{{margin:0;padding:32px;background:#101827;color:#f5f7fb;font-family:system-ui,sans-serif}}main{{display:grid;gap:40px;max-width:1180px;margin:auto}}h1{{margin:0 0 8px}}p{{color:#b8c3d6}}section{{border-top:1px solid #354156;padding-top:20px}}.viewport{{width:{preview_width}px;height:{preview_height}px;overflow:hidden;background:white;box-shadow:0 16px 50px #0008}}iframe{{width:{width}px;height:{height}px;border:0;transform:scale({scale});transform-origin:top left}}
</style></head><body><main><header><h1>Service Requirement Blueprint</h1><p>{len(pages)} synthetic, de-identified {width}×{height} review surfaces. Generated locally without runtime credentials or external assets.</p><p>Source HEAD <code>{escape(head)}</code> · template/CSS SHA-256 <code>{escape(source_digest)}</code></p></header>""" + "".join(sections) + "</main></body></html>"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    args = parser.parse_args()
    if args.width < 320 or args.height < 480:
        raise SystemExit("viewport_too_small")
    with tempfile.TemporaryDirectory(prefix="srb-ui-review-") as directory:
        pages = collect_pages(build_demo_runtime(Path(directory) / "instance"))
    artifact = render_artifact(pages, args.width, args.height)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(artifact, encoding="utf-8")
    print(f"wrote {args.output} pages={len(pages)} bytes={args.output.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
