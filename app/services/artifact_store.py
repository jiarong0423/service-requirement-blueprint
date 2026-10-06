from __future__ import annotations

import hashlib
import ctypes
import errno
import os
import shutil
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path


class ArtifactStoreError(RuntimeError):
    pass


CANDIDATE_FILENAMES = ("manifest.json", "document.pdf", "diagram.svg")


def _within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    return resolved


def install_json_artifact(data_root: Path, case_id: str, artifact_id: str, raw: bytes) -> str:
    if str(uuid.UUID(case_id)) != case_id or str(uuid.UUID(artifact_id)) != artifact_id:
        raise ArtifactStoreError("identifier_invalid")
    resolved_root = data_root.resolve()
    current = resolved_root
    for component in ("cases", case_id, "artifacts"):
        current = current / component
        current.mkdir(mode=0o700, exist_ok=True)
        os.chmod(current, 0o700)
    artifact_dir = _within(current / artifact_id, resolved_root)
    artifact_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(artifact_dir, 0o700)
    temporary = artifact_dir / f".{uuid.uuid4()}.tmp"
    target = artifact_dir / "content.json"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
            temporary.unlink()
            os.chmod(target, 0o400)
            directory_descriptor = os.open(artifact_dir, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            if temporary.exists():
                temporary.unlink()
            raise
    except BaseException:
        shutil.rmtree(artifact_dir, ignore_errors=True)
        raise
    return target.relative_to(resolved_root).as_posix()


def remove_new_artifact(data_root: Path, relative_path: str) -> None:
    target = _within(data_root / relative_path, data_root)
    shutil.rmtree(target.parent, ignore_errors=True)


def read_verified(data_root: Path, relative_path: str, expected_digest: str) -> bytes:
    current = data_root.resolve()
    for part in Path(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise ArtifactStoreError("symlink_rejected")
    target = _within(data_root / relative_path, data_root)
    if not target.is_file() or target.is_symlink():
        raise ArtifactStoreError("artifact_file_invalid")
    raw = target.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ArtifactStoreError("digest_mismatch")
    return raw


def install_fixed_blob(
    data_root: Path,
    case_id: str,
    collection: str,
    record_id: str,
    filename: str,
    raw: bytes,
) -> str:
    """Install one immutable blob below a controlled case collection."""
    try:
        if str(uuid.UUID(case_id)) != case_id or str(uuid.UUID(record_id)) != record_id:
            raise ValueError
    except ValueError as error:
        raise ArtifactStoreError("identifier_invalid") from error
    if (collection, filename) not in {("exports", "evidence.zip"), ("deliveries", "delivery.json")} or not raw:
        raise ArtifactStoreError("fixed_blob_contract_invalid")
    root = data_root.resolve()
    case_root = _within(root / "cases" / case_id, root)
    collection_root = _within(case_root / collection, root)
    staging_root = _within(case_root / "staging", root)
    quarantine_root = _within(case_root / "quarantine", root)
    for directory in (root, root / "cases", case_root, collection_root, staging_root, quarantine_root):
        _ensure_private_directory(directory)
    target_dir = _within(collection_root / record_id, root)
    stage = _within(staging_root / record_id, root)
    if target_dir.exists() or stage.exists():
        raise ArtifactStoreError("fixed_blob_exists")
    stage.mkdir(mode=0o700)
    target = target_dir / filename
    staged_file = stage / filename
    published = False
    try:
        descriptor = os.open(staged_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged_file, 0o400)
        _fsync_directory(stage)
        _rename_directory_no_replace(stage, target_dir)
        published = True
        _fsync_directory(staging_root)
        _fsync_directory(collection_root)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
            _fsync_directory(staging_root)
        if published and target_dir.exists():
            quarantine = quarantine_root / f"{collection}-{record_id}-{uuid.uuid4()}"
            _rename_directory_no_replace(target_dir, quarantine)
            _fsync_directory(collection_root)
            _fsync_directory(quarantine_root)
        _fsync_directory(collection_root)
        raise
    return target.relative_to(root).as_posix()


def quarantine_fixed_collection_directory(
    data_root: Path,
    case_id: str,
    collection: str,
    record_id: str,
) -> str:
    if collection not in {"deliveries", "exports"}:
        raise ArtifactStoreError("fixed_blob_contract_invalid")
    try:
        if str(uuid.UUID(case_id)) != case_id or str(uuid.UUID(record_id)) != record_id:
            raise ValueError
    except ValueError as error:
        raise ArtifactStoreError("identifier_invalid") from error
    root = data_root.resolve()
    source = _within(root / "cases" / case_id / collection / record_id, root)
    if not source.is_dir() or source.is_symlink():
        raise ArtifactStoreError("orphan_directory_invalid")
    quarantine_root = _within(root / "cases" / case_id / "quarantine", root)
    _ensure_private_directory(quarantine_root)
    target = _within(quarantine_root / f"{collection}-{record_id}-{uuid.uuid4()}", root)
    _rename_directory_no_replace(source, target)
    _fsync_directory(source.parent)
    _fsync_directory(quarantine_root)
    return target.relative_to(root).as_posix()


def _ensure_private_directory(path: Path) -> bool:
    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    if path.is_symlink() or not path.is_dir():
        raise ArtifactStoreError("directory_symlink_rejected")
    os.chmod(path, 0o700)
    _fsync_directory(path)
    _fsync_directory(path.parent)
    return created


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise ArtifactStoreError("directory_fsync_failed") from error
    finally:
        os.close(descriptor)


def _rename_directory_no_replace(source: Path, target: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    source_raw = os.fsencode(source)
    target_raw = os.fsencode(target)
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        operation = library.renamex_np
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(source_raw, target_raw, 0x00000004)
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        operation = library.renameat2
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(-100, source_raw, -100, target_raw, 1)
    else:
        raise ArtifactStoreError("atomic_no_replace_unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise ArtifactStoreError("candidate_exists")
        raise ArtifactStoreError("candidate_publish_failed") from OSError(error_number, os.strerror(error_number))


def install_candidate_bundle(
    data_root: Path,
    case_id: str,
    candidate_id: str,
    files: Mapping[str, bytes],
    *,
    operation_id: str | None = None,
) -> str:
    """Publish an exact three-file candidate with one no-overwrite directory rename."""
    published = False
    try:
        if str(uuid.UUID(case_id)) != case_id or str(uuid.UUID(candidate_id)) != candidate_id:
            raise ValueError
        operation = operation_id or str(uuid.uuid4())
        if str(uuid.UUID(operation)) != operation:
            raise ValueError
    except ValueError as error:
        raise ArtifactStoreError("identifier_invalid") from error
    if set(files) != set(CANDIDATE_FILENAMES):
        raise ArtifactStoreError("candidate_file_set_invalid")
    if any(not isinstance(files[name], bytes) or not files[name] for name in CANDIDATE_FILENAMES):
        raise ArtifactStoreError("candidate_file_invalid")

    root = data_root.resolve()
    case_root = _within(root / "cases" / case_id, root)
    staging_root = case_root / "staging"
    candidates_root = case_root / "candidates"
    quarantine_root = case_root / "quarantine"
    for directory in (root, root / "cases", case_root, staging_root, candidates_root, quarantine_root):
        _ensure_private_directory(directory)
    stage = _within(staging_root / operation, root)
    target = _within(candidates_root / candidate_id, root)
    if stage.exists() or target.exists():
        raise ArtifactStoreError("candidate_exists")
    stage.mkdir(mode=0o700)
    try:
        for name in CANDIDATE_FILENAMES:
            path = stage / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(files[name])
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(path, 0o400)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        _fsync_directory(stage)
        _rename_directory_no_replace(stage, target)
        published = True
        _fsync_directory(staging_root)
        _fsync_directory(candidates_root)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
            _fsync_directory(staging_root)
        if published and target.exists():
            quarantine = quarantine_root / f"{candidate_id}-{uuid.uuid4()}"
            _rename_directory_no_replace(target, quarantine)
            _fsync_directory(candidates_root)
            _fsync_directory(quarantine_root)
        raise
    return target.relative_to(root).as_posix()


def verify_candidate_bundle(
    data_root: Path,
    relative_directory: str,
    expected: Mapping[str, tuple[str, int]],
) -> dict[str, bytes]:
    root = data_root.resolve()
    directory = _within(root / relative_directory, root)
    if directory.is_symlink() or not directory.is_dir():
        raise ArtifactStoreError("candidate_directory_invalid")
    entries = list(directory.iterdir())
    if {entry.name for entry in entries} != set(CANDIDATE_FILENAMES):
        raise ArtifactStoreError("candidate_file_set_invalid")
    result: dict[str, bytes] = {}
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise ArtifactStoreError("candidate_file_invalid")
        record = expected.get(entry.name)
        if record is None:
            raise ArtifactStoreError("candidate_file_set_invalid")
        raw = entry.read_bytes()
        digest, length = record
        if len(raw) != length or hashlib.sha256(raw).hexdigest() != digest:
            raise ArtifactStoreError("candidate_integrity_invalid")
        result[entry.name] = raw
    return result


def quarantine_candidate_bundle(
    data_root: Path,
    case_id: str,
    candidate_id: str,
    *,
    event_id: str | None = None,
) -> str | None:
    root = data_root.resolve()
    source = _within(root / "cases" / case_id / "candidates" / candidate_id, root)
    if not source.exists():
        return None
    event = event_id or str(uuid.uuid4())
    try:
        if str(uuid.UUID(event)) != event:
            raise ValueError
    except ValueError as error:
        raise ArtifactStoreError("identifier_invalid") from error
    quarantine_root = _within(root / "cases" / case_id / "quarantine", root)
    _ensure_private_directory(quarantine_root)
    target = _within(quarantine_root / f"{candidate_id}-{event}", root)
    if target.exists():
        raise ArtifactStoreError("quarantine_collision")
    _rename_directory_no_replace(source, target)
    _fsync_directory(source.parent)
    _fsync_directory(quarantine_root)
    return target.relative_to(root).as_posix()
