from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from paper_reader.candidate_integrity import (
    LocalPublicationError,
    candidate_core_digest,
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
    PaperReaderReview,
    PaperReaderReviewPackage,
    PaperReaderRun,
    PaperReaderSummary,
)
from paper_reader.evidence_manifest import (
    EvidenceManifest,
    EvidenceManifestError,
    load_bound_evidence,
)
from paper_reader.note import build_note_labels
from paper_reader.storage import (
    atomic_publish_tree,
    atomic_write_json,
    canonical_json_bytes,
    canonical_json_sha256,
    new_random_id,
    new_uuid,
    rfc3339_utc,
    sha256_file,
)
from paper_reader.v2_loader import LoadedRun, load_v2_run


@dataclass(frozen=True, slots=True)
class BuiltLocalCandidate:
    run_dir: Path
    candidate_dir: Path
    candidate: PaperReaderCandidate
    candidate_digest: str


def _latest_review_package(loaded: LoadedRun) -> tuple[PaperReaderReviewPackage, Path]:
    refs = [item for item in loaded.run.artifacts if item.role == "review_package"]
    if not refs:
        raise LocalPublicationError("sealed_review_missing", "run does not bind a sealed review package")
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    package_path, package_bytes = verify_artifact_ref(run_dir, refs[-1])
    try:
        package = PaperReaderReviewPackage.model_validate_json(package_bytes)
    except ValidationError as exc:
        raise LocalPublicationError(
            "invalid_sealed_review",
            f"sealed review package schema validation failed: {exc}",
        ) from exc
    if canonical_json_bytes(package) != package_bytes:
        raise LocalPublicationError("invalid_sealed_review", "sealed review package is not canonical")
    if package.run_id != loaded.run.run_id or package.gate.status != "passed" or package.gate.blockers:
        raise LocalPublicationError("invalid_sealed_review", "sealed review package did not pass its gate")
    return package, package_path


def _sealed_snapshots(
    loaded: LoadedRun,
    package: PaperReaderReviewPackage,
    package_path: Path,
) -> dict[str, bytes]:
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    package_dir = package_path.parent
    snapshots: dict[str, bytes] = {}
    for artifact in package.artifacts:
        path, raw = verify_artifact_ref(run_dir, artifact)
        if not path.is_relative_to(package_dir):
            raise LocalPublicationError(
                "invalid_sealed_review",
                f"sealed review member escapes its package: {artifact.path}",
            )
        snapshots[path.name] = raw
    required = {
        "summary.json",
        "review.json",
        "evidence.json",
        "validation.json",
        "note.md",
        "note.html",
    }
    if not required <= snapshots.keys():
        raise LocalPublicationError("invalid_sealed_review", "sealed review snapshots are incomplete")
    if package.summary not in package.artifacts or package.review not in package.artifacts:
        raise LocalPublicationError("invalid_sealed_review", "sealed summary/review refs are not members")
    if package.evidence_manifest not in package.artifacts:
        raise LocalPublicationError("invalid_sealed_review", "sealed evidence ref is not a member")
    try:
        summary = PaperReaderSummary.model_validate_json(snapshots["summary.json"])
        review = PaperReaderReview.model_validate_json(snapshots["review.json"])
        evidence = EvidenceManifest.model_validate_json(snapshots["evidence.json"])
        validation = json.loads(snapshots["validation.json"])
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise LocalPublicationError("invalid_sealed_review", f"sealed snapshot validation failed: {exc}") from exc
    if canonical_json_sha256(summary) != package.summary_sha256:
        raise LocalPublicationError("sealed_artifact_tampered", "sealed summary canonical hash mismatch")
    if canonical_json_sha256(review) != package.review_sha256:
        raise LocalPublicationError("sealed_artifact_tampered", "sealed review canonical hash mismatch")
    if hashlib.sha256(snapshots["evidence.json"]).hexdigest() != package.evidence_digest:
        raise LocalPublicationError("sealed_artifact_tampered", "sealed evidence digest mismatch")
    if review.summary_sha256 != package.summary_sha256 or review.evidence_digest != package.evidence_digest:
        raise LocalPublicationError("invalid_sealed_review", "sealed review bindings are inconsistent")
    if not evidence.complete or evidence.run_id != package.run_id:
        raise LocalPublicationError("invalid_sealed_review", "sealed evidence is incomplete or misbound")
    if not isinstance(validation, dict) or validation.get("blockers") != []:
        raise LocalPublicationError("invalid_sealed_review", "sealed validation contains blockers")
    if validation.get("rendered_note_sha256") != hashlib.sha256(snapshots["note.md"]).hexdigest():
        raise LocalPublicationError("sealed_artifact_tampered", "sealed note hash mismatch")
    snapshots["review-package.json"] = package_path.read_bytes()
    return snapshots


def _artifact_ref(
    run_dir: Path,
    staged_path: Path,
    future_path: Path,
    role: str,
    media_type: str,
) -> ArtifactRef:
    return ArtifactRef(
        role=role,
        path=future_path.relative_to(run_dir).as_posix(),
        sha256=sha256_file(staged_path),
        size_bytes=staged_path.stat().st_size,
        media_type=media_type,
    )


def _updated_run(loaded: LoadedRun, candidate_ref: ArtifactRef, gate: GateState) -> PaperReaderRun:
    run = loaded.run
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=run.run_id,
        created_at=run.created_at,
        source=run.source,
        target=run.target,
        status="candidate_built",
        artifacts=(*run.artifacts, candidate_ref),
        gate=gate,
        live_preflight=run.live_preflight,
    )


def build_local_candidate(run_path: Path) -> BuiltLocalCandidate:
    loaded = load_v2_run(run_path)
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    source = loaded.run.source
    target = loaded.run.target
    if not isinstance(source, LocalSourceIdentity) or not isinstance(target, LocalPublicationTarget):
        raise LocalPublicationError(
            "not_implemented",
            "candidate build is not implemented for Zotero-backed V2 runs",
        )
    verify_local_source(source)
    verify_local_target(target, source)
    package, package_path = _latest_review_package(loaded)
    snapshots = _sealed_snapshots(loaded, package, package_path)
    try:
        load_bound_evidence(loaded, package.evidence_digest)
    except EvidenceManifestError as exc:
        raise LocalPublicationError(exc.code, str(exc)) from exc
    source_refs = [item for item in loaded.run.artifacts if item.role == "source_snapshot"]
    if len(source_refs) != 1:
        raise LocalPublicationError("source_snapshot_missing", "run must bind one source snapshot")
    _source_path, source_bytes = verify_artifact_ref(run_dir, source_refs[0])
    summary = PaperReaderSummary.model_validate_json(snapshots["summary.json"])

    candidate_id = new_random_id("candidate")
    candidate_dir = run_dir / "candidates" / candidate_id
    staging = run_dir / f".{candidate_id}.{new_uuid()}.staging"
    staging.mkdir()
    try:
        files = {"run.json": loaded.manifest_path.read_bytes(), "source.json": source_bytes, **snapshots}
        for name, content in files.items():
            (staging / name).write_bytes(content)
        specs = {
            "run.json": ("run_snapshot", "application/json"),
            "source.json": ("source_snapshot", "application/json"),
            "evidence.json": ("evidence_manifest_snapshot", "application/json"),
            "summary.json": ("summary_snapshot", "application/json"),
            "review.json": ("review_snapshot", "application/json"),
            "review-package.json": ("review_package_snapshot", "application/json"),
            "validation.json": ("review_validation", "application/json"),
            "note.md": ("note_markdown", "text/markdown"),
            "note.html": ("note_html", "text/html"),
        }
        refs = {
            name: _artifact_ref(run_dir, staging / name, candidate_dir / name, role, media)
            for name, (role, media) in specs.items()
        }
        checks = (
            "source_identity",
            "evidence_hashes",
            "sealed_review_hashes",
            "rendered_note_hash",
            "fixed_local_target",
        )
        gate = GateState(
            status="write_ready",
            evaluated_at=rfc3339_utc(),
            checks=checks,
            blockers=(),
        )
        note_bytes = files["note.md"]
        candidate = PaperReaderCandidate(
            schema_version="paper_reader.candidate.v2",
            candidate_id=candidate_id,
            run_id=loaded.run.run_id,
            created_at=rfc3339_utc(),
            source=source,
            target=target,
            evidence_manifest=refs["evidence.json"],
            sealed_review=refs["review-package.json"],
            note_title=markdown_note_title(note_bytes),
            tags=tuple(build_note_labels(summary.model_dump(mode="json"))),
            content_sha256=hashlib.sha256(note_bytes).hexdigest(),
            content_length=len(note_bytes),
            artifacts=tuple(refs.values()),
            gate=gate,
            live_preflight=None,
        )
        digest = candidate_core_digest(candidate)
        (staging / "candidate.json").write_bytes(canonical_json_bytes(candidate))
        verify_local_target(target, source)
        try:
            atomic_publish_tree(staging, candidate_dir)
        except Exception as exc:
            raise LocalPublicationError(
                "candidate_publication_failed",
                f"immutable candidate publication failed: {candidate_dir}: {exc}",
            ) from exc
        candidate_path = candidate_dir / "candidate.json"
        candidate_ref = ArtifactRef(
            role="candidate",
            path=candidate_path.relative_to(run_dir).as_posix(),
            sha256=sha256_file(candidate_path),
            size_bytes=candidate_path.stat().st_size,
            media_type="application/json",
        )
        if candidate_ref.sha256 != digest:
            raise LocalPublicationError(
                "candidate_digest_mismatch",
                "canonical candidate core digest does not match candidate.json bytes",
            )
        atomic_write_json(loaded.manifest_path, _updated_run(loaded, candidate_ref, gate))
        return BuiltLocalCandidate(run_dir, candidate_dir, candidate, digest)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


__all__ = ["BuiltLocalCandidate", "build_local_candidate"]
