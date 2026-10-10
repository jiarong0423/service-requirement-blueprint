from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path


PUBLIC_ROOT_FILES = frozenset(
    {
        ".gitattributes",
        ".gitignore",
        "LICENSE",
        "README.md",
        "pytest.ini",
        "requirements.txt",
    }
)
PUBLIC_DEVPOST_FILES = frozenset(
    {
        "devpost/app-map.html",
        "devpost/checklist.md",
        "devpost/demo-guide.md",
        "devpost/demo-capture-guide.md",
        "devpost/demo-production-package.md",
        "devpost/demo-script.srt",
        "devpost/demo-shot-list.md",
        "devpost/demo-voiceover.txt",
        "devpost/frontend-flow-map.md",
        "devpost/media/demo-end-card.svg",
        "devpost/media/architecture.svg",
        "devpost/media/architecture.png",
        "devpost/media/skill-pack-flow.svg",
        "devpost/media/skill-pack-flow.png",
        "devpost/prd.html",
        "devpost/prd.md",
        "devpost/scope.html",
        "devpost/scope.md",
        "devpost/ship-evidence.md",
        "devpost/spec.html",
        "devpost/spec.md",
    }
)
PUBLIC_TOOL_FILES = frozenset(
    {
        "tools/build_gate0_fonts.py",
        "tools/build_public_candidate.py",
        "tools/build_ui_review.py",
        "tools/render_planning_html.py",
    }
)
PUBLIC_PREFIXES = (
    "app/",
    "assets/fonts/",
    "migrations/",
    "tests/",
)
PUBLIC_REQUIRED_FILES = frozenset(
    {
        "LICENSE",
        "README.md",
        "requirements.txt",
        "assets/fonts/NotoSansTC-Regular.ttf",
        "assets/fonts/NotoSansTC-Bold.ttf",
        "assets/fonts/OFL.txt",
        "assets/fonts/SOURCE.json",
        "devpost/scope.md",
        "devpost/prd.md",
        "devpost/spec.md",
    }
)
PUBLIC_MANIFEST_NAME = "public-export-manifest.json"
PUBLIC_MANIFEST_SCHEMA = "public-export-manifest/1.0"
PUBLIC_PACKAGE_KIND = "public_source_candidate"
REVIEW_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
PRIVATE_ABSOLUTE_PREFIX = b"/" + b"Users/"
PRIVATE_PROCESS_MARKERS = (
    b"output/isolation/",
    b"downloads-quarantine",
    b"private red-team",
    b"downloads folder",
    b"in downloads",
)


class PublicCandidateError(RuntimeError):
    pass


def _git(repo_root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        capture_output=True,
    ).stdout


def resolve_commit(repo_root: Path, revision: str) -> str:
    return _git(repo_root, "rev-parse", "--verify", f"{revision}^{{commit}}").decode("ascii").strip()


def is_valid_review_date(value: object) -> bool:
    if not isinstance(value, str) or REVIEW_DATE_PATTERN.fullmatch(value) is None:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _commit_review_date(repo_root: Path, commit: str) -> str:
    review_date = _git(repo_root, "show", "-s", "--format=%cs", commit).decode("ascii").strip()
    if not is_valid_review_date(review_date):
        raise PublicCandidateError("source_commit_review_date_invalid")
    return review_date


def _tracked_files(repo_root: Path, commit: str) -> tuple[str, ...]:
    return tuple(
        value.decode("utf-8")
        for value in _git(repo_root, "ls-tree", "-r", "-z", "--name-only", commit).split(b"\0")
        if value
    )


def public_candidate_files(repo_root: Path, revision: str = "HEAD") -> tuple[str, ...]:
    commit = resolve_commit(repo_root, revision)
    selected = tuple(
        sorted(
            relative
            for relative in _tracked_files(repo_root, commit)
            if (
                relative in PUBLIC_ROOT_FILES
                or relative in PUBLIC_DEVPOST_FILES
                or relative in PUBLIC_TOOL_FILES
                or relative.startswith(PUBLIC_PREFIXES)
            )
            and "__pycache__" not in Path(relative).parts
        )
    )
    missing = sorted(PUBLIC_REQUIRED_FILES.difference(selected))
    if missing:
        raise PublicCandidateError(f"required_public_files_missing:{','.join(missing)}")
    return selected


def _blob(repo_root: Path, commit: str, relative: str) -> bytes:
    return _git(repo_root, "cat-file", "blob", f"{commit}:{relative}")


def _verify_font_provenance(files: dict[str, bytes]) -> None:
    try:
        source = json.loads(files["assets/fonts/SOURCE.json"].decode("utf-8"))
        outputs = source["outputs"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicCandidateError("font_provenance_invalid") from error
    for name in ("NotoSansTC-Regular.ttf", "NotoSansTC-Bold.ttf", "OFL.txt"):
        relative = f"assets/fonts/{name}"
        record = outputs.get(name)
        if not isinstance(record, dict):
            raise PublicCandidateError(f"font_provenance_missing:{name}")
        raw = files[relative]
        if record.get("bytes") != len(raw) or record.get("sha256") != hashlib.sha256(raw).hexdigest():
            raise PublicCandidateError(f"font_provenance_mismatch:{name}")


def _verify_public_payload(files: dict[str, bytes]) -> None:
    forbidden_suffixes = (".sqlite", ".sqlite3", ".db", ".db-wal", ".db-shm", ".key")
    for relative, raw in files.items():
        path = Path(relative)
        if (
            path.name.startswith(".env")
            or path.name.endswith(forbidden_suffixes)
            or any(part in {"instance", "output", "logs", ".agents", ".git", ".venv"} for part in path.parts)
        ):
            raise PublicCandidateError(f"private_public_path:{relative}")
        if PRIVATE_ABSOLUTE_PREFIX in raw:
            raise PublicCandidateError(f"private_absolute_path:{relative}")
        if relative == "README.md" or relative in PUBLIC_DEVPOST_FILES:
            lowered = raw.lower()
            if any(marker in lowered for marker in PRIVATE_PROCESS_MARKERS):
                raise PublicCandidateError(f"private_process_note:{relative}")


def build_public_candidate(repo_root: Path, output_dir: Path, revision: str = "HEAD") -> tuple[int, str]:
    repo_root = repo_root.resolve(strict=True)
    output_dir = output_dir.resolve(strict=False)
    if _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PublicCandidateError("public_worktree_dirty")
    if output_dir.exists():
        raise PublicCandidateError("public_output_exists")
    commit = resolve_commit(repo_root, revision)
    source_tree = _git(repo_root, "rev-parse", f"{commit}^{{tree}}").decode("ascii").strip()
    review_date = _commit_review_date(repo_root, commit)
    selected = public_candidate_files(repo_root, commit)
    blobs = {relative: _blob(repo_root, commit, relative) for relative in selected}
    _verify_font_provenance(blobs)
    _verify_public_payload(blobs)
    records = [
        {
            "path": relative,
            "byte_length": len(blobs[relative]),
            "sha256": hashlib.sha256(blobs[relative]).hexdigest(),
        }
        for relative in selected
    ]
    manifest = (
        json.dumps(
            {
                "schema_version": PUBLIC_MANIFEST_SCHEMA,
                "package_kind": PUBLIC_PACKAGE_KIND,
                "review_date": review_date,
                "source_commit": commit,
                "source_tree": source_tree,
                "files": records,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir(mode=0o700)
        for relative, raw in blobs.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            os.chmod(target, 0o644)
        manifest_path = staging / PUBLIC_MANIFEST_NAME
        manifest_path.write_bytes(manifest)
        os.chmod(manifest_path, 0o644)
        os.replace(staging, output_dir)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return len(selected) + 1, commit


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a sanitized public source candidate from one exact clean Git commit."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--revision", default="HEAD")
    args = parser.parse_args()
    try:
        count, commit = build_public_candidate(args.repo_root, args.output_dir, args.revision)
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError, PublicCandidateError) as error:
        print(f"public_candidate_failed:{error}")
        return 1
    print(f"public_candidate_ready:commit={commit}:files={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
