from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys
from typing import Any, Callable, Iterator, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX only
    msvcrt = None

from pydantic import BaseModel

from paper_reader_batch.v2_errors import BatchRuntimeError


StorageFaultHook = Callable[[str], None]

READ_CHUNK_BYTES = 1024 * 1024
# Batch-owned structured state is metadata, not a transport for PDF bytes. Keep
# JSON inputs substantially below the complete single-run ceiling while still
# allowing large manifests and rendered note payloads.
MAX_JSON_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_OPAQUE_ARTIFACT_BYTES = 512 * 1024 * 1024
# Lock files contain small runtime secrets or markers. A larger locked inode is
# corruption and must be rejected before allocating its declared size.
MAX_LOCKED_FILE_BYTES = 1024 * 1024
_TRANSITION_SCHEMA_VERSION = "paper_reader_batch.storage-transition-owner.v2-internal"
_TRANSITION_DIRECTORY_NAME = ".transitions"
_MAX_GENERAL_DIRECTORY_ENTRIES = 200_000
_MAX_BUNDLE_MEMBERS = 100_000


def canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalized_absolute_path(path: Path) -> Path:
    expanded = os.path.expanduser(os.fspath(path))
    absolute = expanded if os.path.isabs(expanded) else os.path.abspath(expanded)
    normalized = Path(os.path.normpath(absolute))
    if sys.platform == "darwin" and len(normalized.parts) > 1 and normalized.parts[1] in {"tmp", "var", "etc"}:
        alias = Path("/") / normalized.parts[1]
        expected = f"private/{normalized.parts[1]}"
        try:
            metadata = os.lstat(alias)
            link_target = os.readlink(alias)
            trusted_target = Path("/private") / normalized.parts[1]
            target_metadata = os.lstat(trusted_target)
        except OSError:
            pass
        else:
            if (
                stat.S_ISLNK(metadata.st_mode)
                and metadata.st_uid == 0
                and link_target in {expected, f"/{expected}"}
                and stat.S_ISDIR(target_metadata.st_mode)
                and target_metadata.st_uid == 0
            ):
                normalized = trusted_target.joinpath(*normalized.parts[2:])
    return normalized


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_flags(flags: int) -> int:
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _storage_error(message: str, exc: OSError | None = None) -> BatchRuntimeError:
    error = BatchRuntimeError("storage_error", message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _fsync(descriptor: int, *, label: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise _storage_error(f"fsync failed for {label}", exc)


def _verify_directory_binding(path: Path, descriptor: int) -> None:
    try:
        held = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise _storage_error(f"storage directory changed or became unreachable: {path}", exc)
    if not stat.S_ISDIR(current.st_mode) or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
        raise BatchRuntimeError("storage_path_changed", f"storage directory binding is not stable: {path}")


def _open_child_directory(parent_fd: int, name: str, *, create: bool) -> int:
    if name in {"", ".", ".."} or "/" in name:
        raise BatchRuntimeError("unsafe_path", f"unsafe directory component: {name}")
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise BatchRuntimeError("storage_missing", f"directory component does not exist: {name}")
        try:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            else:
                _fsync(parent_fd, label=f"parent of {name}")
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise _storage_error(f"cannot create safe directory component: {name}", exc)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise BatchRuntimeError("unsafe_path", f"directory component is a symlink or not a directory: {name}") from exc
        raise _storage_error(f"cannot open directory component safely: {name}", exc)
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode):
        os.close(descriptor)
        raise BatchRuntimeError("unsafe_path", f"component is not a directory: {name}")
    return descriptor


@contextmanager
def open_directory_fd(path: Path, *, create: bool = False) -> Iterator[tuple[int, Path]]:
    normalized = normalized_absolute_path(path)
    try:
        descriptor = os.open("/", _directory_flags())
    except OSError as exc:  # pragma: no cover - platform root failure
        raise _storage_error("cannot anchor filesystem root", exc)
    try:
        for component in normalized.parts[1:]:
            child = _open_child_directory(descriptor, component, create=create)
            os.close(descriptor)
            descriptor = child
        _verify_directory_binding(normalized, descriptor)
        yield descriptor, normalized
        _verify_directory_binding(normalized, descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _open_parent_fd(path: Path, *, create: bool = False) -> Iterator[tuple[int, Path, str]]:
    normalized = normalized_absolute_path(path)
    name = normalized.name
    if name in {"", ".", ".."} or "/" in name:
        raise BatchRuntimeError("unsafe_path", f"unsafe file name: {path}")
    with open_directory_fd(normalized.parent, create=create) as (descriptor, parent):
        yield descriptor, parent, name


def ensure_directory(path: Path) -> Path:
    with open_directory_fd(path, create=True) as (_descriptor, normalized):
        return normalized


def list_directory(path: Path) -> list[str]:
    with open_directory_fd(path, create=False) as (descriptor, _normalized):
        return _bounded_sorted_names(
            descriptor,
            max_entries=_MAX_GENERAL_DIRECTORY_ENTRIES,
            label=f"directory {path}",
        )


def _bounded_sorted_names(
    descriptor: int,
    *,
    max_entries: int,
    label: str,
) -> list[str]:
    """Enumerate at most ``max_entries`` names before allocating/sorting more."""

    if type(max_entries) is not int or max_entries < 0:
        raise ValueError("max_entries must be a non-negative integer")
    names: list[str] = []
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if len(names) >= max_entries:
                    raise BatchRuntimeError(
                        "resource_limit",
                        f"{label} has more than {max_entries} entries",
                    )
                names.append(entry.name)
    except BatchRuntimeError:
        raise
    except OSError as exc:
        raise _storage_error(f"cannot list {label}", exc)
    return sorted(names)


def list_mutable_directory(
    path: Path,
    *,
    replace_targets: set[str] | frozenset[str] = frozenset(),
    allow_pending_swaps: bool = False,
    allowed_pending_transition_targets: set[str] | frozenset[str] = frozenset(),
    max_entries: int | None = None,
) -> list[str]:
    """List one explicitly mutable surface while hiding validated internals.

    ``.transitions`` is hidden only after its complete closed-world structure
    and immutable owner bindings have been validated.  Prepared transitions
    for caller-declared public targets remain visible as a recovery error unless
    the authoritative mutation preflight explicitly allows them.
    """

    with open_directory_fd(path, create=False) as (descriptor, normalized):
        if max_entries is None:
            names = _bounded_sorted_names(
                descriptor,
                max_entries=_MAX_GENERAL_DIRECTORY_ENTRIES,
                label=f"mutable directory {normalized}",
            )
        else:
            names = _bounded_sorted_names(
                descriptor,
                max_entries=max_entries,
                label=f"mutable directory {normalized}",
            )
        for target_name in replace_targets:
            if target_name in {"", ".", ".."} or "/" in target_name:
                raise ValueError("replace target must be one safe basename")
        if _TRANSITION_DIRECTORY_NAME not in names:
            return names
        active_targets = _validate_transition_directory(descriptor, normalized, replace_targets)
        blocking = active_targets.difference(allowed_pending_transition_targets)
        if blocking and not allow_pending_swaps:
            raise BatchRuntimeError(
                "storage_recovery_required",
                f"immutable transition requires locked recovery: {normalized}",
            )
        return [name for name in names if name != _TRANSITION_DIRECTORY_NAME]


def read_pending_swap(
    path: Path,
    *,
    max_bytes: int,
    replace_targets: set[str] | frozenset[str] | None = None,
) -> tuple[bytes, bytes] | None:
    """Compatibility view of one caller-authoritative pending transition."""

    pending = read_pending_transitions(
        path,
        max_bytes=max_bytes,
        replace_targets=replace_targets,
    )
    if not pending:
        return None
    if len(pending) != 1:
        raise BatchRuntimeError(
            "unsafe_storage",
            f"multiple immutable transitions are pending for one target: {path}",
        )
    public_raw, desired_raw, _transition_name = pending[0]
    return public_raw, desired_raw


def read_pending_transitions(
    path: Path,
    *,
    max_bytes: int,
    replace_targets: set[str] | frozenset[str] | None = None,
) -> list[tuple[bytes, bytes, str]]:
    """Return prepared A->B transitions without choosing domain validity."""

    with _open_parent_fd(path, create=False) as (parent_fd, parent_path, name):
        allowed = frozenset({name} if replace_targets is None else replace_targets)
        if name not in allowed:
            raise ValueError("pending transition target must be declared")
        return _read_pending_transitions_from_parent(
            parent_fd,
            parent_path,
            name,
            max_bytes=max_bytes,
            allowed_targets=allowed,
        )


def read_committed_transitions(
    path: Path,
    *,
    max_bytes: int,
    replace_targets: set[str] | frozenset[str] | None = None,
) -> list[tuple[bytes, bytes, str]]:
    """Return durable D-state transitions for provenance-sensitive recovery."""

    with _open_parent_fd(path, create=False) as (parent_fd, parent_path, name):
        allowed = frozenset({name} if replace_targets is None else replace_targets)
        if name not in allowed:
            raise ValueError("committed transition target must be declared")
        return _read_pending_transitions_from_parent(
            parent_fd,
            parent_path,
            name,
            max_bytes=max_bytes,
            allowed_targets=allowed,
            committed=True,
        )


def active_transition_targets(
    directory: Path,
    *,
    replace_targets: set[str] | frozenset[str],
) -> set[str]:
    with open_directory_fd(directory, create=False) as (parent_fd, parent_path):
        return _validate_transition_directory(parent_fd, parent_path, replace_targets)


def read_active_transition_owner(
    path: Path,
    *,
    replace_targets: set[str] | frozenset[str] | None = None,
) -> bytes | None:
    with _open_parent_fd(path, create=False) as (parent_fd, parent_path, name):
        allowed = frozenset({name} if replace_targets is None else replace_targets)
        transition_fd = _open_existing_transition_directory(parent_fd, parent_path)
        if transition_fd is None:
            return None
        try:
            owners = _validate_transition_directory_fd(parent_fd, parent_path, transition_fd, allowed)
            owner = owners.get(sha256_bytes(name.encode("utf-8")))
            _validate_private_transition_directory(parent_path, transition_fd)
            return None if owner is None else owner[1]
        finally:
            os.close(transition_fd)


def completed_transition_matches(
    path: Path,
    *,
    transition_id: str,
    previous_data: bytes,
    data: bytes,
    mode: int = 0o600,
    replace_targets: set[str] | frozenset[str] | None = None,
) -> bool:
    """Verify exact durable provenance for one completed A->B replacement."""

    with _open_parent_fd(path, create=False) as (parent_fd, parent_path, name):
        allowed = frozenset({name} if replace_targets is None else replace_targets)
        if name not in allowed:
            raise ValueError("completed transition target must be declared")
        target_fd = _hold_regular(parent_fd, name, expected=data)
        transition_fd = completed_fd = -1
        completion_binding_fd = -1
        try:
            if not _public_is_private(target_fd, expected_mode=mode):
                raise BatchRuntimeError(
                    "unsafe_storage",
                    "completed transition target is not private immutable storage",
                )
            transition_fd = _open_existing_transition_directory(parent_fd, parent_path) or -1
            if transition_fd < 0:
                return False
            _validate_transition_directory_fd(
                parent_fd,
                parent_path,
                transition_fd,
                allowed,
            )
            completed_fd = _open_child_directory(
                transition_fd,
                _COMPLETED_DIRECTORY_NAME,
                create=False,
            )
            owner_raw = _transition_owner_bytes(
                os.fstat(parent_fd),
                os.fstat(transition_fd),
                name,
                transition_id,
                sha256_bytes(previous_data),
                len(previous_data),
                sha256_bytes(data),
                len(data),
                mode,
            )
            found = _lookup_completion(
                completed_fd,
                transition_id,
                _completion_bytes(owner_raw),
            )
            if found is not None:
                _status, completion_name, completion_raw, completion_binding_fd = found
            _transition_namespace_guard(
                parent_fd,
                parent_path,
                transition_fd,
                completed_fd,
            )
            if (
                not _held_regular_matches_name(parent_fd, name, target_fd)
                or not _held_regular_is_exact(target_fd, data, name=name)
                or (
                    found is not None
                    and (
                        not _named_matches_held_any_links(
                            completed_fd,
                            completion_name,
                            completion_binding_fd,
                        )
                        or not _held_exact_any_links(
                            completion_binding_fd,
                            completion_raw,
                            name=completion_name,
                        )
                    )
                )
            ):
                raise BatchRuntimeError(
                    "storage_path_changed",
                    "completed transition target changed during provenance check",
                )
            if found is not None:
                _validate_private_transition_file(
                    completion_binding_fd,
                    name=completion_name,
                    expected_mode=0o600,
                )
            _transition_namespace_guard(
                parent_fd,
                parent_path,
                transition_fd,
                completed_fd,
            )
            if (
                not _held_regular_matches_name(parent_fd, name, target_fd)
                or not _held_regular_is_exact(target_fd, data, name=name)
                or (
                    found is not None
                    and (
                        not _named_matches_held_any_links(
                            completed_fd,
                            completion_name,
                            completion_binding_fd,
                        )
                        or not _held_exact_any_links(
                            completion_binding_fd,
                            completion_raw,
                            name=completion_name,
                        )
                    )
                )
            ):
                raise BatchRuntimeError(
                    "storage_path_changed",
                    "completed transition target changed before provenance return",
                )
            if found is not None:
                _validate_private_transition_file(
                    completion_binding_fd,
                    name=completion_name,
                    expected_mode=0o600,
                )
            return found is not None and found[0] == "current"
        finally:
            if completion_binding_fd >= 0:
                os.close(completion_binding_fd)
            if completed_fd >= 0:
                os.close(completed_fd)
            if transition_fd >= 0:
                os.close(transition_fd)
            os.close(target_fd)


def _read_regular_single_link(
    parent_fd: int,
    name: str,
    *,
    code: str,
    max_bytes: int = MAX_OPAQUE_ARTIFACT_BYTES,
    fault: StorageFaultHook | None = None,
) -> bytes:
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    try:
        path_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise BatchRuntimeError(code, f"file does not exist: {name}") from exc
    if not stat.S_ISREG(path_before.st_mode) or path_before.st_nlink != 1:
        raise BatchRuntimeError(
            "unsafe_storage",
            f"file must be regular and single-link before open: {name}",
        )
    try:
        descriptor = os.open(name, _file_flags(os.O_RDONLY), dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise BatchRuntimeError(code, f"file does not exist: {name}") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise BatchRuntimeError("unsafe_path", f"file is a symlink or has an unsafe component: {name}") from exc
        raise BatchRuntimeError(code, f"cannot open file safely: {name}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise BatchRuntimeError("unsafe_storage", f"file must be regular and single-link: {name}")
        if (path_before.st_dev, path_before.st_ino) != (before.st_dev, before.st_ino):
            raise BatchRuntimeError("storage_path_changed", f"file path changed before read: {name}")
        if before.st_size > max_bytes:
            raise BatchRuntimeError(
                code,
                f"file exceeds its read limit of {max_bytes} bytes: {name}",
            )
        if fault is not None:
            fault("after_open")
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            request_size = min(READ_CHUNK_BYTES, max_bytes - total_bytes + 1)
            chunk = os.read(descriptor, request_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise BatchRuntimeError(
                    code,
                    f"file exceeded its read limit of {max_bytes} bytes: {name}",
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        try:
            path_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise BatchRuntimeError("storage_path_changed", f"file path disappeared during read: {name}") from exc
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise BatchRuntimeError("storage_path_changed", f"file changed while reading: {name}")
        if (path_after.st_dev, path_after.st_ino) != (after.st_dev, after.st_ino):
            raise BatchRuntimeError("storage_path_changed", f"file path changed during read: {name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_relative_bytes(
    directory_fd: int,
    relative_path: str,
    *,
    code: str = "artifact_unreadable",
    max_bytes: int | None = None,
) -> bytes:
    """Read one regular file relative to a held directory descriptor."""

    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative_path != relative.as_posix()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise BatchRuntimeError(
            "unsafe_path",
            f"unsafe relative artifact path: {relative_path}",
        )
    descriptor = os.dup(directory_fd)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise BatchRuntimeError(
                "unsafe_storage",
                "relative artifact anchor must be a directory",
            )
        for component in relative.parts[:-1]:
            child = _open_child_directory(descriptor, component, create=False)
            os.close(descriptor)
            descriptor = child
        return _read_regular_single_link(
            descriptor,
            relative.parts[-1],
            code=code,
            max_bytes=(MAX_OPAQUE_ARTIFACT_BYTES if max_bytes is None else max_bytes),
        )
    finally:
        os.close(descriptor)


def walk_relative_regular_files(
    directory_fd: int,
    relative_root: str,
    *,
    directory_memberships: dict[str, frozenset[str]] | None = None,
) -> set[str]:
    root = PurePosixPath(relative_root)
    if (
        root.is_absolute()
        or relative_root != root.as_posix()
        or any(part in {"", ".."} for part in root.parts)
    ):
        raise BatchRuntimeError("unsafe_path", f"unsafe relative directory path: {relative_root}")
    descriptor = os.dup(directory_fd)
    try:
        for component in (() if relative_root in {"", "."} else root.parts):
            child = _open_child_directory(descriptor, component, create=False)
            os.close(descriptor)
            descriptor = child
        found: set[str] = set()
        member_count = 0

        def walk(current_fd: int, prefix: PurePosixPath) -> None:
            nonlocal member_count
            before_names = tuple(
                _bounded_sorted_names(
                    current_fd,
                    max_entries=_MAX_BUNDLE_MEMBERS - member_count,
                    label="artifact bundle directory",
                )
            )
            for name in before_names:
                member_count += 1
                if member_count > _MAX_BUNDLE_MEMBERS:
                    raise BatchRuntimeError(
                        "resource_limit",
                        "artifact bundle has too many members",
                    )
                metadata = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                relative = prefix / name
                if stat.S_ISLNK(metadata.st_mode):
                    raise BatchRuntimeError("unsafe_storage", f"bundle contains symlink: {relative}")
                if stat.S_ISDIR(metadata.st_mode):
                    child_fd = _open_child_directory(current_fd, name, create=False)
                    try:
                        opened = os.fstat(child_fd)
                        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                            raise BatchRuntimeError("storage_path_changed", f"bundle directory changed: {relative}")
                        walk(child_fd, relative)
                        current = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                            raise BatchRuntimeError("storage_path_changed", f"bundle directory changed: {relative}")
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    found.add(relative.as_posix())
                else:
                    raise BatchRuntimeError("unsafe_storage", f"bundle entry is unsafe: {relative}")
            if tuple(
                _bounded_sorted_names(
                    current_fd,
                    max_entries=len(before_names),
                    label="artifact bundle directory",
                )
            ) != before_names:
                raise BatchRuntimeError("storage_path_changed", "bundle membership changed during walk")
            if directory_memberships is not None:
                directory_memberships[
                    "" if not prefix.parts else prefix.as_posix()
                ] = frozenset(before_names)

        walk(descriptor, PurePosixPath())
        return found
    finally:
        os.close(descriptor)


def read_bytes(
    path: Path,
    *,
    code: str = "artifact_unreadable",
    max_bytes: int | None = None,
    fault: StorageFaultHook | None = None,
) -> bytes:
    with _open_parent_fd(path, create=False) as (parent_fd, _parent, name):
        return _read_regular_single_link(
            parent_fd,
            name,
            code=code,
            max_bytes=(MAX_OPAQUE_ARTIFACT_BYTES if max_bytes is None else max_bytes),
            fault=fault,
        )


def read_json_bytes(
    path: Path,
    *,
    code: str = "artifact_unreadable",
    max_bytes: int | None = None,
) -> tuple[bytes, Any]:
    raw = read_bytes(
        path,
        code=code,
        max_bytes=(MAX_JSON_ARTIFACT_BYTES if max_bytes is None else max_bytes),
    )
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is forbidden: {value}")
    try:
        return raw, json.loads(raw, parse_constant=reject_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise BatchRuntimeError(code, f"invalid JSON file: {path}") from exc


def file_sha256(path: Path) -> str:
    return sha256_bytes(read_bytes(path, code="source_unreadable"))


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:  # pragma: no cover - defensive
            raise _storage_error("short write while persisting artifact")
        offset += written


def _rename_no_replace(parent_fd: int, source_name: str, target_name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    target = os.fsencode(target_name)
    result: int
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(parent_fd, source, parent_fd, target, 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(parent_fd, source, parent_fd, target, 0x00000001)  # RENAME_NOREPLACE
    elif os.name == "nt":  # pragma: no cover - Windows rename is no-replace
        try:
            os.rename(source_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except FileExistsError as exc:
            raise BatchRuntimeError("output_conflict", f"target was occupied concurrently: {target_name}") from exc
        return
    else:  # pragma: no cover - fail closed on unsupported platforms
        raise BatchRuntimeError("storage_unsupported", "atomic no-replace rename is unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise BatchRuntimeError("output_conflict", f"target was occupied concurrently: {target_name}")
        raise _storage_error(
            f"atomic no-replace rename failed: {source_name} -> {target_name}",
            OSError(error_number, os.strerror(error_number)),
        )


def _existing_entry(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _storage_error(f"cannot inspect storage entry: {name}", exc)


def _same_entry(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _read_held_descriptor(descriptor: int, *, max_bytes: int, code: str, name: str) -> bytes:
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            request_size = min(READ_CHUNK_BYTES, max_bytes - total_bytes + 1)
            chunk = os.read(descriptor, request_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise BatchRuntimeError(code, f"file exceeded its read limit of {max_bytes} bytes: {name}")
            chunks.append(chunk)
        return b"".join(chunks)
    except BatchRuntimeError:
        raise
    except OSError as exc:
        raise _storage_error(f"cannot read held storage file: {name}", exc)


def _held_regular_matches_name(parent_fd: int, name: str, descriptor: int) -> bool:
    named = _existing_entry(parent_fd, name)
    if named is None:
        return False
    try:
        held = os.fstat(descriptor)
    except OSError as exc:
        raise _storage_error(f"cannot inspect held storage file: {name}", exc)
    return (
        stat.S_ISREG(named.st_mode)
        and stat.S_ISREG(held.st_mode)
        and named.st_nlink == 1
        and held.st_nlink == 1
        and _same_entry(named, held)
    )


def _held_regular_is_exact(descriptor: int, expected: bytes, *, name: str) -> bool:
    try:
        before = os.fstat(descriptor)
    except OSError as exc:
        raise _storage_error(f"cannot inspect held storage file: {name}", exc)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != len(expected):
        return False
    raw = _read_held_descriptor(
        descriptor,
        max_bytes=len(expected),
        code="storage_error",
        name=name,
    )
    try:
        after = os.fstat(descriptor)
    except OSError as exc:
        raise _storage_error(f"cannot inspect held storage file after read: {name}", exc)
    stable_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_nlink,
    )
    stable_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_nlink,
    )
    return stable_before == stable_after and raw == expected


def _hold_regular(
    parent_fd: int,
    name: str,
    *,
    expected: bytes | None = None,
    code: str = "storage_error",
    writable: bool = False,
) -> int:
    before = _existing_entry(parent_fd, name)
    if before is None:
        raise BatchRuntimeError("storage_missing", f"storage entry disappeared: {name}")
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise BatchRuntimeError("unsafe_storage", f"storage entry is not regular single-link: {name}")
    try:
        descriptor = os.open(
            name,
            _file_flags(os.O_RDWR if writable else os.O_RDONLY),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise _storage_error(f"cannot hold storage entry safely: {name}", exc)
    try:
        if not _held_regular_matches_name(parent_fd, name, descriptor):
            raise BatchRuntimeError("storage_path_changed", f"storage entry changed while being held: {name}")
        held = os.fstat(descriptor)
        if not _same_entry(before, held):
            raise BatchRuntimeError("storage_path_changed", f"storage entry changed before hold: {name}")
        if expected is not None and not _held_regular_is_exact(descriptor, expected, name=name):
            raise BatchRuntimeError("storage_path_changed", f"storage entry bytes changed before mutation: {name}")
        if not _held_regular_matches_name(parent_fd, name, descriptor):
            raise BatchRuntimeError("storage_path_changed", f"storage entry changed during hold validation: {name}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _exchange_names_between(
    first_parent_fd: int,
    first_name: str,
    second_parent_fd: int,
    second_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    first = os.fsencode(first_name)
    second = os.fsencode(second_name)
    if sys.platform == "darwin":
        try:
            function = libc.renameatx_np
        except AttributeError as exc:  # pragma: no cover - platform libc mismatch
            raise BatchRuntimeError("storage_unsupported", "atomic name exchange is unavailable") from exc
    elif sys.platform.startswith("linux"):
        try:
            function = libc.renameat2
        except AttributeError as exc:  # pragma: no cover - platform libc mismatch
            raise BatchRuntimeError("storage_unsupported", "atomic name exchange is unavailable") from exc
    else:  # pragma: no cover - compare-and-swap requires a native exchange primitive
        raise BatchRuntimeError("storage_unsupported", "atomic name exchange is unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = function(first_parent_fd, first, second_parent_fd, second, 0x00000002)
    if result != 0:
        error_number = ctypes.get_errno()
        unsupported = {errno.ENOSYS, errno.ENOTSUP}
        if hasattr(errno, "EOPNOTSUPP"):
            unsupported.add(errno.EOPNOTSUPP)
        if error_number in unsupported:
            raise BatchRuntimeError("storage_unsupported", "atomic name exchange is unavailable")
        raise _storage_error(
            f"atomic name exchange failed: {first_name} <-> {second_name}",
            OSError(error_number, os.strerror(error_number)),
        )


def _exchange_names(parent_fd: int, first_name: str, second_name: str) -> None:
    _exchange_names_between(parent_fd, first_name, parent_fd, second_name)


def _resume_internal_writing(
    parent_fd: int,
    parent_path: Path,
    target_name: str,
    data: bytes,
    *,
    mode: int,
) -> tuple[str, int] | None:
    pattern = re.compile(rf"^\.{re.escape(target_name)}\.[0-9a-f]{{32}}\.writing$")
    names = _bounded_sorted_names(
        parent_fd,
        max_entries=_MAX_GENERAL_DIRECTORY_ENTRIES,
        label=f"staging directory for {target_name}",
    )
    candidates = [candidate for candidate in names if pattern.fullmatch(candidate)]
    if len(candidates) > 64:
        raise BatchRuntimeError("resource_limit", f"too many immutable write attempts for {target_name}")
    aggregate = 0
    resumable: list[tuple[str, bytes]] = []
    for candidate in candidates:
        metadata = _existing_entry(parent_fd, candidate)
        if metadata is None:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise BatchRuntimeError("unsafe_storage", f"internal writing entry is unsafe: {candidate}")
        aggregate += metadata.st_size
        if metadata.st_size > len(data) or aggregate > MAX_OPAQUE_ARTIFACT_BYTES:
            raise BatchRuntimeError("resource_limit", f"immutable write attempts are oversized for {target_name}")
        raw = _read_regular_single_link(
            parent_fd,
            candidate,
            code="unsafe_storage",
            max_bytes=len(data),
        )
        if raw == data or (raw and data.startswith(raw)):
            resumable.append((candidate, raw))
    if len(resumable) > 1:
        raise BatchRuntimeError(
            "unsafe_storage",
            f"multiple resumable immutable writes exist for {target_name}",
        )
    if not resumable:
        return None
    candidate, raw = resumable[0]
    descriptor = _hold_regular(
        parent_fd,
        candidate,
        expected=raw,
        writable=len(raw) < len(data),
    )
    try:
        actual_mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
        if actual_mode != mode:
            raise BatchRuntimeError(
                "unsafe_storage",
                f"resumed immutable write has the wrong mode: {candidate}",
            )
        if len(raw) < len(data):
            if (
                not _held_regular_matches_name(parent_fd, candidate, descriptor)
                or not _held_regular_is_exact(descriptor, raw, name=candidate)
            ):
                raise BatchRuntimeError(
                    "storage_path_changed",
                    f"partial immutable write changed before resume: {candidate}",
                )
            os.lseek(descriptor, len(raw), os.SEEK_SET)
            _write_all(descriptor, data[len(raw) :])
        # The original process may have died during write(2), or after write(2)
        # but before fsync(2). A non-empty exact prefix can be completed on its
        # held inode without allocating a second staging entry.
        _fsync(descriptor, label=str(parent_path / candidate))
        if (
            not _held_regular_matches_name(parent_fd, candidate, descriptor)
            or not _held_regular_is_exact(descriptor, data, name=candidate)
        ):
            raise BatchRuntimeError(
                "storage_path_changed",
                f"resumed immutable write is not exact: {candidate}",
            )
        return candidate, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def entry_exists(path: Path) -> bool:
    with _open_parent_fd(path, create=False) as (parent_fd, _parent, name):
        return _existing_entry(parent_fd, name) is not None


def internal_zero_tombstone(path: Path) -> bool:
    """Classify one caller-named internal staging leaf as logically deleted."""
    with _open_parent_fd(path, create=False) as (parent_fd, _parent, name):
        metadata = _existing_entry(parent_fd, name)
        if metadata is None:
            return False
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BatchRuntimeError("unsafe_storage", f"internal tombstone is unsafe: {path}")
        if metadata.st_size != 0:
            return False
        return _read_regular_single_link(
            parent_fd,
            name,
            code="unsafe_storage",
            max_bytes=0,
        ) == b""


def entry_exists_allow_missing_parent(path: Path) -> bool:
    normalized = normalized_absolute_path(path)
    descriptor = os.open("/", _directory_flags())
    try:
        for component in normalized.parent.parts[1:]:
            try:
                child = _open_child_directory(descriptor, component, create=False)
            except BatchRuntimeError as exc:
                if exc.code == "storage_missing":
                    return False
                raise
            os.close(descriptor)
            descriptor = child
        return _existing_entry(descriptor, normalized.name) is not None
    finally:
        os.close(descriptor)


def validate_parent_directory(path: Path) -> None:
    with _open_parent_fd(path, create=False):
        return None


def publish_bytes_no_replace(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    allow_existing_exact: bool = False,
    create_parent: bool = False,
    fault: StorageFaultHook | None = None,
    guard: Callable[[], None] | None = None,
    precommit_guard: Callable[[], None] | None = None,
) -> None:
    with _open_parent_fd(path, create=create_parent) as (parent_fd, parent_path, name):
        existing = _existing_entry(parent_fd, name)
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                raise BatchRuntimeError("output_conflict", f"existing target is not regular single-link: {path}")
            if allow_existing_exact and _read_regular_single_link(
                parent_fd,
                name,
                code="output_conflict",
                max_bytes=len(data),
            ) == data:
                if guard is not None:
                    guard()
                return
            raise BatchRuntimeError("output_conflict", f"target is already occupied: {path}")

        temporary_name = f".{name}.{sha256_bytes(data)}.tmp"
        writing_name = ""
        descriptor = -1
        try:
            temporary = _existing_entry(parent_fd, temporary_name)
            if temporary is not None:
                descriptor = _hold_regular(parent_fd, temporary_name, expected=data)
            else:
                resumed = _resume_internal_writing(
                    parent_fd,
                    parent_path,
                    name,
                    data,
                    mode=mode,
                )
                if resumed is not None:
                    writing_name, descriptor = resumed
                else:
                    writing_name = f".{name}.{secrets.token_hex(16)}.writing"
                    descriptor = os.open(
                        writing_name,
                        _file_flags(os.O_RDWR | os.O_CREAT | os.O_EXCL),
                        mode,
                        dir_fd=parent_fd,
                    )
                    os.fchmod(descriptor, mode)
                    _write_all(descriptor, data)
                    if fault is not None:
                        fault("before_file_fsync")
                        if guard is not None:
                            guard()
                    if fault is not None:
                        fault("after_write_before_fsync")
                        if guard is not None:
                            guard()
                    _fsync(descriptor, label=str(path))
                    if fault is not None:
                        fault("after_writing_fsync")
                        if guard is not None:
                            guard()
                if (
                    not _held_regular_matches_name(parent_fd, writing_name, descriptor)
                    or not _held_regular_is_exact(descriptor, data, name=writing_name)
                ):
                    raise BatchRuntimeError("storage_path_changed", f"writing file changed before staging: {writing_name}")
                _rename_no_replace(parent_fd, writing_name, temporary_name)
                writing_name = ""
                if not _held_regular_matches_name(parent_fd, temporary_name, descriptor):
                    raise BatchRuntimeError("storage_path_changed", f"staging rename changed inode: {temporary_name}")
                _verify_directory_binding(parent_path, parent_fd)
                _fsync(parent_fd, label=str(parent_path))
                if fault is not None:
                    fault("after_file_fsync")
                    if guard is not None:
                        guard()
                if fault is not None:
                    fault("after_pending_rename")
                    if guard is not None:
                        guard()
            if (
                not _held_regular_matches_name(parent_fd, temporary_name, descriptor)
                or not _held_regular_is_exact(descriptor, data, name=temporary_name)
            ):
                raise BatchRuntimeError("storage_path_changed", f"staging file changed before publication: {temporary_name}")
            if guard is not None:
                guard()
            if precommit_guard is not None:
                precommit_guard()
            _rename_no_replace(parent_fd, temporary_name, name)
            if guard is not None:
                guard()
            published = _existing_entry(parent_fd, name)
            public_is_owned = (
                published is not None
                and _held_regular_matches_name(parent_fd, name, descriptor)
            )
            if not public_is_owned or not _held_regular_is_exact(descriptor, data, name=name):
                raise BatchRuntimeError("storage_path_changed", f"published target is not the held staging inode: {path}")
            temporary_name = ""
            if fault is not None:
                fault("after_rename")
                if guard is not None:
                    guard()
            if fault is not None:
                fault("before_parent_fsync")
                if guard is not None:
                    guard()
            _verify_directory_binding(parent_path, parent_fd)
            _fsync(parent_fd, label=str(parent_path))
            if guard is not None:
                guard()
            if (
                not _held_regular_matches_name(parent_fd, name, descriptor)
                or not _held_regular_is_exact(descriptor, data, name=name)
            ):
                raise BatchRuntimeError(
                    "storage_path_changed",
                    f"published target changed after its parent fsync: {path}",
                )
        except BatchRuntimeError:
            raise
        except OSError as exc:
            raise _storage_error(f"cannot publish artifact safely: {path}", exc)
        finally:
            if descriptor >= 0:
                os.close(descriptor)


_OWNER_NAME_PATTERN = re.compile(r"^owner\.([0-9a-f]{64})\.json$")
_OWNER_WRITING_PATTERN = re.compile(r"^\.owner\.([0-9a-f]{64})\.json\.[0-9a-f]{32}\.writing$")
_TRANSITION_NAME_PATTERN = re.compile(
    r"^payload\.([0-9a-f]{64})\.transition$"
)
_TRANSITION_WRITING_PATTERN = re.compile(
    r"^\.payload\.([0-9a-f]{64})\.transition\.[0-9a-f]{32}\.writing$"
)
_COMPLETION_SCHEMA_VERSION = "paper_reader_batch.storage-transition-completion.v2-internal"
_COMPLETED_DIRECTORY_NAME = "completed"
_COMPLETION_NAME_PATTERN = re.compile(r"^([0-9a-f]{64})\.json$")
_COMPLETION_WRITING_PATTERN = re.compile(
    r"^\.([0-9a-f]{64}\.json)\.[0-9a-f]{32}\.writing$"
)
_RETIRED_NAME_PATTERN = re.compile(
    r"^\.retired\.([0-9a-f]{64})\.([0-9a-f]{32})\.artifact$"
)
_MAX_COMPLETION_ENTRIES = 100_000
_MAX_COMPLETION_AGGREGATE_BYTES = 64 * 1024 * 1024
_MAX_TRANSITION_ENTRIES = 100_000


def _transition_owner_bytes(
    parent_metadata: os.stat_result,
    transition_metadata: os.stat_result,
    target_name: str,
    transition_id: str,
    old_sha256: str,
    old_size: int,
    new_sha256: str,
    new_size: int,
    mode: int,
) -> bytes:
    return canonical_json_bytes(
        {
            "parent_device": parent_metadata.st_dev,
            "parent_inode": parent_metadata.st_ino,
            "schema_version": _TRANSITION_SCHEMA_VERSION,
            "target": target_name,
            "transition_id": transition_id,
            "old_sha256": old_sha256,
            "old_size": old_size,
            "new_sha256": new_sha256,
            "new_size": new_size,
            "mode": mode,
            "transition_device": transition_metadata.st_dev,
            "transition_inode": transition_metadata.st_ino,
        }
    )


def _completion_bytes(owner_raw: bytes) -> bytes:
    try:
        value = json.loads(owner_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover - owner validated first
        raise BatchRuntimeError("unsafe_storage", "active transition owner is invalid") from exc
    value["schema_version"] = _COMPLETION_SCHEMA_VERSION
    return canonical_json_bytes(value)


def _validated_completion_payload(
    raw: bytes,
    name: str,
    *,
    parent_metadata: os.stat_result,
    transition_metadata: os.stat_result,
    allowed_by_hash: dict[str, str],
) -> dict[str, Any]:
    try:
        payload = json.loads(
            raw,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BatchRuntimeError(
            "unsafe_storage",
            f"completed transition marker is invalid JSON: {name}",
        ) from exc
    expected_keys = {
        "mode",
        "new_sha256",
        "new_size",
        "old_sha256",
        "old_size",
        "parent_device",
        "parent_inode",
        "schema_version",
        "target",
        "transition_id",
        "transition_device",
        "transition_inode",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise BatchRuntimeError(
            "unsafe_storage",
            f"completed transition marker shape is invalid: {name}",
        )
    target_name = payload.get("target")
    transition_id = payload.get("transition_id")
    old_sha256 = payload.get("old_sha256")
    old_size = payload.get("old_size")
    new_sha256 = payload.get("new_sha256")
    new_size = payload.get("new_size")
    mode = payload.get("mode")
    target_hash = (
        sha256_bytes(target_name.encode("utf-8"))
        if isinstance(target_name, str)
        else ""
    )
    marker_match = _COMPLETION_NAME_PATTERN.fullmatch(name)
    if (
        marker_match is None
        or not isinstance(target_name, str)
        or target_name in {"", ".", ".."}
        or "/" in target_name
        or allowed_by_hash.get(target_hash) != target_name
        or not isinstance(transition_id, str)
        or not transition_id
        or len(transition_id.encode("utf-8")) > 1024
        or marker_match.group(1) != sha256_bytes(transition_id.encode("utf-8"))
        or not isinstance(old_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", old_sha256) is None
        or type(old_size) is not int
        or old_size < 0
        or not isinstance(new_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", new_sha256) is None
        or type(new_size) is not int
        or new_size < 0
        or type(mode) is not int
        or mode < 0
        or mode > 0o777
        or mode & 0o077
    ):
        raise BatchRuntimeError(
            "unsafe_storage",
            f"completed transition marker binding is invalid: {name}",
        )
    expected_owner = _transition_owner_bytes(
        parent_metadata,
        transition_metadata,
        target_name,
        transition_id,
        old_sha256,
        old_size,
        new_sha256,
        new_size,
        mode,
    )
    if raw != _completion_bytes(expected_owner):
        raise BatchRuntimeError(
            "unsafe_storage",
            f"completed transition marker is not canonical or storage-bound: {name}",
        )
    return payload


def _open_existing_transition_directory(parent_fd: int, parent_path: Path) -> int | None:
    if _existing_entry(parent_fd, _TRANSITION_DIRECTORY_NAME) is None:
        return None
    descriptor = _open_child_directory(parent_fd, _TRANSITION_DIRECTORY_NAME, create=False)
    try:
        _validate_private_transition_directory(parent_path, descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _preflight_completed_transition_directory(
    parent_path: Path,
    completed_fd: int,
) -> list[str]:
    """Bound the complete child namespace before reading any transition leaf."""

    _validate_private_transition_directory(
        parent_path / _TRANSITION_DIRECTORY_NAME,
        completed_fd,
        child_name=_COMPLETED_DIRECTORY_NAME,
    )
    names = _bounded_sorted_names(
        completed_fd,
        max_entries=_MAX_COMPLETION_ENTRIES,
        label="completed transition namespace",
    )
    if len(names) > _MAX_COMPLETION_ENTRIES:
        raise BatchRuntimeError("resource_limit", "too many completed transition entries")

    aggregate_bytes = 0
    writing_counts: dict[str, int] = {}
    for name in names:
        metadata = _existing_entry(completed_fd, name)
        if metadata is None:
            raise BatchRuntimeError(
                "storage_path_changed",
                f"completed transition entry disappeared: {name}",
            )
        marker_match = _COMPLETION_NAME_PATTERN.fullmatch(name)
        writing_match = _COMPLETION_WRITING_PATTERN.fullmatch(name)
        retired_match = _RETIRED_NAME_PATTERN.fullmatch(name)
        if marker_match is None and writing_match is None and retired_match is None:
            raise BatchRuntimeError(
                "unsafe_storage",
                f"unknown completed transition entry: {name}",
            )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise BatchRuntimeError(
                "unsafe_storage",
                f"completed transition entry is not private single-link storage: {name}",
            )
        if metadata.st_size > 4096:
            raise BatchRuntimeError(
                "resource_limit",
                f"completed transition entry is oversized: {name}",
            )
        aggregate_bytes += metadata.st_size
        if aggregate_bytes > _MAX_COMPLETION_AGGREGATE_BYTES:
            raise BatchRuntimeError(
                "resource_limit",
                "completed transition namespace exceeds 64 MiB",
            )
        if writing_match is not None:
            marker_name = writing_match.group(1)
            writing_counts[marker_name] = writing_counts.get(marker_name, 0) + 1
            if writing_counts[marker_name] > 1:
                raise BatchRuntimeError(
                    "unsafe_storage",
                    f"multiple completed transition attempts exist for {marker_name}",
                )
    return names


def _validate_completed_transition_directory(
    parent_fd: int,
    parent_path: Path,
    transition_fd: int,
    completed_fd: int,
    allowed_by_hash: dict[str, str],
    owners: dict[str, tuple[str, bytes, str]],
    *,
    require_active_completion: bool = False,
) -> None:
    completed_path = parent_path / _TRANSITION_DIRECTORY_NAME / _COMPLETED_DIRECTORY_NAME
    names = _preflight_completed_transition_directory(parent_path, completed_fd)

    active_completion_by_hash: dict[str, bytes] = {}
    for _target_hash, (_owner_name, owner_raw, _target_name) in owners.items():
        owner_payload = json.loads(owner_raw)
        transition_hash = sha256_bytes(owner_payload["transition_id"].encode("utf-8"))
        expected = _completion_bytes(owner_raw)
        existing = active_completion_by_hash.get(transition_hash)
        if existing is not None and existing != expected:
            raise BatchRuntimeError(
                "unsafe_storage",
                "active transitions reuse one completion identity",
            )
        active_completion_by_hash[transition_hash] = expected

    parent_metadata = os.fstat(parent_fd)
    transition_metadata = os.fstat(transition_fd)
    final_markers_by_target: dict[str, list[bytes]] = {}
    final_markers_by_transition_hash: dict[str, bytes] = {}
    writing_by_transition_hash: dict[str, list[bytes]] = {}
    for name in names:
        raw = _read_regular_single_link(
            completed_fd,
            name,
            code="unsafe_storage",
            max_bytes=4096,
        )
        retired_match = _RETIRED_NAME_PATTERN.fullmatch(name)
        if retired_match is not None:
            if sha256_bytes(raw) != retired_match.group(1):
                raise BatchRuntimeError(
                    "unsafe_storage",
                    f"retired completion bytes do not match their immutable name: {name}",
                )
            continue
        writing_match = _COMPLETION_WRITING_PATTERN.fullmatch(name)
        if writing_match is not None:
            transition_hash = writing_match.group(1).removesuffix(".json")
            expected = active_completion_by_hash.get(transition_hash)
            if expected is None or not expected.startswith(raw):
                raise BatchRuntimeError(
                    "unsafe_storage",
                    f"completed transition attempt is not an exact owner-bound prefix: {name}",
                )
            writing_by_transition_hash.setdefault(transition_hash, []).append(raw)
            continue
        payload = _validated_completion_payload(
            raw,
            name,
            parent_metadata=parent_metadata,
            transition_metadata=transition_metadata,
            allowed_by_hash=allowed_by_hash,
        )
        target_name = payload["target"]
        final_markers_by_target.setdefault(target_name, []).append(raw)
        final_markers_by_transition_hash[
            sha256_bytes(payload["transition_id"].encode("utf-8"))
        ] = raw

    for target_name, marker_bytes in final_markers_by_target.items():
        if len(marker_bytes) > 2:
            raise BatchRuntimeError(
                "unsafe_storage",
                f"too many completed transition markers for target: {target_name}",
            )
        if len(marker_bytes) == 2:
            target_hash = sha256_bytes(target_name.encode("utf-8"))
            active_owner = owners.get(target_hash)
            if active_owner is None:
                raise BatchRuntimeError(
                    "unsafe_storage",
                    f"multiple completed transition markers lack an active owner: {target_name}",
                )
            active_completion = _completion_bytes(active_owner[1])
            if sum(raw == active_completion for raw in marker_bytes) != 1:
                raise BatchRuntimeError(
                    "unsafe_storage",
                    f"completion crash window is not bound to the active owner: {target_name}",
                )

    if require_active_completion:
        for transition_hash, expected in active_completion_by_hash.items():
            if final_markers_by_transition_hash.get(transition_hash) == expected:
                continue
            writing = writing_by_transition_hash.get(transition_hash, [])
            if len(writing) == 1 and expected.startswith(writing[0]):
                continue
            raise BatchRuntimeError(
                "storage_path_changed",
                "active transition lacks its exact final completion marker or unique writing prefix",
            )

    _validate_private_transition_directory(
        parent_path / _TRANSITION_DIRECTORY_NAME,
        completed_fd,
        child_name=_COMPLETED_DIRECTORY_NAME,
    )
    _verify_directory_binding(completed_path, completed_fd)


def _validate_transition_directory_fd(
    parent_fd: int,
    parent_path: Path,
    transition_fd: int,
    allowed_targets: set[str] | frozenset[str],
    *,
    recovery_expected: dict[str, tuple[bytes, bytes]] | None = None,
) -> dict[str, tuple[str, bytes, str]]:
    """Validate the hidden directory as a closed immutable namespace."""

    _validate_private_transition_directory(parent_path, transition_fd)
    names = _bounded_sorted_names(
        transition_fd,
        max_entries=_MAX_TRANSITION_ENTRIES,
        label="immutable transition directory",
    )
    if len(names) > 100_000:
        raise BatchRuntimeError("resource_limit", "too many immutable transition entries")

    allowed_by_hash: dict[str, str] = {}
    for target_name in allowed_targets:
        if target_name in {"", ".", ".."} or "/" in target_name:
            raise ValueError("replace target must be one safe basename")
        target_hash = sha256_bytes(target_name.encode("utf-8"))
        if target_hash in allowed_by_hash:
            raise ValueError("replace targets have a hash collision")
        allowed_by_hash[target_hash] = target_name

    # Bound the complete namespace from stat metadata before reading any
    # attacker-controlled leaf. JSON transitions use the structured artifact
    # limit; only explicitly opaque targets may use the larger ceiling.
    metadata_by_name: dict[str, os.stat_result] = {}
    aggregate_bytes = 0
    active_counts: dict[str, int] = {}
    for name in names:
        metadata = _existing_entry(transition_fd, name)
        if metadata is None:
            raise BatchRuntimeError("storage_path_changed", f"transition entry disappeared: {name}")
        if name == _COMPLETED_DIRECTORY_NAME:
            if not stat.S_ISDIR(metadata.st_mode):
                raise BatchRuntimeError("unsafe_storage", "completed transition namespace is not a directory")
            completed_fd = _open_child_directory(transition_fd, name, create=False)
            try:
                _validate_private_transition_directory(
                    parent_path / _TRANSITION_DIRECTORY_NAME,
                    completed_fd,
                    child_name=_COMPLETED_DIRECTORY_NAME,
                )
            finally:
                os.close(completed_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BatchRuntimeError("unsafe_storage", f"transition entry is unsafe: {name}")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise BatchRuntimeError("unsafe_storage", f"transition entry is not private: {name}")
        owner_match = _OWNER_NAME_PATTERN.fullmatch(name)
        owner_writing = _OWNER_WRITING_PATTERN.fullmatch(name)
        transition_match = _TRANSITION_NAME_PATTERN.fullmatch(name)
        writing_match = _TRANSITION_WRITING_PATTERN.fullmatch(name)
        retired_match = _RETIRED_NAME_PATTERN.fullmatch(name)
        if owner_match is not None or owner_writing is not None:
            matched_hash = (owner_match or owner_writing).group(1)  # type: ignore[union-attr]
            if matched_hash not in allowed_by_hash:
                raise BatchRuntimeError("unsafe_storage", f"unauthorized transition owner entry: {name}")
            leaf_limit = 4096
        elif transition_match is not None or writing_match is not None:
            matched = transition_match or writing_match
            assert matched is not None
            target = allowed_by_hash.get(matched.group(1))
            if target is None:
                raise BatchRuntimeError("unsafe_storage", f"unauthorized transition entry: {name}")
            leaf_limit = (
                MAX_JSON_ARTIFACT_BYTES
                if target.casefold().endswith(".json")
                else MAX_OPAQUE_ARTIFACT_BYTES
            )
            matched_hash = matched.group(1)
        elif retired_match is not None:
            leaf_limit = MAX_OPAQUE_ARTIFACT_BYTES
            matched_hash = None
        else:
            raise BatchRuntimeError("unsafe_storage", f"unknown immutable transition entry: {name}")
        if metadata.st_size > leaf_limit:
            raise BatchRuntimeError("resource_limit", f"immutable transition entry is oversized: {name}")
        aggregate_bytes += metadata.st_size
        if aggregate_bytes > MAX_OPAQUE_ARTIFACT_BYTES:
            raise BatchRuntimeError("resource_limit", "immutable transition directory exceeds 512 MiB")
        metadata_by_name[name] = metadata
        if matched_hash is not None:
            active_counts[matched_hash] = active_counts.get(matched_hash, 0) + 1
            if active_counts[matched_hash] > 130:
                raise BatchRuntimeError("resource_limit", "too many active transition attempts for one target")

    if _COMPLETED_DIRECTORY_NAME not in names:
        raise BatchRuntimeError("unsafe_storage", "completed transition namespace is missing")

    completed_preflight_fd = _open_child_directory(
        transition_fd,
        _COMPLETED_DIRECTORY_NAME,
        create=False,
    )
    try:
        _preflight_completed_transition_directory(parent_path, completed_preflight_fd)
    finally:
        os.close(completed_preflight_fd)

    owners: dict[str, tuple[str, bytes, str]] = {}
    parent_metadata = os.fstat(parent_fd)
    transition_metadata = os.fstat(transition_fd)
    for name in names:
        owner_match = _OWNER_NAME_PATTERN.fullmatch(name)
        if owner_match is None:
            continue
        raw = _read_regular_single_link(
            transition_fd,
            name,
            code="unsafe_storage",
            max_bytes=4096,
        )
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchRuntimeError("unsafe_storage", f"transition owner is invalid JSON: {name}") from exc
        expected_keys = {
            "mode",
            "new_sha256",
            "new_size",
            "old_sha256",
            "old_size",
            "parent_device",
            "parent_inode",
            "schema_version",
            "target",
            "transition_id",
            "transition_device",
            "transition_inode",
        }
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise BatchRuntimeError("unsafe_storage", f"transition owner shape is invalid: {name}")
        target_name = value.get("target")
        if (
            not isinstance(target_name, str)
            or target_name in {"", ".", ".."}
            or "/" in target_name
        ):
            raise BatchRuntimeError("unsafe_storage", f"transition owner target is invalid: {name}")
        target_hash = sha256_bytes(target_name.encode("utf-8"))
        transition_id = value.get("transition_id")
        old_sha256 = value.get("old_sha256")
        old_size = value.get("old_size")
        new_sha256 = value.get("new_sha256")
        new_size = value.get("new_size")
        mode = value.get("mode")
        if (
            not isinstance(transition_id, str)
            or not transition_id
            or len(transition_id.encode("utf-8")) > 1024
            or not isinstance(old_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", old_sha256) is None
            or type(old_size) is not int
            or old_size < 0
            or not isinstance(new_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", new_sha256) is None
            or type(new_size) is not int
            or new_size < 0
            or type(mode) is not int
            or mode < 0
            or mode > 0o777
            or mode & 0o077
        ):
            raise BatchRuntimeError("unsafe_storage", f"transition owner mapping is invalid: {name}")
        expected = _transition_owner_bytes(
            parent_metadata,
            transition_metadata,
            target_name,
            transition_id,
            old_sha256,
            old_size,
            new_sha256,
            new_size,
            mode,
        )
        if (
            owner_match.group(1) != target_hash
            or allowed_by_hash.get(target_hash) != target_name
            or raw != expected
        ):
            raise BatchRuntimeError("unsafe_storage", f"transition owner binding is invalid: {name}")
        descriptor = _hold_regular(transition_fd, name, expected=expected)
        try:
            _validate_private_transition_file(descriptor, name=name, expected_mode=0o600)
        finally:
            os.close(descriptor)
        owners[target_hash] = (name, expected, target_name)

    for name in names:
        if name == _COMPLETED_DIRECTORY_NAME:
            continue
        metadata = metadata_by_name[name]
        if _OWNER_NAME_PATTERN.fullmatch(name):
            continue
        retired_match = _RETIRED_NAME_PATTERN.fullmatch(name)
        if retired_match is not None:
            raw = _read_regular_single_link(
                transition_fd,
                name,
                code="unsafe_storage",
                max_bytes=MAX_OPAQUE_ARTIFACT_BYTES,
            )
            if sha256_bytes(raw) != retired_match.group(1):
                raise BatchRuntimeError(
                    "unsafe_storage",
                    f"retired transition bytes do not match their immutable name: {name}",
                )
            continue
        owner_writing = _OWNER_WRITING_PATTERN.fullmatch(name)
        if owner_writing is not None:
            target_hash = owner_writing.group(1)
            if target_hash not in allowed_by_hash:
                raise BatchRuntimeError("unsafe_storage", f"unauthorized transition owner attempt: {name}")
            if metadata.st_size > 4096:
                raise BatchRuntimeError("resource_limit", f"transition owner attempt is oversized: {name}")
            expected_owner = (
                owners[target_hash][1]
                if target_hash in owners
                else (recovery_expected or {}).get(target_hash, (None, None))[0]
            )
            if expected_owner is not None:
                raw = _read_regular_single_link(
                    transition_fd,
                    name,
                    code="unsafe_storage",
                    max_bytes=len(expected_owner),
                )
                if not expected_owner.startswith(raw):
                    raise BatchRuntimeError(
                        "unsafe_storage",
                        f"transition owner attempt is not an exact expected prefix: {name}",
                    )
            continue
        transition_match = _TRANSITION_NAME_PATTERN.fullmatch(name)
        writing_match = _TRANSITION_WRITING_PATTERN.fullmatch(name)
        if transition_match is None and writing_match is None:
            raise BatchRuntimeError("unsafe_storage", f"unknown immutable transition entry: {name}")
        matched = transition_match or writing_match
        assert matched is not None
        if transition_match is not None:
            target_hash = transition_match.group(1)
            if target_hash not in owners:
                raise BatchRuntimeError("unsafe_storage", f"transition has no immutable owner: {name}")
            owner_payload = json.loads(owners[target_hash][1])
            raw = _read_regular_single_link(
                transition_fd,
                name,
                code="unsafe_storage",
                max_bytes=(
                    MAX_JSON_ARTIFACT_BYTES
                    if owners[target_hash][2].casefold().endswith(".json")
                    else MAX_OPAQUE_ARTIFACT_BYTES
                ),
            )
            if (sha256_bytes(raw), len(raw)) not in {
                (owner_payload["old_sha256"], owner_payload["old_size"]),
                (owner_payload["new_sha256"], owner_payload["new_size"]),
            }:
                raise BatchRuntimeError("unsafe_storage", f"transition bytes do not match its endpoints: {name}")
        else:
            target_hash = matched.group(1)
            if target_hash not in owners:
                raise BatchRuntimeError("unsafe_storage", f"transition attempt has no immutable owner: {name}")
            if metadata.st_size > MAX_OPAQUE_ARTIFACT_BYTES:
                raise BatchRuntimeError("resource_limit", f"transition attempt is oversized: {name}")
            expected_payload = (recovery_expected or {}).get(target_hash, (None, None))[1]
            owner_payload = json.loads(owners[target_hash][1])
            if metadata.st_size > owner_payload["new_size"]:
                raise BatchRuntimeError(
                    "unsafe_storage",
                    f"transition payload attempt exceeds its owner endpoint: {name}",
                )
            if expected_payload is not None:
                raw = _read_regular_single_link(
                    transition_fd,
                    name,
                    code="unsafe_storage",
                    max_bytes=len(expected_payload),
                )
                if not expected_payload.startswith(raw):
                    raise BatchRuntimeError(
                        "unsafe_storage",
                        f"transition payload attempt is not an exact expected prefix: {name}",
                    )

    completed_fd = _open_child_directory(
        transition_fd,
        _COMPLETED_DIRECTORY_NAME,
        create=False,
    )
    try:
        _validate_completed_transition_directory(
            parent_fd,
            parent_path,
            transition_fd,
            completed_fd,
            allowed_by_hash,
            owners,
        )
    finally:
        os.close(completed_fd)
    _validate_private_transition_directory(parent_path, transition_fd)
    return owners


def _validate_transition_directory(
    parent_fd: int,
    parent_path: Path,
    allowed_targets: set[str] | frozenset[str],
) -> set[str]:
    transition_fd = _open_existing_transition_directory(parent_fd, parent_path)
    if transition_fd is None:
        return set()
    try:
        _validate_transition_directory_fd(parent_fd, parent_path, transition_fd, allowed_targets)
        allowed_by_hash = {
            sha256_bytes(target.encode("utf-8")): target for target in allowed_targets
        }
        active: set[str] = set()
        for name in _bounded_sorted_names(
            transition_fd,
            max_entries=_MAX_TRANSITION_ENTRIES,
            label="immutable transition directory",
        ):
            if name == _COMPLETED_DIRECTORY_NAME:
                continue
            if _RETIRED_NAME_PATTERN.fullmatch(name) is not None:
                continue
            matched = (
                _OWNER_NAME_PATTERN.fullmatch(name)
                or _OWNER_WRITING_PATTERN.fullmatch(name)
                or _TRANSITION_NAME_PATTERN.fullmatch(name)
                or _TRANSITION_WRITING_PATTERN.fullmatch(name)
            )
            if matched is None or matched.group(1) not in allowed_by_hash:
                raise BatchRuntimeError("unsafe_storage", f"unknown active transition entry: {name}")
            active.add(allowed_by_hash[matched.group(1)])
        _validate_private_transition_directory(parent_path, transition_fd)
        return active
    finally:
        os.close(transition_fd)


def _read_pending_transitions_from_parent(
    parent_fd: int,
    parent_path: Path,
    target_name: str,
    *,
    max_bytes: int,
    allowed_targets: set[str] | frozenset[str],
    committed: bool = False,
) -> list[tuple[bytes, bytes, str]]:
    transition_fd = _open_existing_transition_directory(parent_fd, parent_path)
    if transition_fd is None:
        return []
    owner_fd = -1
    public_fd = -1
    try:
        owners = _validate_transition_directory_fd(
            parent_fd,
            parent_path,
            transition_fd,
            allowed_targets,
        )
        target_hash = sha256_bytes(target_name.encode("utf-8"))
        owner = owners.get(target_hash)
        if owner is None:
            _validate_private_transition_directory(parent_path, transition_fd)
            return []
        owner_name, owner_expected, _owned_target = owner
        owner_payload = _active_owner_payload(owner_expected)
        owner_fd = _hold_regular(transition_fd, owner_name, expected=owner_expected)
        public_raw = _read_regular_single_link(
            parent_fd,
            target_name,
            code="unsafe_storage",
            max_bytes=max_bytes,
        )
        public_fd = _hold_regular(parent_fd, target_name, expected=public_raw)
        public_sha256 = sha256_bytes(public_raw)
        pending: list[tuple[bytes, bytes, str]] = []
        for transition_name in _bounded_sorted_names(
            transition_fd,
            max_entries=_MAX_TRANSITION_ENTRIES,
            label="immutable transition directory",
        ):
            matched = _TRANSITION_NAME_PATTERN.fullmatch(transition_name)
            if matched is None or matched.group(1) != target_hash:
                continue
            _target_hash = matched.group(1)
            old_sha256 = owner_payload["old_sha256"]
            old_size = owner_payload["old_size"]
            new_sha256 = owner_payload["new_sha256"]
            new_size = owner_payload["new_size"]
            transition_raw = _read_regular_single_link(
                transition_fd,
                transition_name,
                code="unsafe_storage",
                max_bytes=max_bytes,
            )
            transition_sha256 = sha256_bytes(transition_raw)
            transition_endpoint = (transition_sha256, len(transition_raw))
            public_endpoint = (public_sha256, len(public_raw))
            if transition_endpoint not in {
                (old_sha256, int(old_size)),
                (new_sha256, int(new_size)),
            }:
                raise BatchRuntimeError(
                    "unsafe_storage",
                    f"transition bytes do not match its endpoints: {transition_name}",
                )
            if (
                not committed
                and public_endpoint == (old_sha256, int(old_size))
                and transition_endpoint == (new_sha256, int(new_size))
            ):
                pending.append((public_raw, transition_raw, transition_name))
            elif (
                committed
                and public_endpoint == (new_sha256, int(new_size))
                and transition_endpoint == (old_sha256, int(old_size))
            ):
                pending.append((public_raw, transition_raw, transition_name))
        _transition_owner_guard(
            parent_fd,
            parent_path,
            transition_fd,
            owner_name,
            owner_fd,
            owner_expected,
        )
        if not _held_regular_matches_name(parent_fd, target_name, public_fd):
            raise BatchRuntimeError("storage_path_changed", f"transition target changed during inspection: {target_name}")
        _transition_owner_guard(
            parent_fd,
            parent_path,
            transition_fd,
            owner_name,
            owner_fd,
            owner_expected,
        )
        return pending
    finally:
        if public_fd >= 0:
            os.close(public_fd)
        if owner_fd >= 0:
            os.close(owner_fd)
        os.close(transition_fd)


def _validate_private_transition_directory(
    parent_path: Path,
    descriptor: int,
    *,
    child_name: str = _TRANSITION_DIRECTORY_NAME,
) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise BatchRuntimeError("unsafe_storage", "transition directory is not private runtime storage")
    _verify_directory_binding(parent_path / child_name, descriptor)


def _transition_namespace_guard(
    parent_fd: int,
    parent_path: Path,
    transition_fd: int,
    completed_fd: int,
) -> None:
    """Keep both published namespace levels bound to their held descriptors."""

    _verify_directory_binding(parent_path, parent_fd)
    _validate_private_transition_directory(parent_path, transition_fd)
    _validate_private_transition_directory(
        parent_path / _TRANSITION_DIRECTORY_NAME,
        completed_fd,
        child_name=_COMPLETED_DIRECTORY_NAME,
    )


def _transition_public_guard(
    parent_fd: int,
    parent_path: Path,
    target_name: str,
    target_fd: int,
    expected: bytes,
    *,
    expected_mode: int,
    transition_fd: int,
    completed_fd: int,
) -> None:
    _transition_namespace_guard(
        parent_fd,
        parent_path,
        transition_fd,
        completed_fd,
    )
    if (
        not _public_is_private(target_fd, expected_mode=expected_mode)
        or not _held_regular_matches_name(parent_fd, target_name, target_fd)
        or not _held_regular_is_exact(target_fd, expected, name=target_name)
    ):
        raise BatchRuntimeError(
            "storage_path_changed",
            "transition public target binding changed",
        )


def _validate_private_transition_file(
    descriptor: int,
    *,
    name: str,
    expected_mode: int | None = None,
) -> None:
    metadata = os.fstat(descriptor)
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or actual_mode & 0o077
        or (expected_mode is not None and actual_mode != expected_mode)
    ):
        raise BatchRuntimeError("unsafe_storage", f"transition file is not private immutable storage: {name}")


def _transition_owner_guard(
    parent_fd: int,
    parent_path: Path,
    transition_fd: int,
    owner_name: str,
    owner_fd: int,
    owner_expected: bytes,
) -> None:
    _verify_directory_binding(parent_path, parent_fd)
    _validate_private_transition_directory(parent_path, transition_fd)
    if (
        not _held_regular_matches_name(transition_fd, owner_name, owner_fd)
        or not _held_regular_is_exact(owner_fd, owner_expected, name=owner_name)
    ):
        raise BatchRuntimeError("storage_path_changed", "transition owner binding changed")
    _validate_private_transition_file(owner_fd, name=owner_name, expected_mode=0o600)


def _open_transition_storage(
    parent_fd: int,
    parent_path: Path,
    *,
    allowed_targets: set[str] | frozenset[str],
) -> tuple[int, int]:
    transition_was_missing = _existing_entry(parent_fd, _TRANSITION_DIRECTORY_NAME) is None
    transition_fd = _open_child_directory(
        parent_fd,
        _TRANSITION_DIRECTORY_NAME,
        create=transition_was_missing,
    )
    completed_fd = -1
    try:
        _validate_private_transition_directory(parent_path, transition_fd)
        completed_missing = _existing_entry(transition_fd, _COMPLETED_DIRECTORY_NAME) is None
        if completed_missing and not transition_was_missing and _bounded_sorted_names(
            transition_fd,
            max_entries=_MAX_TRANSITION_ENTRIES,
            label="immutable transition directory",
        ):
            raise BatchRuntimeError(
                "unsafe_storage",
                "existing transition namespace is missing its completed child",
            )
        completed_fd = _open_child_directory(
            transition_fd,
            _COMPLETED_DIRECTORY_NAME,
            create=completed_missing,
        )
        _validate_private_transition_directory(
            parent_path / _TRANSITION_DIRECTORY_NAME,
            completed_fd,
            child_name=_COMPLETED_DIRECTORY_NAME,
        )
        return transition_fd, completed_fd
    except BaseException:
        if completed_fd >= 0:
            os.close(completed_fd)
        os.close(transition_fd)
        raise


def _publish_immutable_entry(
    parent_fd: int,
    parent_path: Path,
    name: str,
    data: bytes,
    *,
    mode: int = 0o600,
    fault: StorageFaultHook | None = None,
    stage_prefix: str,
) -> int:
    existing = _existing_entry(parent_fd, name)
    if existing is not None:
        descriptor = _hold_regular(parent_fd, name, expected=data)
        try:
            _fsync(descriptor, label=str(parent_path / name))
            _verify_directory_binding(parent_path, parent_fd)
            _fsync(parent_fd, label=str(parent_path))
            _verify_directory_binding(parent_path, parent_fd)
            if (
                not _held_regular_matches_name(parent_fd, name, descriptor)
                or not _held_regular_is_exact(descriptor, data, name=name)
            ):
                raise BatchRuntimeError("storage_path_changed", f"immutable {stage_prefix} replay changed")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    pattern = re.compile(rf"^\.{re.escape(name)}\.[0-9a-f]{{32}}\.writing$")
    candidates = [
        candidate
        for candidate in _bounded_sorted_names(
            parent_fd,
            max_entries=_MAX_GENERAL_DIRECTORY_ENTRIES,
            label=f"immutable {stage_prefix} staging directory",
        )
        if pattern.fullmatch(candidate)
    ]
    if len(candidates) > 64:
        raise BatchRuntimeError("resource_limit", f"too many immutable {stage_prefix} attempts")
    complete: list[str] = []
    aggregate = 0
    for candidate in candidates:
        metadata = _existing_entry(parent_fd, candidate)
        if metadata is None:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise BatchRuntimeError("unsafe_storage", f"immutable write attempt is unsafe: {candidate}")
        aggregate += metadata.st_size
        if metadata.st_size > len(data) or aggregate > max(len(data) * 64, 4096):
            raise BatchRuntimeError("resource_limit", f"immutable {stage_prefix} attempts are oversized")
        raw = _read_regular_single_link(parent_fd, candidate, code="unsafe_storage", max_bytes=len(data))
        if raw == data:
            complete.append(candidate)
    if len(complete) > 1:
        raise BatchRuntimeError("unsafe_storage", f"multiple complete immutable {stage_prefix} attempts")
    if complete:
        writing_name = complete[0]
        descriptor = _hold_regular(parent_fd, writing_name, expected=data)
    else:
        writing_name = f".{name}.{secrets.token_hex(16)}.writing"
        descriptor = os.open(
            writing_name,
            _file_flags(os.O_RDWR | os.O_CREAT | os.O_EXCL),
            mode,
            dir_fd=parent_fd,
        )
        os.fchmod(descriptor, mode)
        _write_all(descriptor, data)
        if fault is not None:
            fault(f"after_{stage_prefix}_write_before_fsync")
    try:
        _validate_private_transition_file(descriptor, name=writing_name, expected_mode=mode)
        _fsync(descriptor, label=str(parent_path / writing_name))
        if fault is not None:
            fault(f"after_{stage_prefix}_file_fsync")
            if stage_prefix == "owner":
                fault("after_owner_file_fsync")
            elif stage_prefix == "payload":
                fault("after_writing_fsync")
                fault("after_file_fsync")
        if (
            not _held_regular_matches_name(parent_fd, writing_name, descriptor)
            or not _held_regular_is_exact(descriptor, data, name=writing_name)
        ):
            raise BatchRuntimeError("storage_path_changed", f"immutable {stage_prefix} attempt changed")
        _verify_directory_binding(parent_path, parent_fd)
        _rename_no_replace(parent_fd, writing_name, name)
        if fault is not None:
            fault(f"after_{stage_prefix}_rename_before_parent_fsync")
        _fsync(parent_fd, label=str(parent_path))
        _verify_directory_binding(parent_path, parent_fd)
        if fault is not None:
            fault(f"after_{stage_prefix}_publish")
            if stage_prefix == "owner":
                fault("after_owner_staging_fsync")
            elif stage_prefix == "payload":
                fault("after_pending_rename")
        if (
            not _held_regular_matches_name(parent_fd, name, descriptor)
            or not _held_regular_is_exact(descriptor, data, name=name)
        ):
            raise BatchRuntimeError("storage_path_changed", f"immutable {stage_prefix} publication changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _active_owner_payload(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchRuntimeError("unsafe_storage", "active transition owner is invalid") from exc
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise BatchRuntimeError("unsafe_storage", "active transition owner is not canonical")
    return value


def _active_leaf_name(owner: dict[str, Any]) -> str:
    return f"payload.{sha256_bytes(owner['target'].encode('utf-8'))}.transition"


def _completion_marker_name(transition_id: str) -> str:
    return f"{sha256_bytes(transition_id.encode('utf-8'))}.json"


def _lookup_completion(
    completed_fd: int,
    transition_id: str,
    expected: bytes,
) -> tuple[str, str, bytes, int] | None:
    """Return and hold ``current``/``historical`` ID provenance.

    Final marker names are pruned into inert hash-bound retired entries, but
    their transition IDs remain part of the permanent idempotency namespace.
    The completed directory is closed-world validated before every caller uses
    this helper, so scanning its bounded retired history is deterministic.
    """

    name = _completion_marker_name(transition_id)
    current = _existing_entry(completed_fd, name)
    current_binding: tuple[str, str, bytes, int] | None = None
    if current is not None:
        raw = _read_regular_single_link(completed_fd, name, code="unsafe_storage", max_bytes=4096)
        if raw != expected:
            raise BatchRuntimeError(
                "idempotency_conflict",
                "transition id is already bound to a different target or endpoint mapping",
            )
        descriptor = _hold_regular(completed_fd, name, expected=expected)
        try:
            _validate_private_transition_file(descriptor, name=name, expected_mode=0o600)
            current_binding = ("current", name, raw, descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    historical: list[tuple[str, bytes]] = []
    try:
        names = _bounded_sorted_names(
            completed_fd,
            max_entries=_MAX_COMPLETION_ENTRIES,
            label="completed transition namespace",
        )
        if len(names) > _MAX_COMPLETION_ENTRIES:
            raise BatchRuntimeError("resource_limit", "too many completed transition entries")
        aggregate = 0
        for candidate in names:
            retired_match = _RETIRED_NAME_PATTERN.fullmatch(candidate)
            if retired_match is None:
                continue
            metadata = _existing_entry(completed_fd, candidate)
            if metadata is None:
                raise BatchRuntimeError(
                    "storage_path_changed",
                    f"retired completion entry disappeared: {candidate}",
                )
            aggregate += metadata.st_size
            if metadata.st_size > 4096 or aggregate > _MAX_COMPLETION_AGGREGATE_BYTES:
                raise BatchRuntimeError("resource_limit", "retired completion history exceeds its bound")
            raw = _read_regular_single_link(
                completed_fd,
                candidate,
                code="unsafe_storage",
                max_bytes=4096,
            )
            if sha256_bytes(raw) != retired_match.group(1):
                raise BatchRuntimeError(
                    "unsafe_storage",
                    f"retired completion bytes do not match their immutable name: {candidate}",
                )
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("transition_id") != transition_id:
                continue
            if raw != expected:
                raise BatchRuntimeError(
                    "idempotency_conflict",
                    "transition id is already bound to a different target or endpoint mapping",
                )
            historical.append((candidate, raw))
        if current_binding is not None:
            return current_binding
        if not historical:
            return None
        candidate, raw = historical[0]
        descriptor = _hold_regular(completed_fd, candidate, expected=raw)
        try:
            _validate_private_transition_file(
                descriptor,
                name=candidate,
                expected_mode=0o600,
            )
            return "historical", candidate, raw, descriptor
        except BaseException:
            os.close(descriptor)
            raise
    except BaseException:
        if current_binding is not None:
            os.close(current_binding[3])
        raise


def _live_completion_matches_public(
    parent_fd: int,
    transition_fd: int,
    completed_fd: int,
    *,
    target_name: str,
    public_raw: bytes,
    public_mode: int,
    allowed_targets: set[str] | frozenset[str],
) -> tuple[str, bytes, int] | None:
    """Hold the live final marker proving the current public endpoint."""

    allowed_by_hash = {
        sha256_bytes(allowed.encode("utf-8")): allowed for allowed in allowed_targets
    }
    public_endpoint = (sha256_bytes(public_raw), len(public_raw), public_mode)
    parent_metadata = os.fstat(parent_fd)
    transition_metadata = os.fstat(transition_fd)
    matches: list[tuple[str, bytes]] = []
    for marker_name in _bounded_sorted_names(
        completed_fd,
        max_entries=_MAX_COMPLETION_ENTRIES,
        label="completed transition namespace",
    ):
        if _COMPLETION_NAME_PATTERN.fullmatch(marker_name) is None:
            continue
        raw = _read_regular_single_link(
            completed_fd,
            marker_name,
            code="unsafe_storage",
            max_bytes=4096,
        )
        payload = _validated_completion_payload(
            raw,
            marker_name,
            parent_metadata=parent_metadata,
            transition_metadata=transition_metadata,
            allowed_by_hash=allowed_by_hash,
        )
        if payload["target"] != target_name:
            continue
        endpoint = (payload["new_sha256"], payload["new_size"], payload["mode"])
        if endpoint == public_endpoint:
            matches.append((marker_name, raw))
    if not matches:
        return None
    if len(matches) != 1:
        raise BatchRuntimeError(
            "unsafe_storage",
            "current public endpoint has ambiguous live completion provenance",
        )
    marker_name, raw = matches[0]
    descriptor = _hold_regular(completed_fd, marker_name, expected=raw)
    try:
        _validate_private_transition_file(
            descriptor,
            name=marker_name,
            expected_mode=0o600,
        )
        return marker_name, raw, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _cleanup_completion_attempts(
    completed_fd: int,
    completed_path: Path,
    marker_name: str,
    expected: bytes,
    *,
    fault: StorageFaultHook | None,
) -> None:
    pattern = re.compile(rf"^\.{re.escape(marker_name)}\.[0-9a-f]{{32}}\.writing$")
    for candidate in _bounded_sorted_names(
        completed_fd,
        max_entries=_MAX_COMPLETION_ENTRIES,
        label="completed transition namespace",
    ):
        if pattern.fullmatch(candidate) is None:
            continue
        raw = _read_regular_single_link(
            completed_fd,
            candidate,
            code="unsafe_storage",
            max_bytes=4096,
        )
        if not expected.startswith(raw):
            raise BatchRuntimeError(
                "unsafe_storage",
                f"completed transition attempt is not a marker prefix: {candidate}",
            )
        _retire_internal_immutable(
            completed_fd,
            completed_path,
            candidate,
            raw,
        )
    if fault is not None:
        fault("after_completion_writing_cleanup")


def _prune_completed_markers(
    parent_fd: int,
    parent_path: Path,
    transition_fd: int,
    completed_fd: int,
    *,
    target_name: str,
    current_marker_name: str,
    allowed_targets: set[str] | frozenset[str],
    fault: StorageFaultHook | None,
) -> None:
    allowed_by_hash = {
        sha256_bytes(allowed.encode("utf-8")): allowed for allowed in allowed_targets
    }
    parent_metadata = os.fstat(parent_fd)
    transition_metadata = os.fstat(transition_fd)
    completed_path = parent_path / _TRANSITION_DIRECTORY_NAME / _COMPLETED_DIRECTORY_NAME
    for name in _bounded_sorted_names(
        completed_fd,
        max_entries=_MAX_COMPLETION_ENTRIES,
        label="completed transition namespace",
    ):
        if name == current_marker_name or _COMPLETION_NAME_PATTERN.fullmatch(name) is None:
            continue
        raw = _read_regular_single_link(
            completed_fd,
            name,
            code="unsafe_storage",
            max_bytes=4096,
        )
        payload = _validated_completion_payload(
            raw,
            name,
            parent_metadata=parent_metadata,
            transition_metadata=transition_metadata,
            allowed_by_hash=allowed_by_hash,
        )
        if payload["target"] != target_name:
            continue
        if fault is not None:
            fault("before_completion_prune_unlink")
        _retire_internal_immutable(completed_fd, completed_path, name, raw)
        if fault is not None:
            fault("after_completion_prune_unlink")
    _verify_directory_binding(completed_path, completed_fd)
    if fault is not None:
        fault("after_completion_prune")


def _named_matches_held_any_links(parent_fd: int, name: str, descriptor: int) -> bool:
    named = _existing_entry(parent_fd, name)
    if named is None:
        return False
    held = os.fstat(descriptor)
    return stat.S_ISREG(named.st_mode) and stat.S_ISREG(held.st_mode) and _same_entry(named, held)


def _held_exact_any_links(descriptor: int, expected: bytes, *, name: str) -> bool:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size != len(expected):
        return False
    raw = _read_held_descriptor(descriptor, max_bytes=len(expected), code="unsafe_storage", name=name)
    after = os.fstat(descriptor)
    return (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        and raw == expected
    )


def _hold_internal_any_links(parent_fd: int, name: str, expected: bytes) -> int:
    metadata = _existing_entry(parent_fd, name)
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise BatchRuntimeError("storage_path_changed", f"internal transition entry disappeared: {name}")
    descriptor = os.open(name, _file_flags(os.O_RDONLY), dir_fd=parent_fd)
    try:
        held = os.fstat(descriptor)
        if (
            not _named_matches_held_any_links(parent_fd, name, descriptor)
            or held.st_uid != os.getuid()
            or stat.S_IMODE(held.st_mode) & 0o077
            or not _held_exact_any_links(descriptor, expected, name=name)
        ):
            raise BatchRuntimeError("storage_path_changed", f"internal transition entry changed: {name}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _retire_internal_immutable(parent_fd: int, parent_path: Path, name: str, expected: bytes) -> None:
    if _existing_entry(parent_fd, name) is None:
        raise BatchRuntimeError(
            "storage_path_changed",
            f"observed internal retirement member disappeared: {name}",
        )
    descriptor = _hold_internal_any_links(parent_fd, name, expected)
    retired_name = f".retired.{sha256_bytes(expected)}.{secrets.token_hex(16)}.artifact"
    try:
        if (
            os.fstat(descriptor).st_nlink != 1
            or not _named_matches_held_any_links(parent_fd, name, descriptor)
        ):
            raise BatchRuntimeError("storage_path_changed", f"internal retirement target changed: {name}")

        # POSIX/macOS have no descriptor-conditional unlink primitive.  Keep a
        # bounded append-only history instead: atomically relocate the member to
        # a hash-bound inert name and never unlink a pathname after a separate
        # identity check.  Closed-world validation accepts these entries only as
        # retired bytes and directory count/aggregate limits bound their growth.
        _rename_no_replace(parent_fd, name, retired_name)
        if (
            not _named_matches_held_any_links(parent_fd, retired_name, descriptor)
            or not _held_exact_any_links(descriptor, expected, name=retired_name)
        ):
            raise BatchRuntimeError(
                "storage_path_changed",
                f"retired internal member is not the held target: {name}",
            )
        _fsync(parent_fd, label=str(parent_path))
        if (
            not _named_matches_held_any_links(parent_fd, retired_name, descriptor)
            or not _held_exact_any_links(descriptor, expected, name=retired_name)
        ):
            raise BatchRuntimeError(
                "storage_path_changed",
                f"retired internal member changed after parent fsync: {name}",
            )
        if _existing_entry(parent_fd, name) is not None:
            raise BatchRuntimeError("storage_path_changed", f"retired internal name reappeared: {name}")
        if not _held_exact_any_links(descriptor, expected, name=name):
            raise BatchRuntimeError("storage_path_changed", f"internal retirement mutated bytes: {name}")
    finally:
        os.close(descriptor)


def _cleanup_active_transition(
    transition_fd: int,
    transition_path: Path,
    owner_name: str,
    owner_raw: bytes,
    leaf_name: str,
    retired_raw: bytes | None,
    *,
    fault: StorageFaultHook | None = None,
    guard: Callable[[], None] | None = None,
) -> None:
    if guard is not None:
        guard()
    if retired_raw is not None:
        _retire_internal_immutable(transition_fd, transition_path, leaf_name, retired_raw)
        if guard is not None:
            guard()
        if fault is not None:
            fault("after_retired_leaf_unlink")
    prefixes = (f".{owner_name}.", f".{leaf_name}.")
    for candidate in _bounded_sorted_names(
        transition_fd,
        max_entries=_MAX_TRANSITION_ENTRIES,
        label="immutable transition directory",
    ):
        if not candidate.endswith(".writing") or not candidate.startswith(prefixes):
            continue
        metadata = _existing_entry(transition_fd, candidate)
        if metadata is None or metadata.st_size > MAX_OPAQUE_ARTIFACT_BYTES:
            raise BatchRuntimeError("unsafe_storage", "active transition attempt is unsafe during cleanup")
        raw = _read_regular_single_link(
            transition_fd,
            candidate,
            code="unsafe_storage",
            max_bytes=MAX_OPAQUE_ARTIFACT_BYTES,
        )
        _retire_internal_immutable(transition_fd, transition_path, candidate, raw)
        if guard is not None:
            guard()
    if fault is not None:
        fault("after_active_writing_cleanup")
    if guard is not None:
        guard()
    # The owner is the closed-world authorization for every active leaf and
    # writing attempt, so it must be the final active name removed.
    _retire_internal_immutable(transition_fd, transition_path, owner_name, owner_raw)
    if guard is not None:
        guard()
    if fault is not None:
        fault("after_active_owner_unlink")
    _fsync(transition_fd, label=str(transition_path))
    if guard is not None:
        guard()


def _public_is_private(descriptor: int, *, expected_mode: int | None = None) -> bool:
    metadata = os.fstat(descriptor)
    mode = stat.S_IMODE(metadata.st_mode)
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and metadata.st_uid == os.getuid()
        and not mode & 0o077
        and (expected_mode is None or mode == expected_mode)
    )


@contextmanager
def held_exact_sibling_files(
    directory: Path,
    expected_by_name: Mapping[str, bytes],
    *,
    expected_mode: int = 0o600,
    require_private: bool = True,
    exact_membership: bool = False,
) -> Iterator[Callable[[], None]]:
    """Hold and jointly guard exact bytes and path identities for sibling files."""

    if not expected_by_name:
        raise ValueError("at least one expected sibling file is required")
    if require_private and (
        expected_mode < 0 or expected_mode > 0o777 or expected_mode & 0o077
    ):
        raise ValueError("expected sibling mode must be private")
    expected = tuple(sorted(expected_by_name.items()))
    for name, raw in expected:
        if name in {"", ".", ".."} or "/" in name:
            raise ValueError("held sibling names must be safe basenames")
        if not isinstance(raw, bytes):
            raise TypeError("held sibling bytes must be exact bytes")

    with open_directory_fd(directory, create=False) as (parent_fd, parent_path):
        descriptors: dict[str, int] = {}
        try:
            for name, raw in expected:
                descriptors[name] = _hold_regular(parent_fd, name, expected=raw)

            def guard() -> None:
                for _attempt in range(2):
                    _verify_directory_binding(parent_path, parent_fd)
                    if exact_membership and set(
                        _bounded_sorted_names(
                            parent_fd,
                            max_entries=len(expected),
                            label="held sibling directory",
                        )
                    ) != {name for name, _raw in expected}:
                        raise BatchRuntimeError(
                            "storage_path_changed",
                            f"held sibling directory membership changed: {parent_path}",
                        )
                    for name, raw in expected:
                        descriptor = descriptors[name]
                        metadata = os.fstat(descriptor)
                        safe_regular = stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
                        if (
                            not safe_regular
                            or (
                                require_private
                                and not _public_is_private(
                                    descriptor,
                                    expected_mode=expected_mode,
                                )
                            )
                            or not _held_regular_matches_name(parent_fd, name, descriptor)
                            or not _held_regular_is_exact(descriptor, raw, name=name)
                        ):
                            raise BatchRuntimeError(
                                "storage_path_changed",
                                f"held sibling file changed: {parent_path / name}",
                            )
                    _verify_directory_binding(parent_path, parent_fd)

            guard()
            yield guard
            guard()
        finally:
            for descriptor in descriptors.values():
                os.close(descriptor)


def _complete_active_transition(
    parent_fd: int,
    parent_path: Path,
    target_name: str,
    transition_fd: int,
    completed_fd: int,
    owner_name: str,
    owner_raw: bytes,
    *,
    allowed_targets: set[str] | frozenset[str],
    owners: dict[str, tuple[str, bytes, str]],
    fault: StorageFaultHook | None,
) -> None:
    owner = _active_owner_payload(owner_raw)
    leaf_name = _active_leaf_name(owner)
    if _existing_entry(transition_fd, leaf_name) is None:
        raise BatchRuntimeError("storage_recovery_required", "active transition owner has no durable payload")
    max_size = max(owner["old_size"], owner["new_size"])
    public_raw = _read_regular_single_link(parent_fd, target_name, code="unsafe_storage", max_bytes=max_size)
    leaf_raw = _read_regular_single_link(transition_fd, leaf_name, code="unsafe_storage", max_bytes=max_size)
    public_fd = _hold_regular(parent_fd, target_name, expected=public_raw)
    leaf_fd = _hold_regular(transition_fd, leaf_name, expected=leaf_raw)
    owner_fd = _hold_regular(transition_fd, owner_name, expected=owner_raw)
    marker_fd = -1
    try:
        old_endpoint = (owner["old_sha256"], owner["old_size"])
        new_endpoint = (owner["new_sha256"], owner["new_size"])
        public_endpoint = (sha256_bytes(public_raw), len(public_raw))
        leaf_endpoint = (sha256_bytes(leaf_raw), len(leaf_raw))
        if public_endpoint == old_endpoint and leaf_endpoint == new_endpoint:
            if not _public_is_private(public_fd) or not _public_is_private(leaf_fd, expected_mode=owner["mode"]):
                raise BatchRuntimeError("unsafe_storage", "replace operands must be private before exchange")
            _transition_owner_guard(
                parent_fd, parent_path, transition_fd, owner_name, owner_fd, owner_raw
            )
            _exchange_names_between(transition_fd, leaf_name, parent_fd, target_name)
            if fault is not None:
                fault("after_exchange")
            public_fd, leaf_fd = leaf_fd, public_fd
            public_raw, leaf_raw = leaf_raw, public_raw
        elif public_endpoint != new_endpoint or leaf_endpoint != old_endpoint:
            raise BatchRuntimeError("unsafe_storage", "active transition is not in one recoverable endpoint state")
        if (
            not _public_is_private(public_fd, expected_mode=owner["mode"])
            or not _held_regular_matches_name(parent_fd, target_name, public_fd)
            or not _held_regular_is_exact(public_fd, public_raw, name=target_name)
            or not _named_matches_held_any_links(transition_fd, leaf_name, leaf_fd)
            or not _held_exact_any_links(leaf_fd, leaf_raw, name=leaf_name)
        ):
            raise BatchRuntimeError("storage_path_changed", "completed transition bindings changed")
        _transition_owner_guard(
            parent_fd, parent_path, transition_fd, owner_name, owner_fd, owner_raw
        )
        _fsync(transition_fd, label=str(parent_path / _TRANSITION_DIRECTORY_NAME))
        if fault is not None:
            fault("after_transition_parent_fsync")
        _fsync(parent_fd, label=str(parent_path))
        if fault is not None:
            fault("after_exchange_fsync")
        if (
            not _held_regular_matches_name(parent_fd, target_name, public_fd)
            or not _held_regular_is_exact(public_fd, public_raw, name=target_name)
            or not _named_matches_held_any_links(transition_fd, leaf_name, leaf_fd)
            or not _held_exact_any_links(leaf_fd, leaf_raw, name=leaf_name)
        ):
            raise BatchRuntimeError("storage_path_changed", "durable transition bindings changed")
        _transition_owner_guard(
            parent_fd, parent_path, transition_fd, owner_name, owner_fd, owner_raw
        )
        _validate_private_transition_directory(
            parent_path / _TRANSITION_DIRECTORY_NAME,
            completed_fd,
            child_name=_COMPLETED_DIRECTORY_NAME,
        )
        completion_raw = _completion_bytes(owner_raw)
        marker_name = _completion_marker_name(owner["transition_id"])
        marker_fd = _publish_immutable_entry(
            completed_fd,
            parent_path / _TRANSITION_DIRECTORY_NAME / _COMPLETED_DIRECTORY_NAME,
            marker_name,
            completion_raw,
            fault=fault,
            stage_prefix="completion",
        )
        _cleanup_completion_attempts(
            completed_fd,
            parent_path / _TRANSITION_DIRECTORY_NAME / _COMPLETED_DIRECTORY_NAME,
            marker_name,
            completion_raw,
            fault=fault,
        )
        allowed_by_hash = {
            sha256_bytes(allowed.encode("utf-8")): allowed
            for allowed in allowed_targets
        }
        _validate_completed_transition_directory(
            parent_fd,
            parent_path,
            transition_fd,
            completed_fd,
            allowed_by_hash,
            owners,
            require_active_completion=True,
        )
        _prune_completed_markers(
            parent_fd,
            parent_path,
            transition_fd,
            completed_fd,
            target_name=target_name,
            current_marker_name=marker_name,
            allowed_targets=allowed_targets,
            fault=fault,
        )
        _validate_completed_transition_directory(
            parent_fd,
            parent_path,
            transition_fd,
            completed_fd,
            allowed_by_hash,
            owners,
            require_active_completion=True,
        )
        if (
            not _public_is_private(public_fd, expected_mode=owner["mode"])
            or not _held_regular_matches_name(parent_fd, target_name, public_fd)
            or not _held_regular_is_exact(public_fd, public_raw, name=target_name)
            or not _named_matches_held_any_links(transition_fd, leaf_name, leaf_fd)
            or not _held_exact_any_links(leaf_fd, leaf_raw, name=leaf_name)
        ):
            raise BatchRuntimeError(
                "storage_path_changed",
                "transition endpoints changed during completion pruning",
            )
        _transition_owner_guard(
            parent_fd, parent_path, transition_fd, owner_name, owner_fd, owner_raw
        )
        if fault is not None:
            fault("before_active_cleanup")

        def cleanup_guard() -> None:
            _transition_public_guard(
                parent_fd,
                parent_path,
                target_name,
                public_fd,
                public_raw,
                expected_mode=owner["mode"],
                transition_fd=transition_fd,
                completed_fd=completed_fd,
            )
            if (
                marker_fd < 0
                or not _named_matches_held_any_links(completed_fd, marker_name, marker_fd)
                or not _held_exact_any_links(marker_fd, completion_raw, name=marker_name)
            ):
                raise BatchRuntimeError(
                    "storage_path_changed",
                    "current completion marker changed during active retirement",
                )
            _validate_private_transition_file(
                marker_fd,
                name=marker_name,
                expected_mode=0o600,
            )

        cleanup_guard()
        _cleanup_active_transition(
            transition_fd,
            parent_path / _TRANSITION_DIRECTORY_NAME,
            owner_name,
            owner_raw,
            leaf_name,
            leaf_raw,
            fault=fault,
            guard=cleanup_guard,
        )
        cleanup_guard()
    finally:
        if marker_fd >= 0:
            os.close(marker_fd)
        os.close(owner_fd)
        os.close(leaf_fd)
        os.close(public_fd)


def replace_bytes_atomic(
    path: Path,
    data: bytes,
    *,
    transition_id: str,
    allowed_transition_targets: set[str] | frozenset[str] | None = None,
    mode: int = 0o600,
    expected_current: bytes | None = None,
    expected_current_sha256: str | None = None,
    expected_current_size: int | None = None,
    fault: StorageFaultHook | None = None,
) -> None:
    if not isinstance(transition_id, str) or not transition_id or len(transition_id.encode("utf-8")) > 1024:
        raise ValueError("transition_id must be a non-empty string of at most 1024 UTF-8 bytes")
    if type(mode) is not int or mode < 0 or mode > 0o777 or mode & 0o077:
        raise ValueError("replacement mode must be private")
    if expected_current is None:
        if (
            expected_current_sha256 is None
            or re.fullmatch(r"[0-9a-f]{64}", expected_current_sha256) is None
            or type(expected_current_size) is not int
            or expected_current_size < 0
        ):
            raise ValueError(
                "expected_current bytes or exact sha256/size metadata are required"
            )
    else:
        actual_sha256 = sha256_bytes(expected_current)
        actual_size = len(expected_current)
        if expected_current_sha256 is not None and expected_current_sha256 != actual_sha256:
            raise ValueError("expected_current_sha256 differs from expected_current bytes")
        if expected_current_size is not None and expected_current_size != actual_size:
            raise ValueError("expected_current_size differs from expected_current bytes")
        expected_current_sha256 = actual_sha256
        expected_current_size = actual_size
    assert expected_current_sha256 is not None
    assert expected_current_size is not None
    with _open_parent_fd(path, create=False) as (parent_fd, parent_path, name):
        allowed_targets = frozenset({name} if allowed_transition_targets is None else allowed_transition_targets)
        if name not in allowed_targets:
            raise ValueError("replacement target must be included in allowed_transition_targets")
        target_fd = _hold_regular(parent_fd, name)
        transition_fd = completed_fd = -1
        lookup_binding_fd = -1
        lookup_binding_name = ""
        lookup_binding_raw = b""
        try:
            if not _public_is_private(target_fd):
                raise BatchRuntimeError("unsafe_storage", "replace target must be private before transition")
            target_limit = (
                MAX_JSON_ARTIFACT_BYTES
                if name.casefold().endswith(".json")
                else MAX_OPAQUE_ARTIFACT_BYTES
            )
            target_metadata = os.fstat(target_fd)
            if target_metadata.st_size > target_limit:
                raise BatchRuntimeError("resource_limit", "replace target exceeds its artifact limit")
            public_snapshot = _read_held_descriptor(
                target_fd,
                max_bytes=target_limit,
                code="unsafe_storage",
                name=name,
            )
            if (
                not _held_regular_matches_name(parent_fd, name, target_fd)
                or not _held_regular_is_exact(target_fd, public_snapshot, name=name)
            ):
                raise BatchRuntimeError("storage_path_changed", "replace target changed during preflight")
            public_is_previous = (
                expected_current is not None and public_snapshot == expected_current
            )
            public_is_desired = public_snapshot == data

            def guard_historical_replay(provenance: tuple[str, bytes, int]) -> None:
                marker_name, marker_raw, marker_fd = provenance
                try:
                    for _attempt in range(2):
                        _transition_namespace_guard(
                            parent_fd,
                            parent_path,
                            transition_fd,
                            completed_fd,
                        )
                        if (
                            not _public_is_private(target_fd)
                            or not _held_regular_matches_name(parent_fd, name, target_fd)
                            or not _held_regular_is_exact(target_fd, public_snapshot, name=name)
                            or not _named_matches_held_any_links(
                                completed_fd,
                                marker_name,
                                marker_fd,
                            )
                            or not _held_exact_any_links(
                                marker_fd,
                                marker_raw,
                                name=marker_name,
                            )
                            or lookup_binding_fd < 0
                            or not _named_matches_held_any_links(
                                completed_fd,
                                lookup_binding_name,
                                lookup_binding_fd,
                            )
                            or not _held_exact_any_links(
                                lookup_binding_fd,
                                lookup_binding_raw,
                                name=lookup_binding_name,
                            )
                        ):
                            raise BatchRuntimeError(
                                "storage_path_changed",
                                "historical replay provenance changed before return",
                            )
                        _validate_private_transition_file(
                            marker_fd,
                            name=marker_name,
                            expected_mode=0o600,
                        )
                        _validate_private_transition_file(
                            lookup_binding_fd,
                            name=lookup_binding_name,
                            expected_mode=0o600,
                        )
                finally:
                    os.close(marker_fd)

            if not public_is_previous and not public_is_desired:
                transition_fd = _open_existing_transition_directory(parent_fd, parent_path) or -1
                if transition_fd < 0:
                    raise BatchRuntimeError(
                        "storage_path_changed",
                        "replace target bytes differ from both transition endpoints",
                    )
                completed_fd = _open_child_directory(
                    transition_fd,
                    _COMPLETED_DIRECTORY_NAME,
                    create=False,
                )
                target_hash = sha256_bytes(name.encode("utf-8"))
                owner_raw = _transition_owner_bytes(
                    os.fstat(parent_fd),
                    os.fstat(transition_fd),
                    name,
                    transition_id,
                    expected_current_sha256,
                    expected_current_size,
                    sha256_bytes(data),
                    len(data),
                    mode,
                )
                _validate_transition_directory_fd(
                    parent_fd,
                    parent_path,
                    transition_fd,
                    allowed_targets,
                )
                completion_binding = _lookup_completion(
                    completed_fd,
                    transition_id,
                    _completion_bytes(owner_raw),
                )
                if completion_binding is None:
                    raise BatchRuntimeError(
                        "storage_path_changed",
                        "replace target bytes differ from both transition endpoints",
                    )
                (
                    completion_status,
                    lookup_binding_name,
                    lookup_binding_raw,
                    lookup_binding_fd,
                ) = completion_binding
                provenance = _live_completion_matches_public(
                    parent_fd,
                    transition_fd,
                    completed_fd,
                    target_name=name,
                    public_raw=public_snapshot,
                    public_mode=stat.S_IMODE(os.fstat(target_fd).st_mode),
                    allowed_targets=allowed_targets,
                )
                if provenance is None:
                    raise BatchRuntimeError(
                        "storage_path_changed",
                        "historical replay target lacks current completion provenance",
                    )
                guard_historical_replay(provenance)
                return
            if public_is_desired:
                existing_transition_fd = _open_existing_transition_directory(parent_fd, parent_path)
                try:
                    if (
                        existing_transition_fd is None
                        or _existing_entry(existing_transition_fd, _COMPLETED_DIRECTORY_NAME) is None
                    ):
                        raise BatchRuntimeError(
                            "storage_path_changed",
                            "desired bytes exist without a durable transition record",
                        )
                finally:
                    if existing_transition_fd is not None:
                        os.close(existing_transition_fd)
            transition_fd, completed_fd = _open_transition_storage(
                parent_fd,
                parent_path,
                allowed_targets=allowed_targets,
            )
            target_hash = sha256_bytes(name.encode("utf-8"))
            owner_name = f"owner.{target_hash}.json"
            owner_raw = _transition_owner_bytes(
                os.fstat(parent_fd),
                os.fstat(transition_fd),
                name,
                transition_id,
                expected_current_sha256,
                expected_current_size,
                sha256_bytes(data),
                len(data),
                mode,
            )
            owners = _validate_transition_directory_fd(
                parent_fd,
                parent_path,
                transition_fd,
                allowed_targets,
                recovery_expected={target_hash: (owner_raw, data)},
            )
            completion_raw = _completion_bytes(owner_raw)
            completion_binding = _lookup_completion(completed_fd, transition_id, completion_raw)
            if completion_binding is not None:
                (
                    completion_status,
                    lookup_binding_name,
                    lookup_binding_raw,
                    lookup_binding_fd,
                ) = completion_binding
                if completion_status == "historical":
                    provenance = _live_completion_matches_public(
                        parent_fd,
                        transition_fd,
                        completed_fd,
                        target_name=name,
                        public_raw=public_snapshot,
                        public_mode=stat.S_IMODE(os.fstat(target_fd).st_mode),
                        allowed_targets=allowed_targets,
                    )
                    if provenance is None:
                        raise BatchRuntimeError(
                            "storage_path_changed",
                            "historical replay target lacks current completion provenance",
                        )
                    guard_historical_replay(provenance)
                    return

                def replay_guard() -> None:
                    _transition_public_guard(
                        parent_fd,
                        parent_path,
                        name,
                        target_fd,
                        data,
                        expected_mode=mode,
                        transition_fd=transition_fd,
                        completed_fd=completed_fd,
                    )
                    if (
                        lookup_binding_fd < 0
                        or not _named_matches_held_any_links(
                            completed_fd,
                            lookup_binding_name,
                            lookup_binding_fd,
                        )
                        or not _held_exact_any_links(
                            lookup_binding_fd,
                            lookup_binding_raw,
                            name=lookup_binding_name,
                        )
                    ):
                        raise BatchRuntimeError(
                            "storage_path_changed",
                            "completion request binding changed during replay",
                        )
                    _validate_private_transition_file(
                        lookup_binding_fd,
                        name=lookup_binding_name,
                        expected_mode=0o600,
                    )

                replay_guard()
                completed_owner = _active_owner_payload(owner_raw)
                completed_leaf_name = _active_leaf_name(completed_owner)
                if _existing_entry(transition_fd, owner_name) is not None:
                    active_raw = _read_regular_single_link(
                        transition_fd,
                        owner_name,
                        code="unsafe_storage",
                        max_bytes=4096,
                    )
                    if active_raw != owner_raw:
                        raise BatchRuntimeError("unsafe_storage", "completion marker conflicts with active owner")
                    if _existing_entry(transition_fd, completed_leaf_name) is not None:
                        _complete_active_transition(
                            parent_fd,
                            parent_path,
                            name,
                            transition_fd,
                            completed_fd,
                            owner_name,
                            owner_raw,
                            allowed_targets=allowed_targets,
                            owners=owners,
                            fault=fault,
                        )
                    else:
                        _cleanup_active_transition(
                            transition_fd,
                            parent_path / _TRANSITION_DIRECTORY_NAME,
                            owner_name,
                            owner_raw,
                            completed_leaf_name,
                            None,
                            fault=fault,
                            guard=replay_guard,
                        )
                elif _existing_entry(transition_fd, completed_leaf_name) is not None:
                    if expected_current is None:
                        raise BatchRuntimeError(
                            "unsafe_storage",
                            "digest-only transition recovery unexpectedly retained its retired leaf",
                        )
                    _cleanup_active_transition(
                        transition_fd,
                        parent_path / _TRANSITION_DIRECTORY_NAME,
                        owner_name,
                        owner_raw,
                        completed_leaf_name,
                        expected_current,
                        fault=fault,
                        guard=replay_guard,
                    )
                replay_guard()
                return

            existing_owner_raw: bytes | None = None
            if _existing_entry(transition_fd, owner_name) is not None:
                existing_owner_raw = _read_regular_single_link(
                    transition_fd, owner_name, code="unsafe_storage", max_bytes=4096
                )
                if existing_owner_raw != owner_raw:
                    existing_owner = _active_owner_payload(existing_owner_raw)
                    if _existing_entry(transition_fd, _active_leaf_name(existing_owner)) is not None:
                        _complete_active_transition(
                            parent_fd,
                            parent_path,
                            name,
                            transition_fd,
                            completed_fd,
                            owner_name,
                            existing_owner_raw,
                            allowed_targets=allowed_targets,
                            owners=owners,
                            fault=fault,
                        )
                        raise BatchRuntimeError(
                            "storage_recovery_required",
                            "a prior active transition was forward-completed; reload before starting another",
                        )
                    raise BatchRuntimeError(
                        "idempotency_conflict",
                        "target has a different owner-only active transition",
                    )
            if public_is_desired and existing_owner_raw is None:
                raise BatchRuntimeError(
                    "storage_path_changed",
                    "desired bytes lack both an exact completion marker and active owner",
                )
            if existing_owner_raw is None:
                owner_fd = _publish_immutable_entry(
                    transition_fd,
                    parent_path / _TRANSITION_DIRECTORY_NAME,
                    owner_name,
                    owner_raw,
                    fault=fault,
                    stage_prefix="owner",
                )
                os.close(owner_fd)
                owners[target_hash] = (owner_name, owner_raw, name)
            leaf_name = _active_leaf_name(_active_owner_payload(owner_raw))
            if _existing_entry(transition_fd, leaf_name) is not None:
                _complete_active_transition(
                    parent_fd,
                    parent_path,
                    name,
                    transition_fd,
                    completed_fd,
                    owner_name,
                    owner_raw,
                    allowed_targets=allowed_targets,
                    owners=owners,
                    fault=fault,
                )
                return
            leaf_fd = _publish_immutable_entry(
                transition_fd,
                parent_path / _TRANSITION_DIRECTORY_NAME,
                leaf_name,
                data,
                mode=mode,
                fault=fault,
                stage_prefix="payload",
            )
            os.close(leaf_fd)
            _complete_active_transition(
                parent_fd,
                parent_path,
                name,
                transition_fd,
                completed_fd,
                owner_name,
                owner_raw,
                allowed_targets=allowed_targets,
                owners=owners,
                fault=fault,
            )
            if not _read_regular_single_link(
                parent_fd,
                name,
                code="storage_path_changed",
                max_bytes=len(data),
            ) == data:
                raise BatchRuntimeError("storage_path_changed", "replace target changed after completion")
        finally:
            if lookup_binding_fd >= 0:
                os.close(lookup_binding_fd)
            if completed_fd >= 0:
                os.close(completed_fd)
            if transition_fd >= 0:
                os.close(transition_fd)
            os.close(target_fd)


def promote_bytes_no_replace(
    staging: Path,
    target: Path,
    expected: bytes,
    *,
    guard: Callable[[], None] | None = None,
    precommit_guard: Callable[[], None] | None = None,
) -> None:
    source = normalized_absolute_path(staging)
    destination = normalized_absolute_path(target)
    if source.parent != destination.parent:
        raise BatchRuntimeError("unsafe_path", "staging and target files must share one parent")
    with open_directory_fd(source.parent, create=False) as (parent_fd, parent_path):
        source_metadata = _existing_entry(parent_fd, source.name)
        if source_metadata is None:
            raise BatchRuntimeError("storage_missing", f"staging file disappeared: {source}")
        descriptor = _hold_regular(parent_fd, source.name, expected=expected)
        try:
            if _existing_entry(parent_fd, destination.name) is not None:
                raise BatchRuntimeError("output_conflict", f"target is occupied while promoting staging: {destination}")
            if (
                not _held_regular_matches_name(parent_fd, source.name, descriptor)
                or not _held_regular_is_exact(descriptor, expected, name=source.name)
            ):
                raise BatchRuntimeError("storage_path_changed", f"staging file changed before promotion: {source}")
            if guard is not None:
                guard()
            if precommit_guard is not None:
                precommit_guard()
            _rename_no_replace(parent_fd, source.name, destination.name)
            if guard is not None:
                guard()
            published = _existing_entry(parent_fd, destination.name)
            public_is_owned = (
                published is not None
                and _held_regular_matches_name(parent_fd, destination.name, descriptor)
            )
            if not public_is_owned or not _held_regular_is_exact(
                descriptor,
                expected,
                name=destination.name,
            ):
                raise BatchRuntimeError("storage_path_changed", f"promoted target is not the held staging inode: {destination}")
            _verify_directory_binding(parent_path, parent_fd)
            _fsync(parent_fd, label=str(parent_path))
            if guard is not None:
                guard()
            if (
                not _held_regular_matches_name(parent_fd, destination.name, descriptor)
                or not _held_regular_is_exact(descriptor, expected, name=destination.name)
            ):
                raise BatchRuntimeError(
                    "storage_path_changed",
                    f"promoted target changed after its parent fsync: {destination}",
                )
        finally:
            os.close(descriptor)


def zero_exact_staging(
    path: Path,
    expected: bytes,
    *,
    guard: Callable[[], None] | None = None,
) -> None:
    """Free an exact internal staging payload without pathname-based deletion."""

    with _open_parent_fd(path, create=False) as (parent_fd, parent_path, name):
        descriptor = _hold_regular(
            parent_fd,
            name,
            expected=expected,
            writable=True,
        )
        try:
            if guard is not None:
                guard()
            if (
                not _held_regular_matches_name(parent_fd, name, descriptor)
                or not _held_regular_is_exact(descriptor, expected, name=name)
            ):
                raise BatchRuntimeError(
                    "storage_path_changed",
                    f"staging file changed before logical cleanup: {path}",
                )
            try:
                os.ftruncate(descriptor, 0)
            except OSError as exc:
                raise _storage_error(f"cannot clear exact staging payload: {path}", exc)
            _fsync(descriptor, label=str(path))
            if guard is not None:
                guard()
            if (
                not _held_regular_matches_name(parent_fd, name, descriptor)
                or not _held_regular_is_exact(descriptor, b"", name=name)
            ):
                raise BatchRuntimeError(
                    "storage_path_changed",
                    f"staging file changed during logical cleanup: {path}",
                )
            _verify_directory_binding(parent_path, parent_fd)
            _fsync(parent_fd, label=str(parent_path))
            if guard is not None:
                guard()
            if (
                not _held_regular_matches_name(parent_fd, name, descriptor)
                or not _held_regular_is_exact(descriptor, b"", name=name)
            ):
                raise BatchRuntimeError(
                    "storage_path_changed",
                    f"cleared staging tombstone changed after parent fsync: {path}",
                )
        finally:
            os.close(descriptor)


def fsync_directory(path: Path) -> None:
    with open_directory_fd(path, create=False) as (descriptor, normalized):
        _fsync(descriptor, label=str(normalized))


def _directory_tree_snapshot(
    descriptor: int,
    *,
    max_members: int = 10_000,
    max_bytes: int = MAX_OPAQUE_ARTIFACT_BYTES,
) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    total_bytes = 0

    def walk(current_fd: int, prefix: PurePosixPath) -> None:
        nonlocal total_bytes
        before = os.fstat(current_fd)
        if not stat.S_ISDIR(before.st_mode):
            raise BatchRuntimeError("unsafe_storage", "published tree contains a non-directory root")
        if len(entries) >= max_members:
            raise BatchRuntimeError("resource_limit", "directory publication tree has too many members")
        entries.append(
            (
                (prefix.as_posix() if prefix.parts else "."),
                "directory-metadata",
                before.st_dev,
                before.st_ino,
                stat.S_IMODE(before.st_mode),
                before.st_uid,
                before.st_gid,
            )
        )
        names = tuple(
            _bounded_sorted_names(
                current_fd,
                max_entries=max_members - len(entries),
                label="directory publication tree",
            )
        )
        for name in names:
            if name in {"", ".", ".."} or "/" in name:
                raise BatchRuntimeError("unsafe_storage", f"unsafe tree member name: {name}")
            relative = prefix / name
            if len(entries) >= max_members:
                raise BatchRuntimeError("resource_limit", "directory publication tree has too many members")
            metadata = _existing_entry(current_fd, name)
            if metadata is None:
                raise BatchRuntimeError("storage_path_changed", f"tree member disappeared: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = _open_child_directory(current_fd, name, create=False)
                try:
                    opened = os.fstat(child_fd)
                    if not _same_entry(metadata, opened):
                        raise BatchRuntimeError("storage_path_changed", f"tree directory changed: {relative}")
                    walk(child_fd, relative)
                    named_after = _existing_entry(current_fd, name)
                    if named_after is None or not _same_entry(opened, named_after):
                        raise BatchRuntimeError("storage_path_changed", f"tree directory changed: {relative}")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                remaining = max_bytes - total_bytes
                raw = _read_regular_single_link(
                    current_fd,
                    name,
                    code="resource_limit",
                    max_bytes=remaining,
                )
                named_after = _existing_entry(current_fd, name)
                if named_after is None or not _same_entry(metadata, named_after):
                    raise BatchRuntimeError("storage_path_changed", f"tree file changed: {relative}")
                total_bytes += len(raw)
                entries.append(
                    (
                        relative.as_posix(),
                        "file",
                        metadata.st_dev,
                        metadata.st_ino,
                        stat.S_IMODE(metadata.st_mode),
                        metadata.st_uid,
                        metadata.st_gid,
                        len(raw),
                        sha256_bytes(raw),
                    )
                )
            else:
                raise BatchRuntimeError(
                    "unsafe_storage",
                    f"directory publication tree contains an unsafe member: {relative}",
                )
        after = os.fstat(current_fd)
        if not _same_entry(before, after) or tuple(
            _bounded_sorted_names(
                current_fd,
                max_entries=len(names),
                label="directory publication tree",
            )
        ) != names:
            raise BatchRuntimeError("storage_path_changed", f"tree membership changed: {prefix or '.'}")

    walk(descriptor, PurePosixPath())
    return tuple(entries)


def publish_directory_no_replace(
    staging: Path,
    target: Path,
    *,
    fault: StorageFaultHook | None = None,
) -> None:
    source = normalized_absolute_path(staging)
    destination = normalized_absolute_path(target)
    if source.parent != destination.parent:
        raise BatchRuntimeError("unsafe_path", "staging and target directories must share one parent")
    with open_directory_fd(source.parent, create=False) as (parent_fd, parent_path):
        source_metadata = _existing_entry(parent_fd, source.name)
        if source_metadata is None or not stat.S_ISDIR(source_metadata.st_mode):
            raise BatchRuntimeError("unsafe_storage", f"staging tree is not a directory: {source}")
        try:
            source_fd = os.open(source.name, _directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise _storage_error(f"cannot hold staging tree safely: {source}", exc)
        try:
            held_source = os.fstat(source_fd)
            named_source = _existing_entry(parent_fd, source.name)
            if (
                named_source is None
                or not stat.S_ISDIR(held_source.st_mode)
                or not stat.S_ISDIR(named_source.st_mode)
                or not _same_entry(held_source, named_source)
                or not _same_entry(source_metadata, held_source)
            ):
                raise BatchRuntimeError("storage_path_changed", f"staging tree changed before publication: {source}")
            expected_tree = _directory_tree_snapshot(source_fd)
            if _existing_entry(parent_fd, destination.name) is not None:
                raise BatchRuntimeError("output_conflict", f"target directory already exists: {destination}")
            if _directory_tree_snapshot(source_fd) != expected_tree:
                raise BatchRuntimeError("storage_path_changed", f"staging tree changed before publication: {source}")
            _rename_no_replace(parent_fd, source.name, destination.name)
            published = _existing_entry(parent_fd, destination.name)
            public_is_owned = (
                published is not None
                and stat.S_ISDIR(published.st_mode)
                and _same_entry(held_source, published)
            )
            if not public_is_owned:
                raise BatchRuntimeError("storage_path_changed", f"published tree is not the held staging inode: {destination}")
            if _directory_tree_snapshot(source_fd) != expected_tree:
                raise BatchRuntimeError("storage_path_changed", f"published tree members changed: {destination}")
            if fault is not None:
                fault("after_rename")
            _verify_directory_binding(parent_path, parent_fd)
            if fault is not None:
                fault("before_parent_fsync")
            _fsync(parent_fd, label=str(parent_path))
            final = _existing_entry(parent_fd, destination.name)
            held_final = os.fstat(source_fd)
            final_is_owned = (
                final is not None
                and stat.S_ISDIR(final.st_mode)
                and _same_entry(held_final, final)
            )
            if (
                not _same_entry(held_source, held_final)
                or not final_is_owned
                or _directory_tree_snapshot(source_fd) != expected_tree
            ):
                raise BatchRuntimeError("storage_path_changed", f"published tree changed before commit: {destination}")
        finally:
            os.close(source_fd)


def validate_locked_path(path: Path, descriptor: int) -> None:
    with _open_parent_fd(path, create=False) as (parent_fd, _parent_path, name):
        try:
            bound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise BatchRuntimeError("storage_path_changed", f"locked file path disappeared: {path}") from exc
        held = os.fstat(descriptor)
        if (
            not stat.S_ISREG(bound.st_mode)
            or bound.st_nlink != 1
            or (bound.st_dev, bound.st_ino) != (held.st_dev, held.st_ino)
        ):
            raise BatchRuntimeError("storage_path_changed", f"locked file path no longer binds held fd: {path}")


@contextmanager
def _locked_parent_binding(
    path: Path,
    *,
    create: bool,
    enabled: bool,
    preserve_on_dup: bool,
) -> Iterator[tuple[int, ...]]:
    """Lock the complete root-to-ancestor chain for one named lock.

    Locking only ``path.parent.parent`` leaves that directory's own pathname
    replaceable. A contender can then traverse a replacement tree and create a
    second parent/name lock domain. Root-first locking gives all cooperating
    contenders one non-replaceable acquisition point and preserves every
    ancestor descriptor through child marker publication.
    """

    parent = path.parent
    ancestor = parent.parent
    if not enabled or fcntl is None or ancestor == parent:
        yield ()
        return

    normalized = normalized_absolute_path(ancestor)
    descriptors: list[int] = []
    component_names: list[str] = []
    try:
        try:
            root_fd = os.open("/", _directory_flags())
        except OSError as exc:  # pragma: no cover - platform root failure
            raise _storage_error("cannot anchor lock ancestor chain", exc)
        descriptors.append(root_fd)
        fcntl.flock(root_fd, fcntl.LOCK_EX)

        for component in normalized.parts[1:]:
            child_fd = _open_child_directory(descriptors[-1], component, create=create)
            try:
                fcntl.flock(child_fd, fcntl.LOCK_EX)
                named = os.stat(component, dir_fd=descriptors[-1], follow_symlinks=False)
                held = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(named.st_mode)
                    or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino)
                ):
                    raise BatchRuntimeError(
                        "storage_path_changed",
                        f"lock ancestor changed during acquisition: {normalized}",
                    )
            except Exception:
                os.close(child_fd)
                raise
            descriptors.append(child_fd)
            component_names.append(component)

        yield tuple(descriptors)

        for parent_fd, child_fd, component in zip(
            descriptors[:-1],
            descriptors[1:],
            component_names,
            strict=True,
        ):
            try:
                named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise BatchRuntimeError(
                    "storage_path_changed",
                    f"lock ancestor disappeared while held: {normalized}",
                ) from exc
            held = os.fstat(child_fd)
            if (
                not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino)
            ):
                raise BatchRuntimeError(
                    "storage_path_changed",
                    f"lock ancestor changed while held: {normalized}",
                )
    finally:
        if fcntl is not None and not preserve_on_dup:
            for descriptor in reversed(descriptors):
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


@contextmanager
def locked_file(
    path: Path,
    *,
    create: bool = True,
    fault: StorageFaultHook | None = None,
    inherited_lock_descriptors: list[int] | None = None,
    guard_parent_replacement: bool = True,
) -> Iterator[int]:
    normalized = normalized_absolute_path(path)
    guard_binding = guard_parent_replacement and normalized.name.endswith(".lock")
    with _locked_parent_binding(
        normalized,
        create=create,
        enabled=guard_binding,
        preserve_on_dup=inherited_lock_descriptors is not None,
    ) as ancestor_fds, _open_parent_fd(normalized, create=create) as (parent_fd, parent_path, name):
        # A pathname can be replaced while an inode-scoped flock is held. All
        # runtime lock files therefore also serialize on their stable parent
        # directory descriptor before opening the named lock. A replacement
        # lock inode cannot create a second live critical section in the same
        # storage directory; the original holder still fails closed when its
        # path binding is checked.
        if fcntl is not None and name.endswith(".lock"):
            fcntl.flock(parent_fd, fcntl.LOCK_EX)
        existed = _existing_entry(parent_fd, name) is not None
        if not existed and not create:
            raise BatchRuntimeError("lease_secret_missing", f"required lock file is missing: {path}")
        descriptor = -1
        open_error: OSError | None = None
        for _attempt in range(3):
            try:
                descriptor = os.open(
                    name,
                    _file_flags(os.O_RDWR | (os.O_CREAT if create else 0)),
                    0o600,
                    dir_fd=parent_fd,
                )
                break
            except OSError as exc:
                open_error = exc
                if exc.errno != errno.ENOENT:
                    break
                _verify_directory_binding(parent_path, parent_fd)
        if descriptor < 0:
            exc = open_error or OSError("unknown lock open failure")
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise BatchRuntimeError("unsafe_path", f"lock is a symlink or unsafe file: {path}") from exc
            raise _storage_error(f"cannot open lock safely: {path}: {exc}", exc)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise BatchRuntimeError("unsafe_storage", f"lock must be a regular single-link file: {path}")
            if not existed:
                _fsync(parent_fd, label=str(parent_path))
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    if fault is not None:
                        fault("after_flock")
                    bound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    held = os.fstat(descriptor)
                    if (bound.st_dev, bound.st_ino) != (held.st_dev, held.st_ino):
                        raise BatchRuntimeError("storage_path_changed", f"lock path changed after flock: {path}")
                    if inherited_lock_descriptors is not None:
                        inherited_lock_descriptors.append(parent_fd)
                        inherited_lock_descriptors.extend(ancestor_fds)
                    yield descriptor
                    validate_locked_path(path, descriptor)
                finally:
                    if inherited_lock_descriptors is None:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows only
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                try:
                    yield descriptor
                finally:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                yield descriptor
            _verify_directory_binding(parent_path, parent_fd)
        finally:
            os.close(descriptor)


@contextmanager
def exclusive_lock(path: Path, *, create: bool = True) -> Iterator[None]:
    with locked_file(path, create=create):
        yield


def read_locked_bytes(descriptor: int, *, max_bytes: int | None = None) -> bytes:
    limit = MAX_LOCKED_FILE_BYTES if max_bytes is None else max_bytes
    if type(limit) is not int or limit < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise BatchRuntimeError("unsafe_storage", "locked file must be regular and single-link")
    if metadata.st_size > limit:
        raise BatchRuntimeError(
            "unsafe_storage",
            f"locked file exceeds its read limit of {limit} bytes",
        )
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total_bytes = 0
    while True:
        request_size = min(READ_CHUNK_BYTES, limit - total_bytes + 1)
        chunk = os.read(descriptor, request_size)
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > limit:
            raise BatchRuntimeError(
                "unsafe_storage",
                f"locked file exceeded its read limit of {limit} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def initialize_locked_secret(
    descriptor: int,
    *,
    size: int = 32,
    allow_partial_reset: bool = False,
    fault: StorageFaultHook | None = None,
) -> bytes:
    existing = read_locked_bytes(descriptor)
    if existing:
        if len(existing) != size:
            if not allow_partial_reset:
                raise BatchRuntimeError("storage_path_changed", "lock secret has an invalid size")
        else:
            return existing
    secret = secrets.token_bytes(size)
    os.lseek(descriptor, 0, os.SEEK_SET)
    if fault is not None:
        _write_all(descriptor, secret[:1])
        os.ftruncate(descriptor, 1)
        _fsync(descriptor, label="partial lock secret")
        fault("after_secret_partial_write")
        _write_all(descriptor, secret[1:])
    else:
        _write_all(descriptor, secret)
    os.ftruncate(descriptor, size)
    _fsync(descriptor, label="lock secret")
    return secret
