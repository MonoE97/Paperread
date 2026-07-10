from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def skill_root_for_zotero_run(run_dir: Path) -> Path:
    resolved = Path(run_dir).resolve(strict=True)
    if resolved.parent.parent.name != "runs":
        raise ValueError(f"Zotero run is outside <skill_root>/runs/YYYY-MM-DD: {resolved}")
    return resolved.parent.parent.parent


@contextmanager
def locked_zotero_parent(run_dir: Path, parent_key: str) -> Iterator[None]:
    skill_root = skill_root_for_zotero_run(run_dir)
    lock_dir = skill_root / ".zotero-parent-locks"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    filename = hashlib.sha256(parent_key.encode("utf-8")).hexdigest() + ".lock"
    lock_path = lock_dir / filename
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Zotero parent lock must be a regular file: {lock_path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


__all__ = ["locked_zotero_parent", "skill_root_for_zotero_run"]
