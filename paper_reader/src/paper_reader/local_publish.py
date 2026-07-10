from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from paper_reader.candidate_integrity import (
    LocalPublicationError,
    candidate_core_digest,
    candidate_manifest_path,
    markdown_note_title,
    verify_artifact_ref,
    verify_local_source,
    verify_local_target,
)
from paper_reader.contracts import (
    ArtifactRef,
    GateState,
    LocalPublicationTarget,
    LocalSourceIdentity,
    PaperReaderCandidate,
    PaperReaderRun,
)
from paper_reader.storage import (
    PublishConflictError,
    atomic_write_json,
    canonical_json_bytes,
    new_random_id,
    publish_file_no_replace,
    rfc3339_utc,
    sha256_file,
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
    loaded = load_v2_run(run_dir)
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


def _updated_run(loaded: LoadedRun, receipt_ref: ArtifactRef, gate: GateState) -> PaperReaderRun:
    run = loaded.run
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=run.run_id,
        created_at=run.created_at,
        source=run.source,
        target=run.target,
        status="published",
        artifacts=(*run.artifacts, receipt_ref),
        gate=gate,
        live_preflight=run.live_preflight,
    )


def publish_local_candidate(candidate_input: Path) -> PublishedLocalCandidate:
    loaded, candidate_path, candidate, digest, verified = _load_candidate(candidate_input)
    source = candidate.source
    target = candidate.target
    assert isinstance(source, LocalSourceIdentity)
    assert isinstance(target, LocalPublicationTarget)
    verify_local_source(source)
    target_path = verify_local_target(target, source)
    note_path, note_bytes = verified["note_markdown"][0]
    try:
        publish_file_no_replace(note_path, target_path)
    except (PublishConflictError, FileExistsError) as exc:
        raise LocalPublicationError(
            "publish_conflict",
            f"fixed local target became occupied: {target_path}",
            data={"target_path": str(target_path)},
        ) from exc
    except Exception as exc:
        raise LocalPublicationError(
            "publish_failed",
            f"atomic local publication failed: {target_path}: {exc}",
            data={"target_path": str(target_path)},
        ) from exc
    try:
        published = target_path.read_bytes()
    except OSError as exc:
        raise LocalPublicationError(
            "publish_verification_failed",
            f"published target cannot be read back: {target_path}: {exc}",
        ) from exc
    if published != note_bytes or hashlib.sha256(published).hexdigest() != candidate.content_sha256:
        raise LocalPublicationError(
            "publish_verification_failed",
            f"published target bytes do not match immutable candidate: {target_path}",
        )

    run_dir = loaded.manifest_path.resolve(strict=True).parent
    receipt_id = new_random_id("local-receipt")
    receipt_path = run_dir / "receipts" / f"{receipt_id}.json"
    receipt = {
        "format": "paper_reader.local-receipt.v2-internal",
        "receipt_id": receipt_id,
        "run_id": candidate.run_id,
        "candidate_path": candidate_path.relative_to(run_dir).as_posix(),
        "candidate_digest": digest,
        "target_path": str(target_path),
        "content_sha256": candidate.content_sha256,
        "content_length": candidate.content_length,
        "published_at": rfc3339_utc(),
    }
    atomic_write_json(receipt_path, receipt)
    receipt_ref = ArtifactRef(
        role="local_receipt",
        path=receipt_path.relative_to(run_dir).as_posix(),
        sha256=sha256_file(receipt_path),
        size_bytes=receipt_path.stat().st_size,
        media_type="application/json",
    )
    atomic_write_json(loaded.manifest_path, _updated_run(loaded, receipt_ref, candidate.gate))
    return PublishedLocalCandidate(
        run_dir=run_dir,
        candidate_path=candidate_path,
        target_path=target_path,
        receipt_path=receipt_path,
        candidate_digest=digest,
        content_sha256=candidate.content_sha256,
    )


__all__ = ["PublishedLocalCandidate", "publish_local_candidate"]
