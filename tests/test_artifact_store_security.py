from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import app.services.artifact_store as store
from app.services.artifact_store import (
    ArtifactStoreError,
    MAX_CANDIDATE_BINARY_BYTES,
    MAX_CANDIDATE_MANIFEST_BYTES,
    MAX_JSON_ARTIFACT_BYTES,
    read_verified,
    verify_candidate_bundle,
)


def _write(root: Path, relative: str, raw: bytes) -> tuple[str, int]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _read(root: Path, relative: str, digest: str, length: int) -> bytes:
    return read_verified(root, relative, digest, length, max_bytes=MAX_JSON_ARTIFACT_BYTES)


def test_descriptor_reader_accepts_exact_regular_file(tmp_path: Path) -> None:
    digest, length = _write(tmp_path, "cases/case/artifact/content.json", b"exact bytes\n")
    assert _read(tmp_path, "cases/case/artifact/content.json", digest, length) == b"exact bytes\n"


def test_descriptor_reader_rejects_leaf_and_intermediate_symlinks(tmp_path: Path) -> None:
    digest, length = _write(tmp_path, "safe/content.json", b"safe")
    (tmp_path / "safe/leaf.json").symlink_to(tmp_path / "safe/content.json")
    with pytest.raises(ArtifactStoreError):
        _read(tmp_path, "safe/leaf.json", digest, length)
    (tmp_path / "linked").symlink_to(tmp_path / "safe", target_is_directory=True)
    with pytest.raises(ArtifactStoreError):
        _read(tmp_path, "linked/content.json", digest, length)


def test_open_directory_descriptor_anchors_parent_replacement(tmp_path: Path) -> None:
    raw = b"anchored original"
    digest, length = _write(tmp_path, "cases/case/artifact/content.json", raw)
    descriptor = store._open_directory_descriptor(tmp_path, "cases/case/artifact")
    try:
        original = tmp_path / "cases/case/artifact"
        original.rename(tmp_path / "cases/case/artifact-old")
        replacement = tmp_path / "cases/case/artifact"
        replacement.mkdir()
        (replacement / "content.json").write_bytes(b"replacement payload")
        assert store._read_from_directory_descriptor(
            descriptor, "content.json", digest, length, MAX_JSON_ARTIFACT_BYTES
        ) == raw
    finally:
        os.close(descriptor)


def test_leaf_path_swap_after_open_keeps_verified_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = b"descriptor target"
    digest, length = _write(tmp_path, "cases/case/artifact/content.json", raw)
    target = tmp_path / "cases/case/artifact/content.json"
    original_open = store.os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "content.json" and dir_fd is not None and not swapped:
            swapped = True
            target.rename(target.with_name("content-old.json"))
            target.write_bytes(b"untrusted replacement")
        return descriptor

    monkeypatch.setattr(store.os, "open", swapping_open)
    assert _read(tmp_path, "cases/case/artifact/content.json", digest, length) == raw


def test_sparse_oversize_file_is_rejected_before_content_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cases/case/artifact/content.json"
    path.parent.mkdir(parents=True)
    with path.open("wb") as stream:
        stream.truncate(MAX_JSON_ARTIFACT_BYTES + 1)
    called = False
    original_read = store.os.read

    def observed_read(descriptor: int, size: int) -> bytes:
        nonlocal called
        called = True
        return original_read(descriptor, size)

    monkeypatch.setattr(store.os, "read", observed_read)
    with pytest.raises(ArtifactStoreError, match="length_mismatch"):
        _read(tmp_path, "cases/case/artifact/content.json", "0" * 64, 1)
    assert called is False


def test_short_read_and_same_size_digest_mismatch_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest, length = _write(tmp_path, "cases/case/artifact/content.json", b"abcdefgh")
    original_read = store.os.read
    calls = 0

    def short_read(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_read(descriptor, max(1, size // 2))
        return b""

    monkeypatch.setattr(store.os, "read", short_read)
    with pytest.raises(ArtifactStoreError, match="short_read"):
        _read(tmp_path, "cases/case/artifact/content.json", digest, length)
    monkeypatch.setattr(store.os, "read", original_read)
    with pytest.raises(ArtifactStoreError, match="digest_mismatch"):
        _read(tmp_path, "cases/case/artifact/content.json", "0" * 64, length)


def test_growth_during_read_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"a" * (store._READ_CHUNK_BYTES + 10)
    digest, length = _write(tmp_path, "cases/case/artifact/content.json", raw)
    target = tmp_path / "cases/case/artifact/content.json"
    original_read = store.os.read
    calls = 0

    def growing_read(descriptor: int, size: int) -> bytes:
        nonlocal calls
        chunk = original_read(descriptor, size)
        calls += 1
        if calls == 1:
            with target.open("ab") as stream:
                stream.write(b"growth")
        return chunk

    monkeypatch.setattr(store.os, "read", growing_read)
    with pytest.raises(ArtifactStoreError, match="length_mismatch"):
        _read(tmp_path, "cases/case/artifact/content.json", digest, length)


def test_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "cases/case/artifact/content.json"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    with pytest.raises(ArtifactStoreError, match="artifact_file_invalid"):
        _read(tmp_path, "cases/case/artifact/content.json", "0" * 64, 1)


def _candidate_contracts(files: dict[str, bytes]) -> dict[str, tuple[str, int, int]]:
    return {
        "manifest.json": (
            hashlib.sha256(files["manifest.json"]).hexdigest(),
            len(files["manifest.json"]),
            MAX_CANDIDATE_MANIFEST_BYTES,
        ),
        "document.pdf": (
            hashlib.sha256(files["document.pdf"]).hexdigest(),
            len(files["document.pdf"]),
            MAX_CANDIDATE_BINARY_BYTES,
        ),
        "diagram.svg": (
            hashlib.sha256(files["diagram.svg"]).hexdigest(),
            len(files["diagram.svg"]),
            MAX_CANDIDATE_BINARY_BYTES,
        ),
    }


def test_candidate_bundle_rejects_missing_extra_and_symlink_entries(tmp_path: Path) -> None:
    relative = "cases/case/candidates/candidate"
    directory = tmp_path / relative
    directory.mkdir(parents=True)
    files = {
        "manifest.json": b"{}\n",
        "document.pdf": b"%PDF-1.4\n",
        "diagram.svg": b"<svg/>\n",
    }
    for name, raw in files.items():
        (directory / name).write_bytes(raw)
    contracts = _candidate_contracts(files)
    assert verify_candidate_bundle(tmp_path, relative, contracts) == files
    (directory / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(ArtifactStoreError, match="candidate_file_set_invalid"):
        verify_candidate_bundle(tmp_path, relative, contracts)
    (directory / "extra.txt").unlink()
    (directory / "diagram.svg").unlink()
    with pytest.raises(ArtifactStoreError, match="candidate_file_set_invalid"):
        verify_candidate_bundle(tmp_path, relative, contracts)
    (directory / "diagram.svg").symlink_to(directory / "manifest.json")
    with pytest.raises(ArtifactStoreError, match="candidate_integrity_invalid"):
        verify_candidate_bundle(tmp_path, relative, contracts)


@pytest.mark.parametrize("mutation", ["add", "remove"])
def test_candidate_bundle_rechecks_exact_set_after_member_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    relative = f"cases/case/candidates/{mutation}"
    directory = tmp_path / relative
    directory.mkdir(parents=True)
    files = {
        "manifest.json": b"{}\n",
        "document.pdf": b"%PDF-1.4\n",
        "diagram.svg": b"<svg/>\n",
    }
    for name, raw in files.items():
        (directory / name).write_bytes(raw)
    contracts = _candidate_contracts(files)
    original_read = store._read_from_directory_descriptor
    mutated = False

    def mutate_after_read(*args, **kwargs):
        nonlocal mutated
        raw = original_read(*args, **kwargs)
        if not mutated:
            mutated = True
            if mutation == "add":
                (directory / "extra.txt").write_text("persistent extra", encoding="utf-8")
            else:
                (directory / "manifest.json").unlink()
        return raw

    monkeypatch.setattr(store, "_read_from_directory_descriptor", mutate_after_read)
    with pytest.raises(ArtifactStoreError, match="candidate_file_set_invalid"):
        verify_candidate_bundle(tmp_path, relative, contracts)


def test_descriptor_capability_absence_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest, length = _write(tmp_path, "cases/case/artifact/content.json", b"safe")
    monkeypatch.delattr(store.os, "O_NOFOLLOW")
    with pytest.raises(ArtifactStoreError, match="descriptor_safe_read_unavailable"):
        _read(tmp_path, "cases/case/artifact/content.json", digest, length)
