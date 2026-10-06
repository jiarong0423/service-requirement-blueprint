from __future__ import annotations

import uuid
from pathlib import Path

import pytest

import app.services.artifact_store as store
from app.services.artifact_store import ArtifactStoreError, install_candidate_bundle


def _files() -> dict[str, bytes]:
    return {
        "manifest.json": b"{}\n",
        "document.pdf": b"fixed-pdf-bytes",
        "diagram.svg": b"<svg/>\n",
    }


def test_candidate_publish_never_replaces_existing_target(tmp_path: Path) -> None:
    case_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    target = tmp_path / "cases" / case_id / "candidates" / candidate_id
    target.mkdir(parents=True)
    sentinel = target / "sentinel"
    sentinel.write_bytes(b"existing")

    with pytest.raises(ArtifactStoreError, match="candidate_exists"):
        install_candidate_bundle(tmp_path, case_id, candidate_id, _files())

    assert sentinel.read_bytes() == b"existing"
    assert {entry.name for entry in target.iterdir()} == {"sentinel"}


def test_post_publish_directory_fsync_failure_quarantines_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    original = store._fsync_directory
    staging_fsync_count = 0

    def fail_after_publish(path: Path) -> None:
        nonlocal staging_fsync_count
        if path.name == "staging":
            staging_fsync_count += 1
        if path.name == "staging" and staging_fsync_count == 2:
            raise ArtifactStoreError("forced_directory_fsync_failure")
        original(path)

    monkeypatch.setattr(store, "_fsync_directory", fail_after_publish)
    with pytest.raises(ArtifactStoreError, match="forced_directory_fsync_failure"):
        install_candidate_bundle(tmp_path, case_id, candidate_id, _files())

    candidates = tmp_path / "cases" / case_id / "candidates"
    quarantine = tmp_path / "cases" / case_id / "quarantine"
    assert not any(candidates.iterdir())
    quarantined = list(quarantine.iterdir())
    assert len(quarantined) == 1
    assert {entry.name for entry in quarantined[0].iterdir()} == set(_files())


def test_first_candidate_durably_creates_each_directory_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    case_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    calls: list[Path] = []
    original = store._fsync_directory

    def record(path: Path) -> None:
        calls.append(path)
        original(path)

    monkeypatch.setattr(store, "_fsync_directory", record)
    install_candidate_bundle(data_root, case_id, candidate_id, _files())

    expected_new_directories = [
        data_root,
        data_root / "cases",
        data_root / "cases" / case_id,
        data_root / "cases" / case_id / "staging",
        data_root / "cases" / case_id / "candidates",
        data_root / "cases" / case_id / "quarantine",
    ]
    for directory in expected_new_directories:
        directory_call = calls.index(directory)
        parent_call = calls.index(directory.parent, directory_call + 1)
        assert directory_call < parent_call
