from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


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


def ensure_safe_publication_parent(paths: DeterministicArtifactPaths) -> None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current = paths.run_dir
    try:
        parent_descriptor = os.open(paths.run_dir, directory_flags)
    except OSError as exc:
        raise _unsafe(paths.run_dir, f"safe run directory open failed: {exc}") from exc
    try:
        for part in (paths.root_name, *paths.parent_parts):
            current = current / part
            try:
                child_descriptor = os.open(part, directory_flags, dir_fd=parent_descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=parent_descriptor)
                    child_descriptor = os.open(part, directory_flags, dir_fd=parent_descriptor)
                except OSError as exc:
                    raise _unsafe(
                        current,
                        f"safe deterministic artifact directory creation failed: {exc}",
                    ) from exc
            except OSError as exc:
                raise _unsafe(
                    current,
                    f"safe deterministic artifact directory open failed: {exc}",
                ) from exc
            child_metadata = os.fstat(child_descriptor)
            try:
                path_metadata = os.lstat(current)
            except OSError as exc:
                os.close(child_descriptor)
                raise _unsafe(current, f"deterministic artifact directory changed: {exc}") from exc
            if (
                not stat.S_ISDIR(child_metadata.st_mode)
                or stat.S_ISLNK(path_metadata.st_mode)
                or not stat.S_ISDIR(path_metadata.st_mode)
                or (child_metadata.st_dev, child_metadata.st_ino)
                != (path_metadata.st_dev, path_metadata.st_ino)
            ):
                os.close(child_descriptor)
                raise _unsafe(current, "deterministic artifact directory changed during no-follow open")
            os.close(parent_descriptor)
            parent_descriptor = child_descriptor
            _validate_real_directory(current, run_dir=paths.run_dir)
    finally:
        os.close(parent_descriptor)
    inspect_deterministic_artifact_paths(
        paths.run_dir,
        root_name=paths.root_name,
        parent_parts=paths.parent_parts,
        stem=paths.stem,
        allow_existing_sidecar=False,
        allow_existing_main=False,
    )


def revalidate_before_main_publication(paths: DeterministicArtifactPaths) -> None:
    inspect_deterministic_artifact_paths(
        paths.run_dir,
        root_name=paths.root_name,
        parent_parts=paths.parent_parts,
        stem=paths.stem,
        allow_existing_sidecar=True,
        allow_existing_main=False,
    )


__all__ = [
    "DeterministicArtifactPaths",
    "UnsafeZoteroArtifactPathError",
    "ensure_safe_publication_parent",
    "inspect_deterministic_artifact_paths",
    "revalidate_before_main_publication",
]
