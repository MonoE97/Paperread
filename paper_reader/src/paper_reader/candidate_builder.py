from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
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
    ZoteroSourceIdentity,
)
from paper_reader.evidence_manifest import (
    EvidenceManifest,
    EvidenceManifestError,
    load_bound_evidence,
)
from paper_reader.note import build_note_labels
from paper_reader.raw_schema import require_raw_schema_version
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.run_lock import ExpectedRunArtifact, locked_v2_run
from paper_reader.run_size import RunSizeLimitError, enforce_projected_run_size
from paper_reader.storage import (
    atomic_publish_tree,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    canonical_json_sha256,
    create_anchored_directory,
    new_random_id,
    new_uuid,
    rfc3339_utc,
    remove_anchored_tree,
    sha256_file,
    snapshot_anchored_tree,
    tree_snapshot_from_bytes,
    validate_directory_anchor,
)
from paper_reader.v2_loader import (
    DirectoryAnchor,
    LoadedRun,
    RunLoadError,
    load_v2_run,
    run_manifest_path,
)


@dataclass(frozen=True, slots=True)
class BuiltLocalCandidate:
    run_dir: Path
    candidate_dir: Path
    candidate: PaperReaderCandidate
    candidate_digest: str


@dataclass(frozen=True, slots=True)
class PreflightCandidateBinding:
    artifact_ref: ArtifactRef
    candidate_path: Path
    candidate: PaperReaderCandidate
    candidate_digest: str
    candidate_tree_sha256: str


@dataclass(frozen=True, slots=True)
class SealedReviewSchemaPreflight:
    loaded_run: LoadedRun
    expected_artifacts: tuple[ExpectedRunArtifact, ...] = ()
    bound_candidates: tuple[PreflightCandidateBinding, ...] = ()
    skill_root: Path | None = None
    skill_root_device: int | None = None
    skill_root_inode: int | None = None


def preflight_candidate_lock_artifacts(
    preflight: SealedReviewSchemaPreflight,
) -> tuple[ExpectedRunArtifact, ...]:
    artifacts = list(preflight.expected_artifacts)
    for binding in preflight.bound_candidates:
        artifacts.extend(
            (
                ExpectedRunArtifact(
                    path=binding.artifact_ref.path,
                    sha256=binding.candidate_digest,
                ),
                ExpectedRunArtifact(
                    path=(
                        binding.candidate_path.parent.relative_to(
                            preflight.loaded_run.manifest_path.parent
                        ).as_posix()
                    ),
                    sha256=binding.candidate_tree_sha256,
                    kind="tree",
                ),
            )
        )
    return tuple(artifacts)


def validate_candidate_binding_growth(
    baseline: SealedReviewSchemaPreflight,
    refreshed: SealedReviewSchemaPreflight,
) -> tuple[PreflightCandidateBinding, ...]:
    baseline_artifacts = baseline.loaded_run.run.artifacts
    refreshed_artifacts = refreshed.loaded_run.run.artifacts
    if (
        refreshed_artifacts[: len(baseline_artifacts)] != baseline_artifacts
        or any(
            item.role != "candidate"
            for item in refreshed_artifacts[len(baseline_artifacts) :]
        )
    ):
        raise LocalPublicationError(
            "sealed_artifact_tampered",
            "run artifact history changed non-append-only during candidate retry",
        )
    baseline_by_path = {
        item.artifact_ref.path: item for item in baseline.bound_candidates
    }
    refreshed_by_path = {
        item.artifact_ref.path: item for item in refreshed.bound_candidates
    }
    if (
        len(baseline_by_path) != len(baseline.bound_candidates)
        or len(refreshed_by_path) != len(refreshed.bound_candidates)
        or any(
            refreshed_by_path.get(path) != binding
            for path, binding in baseline_by_path.items()
        )
    ):
        raise LocalPublicationError(
            "sealed_artifact_tampered",
            "an existing candidate binding changed during retry",
        )
    return tuple(
        binding
        for path, binding in refreshed_by_path.items()
        if path not in baseline_by_path
    )


def _latest_review_package(
    loaded: LoadedRun,
) -> tuple[PaperReaderReviewPackage, Path, bytes]:
    refs = [item for item in loaded.run.artifacts if item.role == "review_package"]
    if not refs:
        raise LocalPublicationError("sealed_review_missing", "run does not bind a sealed review package")
    run_dir = loaded.manifest_path.parent
    package_path, package_bytes = verify_artifact_ref(
        run_dir,
        refs[-1],
        anchor=loaded.run_directory_anchor,
    )
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
    return package, package_path, package_bytes


def _sealed_snapshots(
    loaded: LoadedRun,
    package: PaperReaderReviewPackage,
    package_path: Path,
    package_bytes: bytes,
) -> dict[str, bytes]:
    run_dir = loaded.manifest_path.parent
    package_dir = package_path.parent
    snapshots: dict[str, bytes] = {}
    for artifact in package.artifacts:
        path, raw = verify_artifact_ref(
            run_dir,
            artifact,
            anchor=loaded.run_directory_anchor,
        )
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
    require_raw_schema_version(
        snapshots["summary.json"],
        expected="paper_reader.summary.v2",
        artifact_path=package_dir / "summary.json",
    )
    require_raw_schema_version(
        snapshots["review.json"],
        expected="paper_reader.review.v2",
        artifact_path=package_dir / "review.json",
    )
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
    snapshots["review-package.json"] = package_bytes
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


def _preflight_sealed_review_from_anchor(
    loaded: LoadedRun,
    anchor: DirectoryAnchor,
) -> tuple[ExpectedRunArtifact, ...]:
    run_dir = loaded.manifest_path.parent
    anchored = replace(loaded, run_directory_anchor=anchor)
    refs = [item for item in anchored.run.artifacts if item.role == "review_package"]
    if not refs:
        raise LocalPublicationError(
            "sealed_review_missing",
            "run does not bind a sealed review package",
        )
    package_path, package_bytes = verify_artifact_ref(
        run_dir,
        refs[-1],
        anchor=anchor,
    )
    try:
        package = PaperReaderReviewPackage.model_validate_json(package_bytes)
    except ValidationError as exc:
        raise LocalPublicationError(
            "invalid_sealed_review",
            f"sealed review package schema validation failed: {exc}",
        ) from exc
    if canonical_json_bytes(package) != package_bytes:
        raise LocalPublicationError(
            "invalid_sealed_review",
            "sealed review package is not canonical",
        )
    expected_artifacts = [
        ExpectedRunArtifact(
            path=refs[-1].path,
            sha256=hashlib.sha256(package_bytes).hexdigest(),
        )
    ]
    for artifact_ref, expected in (
        (package.summary, "paper_reader.summary.v2"),
        (package.review, "paper_reader.review.v2"),
    ):
        artifact_path, raw = verify_artifact_ref(
            run_dir,
            artifact_ref,
            anchor=anchor,
        )
        require_raw_schema_version(
            raw,
            expected=expected,
            artifact_path=artifact_path,
        )
        expected_artifacts.append(
            ExpectedRunArtifact(
                path=artifact_ref.path,
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    return tuple(expected_artifacts)


def _validate_preflight_run_anchor(loaded: LoadedRun, anchor: DirectoryAnchor) -> None:
    if (anchor.device, anchor.inode) != (
        loaded.run_directory_device,
        loaded.run_directory_inode,
    ):
        raise RunLoadError(
            "run_directory_changed",
            f"run directory changed during candidate schema preflight: {anchor.path}",
            manifest_path=loaded.manifest_path,
        )


def _preflight_bound_candidates(
    loaded: LoadedRun,
    anchor: DirectoryAnchor,
) -> tuple[PreflightCandidateBinding, ...]:
    from paper_reader.local_publish import _load_candidate

    anchored = replace(loaded, run_directory_anchor=anchor)
    bindings: list[PreflightCandidateBinding] = []
    for artifact_ref in (
        item for item in loaded.run.artifacts if item.role == "candidate"
    ):
        candidate_path = loaded.manifest_path.parent / artifact_ref.path
        before_snapshot = snapshot_anchored_tree(anchor, candidate_path.parent)
        (
            _loaded,
            verified_path,
            candidate,
            candidate_digest,
            _verified,
        ) = _load_candidate(
            candidate_path,
            loaded_run=anchored,
            require_local=False,
        )
        after_snapshot = snapshot_anchored_tree(anchor, verified_path.parent)
        if after_snapshot != before_snapshot:
            raise LocalPublicationError(
                "sealed_artifact_tampered",
                "candidate tree changed during read-only retry preflight",
            )
        bindings.append(
            PreflightCandidateBinding(
                artifact_ref=artifact_ref,
                candidate_path=verified_path,
                candidate=candidate,
                candidate_digest=candidate_digest,
                candidate_tree_sha256=canonical_json_sha256(after_snapshot),
            )
        )
    return tuple(bindings)


def preflight_sealed_review_schema_versions(
    run_path: Path,
) -> SealedReviewSchemaPreflight:
    """Read-only, lock-free validation of transitive review schema versions."""

    manifest_path = run_manifest_path(run_path)
    run_dir = Path(os.path.abspath(manifest_path.parent))
    runs_root = run_dir.parent.parent
    if runs_root.name == "runs":
        skill_root = runs_root.parent
        with DirectoryAnchor.open(skill_root, manifest_path=manifest_path) as skill_anchor:
            loaded = load_v2_run(run_path)
            with DirectoryAnchor.open(
                run_dir,
                manifest_path=loaded.manifest_path,
            ) as run_anchor:
                _validate_preflight_run_anchor(loaded, run_anchor)
                expected_artifacts = _preflight_sealed_review_from_anchor(
                    loaded,
                    run_anchor,
                )
                bound_candidates = _preflight_bound_candidates(loaded, run_anchor)
                validate_directory_anchor(run_anchor)
                validate_directory_anchor(skill_anchor)
                return SealedReviewSchemaPreflight(
                    loaded_run=loaded,
                    expected_artifacts=expected_artifacts,
                    bound_candidates=bound_candidates,
                    skill_root=skill_anchor.path,
                    skill_root_device=skill_anchor.device,
                    skill_root_inode=skill_anchor.inode,
                )

    loaded = load_v2_run(run_path)
    with DirectoryAnchor.open(run_dir, manifest_path=loaded.manifest_path) as run_anchor:
        _validate_preflight_run_anchor(loaded, run_anchor)
        expected_artifacts = _preflight_sealed_review_from_anchor(
            loaded,
            run_anchor,
        )
        bound_candidates = _preflight_bound_candidates(loaded, run_anchor)
        validate_directory_anchor(run_anchor)
        return SealedReviewSchemaPreflight(
            loaded_run=loaded,
            expected_artifacts=expected_artifacts,
            bound_candidates=bound_candidates,
        )


def build_local_candidate(run_path: Path) -> BuiltLocalCandidate:
    preflight = preflight_sealed_review_schema_versions(run_path)
    return _build_local_candidate_from_preflight(run_path, preflight)


def _build_local_candidate_from_preflight(
    run_path: Path,
    preflight: SealedReviewSchemaPreflight,
) -> BuiltLocalCandidate:
    baseline = preflight
    current = preflight
    for attempt in range(2):
        inspected_run = current.loaded_run
        try:
            with locked_v2_run(
                run_path,
                expected_run_path=inspected_run.manifest_path.parent,
                expected_run_device=inspected_run.run_directory_device,
                expected_run_inode=inspected_run.run_directory_inode,
                expected_run_manifest_sha256=inspected_run.manifest_sha256,
                expected_artifacts=preflight_candidate_lock_artifacts(current),
            ) as loaded:
                return _build_local_candidate_locked(loaded)
        except RunLoadError as exc:
            if exc.code not in {
                "run_manifest_changed",
                "run_artifact_changed",
            } or attempt == 1:
                raise
        refreshed = preflight_sealed_review_schema_versions(run_path)
        if (
            refreshed.loaded_run.manifest_path.parent
            != baseline.loaded_run.manifest_path.parent
            or refreshed.loaded_run.run_directory_device
            != baseline.loaded_run.run_directory_device
            or refreshed.loaded_run.run_directory_inode
            != baseline.loaded_run.run_directory_inode
        ):
            raise RunLoadError(
                "run_directory_changed",
                "local candidate run changed during retry",
                manifest_path=refreshed.loaded_run.manifest_path,
            )
        baseline_run = baseline.loaded_run.run
        refreshed_run = refreshed.loaded_run.run
        if (
            refreshed_run.run_id != baseline_run.run_id
            or refreshed_run.created_at != baseline_run.created_at
            or refreshed_run.source != baseline_run.source
            or refreshed_run.target != baseline_run.target
            or refreshed.expected_artifacts != baseline.expected_artifacts
        ):
            raise LocalPublicationError(
                "sealed_artifact_tampered",
                "local candidate source or sealed review changed during retry",
            )
        validate_candidate_binding_growth(baseline, refreshed)
        current = refreshed
    raise AssertionError("local candidate retry loop did not terminate")


def build_candidate(run_path: Path, *, provider=None):
    preflight = preflight_sealed_review_schema_versions(run_path)
    if isinstance(preflight.loaded_run.run.source, ZoteroSourceIdentity):
        from paper_reader.zotero_candidate import _build_zotero_candidate_from_preflight

        return _build_zotero_candidate_from_preflight(
            run_path,
            preflight,
            provider=provider,
        )
    return _build_local_candidate_from_preflight(run_path, preflight)


def _build_local_candidate_locked(loaded: LoadedRun) -> BuiltLocalCandidate:
    run_dir = loaded.manifest_path.parent
    source = loaded.run.source
    target = loaded.run.target
    if not isinstance(source, LocalSourceIdentity) or not isinstance(target, LocalPublicationTarget):
        raise LocalPublicationError(
            "not_implemented",
            "candidate build is not implemented for Zotero-backed V2 runs",
        )
    verify_local_source(source)
    verify_local_target(target, source)
    package, package_path, package_bytes = _latest_review_package(loaded)
    snapshots = _sealed_snapshots(loaded, package, package_path, package_bytes)
    try:
        load_bound_evidence(loaded, package.evidence_digest)
    except EvidenceManifestError as exc:
        raise LocalPublicationError(exc.code, str(exc)) from exc
    source_refs = [item for item in loaded.run.artifacts if item.role == "source_snapshot"]
    if len(source_refs) != 1:
        raise LocalPublicationError("source_snapshot_missing", "run must bind one source snapshot")
    _source_path, source_bytes = verify_artifact_ref(
        run_dir,
        source_refs[0],
        anchor=loaded.run_directory_anchor,
    )
    summary = PaperReaderSummary.model_validate_json(snapshots["summary.json"])

    candidate_id = new_random_id("candidate")
    candidate_dir = run_dir / "candidates" / candidate_id
    staging = run_dir / f".{candidate_id}.{new_uuid()}.staging"
    run_anchor = loaded.run_directory_anchor
    if run_anchor is None:
        raise LocalPublicationError(
            "run_directory_changed",
            "candidate build requires a locked run directory anchor",
        )
    staging_anchor = create_anchored_directory(run_anchor, staging)
    try:
        files = {"run.json": loaded.manifest_bytes, "source.json": source_bytes, **snapshots}
        for name, content in files.items():
            atomic_write_bytes(staging / name, content, anchor=staging_anchor)
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
        validate_directory_anchor(staging_anchor)
        refs = {
            name: _artifact_ref(run_dir, staging / name, candidate_dir / name, role, media)
            for name, (role, media) in specs.items()
        }
        validate_directory_anchor(staging_anchor)
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
        staged_candidate_path = staging / "candidate.json"
        candidate_bytes = canonical_json_bytes(candidate)
        atomic_write_bytes(
            staged_candidate_path,
            candidate_bytes,
            anchor=staging_anchor,
        )
        candidate_path = candidate_dir / "candidate.json"
        candidate_ref = _artifact_ref(
            run_dir,
            staged_candidate_path,
            candidate_path,
            "candidate",
            "application/json",
        )
        if candidate_ref.sha256 != digest:
            raise LocalPublicationError(
                "candidate_digest_mismatch",
                "canonical candidate core digest does not match candidate.json bytes",
            )
        updated_run = _updated_run(loaded, candidate_ref, gate)
        try:
            enforce_projected_run_size(
                run_dir,
                max_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                staging_dir=staging,
                replacements={loaded.manifest_path: canonical_json_bytes(updated_run)},
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
        verify_local_target(target, source)
        staging_snapshot = tree_snapshot_from_bytes(
            {**files, "candidate.json": candidate_bytes}
        )
        try:
            atomic_publish_tree(
                staging,
                candidate_dir,
                anchor=loaded.run_directory_anchor,
                expected_staging_anchor=staging_anchor,
                expected_tree_snapshot=staging_snapshot,
            )
        except Exception as exc:
            raise LocalPublicationError(
                "candidate_publication_failed",
                f"immutable candidate publication failed: {candidate_dir}: {exc}",
            ) from exc
        try:
            atomic_write_json(
                loaded.manifest_path,
                updated_run,
                anchor=loaded.run_directory_anchor,
            )
        except Exception as exc:
            raise LocalPublicationError(
                "candidate_status_update_failed",
                f"candidate tree is durable but run binding failed: {exc}",
            ) from exc
        return BuiltLocalCandidate(run_dir, candidate_dir, candidate, digest)
    finally:
        try:
            remove_anchored_tree(
                run_anchor,
                staging,
                expected=staging_anchor,
            )
        finally:
            staging_anchor.close()


__all__ = [
    "BuiltLocalCandidate",
    "PreflightCandidateBinding",
    "SealedReviewSchemaPreflight",
    "build_candidate",
    "build_local_candidate",
    "preflight_candidate_lock_artifacts",
    "preflight_sealed_review_schema_versions",
    "validate_candidate_binding_growth",
]
