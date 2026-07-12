from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

from paper_reader.contracts import (
    ArtifactRef,
    LocalPublicationTarget,
    LocalSourceIdentity,
    PaperReaderCandidate,
)
from paper_reader.storage import (
    DirectoryAnchorLike,
    canonical_json_sha256,
    fingerprint_resolved_source,
    read_anchored_bytes,
    safe_relative_artifact_path,
)


class LocalPublicationError(ValueError):
    def __init__(self, code: str, message: str, *, data: dict[str, str | int] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


def candidate_core_digest(candidate: PaperReaderCandidate) -> str:
    """Digest the canonical candidate core; candidate.json never self-references it."""
    return canonical_json_sha256(candidate)


def candidate_manifest_path(candidate_path: Path) -> Path:
    path = Path(candidate_path).expanduser()
    if path.is_dir() or (not path.exists() and path.suffix.lower() != ".json"):
        return path / "candidate.json"
    return path


def reject_symlinks(run_dir: Path, relative_path: str) -> None:
    current = run_dir
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise LocalPublicationError(
                "sealed_artifact_tampered",
                f"sealed artifact path uses a symlink: {relative_path}",
            )


def verify_artifact_ref(
    run_dir: Path,
    artifact: ArtifactRef,
    *,
    anchor: DirectoryAnchorLike | None = None,
) -> tuple[Path, bytes]:
    reject_symlinks(run_dir, artifact.path)
    try:
        relative = safe_relative_artifact_path(artifact.path)
        path = Path(os.path.abspath(run_dir)).joinpath(*PurePosixPath(relative).parts)
        if anchor is not None:
            raw = read_anchored_bytes(anchor, path)
        else:
            before_path = os.lstat(path)
            if (
                stat.S_ISLNK(before_path.st_mode)
                or not stat.S_ISREG(before_path.st_mode)
                or before_path.st_nlink != 1
            ):
                raise OSError("artifact must be one single-link regular file")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                before_fd = os.fstat(descriptor)
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                after_fd = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            after_path = os.lstat(path)
            identities = {
                (
                    item.st_dev,
                    item.st_ino,
                    item.st_size,
                    item.st_mtime_ns,
                    item.st_nlink,
                )
                for item in (before_path, before_fd, after_fd, after_path)
            }
            if len(identities) != 1 or after_path.st_nlink != 1:
                raise OSError("artifact changed while it was read")
            raw = b"".join(chunks)
    except (OSError, ValueError) as exc:
        raise LocalPublicationError(
            "sealed_artifact_tampered",
            f"sealed artifact is unreadable: {artifact.path}: {exc}",
        ) from exc
    if len(raw) != artifact.size_bytes or hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise LocalPublicationError(
            "sealed_artifact_tampered",
            f"sealed artifact hash or size mismatch: {artifact.path}",
            data={"reason": "hash_mismatch"},
        )
    return path, raw


def verify_local_source(source: LocalSourceIdentity) -> None:
    try:
        actual = fingerprint_resolved_source(Path(source.resolved_path))
    except (OSError, RuntimeError, ValueError) as exc:
        raise LocalPublicationError(
            "source_changed",
            f"local PDF source cannot be revalidated: {source.resolved_path}: {exc}",
        ) from exc
    expected = (
        source.resolved_path,
        source.sha256,
        source.size_bytes,
        source.device,
        source.inode,
    )
    observed = (
        actual.resolved_path,
        actual.sha256,
        actual.size_bytes,
        actual.device,
        actual.inode,
    )
    if observed != expected:
        raise LocalPublicationError("source_changed", "local PDF source fingerprint changed")


def validate_local_target_location(
    target: LocalPublicationTarget,
    source: LocalSourceIdentity,
) -> Path:
    target_path = Path(target.resolved_path)
    try:
        parent = target_path.parent.resolve(strict=True)
    except OSError as exc:
        raise LocalPublicationError(
            "invalid_local_target",
            f"local target parent is unavailable: {target_path.parent}: {exc}",
        ) from exc
    if parent != target_path.parent or not parent.is_dir():
        raise LocalPublicationError("invalid_local_target", "local target parent must be canonical")
    parent_metadata = parent.stat()
    if parent_metadata.st_dev != target.parent_device:
        raise LocalPublicationError("target_device_changed", "local target parent device changed")
    if parent_metadata.st_ino != target.parent_inode:
        raise LocalPublicationError("invalid_local_target", "local target parent inode changed")
    if target_path == Path(source.resolved_path):
        raise LocalPublicationError("invalid_local_target", "local target aliases the source path")
    return target_path


def verify_local_target(target: LocalPublicationTarget, source: LocalSourceIdentity) -> Path:
    target_path = validate_local_target_location(target, source)
    if os.path.lexists(target_path):
        raise LocalPublicationError(
            "publish_conflict",
            f"fixed local target is already occupied: {target_path}",
            data={"target_path": str(target_path)},
        )
    return target_path


def markdown_note_title(note_bytes: bytes) -> str:
    note = note_bytes.decode("utf-8")
    first_line = note.splitlines()[0] if note.splitlines() else ""
    if not first_line.startswith("# "):
        raise LocalPublicationError("invalid_sealed_review", "sealed note is missing its H1 title")
    return first_line[2:].strip()


__all__ = [
    "LocalPublicationError",
    "candidate_core_digest",
    "candidate_manifest_path",
    "markdown_note_title",
    "validate_local_target_location",
    "verify_artifact_ref",
    "verify_local_source",
    "verify_local_target",
]
