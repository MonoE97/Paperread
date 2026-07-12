from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from paper_reader.storage import (
    canonical_json_sha256,
    read_anchored_bytes,
    safe_relative_artifact_path,
    snapshot_anchored_tree,
    validate_directory_anchor,
)

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


@dataclass(frozen=True, slots=True)
class ExpectedRunArtifact:
    path: str
    sha256: str
    kind: Literal["file", "tree"] = "file"


class RunArtifactExpectationError(ValueError):
    pass


def _verify_expected_run_artifacts(
    anchor: DirectoryAnchor,
    expected_artifacts: tuple[ExpectedRunArtifact, ...],
) -> None:
    seen: set[str] = set()
    for expected in expected_artifacts:
        relative = safe_relative_artifact_path(expected.path)
        if relative != expected.path or relative in seen:
            raise RunArtifactExpectationError(
                "expected run artifact paths must be unique and canonical"
            )
        seen.add(relative)
        if (
            len(expected.sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected.sha256)
        ):
            raise RunArtifactExpectationError(
                "expected run artifact digest is not canonical SHA-256"
            )
        artifact_path = anchor.path / relative
        if expected.kind == "file":
            actual_digest = hashlib.sha256(
                read_anchored_bytes(anchor, artifact_path)
            ).hexdigest()
        elif expected.kind == "tree":
            actual_digest = canonical_json_sha256(
                snapshot_anchored_tree(anchor, artifact_path)
            )
        else:  # pragma: no cover - Literal plus frozen construction is defensive
            raise RunArtifactExpectationError("unknown expected run artifact kind")
        if actual_digest != expected.sha256:
            raise RunArtifactExpectationError(
                f"run artifact changed after read-only preflight: {relative}"
            )
    validate_directory_anchor(anchor)


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
def locked_v2_run(
    run_path: Path,
    *,
    expected_run_path: Path | None = None,
    expected_run_device: int | None = None,
    expected_run_inode: int | None = None,
    expected_run_manifest_sha256: str | None = None,
    expected_artifacts: tuple[ExpectedRunArtifact, ...] = (),
) -> Iterator[LoadedRun]:
    initial = load_v2_run(run_path)
    expected_identity_mismatch = (
        expected_run_path is not None
        and Path(os.path.abspath(expected_run_path)) != initial.manifest_path.parent
        or expected_run_device is not None
        and expected_run_device != initial.run_directory_device
        or expected_run_inode is not None
        and expected_run_inode != initial.run_directory_inode
    )
    if expected_identity_mismatch:
        raise RunLoadError(
            "run_directory_changed",
            f"run directory changed after read-only preflight: {initial.manifest_path.parent}",
            manifest_path=initial.manifest_path,
        )
    if (
        expected_run_manifest_sha256 is not None
        and expected_run_manifest_sha256 != initial.manifest_sha256
    ):
        raise RunLoadError(
            "run_manifest_changed",
            f"run manifest changed after read-only preflight: {initial.manifest_path}",
            manifest_path=initial.manifest_path,
        )
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
        try:
            _verify_expected_run_artifacts(anchor, expected_artifacts)
        except (OSError, ValueError) as exc:
            raise RunLoadError(
                "run_artifact_changed",
                f"run artifact changed after read-only preflight: {anchor.path}",
                manifest_path=initial.manifest_path,
            ) from exc
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
                    try:
                        _verify_expected_run_artifacts(anchor, expected_artifacts)
                    except (OSError, ValueError) as exc:
                        raise RunLoadError(
                            "run_artifact_changed",
                            (
                                "run artifact changed after acquiring the run lock: "
                                f"{anchor.path}"
                            ),
                            manifest_path=initial.manifest_path,
                        ) from exc
                    locked = _load_v2_run_from_anchor(
                        anchor,
                        manifest_name=initial.manifest_path.name,
                        manifest_path=initial.manifest_path,
                        expose_anchor=True,
                    )
                    if (
                        expected_run_manifest_sha256 is not None
                        and locked.manifest_sha256 != expected_run_manifest_sha256
                    ):
                        raise RunLoadError(
                            "run_manifest_changed",
                            (
                                "run manifest changed after read-only preflight: "
                                f"{locked.manifest_path}"
                            ),
                            manifest_path=locked.manifest_path,
                        )
                    yield locked
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                fcntl.flock(anchor.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


__all__ = [
    "ExpectedRunArtifact",
    "RunArtifactExpectationError",
    "RunLockError",
    "locked_v2_run",
]
