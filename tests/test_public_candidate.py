from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

import tools.build_public_candidate as build_public_candidate_module
from tools.build_public_candidate import (
    PUBLIC_MANIFEST_NAME,
    PublicCandidateError,
    build_public_candidate,
    public_candidate_files,
)


def _git(repo, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _candidate_repo(tmp_path, *, omit: str | None = None, bad_font_digest: bool = False):
    repo = tmp_path / "source"
    repo.mkdir(parents=True)
    files = {
        ".gitattributes": "* text=auto\n",
        ".gitignore": "instance/\n",
        "LICENSE": "MIT License\n",
        "README.md": "# Public candidate\n",
        "pytest.ini": "[pytest]\n",
        "requirements.txt": "Flask==3.1.0\n",
        "devpost/scope.md": "# Scope\n",
        "devpost/prd.md": "# PRD\n",
        "devpost/spec.md": "# Spec\n",
        "app/__init__.py": "APP_NAME = 'candidate'\n",
        "migrations/001.sql": "SELECT 1;\n",
        "tests/test_placeholder.py": "def test_placeholder():\n    assert True\n",
    }
    font_files = {
        "NotoSansTC-Regular.ttf": b"regular-font",
        "NotoSansTC-Bold.ttf": b"bold-font",
        "OFL.txt": b"Open Font License",
    }
    for relative, text_value in files.items():
        if relative == omit:
            continue
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text_value, encoding="utf-8")
    outputs = {}
    for name, raw in font_files.items():
        path = repo / "assets" / "fonts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        outputs[name] = {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if bad_font_digest:
        outputs["NotoSansTC-Regular.ttf"]["sha256"] = "0" * 64
    (repo / "assets" / "fonts" / "SOURCE.json").write_text(
        json.dumps({"outputs": outputs}, sort_keys=True), encoding="utf-8"
    )
    _git(repo, "init")
    _git(repo, "add", ".")
    commit_environment = dict(os.environ)
    commit_environment["GIT_AUTHOR_DATE"] = "2026-10-09T01:02:03+08:00"
    commit_environment["GIT_COMMITTER_DATE"] = "2026-10-09T01:02:03+08:00"
    _git(
        repo,
        "-c",
        "user.name=Candidate Test",
        "-c",
        "user.email=candidate@example.test",
        "commit",
        "-m",
        "candidate fixture",
        env=commit_environment,
    )
    return repo


def test_public_candidate_allowlist_includes_runtime_fonts_and_excludes_private_state(project_root) -> None:
    if (project_root / ".git").is_dir():
        files = set(public_candidate_files(project_root))
    else:
        manifest = json.loads((project_root / PUBLIC_MANIFEST_NAME).read_text(encoding="utf-8"))
        files = {record["path"] for record in manifest["files"]}
    assert {
        "assets/fonts/NotoSansTC-Regular.ttf",
        "assets/fonts/NotoSansTC-Bold.ttf",
        "assets/fonts/OFL.txt",
        "assets/fonts/SOURCE.json",
    }.issubset(files)
    assert not any(
        relative.startswith(prefix)
        for relative in files
        for prefix in (".agents/", "instance/", "logs/", "output/")
    )
    assert "devpost/learner-profile.md" not in files
    assert "devpost/learner-profile.zh-TW.md" not in files


def test_built_candidate_binds_clean_commit_tree_inventory_and_fonts(tmp_path) -> None:
    repo = _candidate_repo(tmp_path)
    output = tmp_path / "candidate"
    count, commit = build_public_candidate(repo, output)
    manifest = json.loads((output / PUBLIC_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "public-export-manifest/1.0"
    assert manifest["package_kind"] == "public_source_candidate"
    assert manifest["review_date"] == "2026-10-09"
    assert manifest["source_commit"] == commit == _git(repo, "rev-parse", "HEAD")
    assert manifest["source_tree"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert count == len(manifest["files"]) + 1
    for record in manifest["files"]:
        raw = (output / record["path"]).read_bytes()
        assert record["byte_length"] == len(raw)
        assert record["sha256"] == hashlib.sha256(raw).hexdigest()
    assert not any(path.is_symlink() for path in output.rglob("*"))
    repeated_output = tmp_path / "candidate-repeated"
    build_public_candidate(repo, repeated_output)
    assert (repeated_output / PUBLIC_MANIFEST_NAME).read_bytes() == (output / PUBLIC_MANIFEST_NAME).read_bytes()


def test_builder_rejects_malformed_source_commit_review_date(tmp_path, monkeypatch) -> None:
    repo = _candidate_repo(tmp_path)
    original_git = build_public_candidate_module._git

    def invalid_date_git(repo_root, *arguments: str) -> bytes:
        if arguments[:3] == ("show", "-s", "--format=%cs"):
            return b"2026-02-30\n"
        return original_git(repo_root, *arguments)

    monkeypatch.setattr(build_public_candidate_module, "_git", invalid_date_git)
    with pytest.raises(PublicCandidateError, match="source_commit_review_date_invalid"):
        build_public_candidate(repo, tmp_path / "invalid-date-output")


def test_builder_fails_closed_for_dirty_source_existing_output_and_missing_file(tmp_path) -> None:
    dirty_repo = _candidate_repo(tmp_path / "dirty")
    (dirty_repo / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(PublicCandidateError, match="public_worktree_dirty"):
        build_public_candidate(dirty_repo, tmp_path / "dirty-output")

    clean_repo = _candidate_repo(tmp_path / "existing")
    existing_output = tmp_path / "already-there"
    existing_output.mkdir()
    with pytest.raises(PublicCandidateError, match="public_output_exists"):
        build_public_candidate(clean_repo, existing_output)

    missing_repo = _candidate_repo(tmp_path / "missing", omit="LICENSE")
    with pytest.raises(PublicCandidateError, match="required_public_files_missing:LICENSE"):
        public_candidate_files(missing_repo)


def test_builder_rejects_font_manifest_mismatch_and_private_absolute_path(tmp_path) -> None:
    mismatch_repo = _candidate_repo(tmp_path / "mismatch", bad_font_digest=True)
    with pytest.raises(PublicCandidateError, match="font_provenance_mismatch"):
        build_public_candidate(mismatch_repo, tmp_path / "mismatch-output")

    private_repo = _candidate_repo(tmp_path / "private")
    private_path = "/" + "Users/example/private"
    (private_repo / "README.md").write_text(f"local path: {private_path}\n", encoding="utf-8")
    _git(private_repo, "add", "README.md")
    _git(
        private_repo,
        "-c",
        "user.name=Candidate Test",
        "-c",
        "user.email=candidate@example.test",
        "commit",
        "-m",
        "private path fixture",
    )
    with pytest.raises(PublicCandidateError, match="private_absolute_path:README.md"):
        build_public_candidate(private_repo, tmp_path / "private-output")


@pytest.mark.parametrize(
    "private_note",
    (
        "output/isolation/current_runs/local-receipt.json",
        "The private red-team repository contains an older review packet.",
        "Old files remain in the Downloads folder.",
        "Other files remain in Downloads.",
        "The old package moved to downloads-quarantine.",
    ),
)
def test_builder_rejects_internal_process_notes(tmp_path, private_note: str) -> None:
    repo = _candidate_repo(tmp_path / "process-note")
    (repo / "devpost" / "ship-evidence.md").write_text(private_note + "\n", encoding="utf-8")
    _git(repo, "add", "devpost/ship-evidence.md")
    _git(
        repo,
        "-c",
        "user.name=Candidate Test",
        "-c",
        "user.email=candidate@example.test",
        "commit",
        "-m",
        "private process note fixture",
    )
    with pytest.raises(PublicCandidateError, match="private_process_note:devpost/ship-evidence.md"):
        build_public_candidate(repo, tmp_path / "private-note-output")
