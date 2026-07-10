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
    canonical_json_sha256,
    fingerprint_resolved_source,
    resolve_artifact_path,
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


def verify_artifact_ref(run_dir: Path, artifact: ArtifactRef) -> tuple[Path, bytes]:
    reject_symlinks(run_dir, artifact.path)
    try:
        path = resolve_artifact_path(run_dir, artifact.path)
        raw = path.read_bytes()
        mode = path.stat().st_mode
    except (OSError, ValueError) as exc:
        raise LocalPublicationError(
            "sealed_artifact_tampered",
            f"sealed artifact is unreadable: {artifact.path}: {exc}",
        ) from exc
    if not stat.S_ISREG(mode):
        raise LocalPublicationError(
            "sealed_artifact_tampered",
            f"sealed artifact is not a regular file: {artifact.path}",
        )
    if len(raw) != artifact.size_bytes or hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise LocalPublicationError(
            "sealed_artifact_tampered",
            f"sealed artifact hash or size mismatch: {artifact.path}",
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


def verify_local_target(target: LocalPublicationTarget, source: LocalSourceIdentity) -> Path:
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
    if parent.stat().st_dev != target.parent_device:
        raise LocalPublicationError("target_device_changed", "local target parent device changed")
    if os.path.lexists(target_path):
        raise LocalPublicationError(
            "publish_conflict",
            f"fixed local target is already occupied: {target_path}",
            data={"target_path": str(target_path)},
        )
    if target_path == Path(source.resolved_path):
        raise LocalPublicationError("invalid_local_target", "local target aliases the source path")
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
    "verify_artifact_ref",
    "verify_local_source",
    "verify_local_target",
]
