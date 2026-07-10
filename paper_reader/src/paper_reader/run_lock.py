from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from paper_reader.v2_loader import LoadedRun, load_v2_run


@contextmanager
def locked_v2_run(run_path: Path) -> Iterator[LoadedRun]:
    initial = load_v2_run(run_path)
    run_dir = initial.manifest_path.resolve(strict=True).parent
    lock_path = run_dir / ".run.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"run lock must be a regular file: {lock_path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield load_v2_run(initial.manifest_path)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


__all__ = ["locked_v2_run"]
