from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from paper_reader.v2_loader import (
    DirectoryAnchor,
    LoadedRun,
    RunLoadError,
    _load_v2_run_from_anchor,
    load_v2_run,
)


class RunLockError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _verify_run_directory(anchor: DirectoryAnchor) -> None:
    try:
        with DirectoryAnchor.open(
            anchor.path,
            manifest_path=anchor.path / "run.json",
        ) as current_anchor:
            current = (current_anchor.device, current_anchor.inode)
    except (OSError, RunLoadError) as exc:
        raise RunLockError(
            "run_directory_changed",
            f"run directory changed while acquiring its lock: {anchor.path}",
        ) from exc
    if (
        current != (anchor.device, anchor.inode)
    ):
        raise RunLockError(
            "run_directory_changed",
            f"run directory changed while acquiring its lock: {anchor.path}",
        )


def _verify_named_lock(anchor: DirectoryAnchor, descriptor: int) -> None:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise RunLockError(
            "run_lock_unsafe",
            f"run lock must be a single-link regular file: {anchor.path / '.run.lock'}",
        )
    try:
        named = os.stat(".run.lock", dir_fd=anchor.descriptor, follow_symlinks=False)
    except OSError as exc:
        raise RunLockError(
            "run_lock_changed",
            f"run lock changed while acquiring it: {anchor.path / '.run.lock'}",
        ) from exc
    if (
        not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or named.st_dev != opened.st_dev
        or named.st_ino != opened.st_ino
    ):
        raise RunLockError(
            "run_lock_changed",
            f"run lock changed while acquiring it: {anchor.path / '.run.lock'}",
        )


@contextmanager
def locked_v2_run(run_path: Path) -> Iterator[LoadedRun]:
    initial = load_v2_run(run_path)
    with DirectoryAnchor.open(
        initial.manifest_path.parent,
        manifest_path=initial.manifest_path,
    ) as anchor:
        if (
            anchor.device != initial.run_directory_device
            or anchor.inode != initial.run_directory_inode
        ):
            raise RunLockError(
                "run_directory_changed",
                f"run directory changed before acquiring its lock: {anchor.path}",
            )
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(
                ".run.lock",
                flags,
                0o644,
                dir_fd=anchor.descriptor,
            )
        except OSError as exc:
            raise RunLockError(
                "run_lock_unsafe",
                f"run lock cannot be opened safely: {anchor.path / '.run.lock'}: {exc}",
            ) from exc
        try:
            _verify_named_lock(anchor, descriptor)
            fcntl.flock(anchor.descriptor, fcntl.LOCK_EX)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    _verify_run_directory(anchor)
                    _verify_named_lock(anchor, descriptor)
                    yield _load_v2_run_from_anchor(
                        anchor,
                        manifest_name=initial.manifest_path.name,
                        manifest_path=initial.manifest_path,
                        expose_anchor=True,
                    )
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                fcntl.flock(anchor.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


__all__ = ["RunLockError", "locked_v2_run"]
