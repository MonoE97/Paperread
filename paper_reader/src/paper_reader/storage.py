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
from typing import Any, Protocol

from paper_reader.resource_policy import V2_RESOURCE_POLICY


@dataclass(frozen=True, slots=True)
class ResolvedSourceFingerprint:
    resolved_path: str
    sha256: str
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


class PublishConflictError(FileExistsError):
    code = "publish_conflict"

    def __init__(self, destination: Path) -> None:
        super().__init__(errno.EEXIST, "atomic publication destination already exists", destination)


class AtomicNoReplaceUnsupportedError(NotImplementedError):
    def __init__(self, platform: str) -> None:
        super().__init__(f"atomic no-replace tree rename is unavailable on platform: {platform}")


class UnsafeStoragePathError(ValueError):
    code = "run_directory_changed"


class UnexpectedStorageSizeError(UnsafeStoragePathError):
    pass


class TreeSnapshotLimitError(UnsafeStoragePathError):
    def __init__(self, limit_name: str, maximum: int, message: str) -> None:
        super().__init__(message)
        self.limit_name = limit_name
        self.maximum = maximum


class DirectoryAnchorLike(Protocol):
    path: Path
    descriptor: int
    device: int
    inode: int


class ExactFinalizationGuard(Protocol):
    def verify(self) -> None: ...


class LoadedRunUpdateLike(Protocol):
    manifest_path: Path
    manifest_bytes: bytes
    run_directory_anchor: DirectoryAnchorLike | None


@dataclass(slots=True)
class OwnedDirectoryAnchor:
    path: Path
    descriptor: int
    device: int
    inode: int

    def close(self) -> None:
        descriptor = self.descriptor
        if descriptor >= 0:
            self.descriptor = -1
            os.close(descriptor)

    def __enter__(self) -> OwnedDirectoryAnchor:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


@dataclass(slots=True)
class OwnedPublishedFile:
    path: Path
    descriptor: int
    identity: tuple[int, int, int, int, int, int]
    content_sha256: str

    def detach_descriptor(self) -> int:
        if self.descriptor < 0:
            raise ValueError(f"published file descriptor is already detached: {self.path}")
        descriptor = self.descriptor
        self.descriptor = -1
        return descriptor

    def close(self) -> None:
        descriptor = self.descriptor
        if descriptor >= 0:
            self.descriptor = -1
            os.close(descriptor)

    def __enter__(self) -> OwnedPublishedFile:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


@dataclass(slots=True)
class OwnedPublishedTree:
    path: Path
    directory: OwnedDirectoryAnchor
    held_file: OwnedPublishedFile
    held_relative_path: str

    def close(self) -> None:
        try:
            self.held_file.close()
        finally:
            self.directory.close()

    def __enter__(self) -> OwnedPublishedTree:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


@dataclass(slots=True)
class HeldExactFileGuard:
    anchor: DirectoryAnchorLike
    owned_file: OwnedPublishedFile
    expected_bytes: bytes
    label: str = "published file"

    def close(self) -> None:
        self.owned_file.close()

    def __enter__(self) -> HeldExactFileGuard:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def verify(self) -> None:
        try:
            validate_directory_anchor(self.anchor)
            opened_before = os.fstat(self.owned_file.descriptor)
            named_before = stat_anchored_entry(
                self.anchor,
                self.owned_file.path,
            )
            chunks: list[bytes] = []
            offset = 0
            limit = len(self.expected_bytes)
            while offset <= limit:
                chunk = os.pread(
                    self.owned_file.descriptor,
                    min(1024 * 1024, limit - offset + 1),
                    offset,
                )
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
                if offset > limit:
                    break
            opened_after = os.fstat(self.owned_file.descriptor)
            named_after = stat_anchored_entry(
                self.anchor,
                self.owned_file.path,
            )
            validate_directory_anchor(self.anchor)
        except (OSError, UnsafeStoragePathError) as exc:
            raise UnsafeStoragePathError(
                f"held {self.label} identity became uncertain: "
                f"{self.owned_file.path}: {exc}"
            ) from exc

        identities = {
            (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
                item.st_nlink,
            )
            for item in (opened_before, named_before, opened_after, named_after)
        }
        raw = b"".join(chunks)
        if (
            identities != {self.owned_file.identity}
            or not all(
                stat.S_ISREG(item.st_mode)
                for item in (opened_before, named_before, opened_after, named_after)
            )
            or self.owned_file.identity[5] != 1
            or raw != self.expected_bytes
            or hashlib.sha256(raw).digest()
            != hashlib.sha256(self.expected_bytes).digest()
        ):
            raise UnsafeStoragePathError(
                f"held {self.label} changed before finalization: "
                f"{self.owned_file.path}"
            )


@dataclass(slots=True)
class HeldExactTreeGuard:
    published_tree: OwnedPublishedTree
    expected_tree: ImmutableTreeSnapshot
    expected_held_bytes: bytes
    label: str = "published tree"

    def close(self) -> None:
        self.published_tree.close()

    def __enter__(self) -> HeldExactTreeGuard:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def verify(self) -> None:
        held_guard = HeldExactFileGuard(
            anchor=self.published_tree.directory,
            owned_file=self.published_tree.held_file,
            expected_bytes=self.expected_held_bytes,
            label=f"{self.label} held manifest",
        )
        held_guard.verify()
        validate_directory_anchor(self.published_tree.directory)
        observed = snapshot_directory_fd(
            self.published_tree.directory.descriptor,
            max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
            max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
            max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
        )
        validate_directory_anchor(self.published_tree.directory)
        held_guard.verify()
        if observed != self.expected_tree:
            raise UnsafeStoragePathError(
                f"held {self.label} closed set changed before finalization: "
                f"{self.published_tree.path}"
            )


@dataclass(slots=True)
class HeldResolvedSourceGuard:
    path: Path
    parent: OwnedDirectoryAnchor
    descriptor: int
    identity: tuple[int, int, int, int, int, int]
    fingerprint: ResolvedSourceFingerprint
    max_bytes: int

    def close(self) -> None:
        descriptor = self.descriptor
        self.descriptor = -1
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            self.parent.close()

    def __enter__(self) -> HeldResolvedSourceGuard:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def verify(self) -> None:
        try:
            validate_directory_anchor(self.parent)
            opened_before = os.fstat(self.descriptor)
            named_before = os.stat(
                self.path.name,
                dir_fd=self.parent.descriptor,
                follow_symlinks=False,
            )
            if opened_before.st_size > self.max_bytes:
                raise UnsafeStoragePathError(
                    f"source exceeds its read limit of {self.max_bytes} bytes: {self.path}"
                )
            digest = _sha256_descriptor(
                self.descriptor,
                expected_size=opened_before.st_size,
            )
            opened_after = os.fstat(self.descriptor)
            named_after = os.stat(
                self.path.name,
                dir_fd=self.parent.descriptor,
                follow_symlinks=False,
            )
            validate_directory_anchor(self.parent)
        except (OSError, UnsafeStoragePathError) as exc:
            raise UnsafeStoragePathError(
                f"source pathname identity became uncertain: {self.path}: {exc}"
            ) from exc
        metadata = (opened_before, named_before, opened_after, named_after)
        identities = {
            (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
                item.st_nlink,
            )
            for item in metadata
        }
        if (
            identities != {self.identity}
            or not all(stat.S_ISREG(item.st_mode) for item in metadata)
            or not digest
            or digest != self.fingerprint.sha256
        ):
            raise UnsafeStoragePathError(
                f"source pathname or complete fingerprint changed: {self.path}"
            )


@dataclass(slots=True)
class HeldTerminalArtifactGuard:
    main: HeldExactFileGuard
    sidecar: OwnedDirectoryAnchor
    expected_sidecar: ImmutableTreeSnapshot
    label: str

    def close(self) -> None:
        try:
            self.main.close()
        finally:
            self.sidecar.close()

    def __enter__(self) -> HeldTerminalArtifactGuard:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def verify(self) -> None:
        self.main.verify()
        validate_directory_anchor(self.sidecar)
        observed = snapshot_directory_fd(
            self.sidecar.descriptor,
            max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
            max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
            max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
        )
        validate_directory_anchor(self.sidecar)
        if observed != self.expected_sidecar:
            raise UnsafeStoragePathError(
                f"held {self.label} sidecar changed before finalization: "
                f"{self.sidecar.path}"
            )


@dataclass(frozen=True, slots=True)
class ImmutableTreeEntry:
    path: str
    kind: str
    size_bytes: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class ImmutableTreeSnapshot:
    entries: tuple[ImmutableTreeEntry, ...]


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


def _sha256_descriptor(
    descriptor: int,
    *,
    expected_size: int,
    chunk_size: int = 1024 * 1024,
) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < expected_size:
        chunk = os.pread(
            descriptor,
            min(chunk_size, expected_size - offset),
            offset,
        )
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    if offset != expected_size or os.pread(descriptor, 1, offset):
        return ""
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
    with open_resolved_source_guard(
        path,
        max_bytes=V2_RESOURCE_POLICY.local_pdf_max_bytes,
    ) as guard:
        guard.verify()
        return guard.fingerprint


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


_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_REGULAR_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _same_entry(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _open_directory_path_nofollow(path: Path) -> int:
    lexical = Path(os.path.abspath(path))
    descriptor = os.open(lexical.anchor or os.sep, _DIRECTORY_OPEN_FLAGS)
    try:
        for component in lexical.parts[1:]:
            child = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(named.st_mode)
                    or not _same_entry(opened, named)
                ):
                    raise UnsafeStoragePathError(
                        f"unsafe directory component while opening {lexical}"
                    )
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def validate_directory_anchor(anchor: DirectoryAnchorLike) -> None:
    try:
        opened = os.fstat(anchor.descriptor)
        current_fd = _open_directory_path_nofollow(anchor.path)
        try:
            current = os.fstat(current_fd)
        finally:
            os.close(current_fd)
    except OSError as exc:
        raise UnsafeStoragePathError(
            f"anchored directory changed or became unsafe: {anchor.path}: {exc}"
        ) from exc
    expected = (anchor.device, anchor.inode)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != expected
        or (current.st_dev, current.st_ino) != expected
    ):
        raise UnsafeStoragePathError(f"anchored directory identity changed: {anchor.path}")


def open_resolved_source_guard(
    path: Path | str,
    *,
    max_bytes: int,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    expected_device: int | None = None,
    expected_inode: int | None = None,
) -> HeldResolvedSourceGuard:
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    resolved = Path(path)
    if not resolved.is_absolute():
        raise ValueError(f"resolved source path must be absolute: {resolved}")
    lexical = Path(os.path.abspath(resolved))
    try:
        if resolved.resolve(strict=True) != lexical:
            raise ValueError(f"resolved source path must be canonical: {resolved}")
    except OSError as exc:
        raise ValueError(f"resolved source path is unavailable: {resolved}: {exc}") from exc
    parent_descriptor = _open_directory_path_nofollow(lexical.parent)
    parent_metadata = os.fstat(parent_descriptor)
    parent = OwnedDirectoryAnchor(
        path=lexical.parent,
        descriptor=parent_descriptor,
        device=parent_metadata.st_dev,
        inode=parent_metadata.st_ino,
    )
    descriptor: int | None = None
    try:
        validate_directory_anchor(parent)
        descriptor = os.open(
            lexical.name,
            _REGULAR_READ_FLAGS,
            dir_fd=parent.descriptor,
        )
        opened_before = os.fstat(descriptor)
        named_before = os.stat(
            lexical.name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or not stat.S_ISREG(named_before.st_mode)
            or not _same_entry(opened_before, named_before)
        ):
            raise UnsafeStoragePathError(
                f"source must be one stable regular file: {lexical}"
            )
        if opened_before.st_size > max_bytes:
            raise ValueError(
                f"source exceeds its read limit of {max_bytes} bytes: {lexical}"
            )
        digest = _sha256_descriptor(
            descriptor,
            expected_size=opened_before.st_size,
        )
        opened_after = os.fstat(descriptor)
        named_after = os.stat(
            lexical.name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        validate_directory_anchor(parent)
        metadata = (opened_before, named_before, opened_after, named_after)
        identities = {
            (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
                item.st_nlink,
            )
            for item in metadata
        }
        if (
            len(identities) != 1
            or not all(stat.S_ISREG(item.st_mode) for item in metadata)
            or not digest
        ):
            raise UnsafeStoragePathError(
                f"source changed while its complete fingerprint was captured: {lexical}"
            )
        identity = next(iter(identities))
        fingerprint = ResolvedSourceFingerprint(
            resolved_path=str(lexical),
            sha256=digest,
            size_bytes=opened_after.st_size,
            device=opened_after.st_dev,
            inode=opened_after.st_ino,
            mtime_ns=opened_after.st_mtime_ns,
            ctime_ns=opened_after.st_ctime_ns,
        )
        expected = (
            expected_sha256,
            expected_size,
            expected_device,
            expected_inode,
        )
        observed = (
            fingerprint.sha256,
            fingerprint.size_bytes,
            fingerprint.device,
            fingerprint.inode,
        )
        for expected_value, observed_value in zip(expected, observed, strict=True):
            if expected_value is not None and expected_value != observed_value:
                raise UnsafeStoragePathError(
                    f"source no longer matches its expected fingerprint: {lexical}"
                )
        guard = HeldResolvedSourceGuard(
            path=lexical,
            parent=parent,
            descriptor=descriptor,
            identity=identity,
            fingerprint=fingerprint,
            max_bytes=max_bytes,
        )
        descriptor = None
        return guard
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        parent.close()
        raise


def _anchor_relative_parts(anchor: DirectoryAnchorLike, path: Path | str) -> tuple[str, ...]:
    lexical = Path(os.path.abspath(Path(path).expanduser()))
    anchor_path = Path(os.path.abspath(anchor.path))
    try:
        relative = lexical.relative_to(anchor_path)
    except ValueError as exc:
        raise UnsafeStoragePathError(
            f"storage path escapes its anchored directory: {lexical}"
        ) from exc
    if relative == Path(".") or any(part in {"", ".", ".."} for part in relative.parts):
        raise UnsafeStoragePathError(f"storage path must name an anchored entry: {lexical}")
    return relative.parts


def _open_relative_directory(
    anchor: DirectoryAnchorLike,
    parts: tuple[str, ...],
    *,
    create: bool,
) -> int:
    descriptor = os.dup(anchor.descriptor)
    try:
        for component in parts:
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(named.st_mode)
                    or not _same_entry(opened, named)
                ):
                    raise UnsafeStoragePathError(
                        f"anchored directory component changed: {component}"
                    )
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_anchored_parent(
    anchor: DirectoryAnchorLike,
    path: Path | str,
    *,
    create: bool,
) -> tuple[int, str]:
    parts = _anchor_relative_parts(anchor, path)
    parent_fd = _open_relative_directory(anchor, parts[:-1], create=create)
    return parent_fd, parts[-1]


def _validate_anchored_parent(
    anchor: DirectoryAnchorLike,
    path: Path | str,
    parent_fd: int,
) -> None:
    parts = _anchor_relative_parts(anchor, path)
    current_fd = _open_relative_directory(anchor, parts[:-1], create=False)
    try:
        opened = os.fstat(parent_fd)
        current = os.fstat(current_fd)
        if not _same_entry(opened, current):
            raise UnsafeStoragePathError(
                f"anchored destination parent identity changed: {Path(path).parent}"
            )
    finally:
        os.close(current_fd)


def _read_anchored_regular_file(
    anchor: DirectoryAnchorLike,
    path: Path | str,
    *,
    expected_size: int | None = None,
    max_bytes: int | None = None,
) -> bytes:
    parent_fd, name = _open_anchored_parent(anchor, path, create=False)
    descriptor: int | None = None
    try:
        _validate_anchored_parent(anchor, path, parent_fd)
        descriptor = os.open(name, _REGULAR_READ_FLAGS, dir_fd=parent_fd)
        before = os.fstat(descriptor)
        named_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(named_before.st_mode)
            or before.st_nlink != 1
            or named_before.st_nlink != 1
            or not _same_entry(before, named_before)
        ):
            raise UnsafeStoragePathError(
                f"anchored file must be one non-symlink, non-hardlinked regular file: {path}"
            )
        if expected_size is not None and before.st_size != expected_size:
            raise UnexpectedStorageSizeError(
                f"anchored file size differs from its expected size: {path}"
            )
        if max_bytes is not None and before.st_size > max_bytes:
            raise UnsafeStoragePathError(
                f"anchored file exceeds its read limit of {max_bytes} bytes: {path}"
            )
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            remaining_limits = [
                limit - total_bytes
                for limit in (expected_size, max_bytes)
                if limit is not None
            ]
            request_size = 1024 * 1024
            if remaining_limits:
                request_size = min(request_size, min(remaining_limits) + 1)
            chunk = os.read(descriptor, request_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            if max_bytes is not None and total_bytes > max_bytes:
                raise UnsafeStoragePathError(
                    f"anchored file exceeded its read limit of {max_bytes} bytes: {path}"
                )
            if expected_size is not None and total_bytes > expected_size:
                raise UnexpectedStorageSizeError(
                    f"anchored file size grew beyond its expected size: {path}"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_nlink,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
        )
        named_identity = (
            named_after.st_dev,
            named_after.st_ino,
            named_after.st_size,
            named_after.st_mtime_ns,
            named_after.st_ctime_ns,
            named_after.st_nlink,
        )
        if before_identity != after_identity or after_identity != named_identity:
            raise UnsafeStoragePathError(f"anchored file changed while read: {path}")
        if expected_size is not None and total_bytes != expected_size:
            raise UnexpectedStorageSizeError(
                f"anchored file size differs from its expected size: {path}"
            )
        _validate_anchored_parent(anchor, path, parent_fd)
        validate_directory_anchor(anchor)
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def read_anchored_bytes(
    anchor: DirectoryAnchorLike,
    path: Path | str,
    *,
    expected_size: int | None = None,
    max_bytes: int | None = None,
) -> bytes:
    for name, value in (("expected_size", expected_size), ("max_bytes", max_bytes)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{name} must be a non-negative integer")
    if expected_size is not None and max_bytes is not None and expected_size > max_bytes:
        raise ValueError("expected_size must not exceed max_bytes")
    validate_directory_anchor(anchor)
    return _read_anchored_regular_file(
        anchor,
        path,
        expected_size=expected_size,
        max_bytes=max_bytes,
    )


def anchored_entry_exists(anchor: DirectoryAnchorLike, path: Path | str) -> bool:
    try:
        parent_fd, name = _open_anchored_parent(anchor, path, create=False)
    except FileNotFoundError:
        return False
    try:
        _validate_anchored_parent(anchor, path, parent_fd)
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(parent_fd)


def stat_anchored_entry(
    anchor: DirectoryAnchorLike,
    path: Path | str,
) -> os.stat_result:
    destination = Path(path)
    validate_directory_anchor(anchor)
    parent_fd, name = _open_anchored_parent(anchor, destination, create=False)
    try:
        _validate_anchored_parent(anchor, destination, parent_fd)
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _validate_anchored_parent(anchor, destination, parent_fd)
        validate_directory_anchor(anchor)
        return metadata
    finally:
        os.close(parent_fd)


def open_anchored_regular_file(
    anchor: DirectoryAnchorLike,
    path: Path | str,
    *,
    expected_size: int,
) -> OwnedPublishedFile:
    if expected_size < 0:
        raise ValueError("expected held file size must be non-negative")
    destination = Path(path)
    validate_directory_anchor(anchor)
    parent_fd, name = _open_anchored_parent(anchor, destination, create=False)
    descriptor: int | None = None
    try:
        _validate_anchored_parent(anchor, destination, parent_fd)
        descriptor = os.open(name, _REGULAR_READ_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or opened.st_nlink != 1
            or named.st_nlink != 1
            or not _same_entry(opened, named)
        ):
            raise UnsafeStoragePathError(
                f"anchored file must be one stable regular file: {destination}"
            )
        if opened.st_size != expected_size or named.st_size != expected_size:
            raise UnexpectedStorageSizeError(
                f"anchored file size differs from its expected size: {destination}"
            )
        content_sha256 = _sha256_descriptor(
            descriptor,
            expected_size=expected_size,
        )
        opened_after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identities = {
            (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
                item.st_nlink,
            )
            for item in (opened, named, opened_after, named_after)
        }
        if len(identities) != 1 or not content_sha256:
            raise UnsafeStoragePathError(
                f"anchored file changed while its identity was captured: {destination}"
            )
        _validate_anchored_parent(anchor, destination, parent_fd)
        validate_directory_anchor(anchor)
        result = OwnedPublishedFile(
            path=destination,
            descriptor=descriptor,
            identity=(
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
                opened.st_nlink,
            ),
            content_sha256=content_sha256,
        )
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def create_anchored_directory(
    anchor: DirectoryAnchorLike,
    path: Path | str,
    *,
    mode: int = 0o755,
) -> OwnedDirectoryAnchor:
    validate_directory_anchor(anchor)
    destination = Path(path)
    parent_fd, name = _open_anchored_parent(anchor, destination, create=True)
    descriptor: int | None = None
    created = False
    opened: os.stat_result | None = None
    created_metadata: os.stat_result | None = None
    try:
        _validate_anchored_parent(anchor, destination, parent_fd)
        os.mkdir(name, mode, dir_fd=parent_fd)
        created = True
        created_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        os.fsync(parent_fd)
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_entry(opened, named):
            raise UnsafeStoragePathError(
                f"anchored directory changed while it was created: {destination}"
            )
        _validate_anchored_parent(anchor, destination, parent_fd)
        validate_directory_anchor(anchor)
        result = OwnedDirectoryAnchor(
            path=Path(os.path.abspath(destination)),
            descriptor=descriptor,
            device=opened.st_dev,
            inode=opened.st_ino,
        )
        descriptor = None
        return result
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISDIR(current.st_mode) and (
                    created_metadata is None or _same_entry(created_metadata, current)
                ):
                    os.rmdir(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def open_anchored_directory(
    anchor: DirectoryAnchorLike,
    path: Path | str,
    *,
    create: bool = False,
) -> OwnedDirectoryAnchor:
    validate_directory_anchor(anchor)
    destination = Path(path)
    parts = _anchor_relative_parts(anchor, destination)
    descriptor = _open_relative_directory(anchor, parts, create=create)
    metadata = os.fstat(descriptor)
    try:
        parent_fd, name = _open_anchored_parent(anchor, destination, create=False)
        try:
            _validate_anchored_parent(anchor, destination, parent_fd)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_entry(metadata, named):
                raise UnsafeStoragePathError(
                    f"anchored directory changed while it was opened: {destination}"
                )
        finally:
            os.close(parent_fd)
        validate_directory_anchor(anchor)
        return OwnedDirectoryAnchor(
            path=Path(os.path.abspath(destination)),
            descriptor=descriptor,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
    except BaseException:
        os.close(descriptor)
        raise


def open_terminal_artifact_guard(
    anchor: DirectoryAnchorLike,
    *,
    main_path: Path,
    main_bytes: bytes,
    sidecar_path: Path,
    sidecar_snapshot: ImmutableTreeSnapshot,
    label: str,
) -> HeldTerminalArtifactGuard:
    sidecar = open_anchored_directory(anchor, sidecar_path)
    try:
        main_file = open_anchored_regular_file(
            anchor,
            main_path,
            expected_size=len(main_bytes),
        )
    except BaseException:
        sidecar.close()
        raise
    guard = HeldTerminalArtifactGuard(
        main=HeldExactFileGuard(
            anchor=anchor,
            owned_file=main_file,
            expected_bytes=main_bytes,
            label=f"{label} main artifact",
        ),
        sidecar=sidecar,
        expected_sidecar=sidecar_snapshot,
        label=label,
    )
    try:
        guard.verify()
    except BaseException:
        guard.close()
        raise
    return guard


def _remove_tree_contents_at(directory_fd: int) -> None:
    for name in sorted(os.listdir(directory_fd)):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if not _same_entry(opened, metadata):
                    raise UnsafeStoragePathError(
                        f"anchored cleanup directory changed: {name}"
                    )
                _remove_tree_contents_at(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def remove_anchored_tree(
    anchor: DirectoryAnchorLike,
    path: Path | str,
    *,
    expected: DirectoryAnchorLike | None = None,
    missing_ok: bool = True,
) -> None:
    destination = Path(path)
    try:
        parent_fd, name = _open_anchored_parent(anchor, destination, create=False)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    child_fd: int | None = None
    try:
        try:
            child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        opened = os.fstat(child_fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_entry(opened, named):
            raise UnsafeStoragePathError(
                f"anchored cleanup target changed: {destination}"
            )
        if expected is not None and (opened.st_dev, opened.st_ino) != (
            expected.device,
            expected.inode,
        ):
            raise UnsafeStoragePathError(
                f"anchored cleanup target no longer matches its staging fd: {destination}"
            )
        _remove_tree_contents_at(child_fd)
        os.close(child_fd)
        child_fd = None
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_entry(opened, current):
            raise UnsafeStoragePathError(
                f"anchored cleanup target changed before removal: {destination}"
            )
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if child_fd is not None:
            os.close(child_fd)
        os.close(parent_fd)


def remove_anchored_file(
    anchor: DirectoryAnchorLike,
    path: Path | str,
    *,
    missing_ok: bool = True,
) -> None:
    destination = Path(path)
    try:
        parent_fd, name = _open_anchored_parent(anchor, destination, create=False)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    try:
        _validate_anchored_parent(anchor, destination, parent_fd)
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeStoragePathError(
                f"anchored cleanup file is not a regular file: {destination}"
            )
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


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


def _native_renameat_tree_no_replace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
    *,
    source: Path,
    destination: Path,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source_name)
    destination_bytes = os.fsencode(destination_name)
    if sys.platform == "darwin":
        try:
            renameatx_np = libc.renameatx_np
        except AttributeError as exc:
            raise AtomicNoReplaceUnsupportedError(sys.platform) from exc
        rename_excl = 0x00000004
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = renameatx_np(
            source_parent_fd,
            source_bytes,
            destination_parent_fd,
            destination_bytes,
            rename_excl,
        )
    elif sys.platform.startswith("linux"):
        try:
            renameat2 = libc.renameat2
        except AttributeError as exc:
            raise AtomicNoReplaceUnsupportedError(sys.platform) from exc
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
            source_parent_fd,
            source_bytes,
            destination_parent_fd,
            destination_bytes,
            rename_noreplace,
        )
    else:
        raise AtomicNoReplaceUnsupportedError(sys.platform)
    if result != 0:
        _raise_native_rename_error(ctypes.get_errno(), source, destination)


def _native_exchangeat(
    parent_fd: int,
    first_name: str,
    second_name: str,
    *,
    first: Path,
    second: Path,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    first_bytes = os.fsencode(first_name)
    second_bytes = os.fsencode(second_name)
    rename_exchange = 0x00000002
    if sys.platform == "darwin":
        try:
            function = libc.renameatx_np
        except AttributeError as exc:
            raise AtomicNoReplaceUnsupportedError(sys.platform) from exc
    elif sys.platform.startswith("linux"):
        try:
            function = libc.renameat2
        except AttributeError as exc:
            raise AtomicNoReplaceUnsupportedError(sys.platform) from exc
    else:
        raise AtomicNoReplaceUnsupportedError(sys.platform)
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = function(
        parent_fd,
        first_bytes,
        parent_fd,
        second_bytes,
        rename_exchange,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        unsupported_errors = {errno.ENOSYS, errno.ENOTSUP}
        if hasattr(errno, "EOPNOTSUPP"):
            unsupported_errors.add(errno.EOPNOTSUPP)
        if error_number in unsupported_errors:
            raise AtomicNoReplaceUnsupportedError(sys.platform)
        raise OSError(error_number, os.strerror(error_number), first, second)


def _fsync_tree_at(directory_fd: int, path: Path) -> None:
    for name in sorted(os.listdir(directory_fd)):
        entry_path = path / name
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if not _same_entry(opened, metadata):
                    raise UnsafeStoragePathError(
                        f"staging directory changed while syncing: {entry_path}"
                    )
                _fsync_tree_at(child_fd, entry_path)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            child_fd = os.open(name, _REGULAR_READ_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if opened.st_nlink != 1 or not _same_entry(opened, metadata):
                    raise UnsafeStoragePathError(
                        f"staging file changed while syncing: {entry_path}"
                    )
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
        else:
            raise UnsafeStoragePathError(
                f"staging tree contains a symlink, hardlink, or special entry: {entry_path}"
            )
    os.fsync(directory_fd)


@dataclass(frozen=True, slots=True)
class _TreeSnapshotLimits:
    max_file_bytes: int | None
    max_total_bytes: int | None
    max_members: int | None
    max_depth: int | None


@dataclass(slots=True)
class _TreeSnapshotState:
    total_bytes: int = 0
    members: int = 0


def _tree_snapshot_limits(
    *,
    max_file_bytes: int | None,
    max_total_bytes: int | None,
    max_members: int | None,
    max_depth: int | None,
) -> _TreeSnapshotLimits:
    values = {
        "max_file_bytes": max_file_bytes,
        "max_total_bytes": max_total_bytes,
        "max_members": max_members,
        "max_depth": max_depth,
    }
    for name, value in values.items():
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{name} must be a non-negative integer")
    return _TreeSnapshotLimits(**values)


def _snapshot_directory_names(
    directory_fd: int,
    *,
    prefix: PurePosixPath,
    limits: _TreeSnapshotLimits,
    state: _TreeSnapshotState,
) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            relative = prefix / entry.name
            depth = len(relative.parts)
            if limits.max_depth is not None and depth > limits.max_depth:
                raise TreeSnapshotLimitError(
                    "max_depth",
                    limits.max_depth,
                    f"immutable tree exceeds its depth limit of {limits.max_depth}: "
                    f"{relative.as_posix()}"
                )
            state.members += 1
            if limits.max_members is not None and state.members > limits.max_members:
                raise TreeSnapshotLimitError(
                    "max_members",
                    limits.max_members,
                    f"immutable tree exceeds its member limit of {limits.max_members}"
                )
            names.append(entry.name)
    return tuple(sorted(names))


def _snapshot_tree_fd(
    directory_fd: int,
    *,
    prefix: PurePosixPath = PurePosixPath(),
    limits: _TreeSnapshotLimits,
    state: _TreeSnapshotState,
) -> ImmutableTreeSnapshot:
    entries: list[ImmutableTreeEntry] = []
    names = _snapshot_directory_names(
        directory_fd,
        prefix=prefix,
        limits=limits,
        state=state,
    )
    for name in names:
        relative = prefix / name
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if not _same_entry(opened, metadata):
                    raise UnsafeStoragePathError(
                        f"immutable tree directory changed: {relative.as_posix()}"
                    )
                entries.append(
                    ImmutableTreeEntry(
                        path=relative.as_posix(),
                        kind="directory",
                        size_bytes=0,
                        sha256=None,
                    )
                )
                entries.extend(
                    _snapshot_tree_fd(
                        child_fd,
                        prefix=relative,
                        limits=limits,
                        state=state,
                    ).entries
                )
                named_after = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if not _same_entry(opened, named_after):
                    raise UnsafeStoragePathError(
                        f"immutable tree directory changed: {relative.as_posix()}"
                    )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            child_fd = os.open(name, _REGULAR_READ_FLAGS, dir_fd=directory_fd)
            try:
                before = os.fstat(child_fd)
                if before.st_nlink != 1 or not _same_entry(before, metadata):
                    raise UnsafeStoragePathError(
                        f"immutable tree file changed: {relative.as_posix()}"
                    )
                if (
                    limits.max_file_bytes is not None
                    and before.st_size > limits.max_file_bytes
                ):
                    raise TreeSnapshotLimitError(
                        "max_file_bytes",
                        limits.max_file_bytes,
                        "immutable tree file exceeds its file limit of "
                        f"{limits.max_file_bytes} bytes: {relative.as_posix()}"
                    )
                if (
                    limits.max_total_bytes is not None
                    and state.total_bytes + before.st_size > limits.max_total_bytes
                ):
                    raise TreeSnapshotLimitError(
                        "max_total_bytes",
                        limits.max_total_bytes,
                        "immutable tree exceeds its total byte limit of "
                        f"{limits.max_total_bytes}: {relative.as_posix()}"
                    )
                digest = hashlib.sha256()
                size = 0
                while True:
                    remaining_limits = [
                        limit - current
                        for limit, current in (
                            (limits.max_file_bytes, size),
                            (limits.max_total_bytes, state.total_bytes),
                        )
                        if limit is not None
                    ]
                    request_size = 1024 * 1024
                    if remaining_limits:
                        request_size = min(
                            request_size,
                            min(remaining_limits) + 1,
                        )
                    chunk = os.read(child_fd, request_size)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    state.total_bytes += len(chunk)
                    if (
                        limits.max_file_bytes is not None
                        and size > limits.max_file_bytes
                    ):
                        raise TreeSnapshotLimitError(
                            "max_file_bytes",
                            limits.max_file_bytes,
                            "immutable tree file exceeded its file limit of "
                            f"{limits.max_file_bytes} bytes while read: "
                            f"{relative.as_posix()}"
                        )
                    if (
                        limits.max_total_bytes is not None
                        and state.total_bytes > limits.max_total_bytes
                    ):
                        raise TreeSnapshotLimitError(
                            "max_total_bytes",
                            limits.max_total_bytes,
                            "immutable tree exceeded its total byte limit of "
                            f"{limits.max_total_bytes} while read: "
                            f"{relative.as_posix()}"
                        )
                after = os.fstat(child_fd)
                named_after = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                before_identity = (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                    before.st_nlink,
                )
                after_identity = (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                    after.st_nlink,
                )
                named_identity = (
                    named_after.st_dev,
                    named_after.st_ino,
                    named_after.st_size,
                    named_after.st_mtime_ns,
                    named_after.st_ctime_ns,
                    named_after.st_nlink,
                )
                if before_identity != after_identity or after_identity != named_identity:
                    raise UnsafeStoragePathError(
                        f"immutable tree file changed: {relative.as_posix()}"
                    )
                entries.append(
                    ImmutableTreeEntry(
                        path=relative.as_posix(),
                        kind="file",
                        size_bytes=size,
                        sha256=digest.hexdigest(),
                    )
                )
            finally:
                os.close(child_fd)
        else:
            raise UnsafeStoragePathError(
                f"immutable tree contains a symlink, hardlink, or special entry: {relative.as_posix()}"
            )
    return ImmutableTreeSnapshot(
        entries=tuple(sorted(entries, key=lambda item: (item.path, item.kind)))
    )


def snapshot_anchored_tree(
    anchor: DirectoryAnchorLike,
    path: Path | str | None = None,
    *,
    max_file_bytes: int | None = None,
    max_total_bytes: int | None = None,
    max_members: int | None = None,
    max_depth: int | None = None,
) -> ImmutableTreeSnapshot:
    limits = _tree_snapshot_limits(
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_members=max_members,
        max_depth=max_depth,
    )
    validate_directory_anchor(anchor)
    if path is None or Path(os.path.abspath(path)) == Path(os.path.abspath(anchor.path)):
        descriptor = os.dup(anchor.descriptor)
        target_path = anchor.path
    else:
        target_path = Path(path)
        parts = _anchor_relative_parts(anchor, target_path)
        descriptor = _open_relative_directory(anchor, parts, create=False)
    try:
        snapshot = _snapshot_tree_fd(
            descriptor,
            limits=limits,
            state=_TreeSnapshotState(),
        )
        if path is not None:
            parent_fd, name = _open_anchored_parent(anchor, target_path, create=False)
            try:
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                opened = os.fstat(descriptor)
                if not _same_entry(named, opened):
                    raise UnsafeStoragePathError(
                        f"immutable tree root changed while snapshotted: {target_path}"
                    )
            finally:
                os.close(parent_fd)
        validate_directory_anchor(anchor)
        return snapshot
    finally:
        os.close(descriptor)


def snapshot_directory_fd(
    directory_fd: int,
    *,
    max_file_bytes: int | None = None,
    max_total_bytes: int | None = None,
    max_members: int | None = None,
    max_depth: int | None = None,
) -> ImmutableTreeSnapshot:
    limits = _tree_snapshot_limits(
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_members=max_members,
        max_depth=max_depth,
    )
    return _snapshot_tree_fd(
        directory_fd,
        limits=limits,
        state=_TreeSnapshotState(),
    )


def tree_snapshot_from_hashes(
    files: dict[str, tuple[int, str]],
) -> ImmutableTreeSnapshot:
    directories: set[str] = set()
    entries: list[ImmutableTreeEntry] = []
    for raw_path, (size_bytes, sha256) in files.items():
        normalized = safe_relative_artifact_path(raw_path)
        path = PurePosixPath(normalized)
        for parent in path.parents:
            if parent != PurePosixPath("."):
                directories.add(parent.as_posix())
        entries.append(
            ImmutableTreeEntry(
                path=normalized,
                kind="file",
                size_bytes=size_bytes,
                sha256=sha256,
            )
        )
    entries.extend(
        ImmutableTreeEntry(
            path=path,
            kind="directory",
            size_bytes=0,
            sha256=None,
        )
        for path in directories
    )
    return ImmutableTreeSnapshot(entries=tuple(sorted(entries, key=lambda item: item.path)))


def tree_snapshot_from_bytes(files: dict[str, bytes]) -> ImmutableTreeSnapshot:
    return tree_snapshot_from_hashes(
        {
            path: (len(content), hashlib.sha256(content).hexdigest())
            for path, content in files.items()
        }
    )


def _require_named_regular_descriptor(
    parent_fd: int,
    name: str,
    descriptor: int,
    *,
    path: Path,
) -> os.stat_result:
    opened = os.fstat(descriptor)
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or opened.st_nlink != 1
        or named.st_nlink != 1
        or not _same_entry(opened, named)
    ):
        raise UnsafeStoragePathError(
            f"anchored temporary file identity changed: {path}"
        )
    return opened


def _require_expected_held_file(
    parent_fd: int,
    name: str,
    expected: OwnedPublishedFile,
    *,
    path: Path,
) -> os.stat_result:
    if Path(os.path.abspath(expected.path)) != Path(os.path.abspath(path)):
        raise UnsafeStoragePathError(
            f"expected held file names a different destination: {expected.path}"
        )
    try:
        opened = os.fstat(expected.descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise UnsafeStoragePathError(
            f"expected held file became unavailable: {path}: {exc}"
        ) from exc
    identities = {
        (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
            item.st_nlink,
        )
        for item in (opened, named)
    }
    if (
        identities != {expected.identity}
        or not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or expected.identity[5] != 1
    ):
        raise UnsafeStoragePathError(
            f"atomic compare-and-replace expected file changed: {path}"
        )
    return opened


def _atomic_write_bytes_anchored(
    anchor: DirectoryAnchorLike,
    destination: Path,
    content: bytes,
    *,
    mode: int,
    hold_open: bool,
    expected_current: OwnedPublishedFile | None,
) -> Path | OwnedPublishedFile:
    validate_directory_anchor(anchor)
    parent_fd, name = _open_anchored_parent(anchor, destination, create=True)
    temporary_name = f".{name}.{new_uuid()}.tmp"
    descriptor: int | None = None
    replaced = False
    owned_temporary: os.stat_result | None = None
    try:
        _validate_anchored_parent(anchor, destination, parent_fd)
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise UnsafeStoragePathError(
                f"atomic write destination is not a single-link regular file: {destination}"
            )
        if expected_current is not None:
            _require_expected_held_file(
                parent_fd,
                name,
                expected_current,
                path=destination,
            )
        descriptor = os.open(
            temporary_name,
            (os.O_RDWR if hold_open else os.O_WRONLY)
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=parent_fd,
        )
        owned_temporary = os.fstat(descriptor)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        temporary_metadata = _require_named_regular_descriptor(
            parent_fd,
            temporary_name,
            descriptor,
            path=destination.parent / temporary_name,
        )
        validate_directory_anchor(anchor)
        _validate_anchored_parent(anchor, destination, parent_fd)
        if expected_current is None:
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            replaced = True
        else:
            _require_expected_held_file(
                parent_fd,
                name,
                expected_current,
                path=destination,
            )
            _native_exchangeat(
                parent_fd,
                temporary_name,
                name,
                first=destination.parent / temporary_name,
                second=destination,
            )
            os.fsync(parent_fd)
            named_new = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            swapped_out = os.stat(
                temporary_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            swapped_out_identity = (
                swapped_out.st_dev,
                swapped_out.st_ino,
                swapped_out.st_size,
                swapped_out.st_mtime_ns,
                swapped_out.st_ctime_ns,
                swapped_out.st_nlink,
            )
            try:
                swapped_out_fd_before = os.fstat(expected_current.descriptor)
                swapped_out_sha256 = _sha256_descriptor(
                    expected_current.descriptor,
                    expected_size=expected_current.identity[2],
                )
                swapped_out_fd_after = os.fstat(expected_current.descriptor)
            except OSError:
                swapped_out_matches_expected = False
            else:
                swapped_out_fd_identities = {
                    (
                        item.st_dev,
                        item.st_ino,
                        item.st_size,
                        item.st_mtime_ns,
                        item.st_ctime_ns,
                        item.st_nlink,
                    )
                    for item in (swapped_out, swapped_out_fd_before, swapped_out_fd_after)
                }
                expected_structural_identity = (
                    *expected_current.identity[:4],
                    expected_current.identity[5],
                )
                swapped_out_structural_identity = (
                    *swapped_out_identity[:4],
                    swapped_out_identity[5],
                )
                swapped_out_matches_expected = (
                    len(swapped_out_fd_identities) == 1
                    and swapped_out_structural_identity
                    == expected_structural_identity
                    and swapped_out_sha256 == expected_current.content_sha256
                )
            if not _same_entry(temporary_metadata, named_new):
                raise UnsafeStoragePathError(
                    f"atomic compare-and-replace new file lost its name: {destination}"
                )
            if not swapped_out_matches_expected:
                # A second exchange cannot be made conditional on the retired
                # name still identifying the file inspected above.  Re-exchanging
                # here would therefore move an unknown concurrent replacement if
                # that private name changed at the last instant.  Fail closed:
                # retain the swapped-out bytes under our unpredictable orphan name
                # and leave the exact newly-written inode at the destination.  A
                # later operation can inspect both durable files without this
                # process deleting or moving either unknown replacement.
                replaced = True
                raise UnsafeStoragePathError(
                    "atomic compare-and-replace observed an external replacement; "
                    f"swapped-out bytes were preserved as {temporary_name}: "
                    f"{destination}"
                )
            # Retain the unpredictable retired name and its exact bytes.
            # POSIX provides neither identity-conditional unlink nor
            # identity-conditional truncate: even after an nlink check, a
            # concurrent hardlink can make descriptor mutation escape this
            # run.  The immutable orphan is therefore the only fail-closed
            # cleanup state.
            replaced = True
        os.fsync(parent_fd)
        _validate_anchored_parent(anchor, destination, parent_fd)
        named_destination = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_entry(temporary_metadata, named_destination):
            raise UnsafeStoragePathError(
                f"atomic write renamed an unbound temporary file: {destination}"
            )
        readback = _read_anchored_regular_file(
            anchor,
            destination,
            expected_size=len(content),
            max_bytes=len(content),
        )
        final_destination = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_entry(temporary_metadata, final_destination):
            raise UnsafeStoragePathError(
                f"atomic write destination name changed after readback: {destination}"
            )
        if readback != content:
            raise UnsafeStoragePathError(
                f"atomic write readback mismatch: {destination}"
            )
        validate_directory_anchor(anchor)
        if hold_open:
            held_metadata = os.fstat(descriptor)
            if (
                not _same_entry(temporary_metadata, held_metadata)
                or held_metadata.st_nlink != 1
            ):
                raise UnsafeStoragePathError(
                    f"held atomic write descriptor changed before return: {destination}"
                )
            published = OwnedPublishedFile(
                path=destination,
                descriptor=descriptor,
                identity=(
                    held_metadata.st_dev,
                    held_metadata.st_ino,
                    held_metadata.st_size,
                    held_metadata.st_mtime_ns,
                    held_metadata.st_ctime_ns,
                    held_metadata.st_nlink,
                ),
                content_sha256=hashlib.sha256(content).hexdigest(),
            )
            descriptor = None
            return published
        return destination
    finally:
        has_primary_error = sys.exc_info()[0] is not None
        cleanup_error: BaseException | None = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException as exc:
                cleanup_error = exc
        try:
            if (
                not replaced
                and owned_temporary is not None
                and expected_current is None
            ):
                try:
                    named_temporary = os.stat(
                        temporary_name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if _same_entry(owned_temporary, named_temporary):
                        os.unlink(temporary_name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                except FileNotFoundError:
                    pass
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        finally:
            try:
                os.close(parent_fd)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None and not has_primary_error:
            raise cleanup_error


def _publish_bytes_no_replace_anchored(
    anchor: DirectoryAnchorLike,
    destination: Path,
    content: bytes,
    *,
    hold_open: bool = False,
) -> Path | OwnedPublishedFile:
    validate_directory_anchor(anchor)
    parent_fd, name = _open_anchored_parent(anchor, destination, create=True)
    temporary_name = f".{name}.{new_uuid()}.tmp"
    descriptor: int | None = None
    renamed = False
    owned_temporary: os.stat_result | None = None
    try:
        _validate_anchored_parent(anchor, destination, parent_fd)
        descriptor = os.open(
            temporary_name,
            (os.O_RDWR if hold_open else os.O_WRONLY)
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o644,
            dir_fd=parent_fd,
        )
        owned_temporary = os.fstat(descriptor)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        temporary_metadata = _require_named_regular_descriptor(
            parent_fd,
            temporary_name,
            descriptor,
            path=destination.parent / temporary_name,
        )
        validate_directory_anchor(anchor)
        _validate_anchored_parent(anchor, destination, parent_fd)
        _native_renameat_tree_no_replace(
            parent_fd,
            temporary_name,
            parent_fd,
            name,
            source=destination.parent / temporary_name,
            destination=destination,
        )
        renamed = True
        temporary_name = ""
        os.fsync(parent_fd)
        _validate_anchored_parent(anchor, destination, parent_fd)
        named_destination = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_entry(temporary_metadata, named_destination):
            raise UnsafeStoragePathError(
                f"atomic no-replace publication renamed an unbound temporary file: {destination}"
            )
        readback = _read_anchored_regular_file(
            anchor,
            destination,
            expected_size=len(content),
            max_bytes=len(content),
        )
        final_destination = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_entry(temporary_metadata, final_destination):
            raise UnsafeStoragePathError(
                f"atomic no-replace destination name changed after readback: {destination}"
            )
        if readback != content:
            raise UnsafeStoragePathError(
                f"atomic no-replace publication readback mismatch: {destination}"
            )
        validate_directory_anchor(anchor)
        if hold_open:
            held_metadata = os.fstat(descriptor)
            if not _same_entry(temporary_metadata, held_metadata):
                raise UnsafeStoragePathError(
                    f"held publication descriptor changed before return: {destination}"
                )
            published = OwnedPublishedFile(
                path=destination,
                descriptor=descriptor,
                identity=(
                    held_metadata.st_dev,
                    held_metadata.st_ino,
                    held_metadata.st_size,
                    held_metadata.st_mtime_ns,
                    held_metadata.st_ctime_ns,
                    held_metadata.st_nlink,
                ),
                content_sha256=hashlib.sha256(content).hexdigest(),
            )
            descriptor = None
            return published
        return destination
    finally:
        has_primary_error = sys.exc_info()[0] is not None
        cleanup_error: BaseException | None = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException as exc:
                cleanup_error = exc
        try:
            if temporary_name and not renamed and owned_temporary is not None:
                try:
                    named_temporary = os.stat(
                        temporary_name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if _same_entry(owned_temporary, named_temporary):
                        os.unlink(temporary_name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                except FileNotFoundError:
                    pass
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        finally:
            try:
                os.close(parent_fd)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None and not has_primary_error:
            raise cleanup_error


def _atomic_publish_tree_anchored(
    anchor: DirectoryAnchorLike,
    staging: Path,
    destination: Path,
    *,
    expected_staging_anchor: DirectoryAnchorLike | None,
    expected_tree_snapshot: ImmutableTreeSnapshot | None,
    hold_open_relative_file: str | None,
) -> Path | OwnedPublishedTree:
    validate_directory_anchor(anchor)
    source_parent_fd: int | None = None
    destination_parent_fd: int | None = None
    source_fd: int | None = None
    published_fd: int | None = None
    try:
        source_parent_fd, source_name = _open_anchored_parent(
            anchor,
            staging,
            create=False,
        )
        destination_parent_fd, destination_name = _open_anchored_parent(
            anchor,
            destination,
            create=True,
        )
        _validate_anchored_parent(anchor, staging, source_parent_fd)
        _validate_anchored_parent(anchor, destination, destination_parent_fd)
        source_fd = os.open(source_name, _DIRECTORY_OPEN_FLAGS, dir_fd=source_parent_fd)
        source_metadata = os.fstat(source_fd)
        named_source = os.stat(
            source_name,
            dir_fd=source_parent_fd,
            follow_symlinks=False,
        )
        if (
            not _same_entry(source_metadata, named_source)
            or expected_staging_anchor is not None
            and (source_metadata.st_dev, source_metadata.st_ino)
            != (expected_staging_anchor.device, expected_staging_anchor.inode)
        ):
            raise UnsafeStoragePathError(f"staging tree changed before publication: {staging}")
        if expected_staging_anchor is not None:
            validate_directory_anchor(expected_staging_anchor)
        if source_metadata.st_dev != os.fstat(destination_parent_fd).st_dev:
            raise OSError(errno.EXDEV, "tree publication requires the same filesystem")
        _fsync_tree_at(source_fd, staging)
        observed_source_snapshot = snapshot_directory_fd(
            source_fd,
            max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
            max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
            max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
        )
        sealed_snapshot = expected_tree_snapshot or observed_source_snapshot
        if observed_source_snapshot != sealed_snapshot:
            raise UnsafeStoragePathError(
                f"staging tree closed-set changed before publication: {staging}"
            )
        validate_directory_anchor(anchor)
        _validate_anchored_parent(anchor, staging, source_parent_fd)
        _validate_anchored_parent(anchor, destination, destination_parent_fd)
        _native_renameat_tree_no_replace(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            source=staging,
            destination=destination,
        )
        os.fsync(source_parent_fd)
        os.fsync(destination_parent_fd)
        published_fd = os.open(
            destination_name,
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=destination_parent_fd,
        )
        published = os.fstat(published_fd)
        named_published = os.stat(
            destination_name,
            dir_fd=destination_parent_fd,
            follow_symlinks=False,
        )
        if (
            not _same_entry(source_metadata, published)
            or not _same_entry(published, named_published)
        ):
            raise UnsafeStoragePathError(
                f"published tree does not match staging identity: {destination}"
            )
        published_snapshot = snapshot_directory_fd(
            published_fd,
            max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
            max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
            max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
        )
        if published_snapshot != sealed_snapshot:
            raise UnsafeStoragePathError(
                f"published tree closed-set changed during publication: {destination}"
            )
        _validate_anchored_parent(anchor, destination, destination_parent_fd)
        validate_directory_anchor(anchor)
        final_destination = os.stat(
            destination_name,
            dir_fd=destination_parent_fd,
            follow_symlinks=False,
        )
        if not _same_entry(published, final_destination):
            raise UnsafeStoragePathError(
                f"published tree name changed before commit: {destination}"
            )
        if hold_open_relative_file is not None:
            held_snapshot_entries = [
                entry
                for entry in sealed_snapshot.entries
                if entry.path == hold_open_relative_file and entry.kind == "file"
            ]
            if len(held_snapshot_entries) != 1:
                raise UnsafeStoragePathError(
                    "held publication file is missing from the sealed tree snapshot: "
                    f"{hold_open_relative_file}"
                )
            published_anchor = OwnedDirectoryAnchor(
                path=Path(os.path.abspath(destination)),
                descriptor=published_fd,
                device=published.st_dev,
                inode=published.st_ino,
            )
            published_fd = None
            try:
                held_file = open_anchored_regular_file(
                    published_anchor,
                    published_anchor.path / hold_open_relative_file,
                    expected_size=held_snapshot_entries[0].size_bytes,
                )
            except BaseException:
                published_anchor.close()
                raise
            return OwnedPublishedTree(
                path=published_anchor.path,
                directory=published_anchor,
                held_file=held_file,
                held_relative_path=hold_open_relative_file,
            )
        return destination
    finally:
        if published_fd is not None:
            os.close(published_fd)
        if source_fd is not None:
            os.close(source_fd)
        if destination_parent_fd is not None:
            os.close(destination_parent_fd)
        if source_parent_fd is not None:
            os.close(source_parent_fd)


def atomic_write_bytes(
    path: Path | str,
    content: bytes,
    *,
    mode: int = 0o644,
    anchor: DirectoryAnchorLike | None = None,
    hold_open: bool = False,
    expected_current: OwnedPublishedFile | None = None,
) -> Path | OwnedPublishedFile:
    destination = Path(path)
    if anchor is not None:
        return _atomic_write_bytes_anchored(
            anchor,
            destination,
            content,
            mode=mode,
            hold_open=hold_open,
            expected_current=expected_current,
        )
    if hold_open or expected_current is not None:
        raise ValueError(
            "held or identity-bound atomic write requires a directory anchor"
        )
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


def atomic_write_json(
    path: Path | str,
    value: Any,
    *,
    anchor: DirectoryAnchorLike | None = None,
    hold_open: bool = False,
    expected_current: OwnedPublishedFile | None = None,
) -> Path | OwnedPublishedFile:
    return atomic_write_bytes(
        path,
        canonical_json_bytes(value),
        anchor=anchor,
        hold_open=hold_open,
        expected_current=expected_current,
    )


def compare_and_swap_bytes(
    path: Path | str,
    content: bytes,
    *,
    anchor: DirectoryAnchorLike,
    expected_current_bytes: bytes,
    hold_new: bool = False,
    finalization_guards: tuple[ExactFinalizationGuard, ...] = (),
) -> Path | OwnedPublishedFile:
    destination = Path(path)
    current = open_anchored_regular_file(
        anchor,
        destination,
        expected_size=len(expected_current_bytes),
    )
    with HeldExactFileGuard(
        anchor=anchor,
        owned_file=current,
        expected_bytes=expected_current_bytes,
        label="compare-and-swap current file",
    ) as current_guard:
        current_guard.verify()
        for guard in finalization_guards:
            guard.verify()
        written = atomic_write_bytes(
            destination,
            content,
            anchor=anchor,
            hold_open=True,
            expected_current=current_guard.owned_file,
        )
    if not isinstance(written, OwnedPublishedFile):  # pragma: no cover - API invariant
        raise UnsafeStoragePathError(
            f"compare-and-swap did not retain the new file identity: {destination}"
        )
    written_guard = HeldExactFileGuard(
        anchor=anchor,
        owned_file=written,
        expected_bytes=content,
        label="compare-and-swap new file",
    )
    try:
        written_guard.verify()
        for guard in finalization_guards:
            guard.verify()
    except BaseException:
        # The exchange is already durable.  A second compare-and-swap would
        # introduce another attacker-controlled pathname window and could move
        # an identity that was never part of the original transaction.  Keep
        # the published manifest and its exact retired predecessor as immutable
        # forensic state; callers must fail closed and let the next loader
        # reject any manifest/artifact inconsistency.
        written_guard.close()
        raise
    if hold_new:
        return written_guard.owned_file
    written_guard.close()
    return destination


def cas_update_run(
    loaded: LoadedRunUpdateLike,
    updated_run: Any,
    *,
    hold_new: bool = False,
    finalization_guards: tuple[ExactFinalizationGuard, ...] = (),
) -> Path | OwnedPublishedFile:
    anchor = loaded.run_directory_anchor
    if anchor is None:
        raise UnsafeStoragePathError(
            "run compare-and-swap requires a locked run directory anchor"
        )
    updated_run_bytes = canonical_json_bytes(updated_run)
    # Every successful identity-bound replacement intentionally retains the
    # exact previous manifest as an immutable orphan.  Enforce the run cap in
    # the generic primitive so recovery/replay callers cannot omit that extra
    # durable copy when they bind an already-published terminal artifact.
    from paper_reader.run_size import enforce_projected_run_size

    validate_directory_anchor(anchor)
    enforce_projected_run_size(
        loaded.manifest_path.parent,
        max_bytes=V2_RESOURCE_POLICY.run_max_bytes,
        replacements={loaded.manifest_path: updated_run_bytes},
        retained_replacement_paths=(loaded.manifest_path,),
    )
    validate_directory_anchor(anchor)
    return compare_and_swap_bytes(
        loaded.manifest_path,
        updated_run_bytes,
        anchor=anchor,
        expected_current_bytes=loaded.manifest_bytes,
        hold_new=hold_new,
        finalization_guards=finalization_guards,
    )


def atomic_publish_tree(
    staging: Path | str,
    destination: Path | str,
    *,
    anchor: DirectoryAnchorLike | None = None,
    expected_staging_anchor: DirectoryAnchorLike | None = None,
    expected_tree_snapshot: ImmutableTreeSnapshot | None = None,
    hold_open_relative_file: str | None = None,
) -> Path | OwnedPublishedTree:
    staging_path = Path(staging)
    destination_path = Path(destination)
    if hold_open_relative_file is not None:
        hold_open_relative_file = safe_relative_artifact_path(
            hold_open_relative_file
        )
    if anchor is not None:
        return _atomic_publish_tree_anchored(
            anchor,
            staging_path,
            destination_path,
            expected_staging_anchor=expected_staging_anchor,
            expected_tree_snapshot=expected_tree_snapshot,
            hold_open_relative_file=hold_open_relative_file,
        )
    if (
        expected_staging_anchor is not None
        or expected_tree_snapshot is not None
        or hold_open_relative_file is not None
    ):
        raise ValueError(
            "expected staging identity and held publication require an anchored publication"
        )
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


def publish_bytes_no_replace(
    content: bytes,
    destination: Path | str,
    *,
    anchor: DirectoryAnchorLike | None = None,
    hold_open: bool = False,
) -> Path | OwnedPublishedFile:
    destination_path = Path(destination).expanduser()
    if anchor is not None:
        return _publish_bytes_no_replace_anchored(
            anchor,
            destination_path,
            content,
            hold_open=hold_open,
        )
    if hold_open:
        raise ValueError("held no-replace publication requires a directory anchor")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.parent / f".{destination_path.name}.{new_uuid()}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb") as target_handle:
            descriptor = None
            target_handle.write(content)
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
    "DirectoryAnchorLike",
    "ExactFinalizationGuard",
    "HeldExactFileGuard",
    "HeldExactTreeGuard",
    "HeldResolvedSourceGuard",
    "HeldTerminalArtifactGuard",
    "ImmutableTreeEntry",
    "ImmutableTreeSnapshot",
    "OwnedDirectoryAnchor",
    "OwnedPublishedFile",
    "OwnedPublishedTree",
    "PublishConflictError",
    "ResolvedSourceFingerprint",
    "TreeSnapshotLimitError",
    "UnsafeStoragePathError",
    "UnexpectedStorageSizeError",
    "anchored_entry_exists",
    "assert_no_source_output_alias",
    "atomic_publish_tree",
    "atomic_write_bytes",
    "atomic_write_json",
    "cas_update_run",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "create_anchored_directory",
    "compare_and_swap_bytes",
    "fingerprint_source",
    "fingerprint_resolved_source",
    "fsync_directory",
    "new_random_id",
    "new_uuid",
    "open_anchored_regular_file",
    "open_anchored_directory",
    "open_resolved_source_guard",
    "open_terminal_artifact_guard",
    "paths_alias",
    "publish_bytes_no_replace",
    "publish_file_no_replace",
    "read_anchored_bytes",
    "remove_anchored_tree",
    "remove_anchored_file",
    "random_token",
    "resolve_artifact_path",
    "rfc3339_utc",
    "safe_relative_artifact_path",
    "sha256_file",
    "snapshot_anchored_tree",
    "snapshot_directory_fd",
    "source_fingerprint",
    "source_matches_fingerprint",
    "stat_anchored_entry",
    "tree_snapshot_from_bytes",
    "tree_snapshot_from_hashes",
    "validate_directory_anchor",
]
