from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import os
import re
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pypdf import PdfReader

import app.gate0 as gate0_module
from app import cli as cli_module
from app.gate0 import Artifact, ArtifactRegistry, Gate0Builder, Gate0Error, _load_json


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(root: Path, output: Path) -> tuple[dict[str, object], Path]:
    result = Gate0Builder(root, output_root=output).build()
    run_dir = output / "runs" / str(result["run_id"])
    return result, run_dir


def test_pinned_requirements_match_installed_environment(project_root: Path) -> None:
    requirements = {}
    for line in (project_root / "requirements.txt").read_text(encoding="utf-8").splitlines():
        name, version = line.split("==", 1)
        requirements[name.lower()] = version
    assert requirements == {
        "pygments": "2.21.0",
        "flask": "3.1.3",
        "jinja2": "3.1.6",
        "markupsafe": "3.0.4",
        "werkzeug": "3.1.9",
        "attrs": "26.1.0",
        "blinker": "1.9.0",
        "charset-normalizer": "3.5.2",
        "click": "8.5.0",
        "fonttools": "4.66.1",
        "iniconfig": "2.3.0",
        "itsdangerous": "2.2.0",
        "jsonschema": "4.26.0",
        "jsonschema-specifications": "2025.9.1",
        "packaging": "26.3",
        "pillow": "12.3.0",
        "pluggy": "1.6.0",
        "pypdf": "6.19.0",
        "pypdfium2": "5.14.0",
        "pytest": "9.1.1",
        "referencing": "0.37.0",
        "reportlab": "5.0.1",
        "rpds-py": "2026.9.1",
        "typing_extensions": "4.16.0",
    }
    for name, expected in requirements.items():
        assert importlib.metadata.version(name) == expected


def test_gate0_builds_exact_immutable_artifact_set(project_root: Path, tmp_path: Path) -> None:
    output = tmp_path / "preflight"
    result, run_dir = build(project_root, output)

    assert result["status"] == "complete"
    assert re.fullmatch(r"[a-f0-9]{32}", str(result["run_id"]))
    assert {path.name for path in run_dir.iterdir()} == {"document.pdf", "diagram.svg", "manifest.json"}
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    for path in run_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o400

    manifest_path = run_dir / "manifest.json"
    manifest = _load_json(manifest_path)
    assert isinstance(manifest, dict)
    assert manifest["schema_version"] == "gate0-preflight-manifest/1.1"
    assert manifest["status"] == "complete"
    assert manifest["label_mode"] == "english_only"
    assert "manifest" not in manifest["artifacts"]
    for kind, filename in (("pdf", "document.pdf"), ("svg", "diagram.svg")):
        path = run_dir / filename
        record = manifest["artifacts"][kind]
        assert record["bytes"] == path.stat().st_size
        assert record["sha256"] == sha256(path)
        assert record["path"] == f"runs/{result['run_id']}/{filename}"

    serialized = manifest_path.read_text(encoding="utf-8")
    assert str(project_root) not in serialized
    assert str(Path.home()) not in serialized
    assert sha256(manifest_path) not in serialized


def test_pdf_and_svg_contain_english_labels_and_original_text_once(project_root: Path, tmp_path: Path) -> None:
    _, run_dir = build(project_root, tmp_path / "preflight")
    sample = _load_json(project_root / "app/preflight/sample.json")
    assert isinstance(sample, dict)
    original = str(sample["user_content"])

    pdf_text = PdfReader(run_dir / "document.pdf", strict=True).pages[0].extract_text()
    assert "Service Requirements and Delivery Document" in pdf_text
    assert "Final Blueprint" in pdf_text
    assert pdf_text.count(original) == 1

    svg_root = ET.fromstring((run_dir / "diagram.svg").read_bytes())
    svg_text = "".join(svg_root.itertext())
    assert "Requirement" in svg_text
    assert "Proposal" in svg_text
    assert "Candidate" in svg_text
    assert svg_text.count(original) == 1
    assert all(element.tag.rsplit("}", 1)[-1].lower() not in {"script", "foreignobject"} for element in svg_root.iter())


def test_repeated_successes_use_unique_directories(project_root: Path, tmp_path: Path) -> None:
    output = tmp_path / "preflight"
    first, first_dir = build(project_root, output)
    first_hashes = {path.name: sha256(path) for path in first_dir.iterdir()}
    second, second_dir = build(project_root, output)

    assert first["run_id"] != second["run_id"]
    assert first_dir != second_dir
    assert first_hashes == {path.name: sha256(path) for path in first_dir.iterdir()}
    assert {path.name for path in second_dir.iterdir()} == {"document.pdf", "diagram.svg", "manifest.json"}


@pytest.mark.parametrize("kind,filename", [("pdf", "a.pdf"), ("svg", "a.svg"), ("manifest", "a.json")])
def test_second_artifact_of_same_kind_is_rejected(kind: str, filename: str) -> None:
    registry = ArtifactRegistry()
    registry.add(Artifact(kind, filename, b"first"))
    with pytest.raises(Gate0Error, match="^duplicate_artifact_kind$"):
        registry.add(Artifact(kind, filename, b"second"))


def test_unknown_artifact_kind_is_rejected() -> None:
    registry = ArtifactRegistry()
    with pytest.raises(Gate0Error, match="^unsupported_artifact_kind$"):
        registry.add(Artifact("png", "preview.png", b"content"))


def test_missing_font_fails_without_output(isolated_project: Path, tmp_path: Path) -> None:
    (isolated_project / "assets/fonts/NotoSansTC-Bold.ttf").unlink()
    output = tmp_path / "preflight"
    with pytest.raises(Gate0Error, match="^required_file_missing$"):
        Gate0Builder(isolated_project, output_root=output).build()
    assert not (output / "runs").exists()


def test_changed_font_fails_digest_validation(isolated_project: Path, tmp_path: Path) -> None:
    path = isolated_project / "assets/fonts/NotoSansTC-Regular.ttf"
    path.unlink()
    path.write_bytes(b"not-a-font")
    with pytest.raises(Gate0Error, match="^font_digest_mismatch$"):
        Gate0Builder(isolated_project, output_root=tmp_path / "preflight").build()


def test_missing_license_fails_without_output(isolated_project: Path, tmp_path: Path) -> None:
    (isolated_project / "assets/fonts/OFL.txt").unlink()
    output = tmp_path / "preflight"
    with pytest.raises(Gate0Error, match="^required_file_missing$"):
        Gate0Builder(isolated_project, output_root=output).build()
    assert not (output / "runs").exists()


def test_render_failure_leaves_no_reviewable_run(project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "preflight"

    def fail_render(self: Gate0Builder, drawing: object) -> bytes:
        raise Gate0Error("pdf_render_failed")

    monkeypatch.setattr(Gate0Builder, "_render_pdf", fail_render)
    with pytest.raises(Gate0Error, match="^pdf_render_failed$"):
        Gate0Builder(project_root, output_root=output).build()
    assert not (output / "runs").exists()


def test_publish_is_one_atomic_directory_rename(
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "preflight"
    run_id = "b" * 32
    real_rename = os.rename
    observations: list[tuple[bool, set[str]]] = []

    def observe_rename(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        destination_path = Path(destination)
        if destination_path == output / "runs" / run_id:
            observations.append((destination_path.exists(), set()))
            real_rename(source, destination)
            observations.append((destination_path.exists(), {path.name for path in destination_path.iterdir()}))
            return
        real_rename(source, destination)

    monkeypatch.setattr(gate0_module.os, "rename", observe_rename)
    Gate0Builder(project_root, output_root=output, run_id_factory=lambda: run_id).build()

    assert observations == [
        (False, set()),
        (True, {"document.pdf", "diagram.svg", "manifest.json"}),
    ]


def test_publish_failure_quarantines_complete_staging_without_exposing_run(
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "preflight"
    real_rename = os.rename

    def fail_publish(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        if Path(destination).parent == output / "runs":
            raise OSError("injected_publish_failure")
        real_rename(source, destination)

    monkeypatch.setattr(gate0_module.os, "rename", fail_publish)
    with pytest.raises(Gate0Error, match="^artifact_install_failed$"):
        Gate0Builder(project_root, output_root=output).build()

    assert list((output / "runs").iterdir()) == []
    quarantined = list((output / "quarantine").iterdir())
    assert len(quarantined) == 1
    assert {path.name for path in quarantined[0].iterdir()} == {"document.pdf", "diagram.svg", "manifest.json"}
    assert list((output / ".staging").iterdir()) == []


def test_quarantine_failure_is_observable_and_never_exposes_run(
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "preflight"

    def fail_all_renames(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        raise OSError("injected_rename_failure")

    monkeypatch.setattr(gate0_module.os, "rename", fail_all_renames)
    with pytest.raises(Gate0Error, match="^quarantine_failed$"):
        Gate0Builder(project_root, output_root=output).build()

    assert list((output / "runs").iterdir()) == []
    assert list((output / ".staging").iterdir()) == []


def test_destination_parent_fsync_failure_removes_published_run(
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "preflight"
    real_fsync = gate0_module._safe_fsync_directory
    failed_once = False

    def fail_after_publish(path: Path) -> None:
        nonlocal failed_once
        if path == output / "runs" and any((output / "runs").iterdir()) and not failed_once:
            failed_once = True
            raise Gate0Error("directory_fsync_failed")
        real_fsync(path)

    monkeypatch.setattr(gate0_module, "_safe_fsync_directory", fail_after_publish)
    with pytest.raises(Gate0Error, match="^directory_fsync_failed$"):
        Gate0Builder(project_root, output_root=output).build()

    assert failed_once
    assert list((output / "runs").iterdir()) == []
    quarantined = list((output / "quarantine").iterdir())
    assert len(quarantined) == 1
    assert {path.name for path in quarantined[0].iterdir()} == {"document.pdf", "diagram.svg", "manifest.json"}


def test_destination_fsync_and_quarantine_failure_deletes_failed_publish(
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "preflight"
    real_fsync = gate0_module._safe_fsync_directory
    real_rename = os.rename
    published = False
    failed_once = False

    def track_publish_then_fail_quarantine(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        nonlocal published
        destination_path = Path(destination)
        if destination_path.parent == output / "runs":
            real_rename(source, destination)
            published = True
            return
        if published and destination_path.parent == output / "quarantine":
            raise OSError("injected_quarantine_failure")
        real_rename(source, destination)

    def fail_after_publish(path: Path) -> None:
        nonlocal failed_once
        if path == output / "runs" and published and not failed_once:
            failed_once = True
            raise Gate0Error("directory_fsync_failed")
        real_fsync(path)

    monkeypatch.setattr(gate0_module.os, "rename", track_publish_then_fail_quarantine)
    monkeypatch.setattr(gate0_module, "_safe_fsync_directory", fail_after_publish)
    with pytest.raises(Gate0Error, match="^quarantine_failed$"):
        Gate0Builder(project_root, output_root=output).build()

    assert published and failed_once
    assert list((output / "runs").iterdir()) == []
    assert list((output / "quarantine").iterdir()) == []


def test_reused_run_id_cannot_overwrite_existing_artifacts(project_root: Path, tmp_path: Path) -> None:
    run_id = "a" * 32
    output = tmp_path / "preflight"
    first = Gate0Builder(project_root, output_root=output, run_id_factory=lambda: run_id).build()
    run_dir = output / "runs" / run_id
    before = {path.name: sha256(path) for path in run_dir.iterdir()}
    with pytest.raises(Gate0Error, match="^run_id_collision$"):
        Gate0Builder(project_root, output_root=output, run_id_factory=lambda: run_id).build()
    assert first["run_id"] == run_id
    assert before == {path.name: sha256(path) for path in run_dir.iterdir()}


def test_cli_reports_only_relative_artifact_paths(project_root: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "app.cli", "gate0"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "complete"
    assert str(project_root) not in completed.stdout
    assert str(Path.home()) not in completed.stdout
    assert all(not Path(record["path"]).is_absolute() for record in payload["artifacts"].values())
    assert completed.stderr == ""


def test_cli_sanitizes_unexpected_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail_build(self: Gate0Builder) -> dict[str, object]:
        raise RuntimeError("sensitive absolute path: /Users/example/private")

    monkeypatch.setattr(Gate0Builder, "build", fail_build)
    assert cli_module.main(["gate0"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "failed", "error_code": "unexpected_failure"}
    assert "/Users" not in captured.err
