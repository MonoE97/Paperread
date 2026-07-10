from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True, slots=True)
class ResolvedSourceFingerprint:
    resolved_path: str
    sha256: str
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


class PublishConflictError(FileExistsError):
    code = "publish_conflict"

    def __init__(self, destination: Path) -> None:
        super().__init__(errno.EEXIST, "atomic publication destination already exists", destination)


class AtomicNoReplaceUnsupportedError(NotImplementedError):
    def __init__(self, platform: str) -> None:
        super().__init__(f"atomic no-replace tree rename is unavailable on platform: {platform}")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path | str, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rfc3339_utc(value: datetime | None = None) -> str:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("RFC3339 UTC timestamps require a timezone-aware datetime")
    text = instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if "." in text:
        prefix, fractional = text[:-1].split(".", 1)
        fractional = fractional.rstrip("0")
        text = f"{prefix}.{fractional}Z" if fractional else f"{prefix}Z"
    return text


def new_uuid() -> str:
    return str(uuid.uuid4())


def new_random_id(prefix: str) -> str:
    if not prefix or not prefix.replace("-", "_").replace("_", "").isalnum():
        raise ValueError("identifier prefix must contain only letters, digits, hyphens, or underscores")
    return f"{prefix}_{new_uuid()}"


def random_token(nbytes: int = 32) -> str:
    if nbytes < 16:
        raise ValueError("random tokens must contain at least 16 bytes of entropy")
    return secrets.token_urlsafe(nbytes)


def safe_relative_artifact_path(value: str | PurePosixPath) -> str:
    raw = str(value)
    if not raw or raw == "." or "\\" in raw or "\x00" in raw:
        raise ValueError("artifact path must be a non-empty POSIX relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("artifact path must not be absolute or contain dot segments")
    if str(path) != raw:
        raise ValueError("artifact path must be normalized")
    return raw


def resolve_artifact_path(root: Path | str, relative_path: str | PurePosixPath) -> Path:
    relative = safe_relative_artifact_path(relative_path)
    resolved_root = Path(root).expanduser().resolve(strict=True)
    candidate = resolved_root.joinpath(*PurePosixPath(relative).parts)
    resolved_candidate = candidate.resolve(strict=False)
    if not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError(f"artifact path escapes its run root: {relative}")
    return resolved_candidate


def fingerprint_resolved_source(path: Path | str) -> ResolvedSourceFingerprint:
    resolved = Path(path)
    if not resolved.is_absolute():
        raise ValueError(f"resolved source path must be absolute: {resolved}")
    with resolved.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"source must be a regular file: {resolved}")
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise RuntimeError(f"source changed while it was fingerprinted: {resolved}")
    return ResolvedSourceFingerprint(
        resolved_path=str(resolved),
        sha256=digest.hexdigest(),
        size_bytes=after.st_size,
        device=after.st_dev,
        inode=after.st_ino,
        mtime_ns=after.st_mtime_ns,
    )


def fingerprint_source(path: Path | str) -> ResolvedSourceFingerprint:
    requested = Path(path).expanduser()
    resolved = requested.resolve(strict=True)
    return fingerprint_resolved_source(resolved)


source_fingerprint = fingerprint_source


def source_matches_fingerprint(
    path: Path | str,
    expected: ResolvedSourceFingerprint,
) -> bool:
    try:
        actual = fingerprint_source(path)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return False
    return actual == expected


def paths_alias(first: Path | str, second: Path | str) -> bool:
    first_path = Path(first).expanduser()
    second_path = Path(second).expanduser()
    try:
        if first_path.resolve(strict=False) == second_path.resolve(strict=False):
            return True
    except OSError:
        pass
    try:
        return os.path.samefile(first_path, second_path)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False


def assert_no_source_output_alias(source: Path | str, output: Path | str) -> None:
    if paths_alias(source, output):
        raise ValueError(f"output aliases source: {output}")


def fsync_directory(path: Path | str) -> None:
    directory = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for current_root, directories, filenames in os.walk(root, topdown=False):
        current = Path(current_root)
        for filename in filenames:
            path = current / filename
            if path.is_symlink():
                continue
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        for directory in directories:
            path = current / directory
            if not path.is_symlink():
                fsync_directory(path)
        fsync_directory(current)


def _raise_native_rename_error(error_number: int, source: Path, destination: Path) -> None:
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise PublishConflictError(destination)
    unsupported_errors = {errno.ENOSYS, errno.ENOTSUP}
    if hasattr(errno, "EOPNOTSUPP"):
        unsupported_errors.add(errno.EOPNOTSUPP)
    if error_number in unsupported_errors:
        raise AtomicNoReplaceUnsupportedError(sys.platform)
    raise OSError(error_number, os.strerror(error_number), source, destination)


def _native_rename_tree_no_replace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)

    if sys.platform == "darwin":
        try:
            renamex_np = libc.renamex_np
        except AttributeError as exc:
            raise AtomicNoReplaceUnsupportedError(sys.platform) from exc
        # Verified from the installed macOS SDK sys/stdio.h.
        rename_excl = 0x00000004
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = renamex_np(source_bytes, destination_bytes, rename_excl)
    elif sys.platform.startswith("linux"):
        try:
            renameat2 = libc.renameat2
        except AttributeError as exc:
            raise AtomicNoReplaceUnsupportedError(sys.platform) from exc
        at_fdcwd = -100
        rename_noreplace = 1
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = renameat2(
            at_fdcwd,
            source_bytes,
            at_fdcwd,
            destination_bytes,
            rename_noreplace,
        )
    else:
        raise AtomicNoReplaceUnsupportedError(sys.platform)

    if result != 0:
        _raise_native_rename_error(ctypes.get_errno(), source, destination)


def atomic_write_bytes(path: Path | str, content: bytes, *, mode: int = 0o644) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{new_uuid()}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def atomic_write_json(path: Path | str, value: Any) -> Path:
    return atomic_write_bytes(path, canonical_json_bytes(value))


def atomic_publish_tree(staging: Path | str, destination: Path | str) -> Path:
    staging_path = Path(staging)
    destination_path = Path(destination)
    if not staging_path.is_dir() or staging_path.is_symlink():
        raise ValueError(f"staging tree must be a real directory: {staging_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if staging_path.stat().st_dev != destination_path.parent.stat().st_dev:
        raise OSError(errno.EXDEV, "tree publication requires the same filesystem")
    _fsync_tree(staging_path)
    _native_rename_tree_no_replace(staging_path, destination_path)
    fsync_directory(destination_path.parent)
    return destination_path


def publish_file_no_replace(source: Path | str, destination: Path | str) -> Path:
    source_path = Path(source).expanduser().resolve(strict=True)
    if not source_path.is_file():
        raise ValueError(f"publication source must be a regular file: {source_path}")
    destination_path = Path(destination).expanduser()
    assert_no_source_output_alias(source_path, destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path.stat().st_dev != destination_path.parent.stat().st_dev:
        raise OSError(errno.EXDEV, "file publication requires the same filesystem")
    temporary = destination_path.parent / f".{destination_path.name}.{new_uuid()}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with source_path.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
            descriptor = None
            shutil.copyfileobj(source_handle, target_handle)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.link(temporary, destination_path)
        fsync_directory(destination_path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
            fsync_directory(destination_path.parent)
        except FileNotFoundError:
            pass
    return destination_path


__all__ = [
    "AtomicNoReplaceUnsupportedError",
    "PublishConflictError",
    "ResolvedSourceFingerprint",
    "assert_no_source_output_alias",
    "atomic_publish_tree",
    "atomic_write_bytes",
    "atomic_write_json",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "fingerprint_source",
    "fingerprint_resolved_source",
    "fsync_directory",
    "new_random_id",
    "new_uuid",
    "paths_alias",
    "publish_file_no_replace",
    "random_token",
    "resolve_artifact_path",
    "rfc3339_utc",
    "safe_relative_artifact_path",
    "sha256_file",
    "source_fingerprint",
    "source_matches_fingerprint",
]
