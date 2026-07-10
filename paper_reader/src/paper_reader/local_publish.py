from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from paper_reader.candidate_integrity import (
    LocalPublicationError,
    candidate_core_digest,
    candidate_manifest_path,
    markdown_note_title,
    validate_local_target_location,
    verify_artifact_ref,
    verify_local_source,
)
from paper_reader.contracts import (
    ArtifactRef,
    GateState,
    LocalPublicationTarget,
    LocalSourceIdentity,
    PaperReaderCandidate,
    PaperReaderRun,
)
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.run_lock import locked_v2_run
from paper_reader.run_size import RunSizeLimitError, enforce_projected_run_size
from paper_reader.storage import (
    PublishConflictError,
    atomic_write_json,
    canonical_json_bytes,
    publish_bytes_no_replace,
)
from paper_reader.v2_loader import LoadedRun, load_v2_run


@dataclass(frozen=True, slots=True)
class PublishedLocalCandidate:
    run_dir: Path
    candidate_path: Path
    target_path: Path
    receipt_path: Path
    candidate_digest: str
    content_sha256: str


def _load_candidate(
    candidate_input: Path,
    *,
    loaded_run: LoadedRun | None = None,
) -> tuple[LoadedRun, Path, PaperReaderCandidate, str, dict[str, tuple[tuple[Path, bytes], ...]]]:
    requested = candidate_manifest_path(candidate_input)
    if requested.is_symlink() or requested.parent.is_symlink():
        raise LocalPublicationError("candidate_tampered", "candidate path must not use symlinks")
    try:
        candidate_path = requested.resolve(strict=True)
        raw = candidate_path.read_bytes()
    except OSError as exc:
        raise LocalPublicationError("candidate_unreadable", f"candidate is unreadable: {requested}: {exc}") from exc
    candidate_dir = candidate_path.parent
    if candidate_dir.parent.name != "candidates":
        raise LocalPublicationError("candidate_tampered", "candidate left its run candidates directory")
    run_dir = candidate_dir.parent.parent
    loaded = loaded_run or load_v2_run(run_dir)
    if loaded.manifest_path.resolve(strict=True).parent != run_dir:
        raise LocalPublicationError("candidate_tampered", "candidate run directory binding mismatch")
    try:
        candidate = PaperReaderCandidate.model_validate_json(raw)
    except ValidationError as exc:
        raise LocalPublicationError("candidate_tampered", f"strict candidate validation failed: {exc}") from exc
    if canonical_json_bytes(candidate) != raw:
        raise LocalPublicationError("candidate_tampered", "candidate.json is not canonical JSON")
    digest = candidate_core_digest(candidate)
    relative = candidate_path.relative_to(run_dir).as_posix()
    refs = [item for item in loaded.run.artifacts if item.role == "candidate" and item.path == relative]
    if len(refs) != 1:
        raise LocalPublicationError("candidate_not_bound", "run does not bind this candidate")
    if refs[0].sha256 != digest or refs[0].size_bytes != len(raw) or hashlib.sha256(raw).hexdigest() != digest:
        raise LocalPublicationError("candidate_tampered", "candidate core digest or size mismatch")
    if candidate.run_id != loaded.run.run_id:
        raise LocalPublicationError("candidate_tampered", "candidate run_id mismatch")
    if candidate.source != loaded.run.source or candidate.target != loaded.run.target:
        raise LocalPublicationError("candidate_tampered", "candidate source/target binding mismatch")
    if candidate.gate.status != "write_ready" or candidate.gate.blockers:
        raise LocalPublicationError("candidate_not_ready", "candidate gate is not write_ready")
    if not isinstance(candidate.source, LocalSourceIdentity) or not isinstance(
        candidate.target, LocalPublicationTarget
    ):
        raise LocalPublicationError("not_implemented", "local publish accepts only local candidates")

    verified: dict[str, list[tuple[Path, bytes]]] = {}
    for artifact in candidate.artifacts:
        try:
            path, content = verify_artifact_ref(run_dir, artifact)
        except LocalPublicationError as exc:
            raise LocalPublicationError("candidate_tampered", str(exc)) from exc
        if not path.is_relative_to(candidate_dir):
            raise LocalPublicationError("candidate_tampered", "candidate artifact escapes its tree")
        verified.setdefault(artifact.role, []).append((path, content))
    required = {
        "run_snapshot",
        "source_snapshot",
        "evidence_manifest_snapshot",
        "summary_snapshot",
        "review_snapshot",
        "review_package_snapshot",
        "review_validation",
        "note_markdown",
        "note_html",
    }
    if any(len(verified.get(role, [])) != 1 for role in required):
        raise LocalPublicationError("candidate_tampered", "candidate artifact membership is incomplete")
    if candidate.evidence_manifest not in candidate.artifacts or candidate.sealed_review not in candidate.artifacts:
        raise LocalPublicationError("candidate_tampered", "candidate gate refs are not artifact members")
    _note_path, note_bytes = verified["note_markdown"][0]
    if (
        hashlib.sha256(note_bytes).hexdigest() != candidate.content_sha256
        or len(note_bytes) != candidate.content_length
        or markdown_note_title(note_bytes) != candidate.note_title
    ):
        raise LocalPublicationError("candidate_tampered", "candidate Markdown binding mismatch")
    return loaded, candidate_path, candidate, digest, {
        role: tuple(items) for role, items in verified.items()
    }


def _bind_unique_artifact(
    artifacts: tuple[ArtifactRef, ...],
    artifact_ref: ArtifactRef,
    *,
    conflict_code: str,
) -> tuple[ArtifactRef, ...]:
    matching = [
        item
        for item in artifacts
        if item.role == artifact_ref.role or item.path == artifact_ref.path
    ]
    if any(item != artifact_ref for item in matching):
        raise LocalPublicationError(
            conflict_code,
            "run manifest binds conflicting publication identity bytes",
        )
    return tuple(item for item in artifacts if item != artifact_ref) + (artifact_ref,)


def _updated_run(
    loaded: LoadedRun,
    intent_ref: ArtifactRef,
    receipt_ref: ArtifactRef,
    gate: GateState,
) -> PaperReaderRun:
    run = loaded.run
    artifacts = _bind_unique_artifact(
        run.artifacts,
        intent_ref,
        conflict_code="publication_identity_conflict",
    )
    artifacts = _bind_unique_artifact(
        artifacts,
        receipt_ref,
        conflict_code="receipt_conflict",
    )
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=run.run_id,
        created_at=run.created_at,
        source=run.source,
        target=run.target,
        status="published",
        artifacts=artifacts,
        gate=gate,
        live_preflight=run.live_preflight,
    )


def _read_stable_regular_file(path: Path, *, conflict_code: str) -> bytes:
    try:
        before_path = os.lstat(path)
    except OSError as exc:
        raise LocalPublicationError(
            conflict_code,
            f"publication state is unreadable: {path}: {exc}",
        ) from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise LocalPublicationError(
            conflict_code,
            f"publication path must be one canonical regular file: {path}",
        )

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            before_fd = os.fstat(handle.fileno())
            raw = handle.read()
            after_fd = os.fstat(handle.fileno())
        after_path = os.lstat(path)
    except OSError as exc:
        raise LocalPublicationError(
            conflict_code,
            f"publication path changed while it was verified: {path}: {exc}",
        ) from exc
    identities = {
        (before_path.st_dev, before_path.st_ino, before_path.st_size, before_path.st_mtime_ns),
        (before_fd.st_dev, before_fd.st_ino, before_fd.st_size, before_fd.st_mtime_ns),
        (after_fd.st_dev, after_fd.st_ino, after_fd.st_size, after_fd.st_mtime_ns),
        (after_path.st_dev, after_path.st_ino, after_path.st_size, after_path.st_mtime_ns),
    }
    if len(identities) != 1 or not stat.S_ISREG(after_path.st_mode):
        raise LocalPublicationError(
            conflict_code,
            f"publication path changed while it was verified: {path}",
        )
    return raw


def _verify_exact_target(target_path: Path, expected: bytes, expected_sha256: str) -> bool:
    if not os.path.lexists(target_path):
        return False
    raw = _read_stable_regular_file(target_path, conflict_code="publish_conflict")
    if (
        len(raw) != len(expected)
        or hashlib.sha256(raw).hexdigest() != expected_sha256
        or raw != expected
    ):
        raise LocalPublicationError(
            "publish_conflict",
            f"fixed local target contains bytes from another publication: {target_path}",
            data={"target_path": str(target_path)},
        )
    return True


def _publish_or_recover_target(
    target_path: Path,
    note_bytes: bytes,
    content_sha256: str,
) -> None:
    if _verify_exact_target(target_path, note_bytes, content_sha256):
        return
    try:
        publish_bytes_no_replace(note_bytes, target_path)
    except (PublishConflictError, FileExistsError):
        _verify_exact_target(target_path, note_bytes, content_sha256)
    except Exception as exc:
        if _verify_exact_target(target_path, note_bytes, content_sha256):
            return
        raise LocalPublicationError(
            "publish_failed",
            f"atomic local publication failed before commit: {target_path}: {exc}",
            data={"target_path": str(target_path)},
        ) from exc
    if not _verify_exact_target(target_path, note_bytes, content_sha256):
        raise LocalPublicationError(
            "publish_failed",
            f"atomic local publication did not create its target: {target_path}",
        )


def _intent_bytes_and_path(
    *,
    run_dir: Path,
    candidate: PaperReaderCandidate,
    candidate_digest: str,
) -> tuple[bytes, Path, ArtifactRef]:
    intent_path = run_dir / "publication-intent.json"
    intent = {
        "format": "paper_reader.local-publication-intent.v2-internal",
        "run_id": candidate.run_id,
        "candidate_id": candidate.candidate_id,
        "candidate_digest": candidate_digest,
        "target_path": candidate.target.resolved_path,
        "content_sha256": candidate.content_sha256,
        "content_length": candidate.content_length,
    }
    intent_bytes = canonical_json_bytes(intent)
    intent_ref = ArtifactRef(
        role="local_publication_intent",
        path=intent_path.relative_to(run_dir).as_posix(),
        sha256=hashlib.sha256(intent_bytes).hexdigest(),
        size_bytes=len(intent_bytes),
        media_type="application/json",
    )
    return intent_bytes, intent_path, intent_ref


def _publish_or_verify_intent(
    *,
    intent_bytes: bytes,
    intent_path: Path,
    target_path: Path,
) -> None:
    if os.path.lexists(intent_path):
        actual = _read_stable_regular_file(
            intent_path,
            conflict_code="publication_identity_conflict",
        )
        if actual != intent_bytes:
            raise LocalPublicationError(
                "publication_identity_conflict",
                f"run publication intent belongs to another candidate: {intent_path}",
            )
        return

    if os.path.lexists(target_path):
        raise LocalPublicationError(
            "publish_conflict",
            f"fixed local target predates this run publication intent: {target_path}",
            data={"target_path": str(target_path)},
        )
    try:
        publish_bytes_no_replace(intent_bytes, intent_path)
    except (PublishConflictError, FileExistsError):
        actual = _read_stable_regular_file(
            intent_path,
            conflict_code="publication_identity_conflict",
        )
        if actual != intent_bytes:
            raise LocalPublicationError(
                "publication_identity_conflict",
                f"run publication intent belongs to another candidate: {intent_path}",
            )
    except Exception as exc:
        if os.path.lexists(intent_path):
            actual = _read_stable_regular_file(
                intent_path,
                conflict_code="publication_identity_conflict",
            )
            if actual == intent_bytes:
                return
            raise LocalPublicationError(
                "publication_identity_conflict",
                f"run publication intent belongs to another candidate: {intent_path}",
            ) from exc
        raise LocalPublicationError(
            "publication_intent_failed",
            f"atomic publication intent commit failed: {intent_path}: {exc}",
        ) from exc
    actual = _read_stable_regular_file(
        intent_path,
        conflict_code="publication_identity_conflict",
    )
    if actual != intent_bytes:
        raise LocalPublicationError(
            "publication_identity_conflict",
            f"run publication intent belongs to another candidate: {intent_path}",
        )


def _receipt_bytes_and_path(
    *,
    run_dir: Path,
    candidate_path: Path,
    candidate: PaperReaderCandidate,
    candidate_digest: str,
    intent_ref: ArtifactRef,
) -> tuple[bytes, Path, ArtifactRef]:
    receipt_id = f"local-receipt-{candidate.candidate_id}"
    receipt_path = run_dir / "receipts" / f"{candidate.candidate_id}.json"
    receipt = {
        "format": "paper_reader.local-receipt.v2-internal",
        "receipt_id": receipt_id,
        "run_id": candidate.run_id,
        "candidate_path": candidate_path.relative_to(run_dir).as_posix(),
        "candidate_digest": candidate_digest,
        "intent_path": intent_ref.path,
        "intent_sha256": intent_ref.sha256,
        "target_path": candidate.target.resolved_path,
        "content_sha256": candidate.content_sha256,
        "content_length": candidate.content_length,
    }
    receipt_bytes = canonical_json_bytes(receipt)
    receipt_ref = ArtifactRef(
        role="local_receipt",
        path=receipt_path.relative_to(run_dir).as_posix(),
        sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        size_bytes=len(receipt_bytes),
        media_type="application/json",
    )
    return receipt_bytes, receipt_path, receipt_ref


def _publish_or_verify_receipt(
    *,
    run_dir: Path,
    candidate_path: Path,
    candidate: PaperReaderCandidate,
    candidate_digest: str,
    intent_ref: ArtifactRef,
) -> tuple[Path, ArtifactRef]:
    receipt_bytes, receipt_path, receipt_ref = _receipt_bytes_and_path(
        run_dir=run_dir,
        candidate_path=candidate_path,
        candidate=candidate,
        candidate_digest=candidate_digest,
        intent_ref=intent_ref,
    )
    if os.path.lexists(receipt_path):
        actual = _read_stable_regular_file(receipt_path, conflict_code="receipt_conflict")
        if actual != receipt_bytes:
            raise LocalPublicationError(
                "receipt_conflict",
                f"deterministic local receipt contains different bytes: {receipt_path}",
            )
    else:
        try:
            publish_bytes_no_replace(receipt_bytes, receipt_path)
        except (PublishConflictError, FileExistsError):
            actual = _read_stable_regular_file(receipt_path, conflict_code="receipt_conflict")
            if actual != receipt_bytes:
                raise LocalPublicationError(
                    "receipt_conflict",
                    f"deterministic local receipt contains different bytes: {receipt_path}",
                )
        except Exception as exc:
            if os.path.lexists(receipt_path):
                actual = _read_stable_regular_file(
                    receipt_path,
                    conflict_code="receipt_conflict",
                )
                if actual == receipt_bytes:
                    pass
                else:
                    raise LocalPublicationError(
                        "receipt_conflict",
                        f"deterministic local receipt contains different bytes: {receipt_path}",
                    ) from exc
            else:
                raise
    return receipt_path, receipt_ref


def _candidate_run_dir(candidate_input: Path) -> Path:
    requested = candidate_manifest_path(candidate_input)
    if requested.is_symlink() or requested.parent.is_symlink():
        raise LocalPublicationError("candidate_tampered", "candidate path must not use symlinks")
    try:
        candidate_path = requested.resolve(strict=True)
    except OSError as exc:
        raise LocalPublicationError(
            "candidate_unreadable",
            f"candidate is unreadable: {requested}: {exc}",
        ) from exc
    if candidate_path.parent.parent.name != "candidates":
        raise LocalPublicationError("candidate_tampered", "candidate left its run candidates directory")
    return candidate_path.parent.parent.parent


def publish_local_candidate(candidate_input: Path) -> PublishedLocalCandidate:
    run_dir = _candidate_run_dir(candidate_input)
    with locked_v2_run(run_dir) as loaded:
        return _publish_local_candidate_locked(candidate_input, loaded)


def _publish_local_candidate_locked(
    candidate_input: Path,
    locked_run: LoadedRun,
) -> PublishedLocalCandidate:
    loaded, candidate_path, candidate, digest, verified = _load_candidate(
        candidate_input,
        loaded_run=locked_run,
    )
    source = candidate.source
    target = candidate.target
    assert isinstance(source, LocalSourceIdentity)
    assert isinstance(target, LocalPublicationTarget)
    verify_local_source(source)
    target_path = validate_local_target_location(target, source)
    _note_path, note_bytes = verified["note_markdown"][0]
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    intent_bytes, intent_path, intent_ref = _intent_bytes_and_path(
        run_dir=run_dir,
        candidate=candidate,
        candidate_digest=digest,
    )
    receipt_bytes, projected_receipt_path, projected_receipt_ref = _receipt_bytes_and_path(
        run_dir=run_dir,
        candidate_path=candidate_path,
        candidate=candidate,
        candidate_digest=digest,
        intent_ref=intent_ref,
    )
    projected_run = _updated_run(
        loaded,
        intent_ref,
        projected_receipt_ref,
        candidate.gate,
    )
    try:
        enforce_projected_run_size(
            run_dir,
            max_bytes=V2_RESOURCE_POLICY.run_max_bytes,
            replacements={
                intent_path: intent_bytes,
                projected_receipt_path: receipt_bytes,
                loaded.manifest_path: canonical_json_bytes(projected_run),
            },
        )
    except RunSizeLimitError as exc:
        raise LocalPublicationError(
            "run_size_limit_exceeded",
            str(exc),
            data={
                "run_size_bytes": exc.actual_bytes,
                "max_bytes": exc.max_bytes,
            },
        ) from exc
    _publish_or_verify_intent(
        intent_bytes=intent_bytes,
        intent_path=intent_path,
        target_path=target_path,
    )
    _publish_or_recover_target(target_path, note_bytes, candidate.content_sha256)

    try:
        receipt_path, receipt_ref = _publish_or_verify_receipt(
            run_dir=run_dir,
            candidate_path=candidate_path,
            candidate=candidate,
            candidate_digest=digest,
            intent_ref=intent_ref,
        )
    except LocalPublicationError:
        raise
    except Exception as exc:
        raise LocalPublicationError(
            "publication_recovery_required",
            f"target is committed but local receipt is incomplete: {exc}",
            data={"target_path": str(target_path)},
        ) from exc
    try:
        updated_run = _updated_run(loaded, intent_ref, receipt_ref, candidate.gate)
        if updated_run != loaded.run:
            atomic_write_json(loaded.manifest_path, updated_run)
    except LocalPublicationError:
        raise
    except Exception as exc:
        raise LocalPublicationError(
            "publication_recovery_required",
            f"target and receipt are committed but run status is incomplete: {exc}",
            data={
                "target_path": str(target_path),
                "receipt_path": str(receipt_path),
            },
        ) from exc
    return PublishedLocalCandidate(
        run_dir=run_dir,
        candidate_path=candidate_path,
        target_path=target_path,
        receipt_path=receipt_path,
        candidate_digest=digest,
        content_sha256=candidate.content_sha256,
    )


__all__ = ["PublishedLocalCandidate", "publish_local_candidate"]
