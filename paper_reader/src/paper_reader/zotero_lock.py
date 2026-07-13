from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from paper_reader.run_lock import ExpectedRunArtifact, _verify_expected_run_artifacts
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.storage import (
    OwnedDirectoryAnchor,
    UnsafeStoragePathError,
    create_anchored_directory,
    open_anchored_directory,
    read_anchored_bytes,
    validate_directory_anchor,
)
from paper_reader.v2_loader import DirectoryAnchor, RunLoadError


LOCK_DIRECTORY_NAME = ".zotero-parent-locks"


class ZoteroLockError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def skill_root_for_zotero_run(run_dir: Path) -> Path:
    lexical = Path(os.path.abspath(Path(run_dir).expanduser()))
    if lexical.parent.parent.name != "runs":
        raise ValueError(f"Zotero run is outside <skill_root>/runs/YYYY-MM-DD: {lexical}")
    return lexical.parent.parent.parent


def _same_entry(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _open_or_create_lock_directory(
    root_anchor: DirectoryAnchor,
    lock_dir: Path,
) -> OwnedDirectoryAnchor:
    try:
        return create_anchored_directory(root_anchor, lock_dir, mode=0o700)
    except FileExistsError:
        return open_anchored_directory(root_anchor, lock_dir)


def _verify_named_lock(
    lock_directory: OwnedDirectoryAnchor,
    *,
    filename: str,
    descriptor: int,
) -> None:
    try:
        opened = os.fstat(descriptor)
        named = os.stat(
            filename,
            dir_fd=lock_directory.descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ZoteroLockError(
            "authorization_lock_unsafe",
            "Zotero authorization lock changed while it was held",
        ) from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or opened.st_nlink != 1
        or named.st_nlink != 1
        or not _same_entry(opened, named)
    ):
        raise ZoteroLockError(
            "authorization_lock_unsafe",
            "Zotero authorization lock must be one single-link regular file",
        )


@dataclass(frozen=True, slots=True)
class LockedZoteroParent:
    skill_root: Path
    parent_key: str
    root_anchor: DirectoryAnchor
    run_anchor: DirectoryAnchor | None
    lock_directory: OwnedDirectoryAnchor
    lock_filename: str
    lock_descriptor: int

    def validate(self) -> None:
        try:
            validate_directory_anchor(self.root_anchor)
            if self.run_anchor is not None:
                validate_directory_anchor(self.run_anchor)
            validate_directory_anchor(self.lock_directory)
        except (OSError, UnsafeStoragePathError) as exc:
            raise ZoteroLockError(
                "run_directory_changed",
                "Zotero skill root changed while its authorization lock was held",
            ) from exc
        _verify_named_lock(
            self.lock_directory,
            filename=self.lock_filename,
            descriptor=self.lock_descriptor,
        )


@contextmanager
def locked_zotero_parent(
    run_dir: Path,
    parent_key: str,
    *,
    expected_skill_root: Path | None = None,
    expected_skill_root_device: int | None = None,
    expected_skill_root_inode: int | None = None,
    expected_run_path: Path | None = None,
    expected_run_device: int | None = None,
    expected_run_inode: int | None = None,
    expected_run_manifest_sha256: str | None = None,
    expected_artifacts: tuple[ExpectedRunArtifact, ...] = (),
) -> Iterator[LockedZoteroParent]:
    skill_root = skill_root_for_zotero_run(run_dir)
    if expected_skill_root is not None and Path(os.path.abspath(expected_skill_root)) != skill_root:
        raise ZoteroLockError(
            "run_directory_changed",
            "Zotero skill root changed after authorization preflight",
        )
    try:
        root_anchor = DirectoryAnchor.open(
            skill_root,
            manifest_path=skill_root / LOCK_DIRECTORY_NAME,
        )
    except RunLoadError as exc:
        raise ZoteroLockError(
            "authorization_lock_unsafe",
            "Zotero skill root cannot be locked safely",
        ) from exc
    with root_anchor:
        if (
            expected_skill_root_device is not None
            and root_anchor.device != expected_skill_root_device
            or expected_skill_root_inode is not None
            and root_anchor.inode != expected_skill_root_inode
        ):
            raise ZoteroLockError(
                "run_directory_changed",
                "Zotero skill root changed after authorization preflight",
            )
        fcntl.flock(root_anchor.descriptor, fcntl.LOCK_EX)
        run_anchor: DirectoryAnchor | None = None
        try:
            expected_run_values = (
                expected_run_path,
                expected_run_device,
                expected_run_inode,
            )
            if any(value is not None for value in expected_run_values):
                if any(value is None for value in expected_run_values):
                    raise ZoteroLockError(
                        "run_directory_changed",
                        "expected Zotero run identity is incomplete",
                    )
                assert expected_run_path is not None
                assert expected_run_device is not None
                assert expected_run_inode is not None
                lexical_run = Path(os.path.abspath(expected_run_path))
                if lexical_run != Path(os.path.abspath(run_dir)):
                    raise ZoteroLockError(
                        "run_directory_changed",
                        "Zotero run path changed after authorization preflight",
                    )
                try:
                    run_anchor = DirectoryAnchor.open(
                        lexical_run,
                        manifest_path=lexical_run / "run.json",
                    )
                except RunLoadError as exc:
                    raise ZoteroLockError(
                        "run_directory_changed",
                        "Zotero run changed after authorization preflight",
                    ) from exc
                if (
                    run_anchor.device != expected_run_device
                    or run_anchor.inode != expected_run_inode
                ):
                    raise ZoteroLockError(
                        "run_directory_changed",
                        "Zotero run changed after authorization preflight",
                    )
            if expected_run_manifest_sha256 is not None:
                if run_anchor is None:
                    raise ZoteroLockError(
                        "run_directory_changed",
                        "expected Zotero run manifest binding is incomplete",
                    )
                try:
                    manifest_bytes = read_anchored_bytes(
                        run_anchor,
                        run_anchor.path / "run.json",
                        max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                    )
                except (OSError, ValueError) as exc:
                    raise ZoteroLockError(
                        "run_manifest_changed",
                        "Zotero run manifest changed after read-only preflight",
                    ) from exc
                if (
                    hashlib.sha256(manifest_bytes).hexdigest()
                    != expected_run_manifest_sha256
                ):
                    raise ZoteroLockError(
                        "run_manifest_changed",
                        "Zotero run manifest changed after read-only preflight",
                    )
            if expected_artifacts:
                if run_anchor is None:
                    raise ZoteroLockError(
                        "run_directory_changed",
                        "expected Zotero run artifact binding is incomplete",
                    )
                try:
                    _verify_expected_run_artifacts(
                        run_anchor,
                        expected_artifacts,
                    )
                except (OSError, ValueError) as exc:
                    raise ZoteroLockError(
                        "run_artifact_changed",
                        "Zotero run artifact changed after read-only preflight",
                    ) from exc
            try:
                validate_directory_anchor(root_anchor)
                if run_anchor is not None:
                    validate_directory_anchor(run_anchor)
                lock_dir = skill_root / LOCK_DIRECTORY_NAME
                lock_directory = _open_or_create_lock_directory(root_anchor, lock_dir)
            except (OSError, UnsafeStoragePathError) as exc:
                raise ZoteroLockError(
                    "authorization_lock_unsafe",
                    "Zotero authorization lock directory is unsafe",
                ) from exc
            with lock_directory:
                filename = hashlib.sha256(parent_key.encode("utf-8")).hexdigest() + ".lock"
                flags = (
                    os.O_RDWR
                    | os.O_CREAT
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                )
                try:
                    descriptor = os.open(
                        filename,
                        flags,
                        0o600,
                        dir_fd=lock_directory.descriptor,
                    )
                except OSError as exc:
                    raise ZoteroLockError(
                        "authorization_lock_unsafe",
                        "Zotero authorization lock cannot be opened safely",
                    ) from exc
                try:
                    _verify_named_lock(
                        lock_directory,
                        filename=filename,
                        descriptor=descriptor,
                    )
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    try:
                        if expected_artifacts:
                            assert run_anchor is not None
                            try:
                                _verify_expected_run_artifacts(
                                    run_anchor,
                                    expected_artifacts,
                                )
                            except (OSError, ValueError) as exc:
                                raise ZoteroLockError(
                                    "run_artifact_changed",
                                    (
                                        "Zotero run artifact changed after acquiring "
                                        "the parent lock"
                                    ),
                                ) from exc
                        locked = LockedZoteroParent(
                            skill_root=skill_root,
                            parent_key=parent_key,
                            root_anchor=root_anchor,
                            run_anchor=run_anchor,
                            lock_directory=lock_directory,
                            lock_filename=filename,
                            lock_descriptor=descriptor,
                        )
                        locked.validate()
                        yield locked
                        locked.validate()
                    finally:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        finally:
            if run_anchor is not None:
                run_anchor.close()
            fcntl.flock(root_anchor.descriptor, fcntl.LOCK_UN)


__all__ = [
    "LOCK_DIRECTORY_NAME",
    "LockedZoteroParent",
    "ZoteroLockError",
    "locked_zotero_parent",
    "skill_root_for_zotero_run",
]
