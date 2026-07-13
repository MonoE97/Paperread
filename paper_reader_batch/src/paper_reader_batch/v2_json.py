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
from typing import Any, Callable, Iterator

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
        try:
            return sorted(os.listdir(descriptor))
        except OSError as exc:
            raise _storage_error(f"cannot list directory safely: {path}", exc)


def _read_regular_single_link(
    parent_fd: int,
    name: str,
    *,
    code: str,
    fault: StorageFaultHook | None = None,
) -> bytes:
    try:
        path_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise BatchRuntimeError(code, f"file does not exist: {name}") from exc
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
        if fault is not None:
            fault("after_open")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
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
        )
    finally:
        os.close(descriptor)


def walk_relative_regular_files(
    directory_fd: int,
    relative_root: str,
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

        def walk(current_fd: int, prefix: PurePosixPath) -> None:
            before_names = tuple(sorted(os.listdir(current_fd)))
            for name in before_names:
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
            if tuple(sorted(os.listdir(current_fd))) != before_names:
                raise BatchRuntimeError("storage_path_changed", "bundle membership changed during walk")

        walk(descriptor, PurePosixPath())
        return found
    finally:
        os.close(descriptor)


def read_bytes(
    path: Path,
    *,
    code: str = "artifact_unreadable",
    fault: StorageFaultHook | None = None,
) -> bytes:
    with _open_parent_fd(path, create=False) as (parent_fd, _parent, name):
        return _read_regular_single_link(parent_fd, name, code=code, fault=fault)


def read_json_bytes(path: Path, *, code: str = "artifact_unreadable") -> tuple[bytes, Any]:
    raw = read_bytes(path, code=code)
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


def _cleanup_internal_writing(parent_fd: int, parent_path: Path, target_name: str) -> None:
    pattern = re.compile(rf"^\.{re.escape(target_name)}\.[0-9a-f]{{32}}\.writing$")
    try:
        names = os.listdir(parent_fd)
    except OSError as exc:
        raise _storage_error(f"cannot list staging entries for {target_name}", exc)
    changed = False
    for candidate in names:
        if pattern.fullmatch(candidate) is None:
            continue
        metadata = _existing_entry(parent_fd, candidate)
        if metadata is None:
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BatchRuntimeError("unsafe_storage", f"internal writing entry is unsafe: {candidate}")
        try:
            os.unlink(candidate, dir_fd=parent_fd)
        except OSError as exc:
            raise _storage_error(f"cannot clean internal writing entry: {candidate}", exc)
        changed = True
    if changed:
        _verify_directory_binding(parent_path, parent_fd)
        _fsync(parent_fd, label=str(parent_path))


def entry_exists(path: Path) -> bool:
    with _open_parent_fd(path, create=False) as (parent_fd, _parent, name):
        return _existing_entry(parent_fd, name) is not None


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


def unlink_regular_exact(path: Path, expected: bytes) -> None:
    with _open_parent_fd(path, create=False) as (parent_fd, parent_path, name):
        if _read_regular_single_link(parent_fd, name, code="storage_error") != expected:
            raise BatchRuntimeError("storage_path_changed", f"refusing to unlink changed internal file: {path}")
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError as exc:
            raise _storage_error(f"cannot unlink internal file safely: {path}", exc)
        _verify_directory_binding(parent_path, parent_fd)
        _fsync(parent_fd, label=str(parent_path))


def unlink_internal_regular(path: Path) -> None:
    """Remove only a runtime-owned staging entry after an external name check."""
    with _open_parent_fd(path, create=False) as (parent_fd, parent_path, name):
        metadata = _existing_entry(parent_fd, name)
        if metadata is None:
            return
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BatchRuntimeError("unsafe_storage", f"internal staging entry is not regular single-link: {path}")
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError as exc:
            raise _storage_error(f"cannot remove internal staging entry safely: {path}", exc)
        _verify_directory_binding(parent_path, parent_fd)
        _fsync(parent_fd, label=str(parent_path))


def publish_bytes_no_replace(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    allow_existing_exact: bool = False,
    create_parent: bool = False,
    fault: StorageFaultHook | None = None,
) -> None:
    with _open_parent_fd(path, create=create_parent) as (parent_fd, parent_path, name):
        existing = _existing_entry(parent_fd, name)
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                raise BatchRuntimeError("output_conflict", f"existing target is not regular single-link: {path}")
            if allow_existing_exact and _read_regular_single_link(parent_fd, name, code="output_conflict") == data:
                return
            raise BatchRuntimeError("output_conflict", f"target is already occupied: {path}")

        _cleanup_internal_writing(parent_fd, parent_path, name)

        temporary_name = f".{name}.{sha256_bytes(data)}.tmp"
        writing_name = ""
        descriptor = -1
        try:
            temporary = _existing_entry(parent_fd, temporary_name)
            if temporary is not None:
                if (
                    not stat.S_ISREG(temporary.st_mode)
                    or temporary.st_nlink != 1
                    or _read_regular_single_link(parent_fd, temporary_name, code="storage_error") != data
                ):
                    raise BatchRuntimeError("unsafe_storage", f"staging file is unsafe or changed: {temporary_name}")
            else:
                writing_name = f".{name}.{secrets.token_hex(16)}.writing"
                descriptor = os.open(
                    writing_name,
                    _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                    mode,
                    dir_fd=parent_fd,
                )
                _write_all(descriptor, data)
                if fault is not None:
                    fault("before_file_fsync")
                _fsync(descriptor, label=str(path))
                if fault is not None:
                    fault("after_writing_fsync")
                os.close(descriptor)
                descriptor = -1
                _rename_no_replace(parent_fd, writing_name, temporary_name)
                writing_name = ""
                _verify_directory_binding(parent_path, parent_fd)
                _fsync(parent_fd, label=str(parent_path))
                if fault is not None:
                    fault("after_file_fsync")
                if fault is not None:
                    fault("after_pending_rename")
            _rename_no_replace(parent_fd, temporary_name, name)
            temporary_name = ""
            if fault is not None:
                fault("after_rename")
            if fault is not None:
                fault("before_parent_fsync")
            _verify_directory_binding(parent_path, parent_fd)
            _fsync(parent_fd, label=str(parent_path))
        except BatchRuntimeError:
            raise
        except OSError as exc:
            raise _storage_error(f"cannot publish artifact safely: {path}", exc)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if writing_name:
                try:
                    os.unlink(writing_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass


def replace_bytes_atomic(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    expected_current: bytes | None = None,
    fault: StorageFaultHook | None = None,
) -> None:
    with _open_parent_fd(path, create=False) as (parent_fd, parent_path, name):
        existing = _existing_entry(parent_fd, name)
        if existing is None or not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
            raise BatchRuntimeError("unsafe_storage", f"replace target must be an existing regular single-link file: {path}")
        if expected_current is not None:
            current = _read_regular_single_link(parent_fd, name, code="storage_error")
            if current != expected_current:
                raise BatchRuntimeError("storage_path_changed", f"replace target changed before commit: {path}")
        _cleanup_internal_writing(parent_fd, parent_path, name)
        temporary_name = f".{name}.{sha256_bytes(data)}.tmp"
        writing_name = ""
        descriptor = -1
        try:
            temporary = _existing_entry(parent_fd, temporary_name)
            if temporary is not None:
                if (
                    not stat.S_ISREG(temporary.st_mode)
                    or temporary.st_nlink != 1
                    or _read_regular_single_link(parent_fd, temporary_name, code="storage_error") != data
                ):
                    raise BatchRuntimeError("unsafe_storage", f"replace staging file is unsafe or changed: {temporary_name}")
            else:
                writing_name = f".{name}.{secrets.token_hex(16)}.writing"
                descriptor = os.open(
                    writing_name,
                    _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                    mode,
                    dir_fd=parent_fd,
                )
                _write_all(descriptor, data)
                if fault is not None:
                    fault("before_file_fsync")
                _fsync(descriptor, label=str(path))
                if fault is not None:
                    fault("after_writing_fsync")
                os.close(descriptor)
                descriptor = -1
                _rename_no_replace(parent_fd, writing_name, temporary_name)
                writing_name = ""
                _verify_directory_binding(parent_path, parent_fd)
                _fsync(parent_fd, label=str(parent_path))
                if fault is not None:
                    fault("after_file_fsync")
                if fault is not None:
                    fault("after_pending_rename")
            os.replace(temporary_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temporary_name = ""
            if fault is not None:
                fault("before_parent_fsync")
            _verify_directory_binding(parent_path, parent_fd)
            _fsync(parent_fd, label=str(parent_path))
        except BatchRuntimeError:
            raise
        except OSError as exc:
            raise _storage_error(f"cannot replace artifact safely: {path}", exc)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if writing_name:
                try:
                    os.unlink(writing_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass


def promote_bytes_no_replace(staging: Path, target: Path, expected: bytes) -> None:
    source = normalized_absolute_path(staging)
    destination = normalized_absolute_path(target)
    if source.parent != destination.parent:
        raise BatchRuntimeError("unsafe_path", "staging and target files must share one parent")
    with open_directory_fd(source.parent, create=False) as (parent_fd, parent_path):
        source_metadata = _existing_entry(parent_fd, source.name)
        if source_metadata is None:
            raise BatchRuntimeError("storage_missing", f"staging file disappeared: {source}")
        if (
            not stat.S_ISREG(source_metadata.st_mode)
            or source_metadata.st_nlink != 1
            or _read_regular_single_link(parent_fd, source.name, code="storage_error") != expected
        ):
            raise BatchRuntimeError("unsafe_storage", f"staging file is unsafe or changed: {source}")
        if _existing_entry(parent_fd, destination.name) is not None:
            raise BatchRuntimeError("output_conflict", f"target is occupied while promoting staging: {destination}")
        _rename_no_replace(parent_fd, source.name, destination.name)
        _verify_directory_binding(parent_path, parent_fd)
        _fsync(parent_fd, label=str(parent_path))


def fsync_directory(path: Path) -> None:
    with open_directory_fd(path, create=False) as (descriptor, normalized):
        _fsync(descriptor, label=str(normalized))


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
        if _existing_entry(parent_fd, destination.name) is not None:
            raise BatchRuntimeError("output_conflict", f"target directory already exists: {destination}")
        _rename_no_replace(parent_fd, source.name, destination.name)
        if fault is not None:
            fault("after_rename")
        _verify_directory_binding(parent_path, parent_fd)
        if fault is not None:
            fault("before_parent_fsync")
        _fsync(parent_fd, label=str(parent_path))


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
) -> Iterator[int | None]:
    """Hold the directory above a lock parent so replacing the parent cannot fork the lock domain."""

    parent = path.parent
    ancestor = parent.parent
    if not enabled or fcntl is None or ancestor == parent:
        yield None
        return
    with open_directory_fd(ancestor, create=create) as (descriptor, _normalized):
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield descriptor
        finally:
            if not preserve_on_dup:
                fcntl.flock(descriptor, fcntl.LOCK_UN)


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
    ) as ancestor_fd, _open_parent_fd(normalized, create=create) as (parent_fd, parent_path, name):
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
                        if ancestor_fd is not None:
                            inherited_lock_descriptors.append(ancestor_fd)
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


def read_locked_bytes(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
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
