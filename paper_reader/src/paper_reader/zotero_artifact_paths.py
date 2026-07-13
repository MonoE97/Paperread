from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.storage import (
    AtomicNoReplaceUnsupportedError,
    DirectoryAnchorLike,
    ImmutableTreeSnapshot,
    PublishConflictError,
    new_uuid,
    snapshot_directory_fd,
    validate_directory_anchor,
)


class UnsafeZoteroArtifactPathError(ValueError):
    code = "unsafe_artifact_path"

    def __init__(self, message: str, *, path: Path) -> None:
        super().__init__(message)
        self.data = {"path": str(path)}


@dataclass(frozen=True, slots=True)
class DeterministicArtifactPaths:
    run_dir: Path
    root_name: str
    parent_parts: tuple[str, ...]
    stem: str
    root: Path
    parent: Path
    sidecar: Path
    main: Path


@dataclass(frozen=True, slots=True)
class _AnchoredDirectory:
    parent_fd: int
    name: str
    path: Path
    fd: int
    device: int
    inode: int


@dataclass(slots=True)
class AnchoredArtifactPublication:
    paths: DeterministicArtifactPaths
    run_fd: int
    run_device: int
    run_inode: int
    directories: tuple[_AnchoredDirectory, ...]
    staging_dir: Path | None = None
    staging_fd: int | None = None
    staging_device: int | None = None
    staging_inode: int | None = None
    locked_run_anchor: DirectoryAnchorLike | None = None
    expected_staging_anchor: DirectoryAnchorLike | None = None
    expected_sidecar_snapshot: ImmutableTreeSnapshot | None = None

    @property
    def parent_fd(self) -> int:
        return self.directories[-1].fd

    def publish_sidecar(self, source: Path) -> Path:
        source_path = Path(source)
        if self.staging_dir is None or self.staging_fd is None:
            raise _unsafe(
                source_path,
                "sidecar publication requires one anchored staging directory",
            )
        if source_path.parent != self.staging_dir:
            raise _unsafe(source_path, "sidecar source is outside the anchored staging directory")
        source_name = _safe_component(source_path.name)
        source_fd = _open_directory_entry(
            self.staging_fd,
            source_name,
            source_path,
        )
        destination_fd: int | None = None
        try:
            _fsync_tree_fd(source_fd, source_path)
            source_metadata = os.fstat(source_fd)
            source_snapshot = snapshot_directory_fd(
                source_fd,
                max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
                max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
            )
            sealed_snapshot = self.expected_sidecar_snapshot or source_snapshot
            if source_snapshot != sealed_snapshot:
                raise _unsafe(
                    source_path,
                    "sidecar closed-set changed before publication",
                )
            _validate_anchor_identity(self)
            _require_absent(self.parent_fd, self.paths.stem, self.paths.sidecar)
            _require_absent(self.parent_fd, self.paths.main.name, self.paths.main)
            if source_metadata.st_dev != os.fstat(self.parent_fd).st_dev:
                raise OSError(errno.EXDEV, "sidecar publication requires the same filesystem")
            named_source = os.stat(
                source_name,
                dir_fd=self.staging_fd,
                follow_symlinks=False,
            )
            if not _same_identity(source_metadata, named_source):
                raise _unsafe(source_path, "sidecar source name changed before publication")
            _renameat_no_replace(
                self.staging_fd,
                source_name,
                self.parent_fd,
                self.paths.stem,
                source=source_path,
                destination=self.paths.sidecar,
            )
            os.fsync(self.staging_fd)
            os.fsync(self.parent_fd)
            destination_fd = _open_directory_entry(
                self.parent_fd,
                self.paths.stem,
                self.paths.sidecar,
            )
            destination_metadata = os.fstat(destination_fd)
            if (source_metadata.st_dev, source_metadata.st_ino) != (
                destination_metadata.st_dev,
                destination_metadata.st_ino,
            ):
                named_destination = os.stat(
                    self.paths.stem,
                    dir_fd=self.parent_fd,
                    follow_symlinks=False,
                )
                raise _unsafe(
                    self.paths.sidecar,
                    "published sidecar does not match the anchored staging directory",
                )
            destination_snapshot = snapshot_directory_fd(
                destination_fd,
                max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
                max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
            )
            if destination_snapshot != sealed_snapshot:
                raise _unsafe(
                    self.paths.sidecar,
                    "published sidecar closed-set changed during publication",
                )
            os.fsync(destination_fd)
            os.fsync(self.parent_fd)
            _validate_anchor_identity(self)
            final_destination = os.stat(
                self.paths.stem,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            if not _same_identity(destination_metadata, final_destination):
                raise _unsafe(
                    self.paths.sidecar,
                    "published sidecar name changed before commit",
                )
            return self.paths.sidecar
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            os.close(source_fd)

    def publish_main(self, source: Path, *, expected_bytes: bytes) -> Path:
        source_path = Path(source)
        _validate_anchor_identity(self)
        _require_real_sidecar(self)
        _require_absent(self.parent_fd, self.paths.main.name, self.paths.main)
        source_parent_fd, close_source_parent = _main_source_parent(self, source_path)
        source_fd: int | None = None
        temporary_fd: int | None = None
        owned_temporary: os.stat_result | None = None
        published_fd: int | None = None
        temporary_name = f".{self.paths.main.name}.{new_uuid()}.tmp"
        renamed = False
        try:
            source_name = _safe_component(source_path.name)
            source_fd = _open_regular_entry(source_parent_fd, source_name, source_path)
            source_before = os.fstat(source_fd)
            source_bytes = _read_all(source_fd)
            source_after = os.fstat(source_fd)
            source_identity_before = (
                source_before.st_dev,
                source_before.st_ino,
                source_before.st_size,
                source_before.st_mtime_ns,
            )
            source_identity_after = (
                source_after.st_dev,
                source_after.st_ino,
                source_after.st_size,
                source_after.st_mtime_ns,
            )
            if source_identity_before != source_identity_after or source_bytes != expected_bytes:
                raise _unsafe(source_path, "main publication source changed after validation")
            temporary_fd = os.open(
                temporary_name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o644,
                dir_fd=self.parent_fd,
            )
            owned_temporary = os.fstat(temporary_fd)
            _write_all(temporary_fd, source_bytes)
            os.fsync(temporary_fd)
            temporary_metadata = os.fstat(temporary_fd)
            named_temporary = os.stat(
                temporary_name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(temporary_metadata.st_mode)
                or temporary_metadata.st_nlink != 1
                or not _same_identity(temporary_metadata, named_temporary)
            ):
                raise _unsafe(
                    self.paths.parent / temporary_name,
                    "main temporary file name changed before publication",
                )
            _validate_anchor_identity(self)
            _require_real_sidecar(self)
            _require_absent(self.parent_fd, self.paths.main.name, self.paths.main)
            _renameat_no_replace(
                self.parent_fd,
                temporary_name,
                self.parent_fd,
                self.paths.main.name,
                source=self.paths.parent / temporary_name,
                destination=self.paths.main,
            )
            renamed = True
            os.fsync(self.parent_fd)
            published_fd = _open_regular_entry(
                self.parent_fd,
                self.paths.main.name,
                self.paths.main,
            )
            temporary_metadata = os.fstat(temporary_fd)
            published_metadata = os.fstat(published_fd)
            if (
                temporary_metadata.st_dev,
                temporary_metadata.st_ino,
                temporary_metadata.st_size,
            ) != (
                published_metadata.st_dev,
                published_metadata.st_ino,
                published_metadata.st_size,
            ) or _read_all(published_fd) != source_bytes:
                named_published = os.stat(
                    self.paths.main.name,
                    dir_fd=self.parent_fd,
                    follow_symlinks=False,
                )
                raise _unsafe(
                    self.paths.main,
                    "published main artifact does not match the anchored temporary file",
                )
            os.fsync(published_fd)
            os.fsync(self.parent_fd)
            _validate_anchor_identity(self)
            final_published = os.stat(
                self.paths.main.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            if not _same_identity(published_metadata, final_published):
                raise _unsafe(
                    self.paths.main,
                    "published main name changed before commit",
                )
            return self.paths.main
        finally:
            if published_fd is not None:
                os.close(published_fd)
            if temporary_fd is not None:
                os.close(temporary_fd)
            if source_fd is not None:
                os.close(source_fd)
            if close_source_parent:
                os.close(source_parent_fd)
            if not renamed:
                try:
                    named_temporary = os.stat(
                        temporary_name,
                        dir_fd=self.parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    if owned_temporary is not None and _same_identity(
                        owned_temporary,
                        named_temporary,
                    ):
                        os.unlink(temporary_name, dir_fd=self.parent_fd)
                        os.fsync(self.parent_fd)


def _unsafe(path: Path, message: str) -> UnsafeZoteroArtifactPathError:
    return UnsafeZoteroArtifactPathError(message, path=path)


def _safe_component(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"unsafe deterministic artifact path component: {value!r}")
    return value


def _resolved_real_run_dir(run_dir: Path) -> Path:
    requested = Path(run_dir)
    try:
        metadata = os.lstat(requested)
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise _unsafe(requested, f"run directory is unreadable: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _unsafe(requested, "run directory must be one real directory")
    return resolved


def _validate_real_directory(path: Path, *, run_dir: Path) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise _unsafe(path, f"deterministic artifact directory is unreadable: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _unsafe(path, "deterministic artifact path component must be a real directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _unsafe(path, f"deterministic artifact directory cannot be resolved: {exc}") from exc
    if resolved == run_dir or not resolved.is_relative_to(run_dir):
        raise _unsafe(path, "deterministic artifact directory escapes the run")


def _validate_main_file(path: Path, *, run_dir: Path) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise _unsafe(path, f"deterministic main artifact is unreadable: {exc}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise _unsafe(
            path,
            "deterministic main artifact must be one non-symlink, non-hardlinked regular file",
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _unsafe(path, f"deterministic main artifact cannot be resolved: {exc}") from exc
    if resolved == run_dir or not resolved.is_relative_to(run_dir):
        raise _unsafe(path, "deterministic main artifact escapes the run")


def inspect_deterministic_artifact_paths(
    run_dir: Path,
    *,
    root_name: str,
    parent_parts: tuple[str, ...],
    stem: str,
    allow_existing_sidecar: bool,
    allow_existing_main: bool,
) -> DeterministicArtifactPaths:
    resolved_run = _resolved_real_run_dir(run_dir)
    safe_root_name = _safe_component(root_name)
    safe_parent_parts = tuple(_safe_component(part) for part in parent_parts)
    safe_stem = _safe_component(stem)
    root = resolved_run / safe_root_name
    parent = root.joinpath(*safe_parent_parts)
    sidecar = parent / safe_stem
    main = parent / f"{safe_stem}.json"

    missing_parent = False
    current = resolved_run
    for part in (safe_root_name, *safe_parent_parts):
        current = current / part
        if missing_parent or not os.path.lexists(current):
            missing_parent = True
            continue
        _validate_real_directory(current, run_dir=resolved_run)

    if not missing_parent and os.path.lexists(sidecar):
        _validate_real_directory(sidecar, run_dir=resolved_run)
        if not allow_existing_sidecar:
            raise _unsafe(sidecar, "deterministic sidecar path is already occupied")
    if not missing_parent and os.path.lexists(main):
        _validate_main_file(main, run_dir=resolved_run)
        if not allow_existing_main:
            raise _unsafe(main, "deterministic main artifact path is already occupied")

    return DeterministicArtifactPaths(
        run_dir=resolved_run,
        root_name=safe_root_name,
        parent_parts=safe_parent_parts,
        stem=safe_stem,
        root=root,
        parent=parent,
        sidecar=sidecar,
        main=main,
    )


_DIRECTORY_FLAGS = (
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


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _open_directory_entry(parent_fd: int, name: str, path: Path) -> int:
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _unsafe(path, f"anchored directory open failed: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or not _same_identity(opened, current)
        ):
            raise _unsafe(path, "anchored directory entry changed during no-follow open")
        return descriptor
    except UnsafeZoteroArtifactPathError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise _unsafe(path, f"anchored directory identity check failed: {exc}") from exc


def _open_regular_entry(parent_fd: int, name: str, path: Path) -> int:
    try:
        descriptor = os.open(name, _REGULAR_READ_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _unsafe(path, f"anchored regular file open failed: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or opened.st_nlink != 1
            or current.st_nlink != 1
            or not _same_identity(opened, current)
        ):
            raise _unsafe(
                path,
                "anchored file must be one non-symlink, non-hardlinked regular file",
            )
        return descriptor
    except UnsafeZoteroArtifactPathError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise _unsafe(path, f"anchored file identity check failed: {exc}") from exc


def _require_absent(parent_fd: int, name: str, path: Path) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _unsafe(path, f"deterministic artifact entry cannot be inspected: {exc}") from exc
    raise _unsafe(path, "deterministic artifact destination is already occupied")


def _require_real_sidecar(anchor: AnchoredArtifactPublication) -> None:
    descriptor = _open_directory_entry(
        anchor.parent_fd,
        anchor.paths.stem,
        anchor.paths.sidecar,
    )
    os.close(descriptor)


def _validate_existing_destination_state(
    anchor: AnchoredArtifactPublication,
    *,
    allow_existing_sidecar: bool,
    allow_existing_main: bool,
) -> None:
    try:
        sidecar_fd = _open_directory_entry(
            anchor.parent_fd,
            anchor.paths.stem,
            anchor.paths.sidecar,
        )
    except UnsafeZoteroArtifactPathError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            sidecar_fd = None
        else:
            raise
    if sidecar_fd is not None:
        os.close(sidecar_fd)
        if not allow_existing_sidecar:
            raise _unsafe(anchor.paths.sidecar, "deterministic sidecar path is already occupied")
    try:
        main_fd = _open_regular_entry(
            anchor.parent_fd,
            anchor.paths.main.name,
            anchor.paths.main,
        )
    except UnsafeZoteroArtifactPathError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            main_fd = None
        else:
            raise
    if main_fd is not None:
        os.close(main_fd)
        if not allow_existing_main:
            raise _unsafe(anchor.paths.main, "deterministic main artifact path is already occupied")


def _validate_anchor_identity(anchor: AnchoredArtifactPublication) -> None:
    if anchor.locked_run_anchor is not None:
        try:
            validate_directory_anchor(anchor.locked_run_anchor)
        except Exception as exc:
            raise _unsafe(
                anchor.paths.run_dir,
                f"locked run directory identity changed: {exc}",
            ) from exc
    if anchor.expected_staging_anchor is not None:
        try:
            validate_directory_anchor(anchor.expected_staging_anchor)
        except Exception as exc:
            raise _unsafe(
                anchor.expected_staging_anchor.path,
                f"staging directory identity changed: {exc}",
            ) from exc
    try:
        opened_run = os.fstat(anchor.run_fd)
        current_run = os.lstat(anchor.paths.run_dir)
    except OSError as exc:
        raise _unsafe(anchor.paths.run_dir, f"anchored run directory changed: {exc}") from exc
    if (
        not stat.S_ISDIR(opened_run.st_mode)
        or not stat.S_ISDIR(current_run.st_mode)
        or stat.S_ISLNK(current_run.st_mode)
        or (opened_run.st_dev, opened_run.st_ino) != (anchor.run_device, anchor.run_inode)
        or not _same_identity(opened_run, current_run)
    ):
        raise _unsafe(anchor.paths.run_dir, "anchored run directory identity changed")
    for component in anchor.directories:
        try:
            opened = os.fstat(component.fd)
            current = os.stat(
                component.name,
                dir_fd=component.parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise _unsafe(component.path, f"anchored directory identity changed: {exc}") from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (component.device, component.inode)
            or not _same_identity(opened, current)
        ):
            raise _unsafe(component.path, "anchored directory identity changed")
    if anchor.staging_fd is not None and anchor.staging_dir is not None:
        try:
            opened = os.fstat(anchor.staging_fd)
            current = os.stat(
                anchor.staging_dir.name,
                dir_fd=anchor.run_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise _unsafe(anchor.staging_dir, f"anchored staging directory changed: {exc}") from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (anchor.staging_device, anchor.staging_inode)
            or not _same_identity(opened, current)
        ):
            raise _unsafe(anchor.staging_dir, "anchored staging directory identity changed")


def _fsync_tree_fd(directory_fd: int, path: Path) -> None:
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise _unsafe(path, f"anchored staging tree cannot be listed: {exc}") from exc
    for name in names:
        entry_path = path / name
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise _unsafe(entry_path, f"anchored staging entry cannot be inspected: {exc}") from exc
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory_entry(directory_fd, name, entry_path)
            try:
                _fsync_tree_fd(child_fd, entry_path)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            child_fd = _open_regular_entry(directory_fd, name, entry_path)
            try:
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
        else:
            raise _unsafe(
                entry_path,
                "anchored staging tree contains a symlink, hardlink, or special file",
            )
    os.fsync(directory_fd)


def _remove_tree_contents_fd(directory_fd: int) -> None:
    for name in sorted(os.listdir(directory_fd)):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory_entry(directory_fd, name, Path(name))
            try:
                _remove_tree_contents_fd(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _read_all(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write during anchored artifact publication")
        view = view[written:]


def _main_source_parent(
    anchor: AnchoredArtifactPublication,
    source: Path,
) -> tuple[int, bool]:
    if (
        anchor.staging_dir is not None
        and anchor.staging_fd is not None
        and source.parent == anchor.staging_dir
    ):
        return anchor.staging_fd, False
    if source.parent == anchor.paths.sidecar:
        descriptor = _open_directory_entry(
            anchor.parent_fd,
            anchor.paths.stem,
            anchor.paths.sidecar,
        )
        return descriptor, True
    raise _unsafe(source, "main source is outside the anchored staging or sidecar directory")


def _raise_rename_error(
    error_number: int,
    *,
    source: Path,
    destination: Path,
) -> None:
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise PublishConflictError(destination)
    unsupported = {errno.ENOSYS, errno.ENOTSUP}
    if hasattr(errno, "EOPNOTSUPP"):
        unsupported.add(errno.EOPNOTSUPP)
    if error_number in unsupported:
        raise AtomicNoReplaceUnsupportedError(sys.platform)
    raise OSError(error_number, os.strerror(error_number), source, destination)


def _renameat_no_replace(
    source_dir_fd: int,
    source_name: str,
    destination_dir_fd: int,
    destination_name: str,
    *,
    source: Path,
    destination: Path,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            renameatx_np = libc.renameatx_np
        except AttributeError as exc:
            raise AtomicNoReplaceUnsupportedError(sys.platform) from exc
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
            source_dir_fd,
            os.fsencode(source_name),
            destination_dir_fd,
            os.fsencode(destination_name),
            0x00000004,
        )
    elif sys.platform.startswith("linux"):
        try:
            renameat2 = libc.renameat2
        except AttributeError as exc:
            raise AtomicNoReplaceUnsupportedError(sys.platform) from exc
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
            source_dir_fd,
            os.fsencode(source_name),
            destination_dir_fd,
            os.fsencode(destination_name),
            1,
        )
    else:
        raise AtomicNoReplaceUnsupportedError(sys.platform)
    if result != 0:
        _raise_rename_error(
            ctypes.get_errno(),
            source=source,
            destination=destination,
        )


@contextmanager
def anchored_artifact_publication(
    paths: DeterministicArtifactPaths,
    *,
    staging_dir: Path | None,
    allow_existing_sidecar: bool,
    allow_existing_main: bool,
    expected_run_anchor: DirectoryAnchorLike | None = None,
    expected_staging_anchor: DirectoryAnchorLike | None = None,
    expected_sidecar_snapshot: ImmutableTreeSnapshot | None = None,
) -> Iterator[AnchoredArtifactPublication]:
    descriptors: list[int] = []
    staging_fd: int | None = None
    try:
        try:
            if expected_run_anchor is None:
                run_fd = os.open(paths.run_dir, _DIRECTORY_FLAGS)
            else:
                validate_directory_anchor(expected_run_anchor)
                if Path(os.path.abspath(paths.run_dir)) != Path(
                    os.path.abspath(expected_run_anchor.path)
                ):
                    raise _unsafe(
                        paths.run_dir,
                        "artifact run directory differs from the locked run anchor",
                    )
                run_fd = os.dup(expected_run_anchor.descriptor)
        except UnsafeZoteroArtifactPathError:
            raise
        except OSError as exc:
            raise _unsafe(paths.run_dir, f"anchored run directory open failed: {exc}") from exc
        descriptors.append(run_fd)
        opened_run = os.fstat(run_fd)
        current_run = os.lstat(paths.run_dir)
        if (
            not stat.S_ISDIR(opened_run.st_mode)
            or not stat.S_ISDIR(current_run.st_mode)
            or stat.S_ISLNK(current_run.st_mode)
            or not _same_identity(opened_run, current_run)
        ):
            raise _unsafe(paths.run_dir, "run directory changed during anchored open")
        if expected_run_anchor is not None and (
            opened_run.st_dev,
            opened_run.st_ino,
        ) != (expected_run_anchor.device, expected_run_anchor.inode):
            raise _unsafe(paths.run_dir, "artifact publisher does not match the locked run")
        components: list[_AnchoredDirectory] = []
        parent_fd = run_fd
        current_path = paths.run_dir
        for part in (paths.root_name, *paths.parent_parts):
            current_path = current_path / part
            try:
                child_fd = _open_directory_entry(parent_fd, part, current_path)
            except UnsafeZoteroArtifactPathError as exc:
                if not isinstance(exc.__cause__, FileNotFoundError):
                    raise
                try:
                    os.mkdir(part, mode=0o755, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                    child_fd = _open_directory_entry(parent_fd, part, current_path)
                except OSError as mkdir_exc:
                    raise _unsafe(
                        current_path,
                        f"anchored artifact directory creation failed: {mkdir_exc}",
                    ) from mkdir_exc
            descriptors.append(child_fd)
            metadata = os.fstat(child_fd)
            components.append(
                _AnchoredDirectory(
                    parent_fd=parent_fd,
                    name=part,
                    path=current_path,
                    fd=child_fd,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                )
            )
            parent_fd = child_fd

        resolved_staging: Path | None = None
        staging_metadata: os.stat_result | None = None
        if staging_dir is not None:
            requested_staging = Path(staging_dir)
            if requested_staging.parent != paths.run_dir:
                raise _unsafe(
                    requested_staging,
                    "staging directory must be one direct child of the anchored run",
                )
            _safe_component(requested_staging.name)
            resolved_staging = paths.run_dir / requested_staging.name
            staging_fd = _open_directory_entry(
                run_fd,
                resolved_staging.name,
                resolved_staging,
            )
            descriptors.append(staging_fd)
            staging_metadata = os.fstat(staging_fd)
            if expected_staging_anchor is not None and (
                staging_metadata.st_dev,
                staging_metadata.st_ino,
            ) != (expected_staging_anchor.device, expected_staging_anchor.inode):
                raise _unsafe(
                    resolved_staging,
                    "artifact staging directory differs from the expected staging anchor",
                )
            if expected_staging_anchor is not None:
                validate_directory_anchor(expected_staging_anchor)
        elif expected_staging_anchor is not None:
            raise _unsafe(
                expected_staging_anchor.path,
                "expected staging anchor requires a staging directory",
            )
        if expected_sidecar_snapshot is not None and staging_dir is None:
            raise _unsafe(
                paths.run_dir,
                "expected sidecar snapshot requires a staging directory",
            )

        anchor = AnchoredArtifactPublication(
            paths=paths,
            run_fd=run_fd,
            run_device=opened_run.st_dev,
            run_inode=opened_run.st_ino,
            directories=tuple(components),
            staging_dir=resolved_staging,
            staging_fd=staging_fd,
            staging_device=None if staging_metadata is None else staging_metadata.st_dev,
            staging_inode=None if staging_metadata is None else staging_metadata.st_ino,
            locked_run_anchor=expected_run_anchor,
            expected_staging_anchor=expected_staging_anchor,
            expected_sidecar_snapshot=expected_sidecar_snapshot,
        )
        _validate_existing_destination_state(
            anchor,
            allow_existing_sidecar=allow_existing_sidecar,
            allow_existing_main=allow_existing_main,
        )
        yield anchor
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


__all__ = [
    "AnchoredArtifactPublication",
    "DeterministicArtifactPaths",
    "UnsafeZoteroArtifactPathError",
    "anchored_artifact_publication",
    "inspect_deterministic_artifact_paths",
]
