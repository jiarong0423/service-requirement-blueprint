from __future__ import annotations

import hashlib
import hmac
import ctypes
import errno
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path


class ArtifactStoreError(RuntimeError):
    pass


CANDIDATE_FILENAMES = ("manifest.json", "document.pdf", "diagram.svg")
MAX_JSON_ARTIFACT_BYTES = 1_048_576
MAX_CANDIDATE_MANIFEST_BYTES = 1_048_576
MAX_CANDIDATE_BINARY_BYTES = 67_108_864
MAX_DELIVERY_ARTIFACT_BYTES = 1_048_576
MAX_EVIDENCE_EXPORT_BYTES = 67_108_864
_READ_CHUNK_BYTES = 65_536
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_LISTDIR_SUPPORTS_FD = os.listdir in os.supports_fd


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


def quarantine_json_artifact(
    data_root: Path,
    case_id: str,
    artifact_id: str,
) -> str:
    try:
        if str(uuid.UUID(case_id)) != case_id or str(uuid.UUID(artifact_id)) != artifact_id:
            raise ValueError
    except ValueError as error:
        raise ArtifactStoreError("identifier_invalid") from error
    root = data_root.resolve()
    source = _within(root / "cases" / case_id / "artifacts" / artifact_id, root)
    if not source.is_dir() or source.is_symlink():
        raise ArtifactStoreError("orphan_directory_invalid")
    quarantine_root = _within(root / "cases" / case_id / "quarantine", root)
    _ensure_private_directory(quarantine_root)
    target = _within(quarantine_root / f"artifacts-{artifact_id}-{uuid.uuid4()}", root)
    _rename_directory_no_replace(source, target)
    _fsync_directory(source.parent)
    _fsync_directory(quarantine_root)
    return target.relative_to(root).as_posix()


def _descriptor_read_flags(*, directory: bool) -> int:
    required = ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK")
    if directory:
        required += ("O_DIRECTORY",)
    if any(not hasattr(os, name) for name in required) or not _OPEN_SUPPORTS_DIR_FD:
        raise ArtifactStoreError("descriptor_safe_read_unavailable")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    else:
        flags |= os.O_NONBLOCK
    return flags


def _relative_parts(relative_path: str, *, allow_directory: bool = False) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or not relative_path:
        raise ArtifactStoreError("artifact_path_invalid")
    path = Path(relative_path)
    parts = path.parts
    if (
        path.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
        or (not allow_directory and len(parts) < 2)
    ):
        raise ArtifactStoreError("artifact_path_invalid")
    return parts


def _open_directory_descriptor(data_root: Path, relative_directory: str = "") -> int:
    flags = _descriptor_read_flags(directory=True)
    try:
        current = os.open(data_root, flags)
    except OSError as error:
        raise ArtifactStoreError("artifact_root_invalid") from error
    try:
        root_stat = os.fstat(current)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ArtifactStoreError("artifact_root_invalid")
        if not relative_directory:
            return current
        for part in _relative_parts(relative_directory, allow_directory=True):
            try:
                child = os.open(part, flags, dir_fd=current)
            except OSError as error:
                raise ArtifactStoreError("artifact_directory_invalid") from error
            child_stat = os.fstat(child)
            if not stat.S_ISDIR(child_stat.st_mode):
                os.close(child)
                raise ArtifactStoreError("artifact_directory_invalid")
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _read_from_directory_descriptor(
    directory_descriptor: int,
    filename: str,
    expected_digest: str,
    expected_length: int,
    max_bytes: int,
) -> bytes:
    if (
        filename in {"", ".", ".."}
        or "/" in filename
        or not isinstance(expected_digest, str)
        or len(expected_digest) != 64
        or any(character not in "0123456789abcdef" for character in expected_digest)
        or isinstance(expected_length, bool)
        or not isinstance(expected_length, int)
        or expected_length < 1
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 1
        or expected_length > max_bytes
    ):
        raise ArtifactStoreError("artifact_read_contract_invalid")
    flags = _descriptor_read_flags(directory=False)
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
    except OSError as error:
        raise ArtifactStoreError("artifact_file_invalid") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactStoreError("artifact_file_invalid")
        if before.st_size != expected_length or before.st_size > max_bytes:
            raise ArtifactStoreError("length_mismatch")
        remaining = expected_length
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                raise ArtifactStoreError("short_read")
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ArtifactStoreError("length_mismatch")
        after = os.fstat(descriptor)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != expected_length
            or not stat.S_ISREG(after.st_mode)
        ):
            raise ArtifactStoreError("artifact_changed_during_read")
        if not hmac.compare_digest(digest.hexdigest(), expected_digest):
            raise ArtifactStoreError("digest_mismatch")
        return b"".join(chunks)
    except OSError as error:
        raise ArtifactStoreError("artifact_read_failed") from error
    finally:
        os.close(descriptor)


def read_verified(
    data_root: Path,
    relative_path: str,
    expected_digest: str,
    expected_length: int,
    *,
    max_bytes: int,
) -> bytes:
    parts = _relative_parts(relative_path)
    directory = _open_directory_descriptor(data_root, Path(*parts[:-1]).as_posix())
    try:
        return _read_from_directory_descriptor(
            directory, parts[-1], expected_digest, expected_length, max_bytes
        )
    finally:
        os.close(directory)


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
    expected: Mapping[str, tuple[str, int, int]],
) -> dict[str, bytes]:
    if set(expected) != set(CANDIDATE_FILENAMES) or not _LISTDIR_SUPPORTS_FD:
        raise ArtifactStoreError("candidate_file_set_invalid")
    directory = _open_directory_descriptor(data_root, relative_directory)
    try:
        try:
            names = os.listdir(directory)
        except OSError as error:
            raise ArtifactStoreError("candidate_directory_invalid") from error
        if set(names) != set(CANDIDATE_FILENAMES) or len(names) != len(CANDIDATE_FILENAMES):
            raise ArtifactStoreError("candidate_file_set_invalid")
        result: dict[str, bytes] = {}
        for name in CANDIDATE_FILENAMES:
            record = expected[name]
            if not isinstance(record, tuple) or len(record) != 3:
                raise ArtifactStoreError("candidate_read_contract_invalid")
            digest, length, max_bytes = record
            try:
                result[name] = _read_from_directory_descriptor(
                    directory, name, digest, length, max_bytes
                )
            except ArtifactStoreError as error:
                raise ArtifactStoreError("candidate_integrity_invalid") from error
        try:
            final_names = os.listdir(directory)
        except OSError as error:
            raise ArtifactStoreError("candidate_directory_invalid") from error
        if set(final_names) != set(CANDIDATE_FILENAMES) or len(final_names) != len(CANDIDATE_FILENAMES):
            raise ArtifactStoreError("candidate_file_set_invalid")
        return result
    finally:
        os.close(directory)


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
