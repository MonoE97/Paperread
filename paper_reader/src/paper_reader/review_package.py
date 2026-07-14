from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import ValidationError

from paper_reader.contracts import (
    ArtifactRef,
    GateBlocker,
    GateState,
    PaperReaderReview,
    PaperReaderReviewPackage,
    PaperReaderRun,
    PaperReaderSummary,
)
from paper_reader.evidence_manifest import (
    BoundEvidence,
    EvidenceManifestError,
    load_bound_evidence,
    locator_membership_error,
)
from paper_reader.note import render_note, render_note_html, validate_note, validate_trusted_summary
from paper_reader.raw_schema import require_raw_schema_version
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.run_lock import locked_v2_run
from paper_reader.run_size import RunSizeLimitError, enforce_projected_run_size
from paper_reader.storage import (
    HeldExactTreeGuard,
    OwnedPublishedTree,
    UnsafeStoragePathError,
    atomic_publish_tree,
    atomic_write_bytes,
    atomic_write_json,
    cas_update_run,
    canonical_json_bytes,
    canonical_json_sha256,
    create_anchored_directory,
    new_random_id,
    new_uuid,
    rfc3339_utc,
    remove_anchored_tree,
    read_anchored_bytes,
    sha256_file,
    tree_snapshot_from_bytes,
    validate_directory_anchor,
)
from paper_reader.summary_lint import lint_rendered_markdown, lint_summary
from paper_reader.v2_loader import DirectoryAnchor, LoadedRun, RunLoadError, load_v2_run


@dataclass(frozen=True, slots=True)
class ReviewValidation:
    loaded_run: LoadedRun
    run_dir: Path
    summary: PaperReaderSummary | None
    review: PaperReaderReview | None
    evidence: BoundEvidence | None
    summary_path: Path
    review_path: Path
    summary_bytes: bytes | None
    review_bytes: bytes | None
    summary_sha256: str | None
    review_sha256: str | None
    rendered_note: str | None
    rendered_html: str | None
    rendered_note_bytes: bytes | None
    rendered_html_bytes: bytes | None
    rendered_note_sha256: str | None
    blockers: tuple[GateBlocker, ...]


@dataclass(frozen=True, slots=True)
class SealedReview:
    run_dir: Path
    package_dir: Path
    review_package: PaperReaderReviewPackage
    package_digest: str


class ReviewSealError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        blockers: tuple[GateBlocker, ...] = (),
        data: dict[str, str | int] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.blockers = blockers
        self.data = data or {}


def _blocker(code: str, message: str, artifact_path: str | None = None) -> GateBlocker:
    return GateBlocker(code=code, message=message, artifact_path=artifact_path)


def _load_summary(
    path: Path,
    blockers: list[GateBlocker],
    *,
    anchor: DirectoryAnchor,
) -> tuple[PaperReaderSummary | None, bytes | None]:
    try:
        raw = read_anchored_bytes(
            anchor,
            path,
            max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
        )
        require_raw_schema_version(
            raw,
            expected="paper_reader.summary.v2",
            artifact_path=path,
        )
        summary = PaperReaderSummary.model_validate_json(raw)
    except FileNotFoundError:
        blockers.append(_blocker("summary_missing", "summary.json is required", "summary.json"))
        return None, None
    except (OSError, UnsafeStoragePathError, ValidationError) as exc:
        blockers.append(
            _blocker(
                "invalid_summary_schema",
                f"strict paper_reader.summary.v2 validation failed: {exc}",
                "summary.json",
            )
        )
        return None, None
    return summary, canonical_json_bytes(summary)


def _load_review(
    path: Path,
    blockers: list[GateBlocker],
    *,
    anchor: DirectoryAnchor,
) -> tuple[PaperReaderReview | None, bytes | None]:
    try:
        raw = read_anchored_bytes(
            anchor,
            path,
            max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
        )
        require_raw_schema_version(
            raw,
            expected="paper_reader.review.v2",
            artifact_path=path,
        )
        review = PaperReaderReview.model_validate_json(raw)
    except FileNotFoundError:
        blockers.append(_blocker("review_missing", "review.json is required", "review.json"))
        return None, None
    except (OSError, UnsafeStoragePathError, ValidationError) as exc:
        blockers.append(
            _blocker(
                "invalid_review_schema",
                f"strict paper_reader.review.v2 validation failed: {exc}",
                "review.json",
            )
        )
        return None, None
    return review, canonical_json_bytes(review)


def _preflight_review_schema_versions(run_path: Path) -> LoadedRun:
    loaded = load_v2_run(run_path)
    run_dir = loaded.manifest_path.parent
    with DirectoryAnchor.open(run_dir, manifest_path=loaded.manifest_path) as anchor:
        if (anchor.device, anchor.inode) != (
            loaded.run_directory_device,
            loaded.run_directory_inode,
        ):
            raise RunLoadError(
                "run_directory_changed",
                f"run directory changed during review schema preflight: {run_dir}",
                manifest_path=loaded.manifest_path,
            )
        for name, expected in (
            ("summary.json", "paper_reader.summary.v2"),
            ("review.json", "paper_reader.review.v2"),
        ):
            path = run_dir / name
            try:
                raw = read_anchored_bytes(
                    anchor,
                    path,
                    max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                )
            except FileNotFoundError:
                # Missing inputs retain the normal review blocker path.
                continue
            except (OSError, UnsafeStoragePathError) as exc:
                raise RunLoadError(
                    "run_artifact_unsafe",
                    f"review input is not a stable single-link run artifact: {path}: {exc}",
                    manifest_path=path,
                ) from exc
            require_raw_schema_version(raw, expected=expected, artifact_path=path)
    return loaded


def _validate_locator_bindings(
    summary: PaperReaderSummary,
    evidence: BoundEvidence,
    blockers: list[GateBlocker],
) -> None:
    for claim_index, claim in enumerate(summary.evidence_summary):
        for evidence_index, item in enumerate(claim.evidence):
            error = locator_membership_error(item.locator, evidence.manifest)
            if error is not None:
                blockers.append(
                    _blocker(
                        "invalid_evidence_locator",
                        (
                            f"evidence_summary[{claim_index}].evidence[{evidence_index}] "
                            f"failed membership: {error}: {item.locator}"
                        ),
                        "summary.json",
                    )
                )
    for field_name, values in (
        ("author_stated_limitations", summary.author_stated_limitations),
        ("inferred_limits", summary.inferred_limits),
    ):
        for index, item in enumerate(values):
            error = locator_membership_error(item.locator, evidence.manifest)
            if error is not None:
                blockers.append(
                    _blocker(
                        "invalid_evidence_locator",
                        f"{field_name}[{index}] failed membership: {error}: {item.locator}",
                        "summary.json",
                    )
                )
    figure_ids = {item.figure_id for item in evidence.manifest.figures}
    for index, figure in enumerate(summary.key_figures):
        if figure.figure_id not in figure_ids:
            blockers.append(
                _blocker(
                    "figure_not_in_evidence",
                    f"key_figures[{index}] is not a manifest figure member: {figure.figure_id}",
                    "summary.json",
                )
            )


def validate_review_run(
    run_path: Path,
    *,
    loaded_run: LoadedRun | None = None,
) -> ReviewValidation:
    loaded = loaded_run or load_v2_run(run_path)
    if loaded.run_directory_anchor is not None:
        return _validate_review_run_loaded(loaded)
    with DirectoryAnchor.open(
        loaded.manifest_path.parent,
        manifest_path=loaded.manifest_path,
    ) as anchor:
        if (anchor.device, anchor.inode) != (
            loaded.run_directory_device,
            loaded.run_directory_inode,
        ):
            raise RunLoadError(
                "run_directory_changed",
                f"run directory changed before review validation: {loaded.manifest_path.parent}",
                manifest_path=loaded.manifest_path,
            )
        anchored = replace(loaded, run_directory_anchor=anchor)
        validation = _validate_review_run_loaded(anchored)
        return replace(validation, loaded_run=loaded)


def _validate_review_run_loaded(loaded: LoadedRun) -> ReviewValidation:
    run_dir = loaded.manifest_path.parent
    summary_path = run_dir / "summary.json"
    review_path = run_dir / "review.json"
    blockers: list[GateBlocker] = []
    anchor = loaded.run_directory_anchor
    if anchor is None:
        raise RunLoadError(
            "run_directory_changed",
            "review validation requires a verified run directory anchor",
            manifest_path=loaded.manifest_path,
        )
    summary, summary_bytes = _load_summary(summary_path, blockers, anchor=anchor)
    review, review_bytes = _load_review(review_path, blockers, anchor=anchor)
    summary_sha256 = canonical_json_sha256(summary) if summary is not None else None
    review_sha256 = canonical_json_sha256(review) if review is not None else None
    evidence: BoundEvidence | None = None
    rendered_note: str | None = None
    rendered_html: str | None = None
    rendered_note_bytes: bytes | None = None
    rendered_html_bytes: bytes | None = None
    rendered_note_sha256: str | None = None

    if summary is not None:
        if summary.run_id != loaded.run.run_id:
            blockers.append(_blocker("summary_run_mismatch", "summary run_id does not match run.json", "summary.json"))
        try:
            evidence = load_bound_evidence(loaded, summary.evidence_digest)
        except EvidenceManifestError as exc:
            blockers.append(_blocker(exc.code, str(exc), exc.artifact_path))
        else:
            if not evidence.manifest.complete:
                blockers.append(
                    _blocker(
                        "incomplete_evidence",
                        "preview evidence cannot be sealed or used for a candidate",
                        evidence.manifest_ref.path,
                    )
                )
            _validate_locator_bindings(summary, evidence, blockers)

        summary_payload = summary.model_dump(mode="json")
        for message in validate_trusted_summary(summary_payload):
            blockers.append(_blocker("summary_not_write_ready", message, "summary.json"))
        for issue in lint_summary(summary_payload):
            blockers.append(_blocker(issue["code"], issue["message"], "summary.json"))

    if review is not None:
        if review.run_id != loaded.run.run_id:
            blockers.append(_blocker("review_run_mismatch", "review run_id does not match run.json", "review.json"))
        if summary_sha256 is not None and review.summary_sha256 != summary_sha256:
            blockers.append(
                _blocker(
                    "summary_hash_mismatch",
                    "review summary_sha256 does not match canonical summary.json",
                    "review.json",
                )
            )
        if summary is not None and review.evidence_digest != summary.evidence_digest:
            blockers.append(
                _blocker(
                    "review_evidence_mismatch",
                    "review evidence_digest does not match summary evidence_digest",
                    "review.json",
                )
            )
        if review.review_status not in {"passed", "passed_with_caveats"}:
            blockers.append(_blocker("review_failed", "review_status must pass before sealing", "review.json"))
        if review.needs_improvement:
            blockers.append(
                _blocker("review_needs_improvement", "review still requires improvement", "review.json")
            )
        for index, issue in enumerate(review.review_issues):
            if issue.severity == "blocker":
                blockers.append(
                    _blocker(
                        "review_issue_blocker",
                        f"review_issues[{index}] is marked as a blocker: {issue.issue}",
                        "review.json",
                    )
                )
        if summary is not None and summary.review_status != review.review_status:
            blockers.append(
                _blocker(
                    "review_status_mismatch",
                    "summary and review status do not match",
                    "review.json",
                )
            )

    if summary is not None and evidence is not None:
        try:
            metadata_artifact = evidence.artifacts_by_role["metadata"][0]
            metadata = json.loads(metadata_artifact.raw_bytes)
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be an object")
            rendered_note = render_note(
                metadata,
                summary.model_dump(mode="json"),
                generated_date=loaded.run.created_at[:10],
            )
            rendered_html = render_note_html(rendered_note)
            rendered_note_bytes = rendered_note.encode("utf-8")
            rendered_html_bytes = rendered_html.encode("utf-8")
            rendered_note_sha256 = hashlib.sha256(rendered_note_bytes).hexdigest()
        except Exception as exc:
            blockers.append(_blocker("note_render_failed", f"note rendering failed: {exc}"))
        else:
            for message in validate_note(rendered_note):
                blockers.append(_blocker("invalid_rendered_note", message))
            for issue in lint_rendered_markdown(rendered_note):
                blockers.append(_blocker(issue["code"], issue["message"], "summary.json"))

    return ReviewValidation(
        loaded_run=loaded,
        run_dir=run_dir,
        summary=summary,
        review=review,
        evidence=evidence,
        summary_path=summary_path,
        review_path=review_path,
        summary_bytes=summary_bytes,
        review_bytes=review_bytes,
        summary_sha256=summary_sha256,
        review_sha256=review_sha256,
        rendered_note=rendered_note,
        rendered_html=rendered_html,
        rendered_note_bytes=rendered_note_bytes,
        rendered_html_bytes=rendered_html_bytes,
        rendered_note_sha256=rendered_note_sha256,
        blockers=tuple(blockers),
    )


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


def _reviewed_run(
    validation: ReviewValidation,
    package_ref: ArtifactRef,
    gate: GateState,
) -> PaperReaderRun:
    run = validation.loaded_run.run
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=run.run_id,
        created_at=run.created_at,
        source=run.source,
        target=run.target,
        status="reviewed",
        artifacts=(*run.artifacts, package_ref),
        gate=gate,
        live_preflight=run.live_preflight,
    )


def seal_review_run(run_path: Path) -> SealedReview:
    preflight = _preflight_review_schema_versions(run_path)
    with locked_v2_run(
        run_path,
        expected_run_path=preflight.manifest_path.parent,
        expected_run_device=preflight.run_directory_device,
        expected_run_inode=preflight.run_directory_inode,
        expected_run_manifest_sha256=preflight.manifest_sha256,
    ) as loaded:
        return _seal_review_run_locked(run_path, loaded)


def _seal_review_run_locked(run_path: Path, loaded: LoadedRun) -> SealedReview:
    validation = validate_review_run(run_path, loaded_run=loaded)
    if validation.blockers:
        raise ReviewSealError(
            "review_blocked",
            "review validation is blocked",
            blockers=validation.blockers,
            data={"run_id": validation.loaded_run.run.run_id},
        )
    if (
        validation.summary is None
        or validation.review is None
        or validation.evidence is None
        or validation.summary_bytes is None
        or validation.review_bytes is None
        or validation.summary_sha256 is None
        or validation.review_sha256 is None
        or validation.rendered_note_bytes is None
        or validation.rendered_html_bytes is None
        or validation.rendered_note_sha256 is None
    ):
        raise ReviewSealError("review_blocked", "validated review inputs are incomplete")

    package_id = new_random_id("review-package")
    package_dir = validation.run_dir / "reviews" / package_id
    staging = validation.run_dir / f".{package_id}.{new_uuid()}.staging"
    run_anchor = validation.loaded_run.run_directory_anchor
    if run_anchor is None:
        raise ReviewSealError(
            "run_directory_changed",
            "review seal requires a locked run directory anchor",
        )
    staging_anchor = create_anchored_directory(run_anchor, staging)
    try:
        snapshot_bytes = {
            "summary.json": validation.summary_bytes,
            "review.json": validation.review_bytes,
            "evidence.json": validation.evidence.manifest_bytes,
            "note.md": validation.rendered_note_bytes,
            "note.html": validation.rendered_html_bytes,
        }
        validation_payload = {
            "format": "paper_reader.review-validation.v2-internal",
            "run_id": validation.loaded_run.run.run_id,
            "summary_sha256": validation.summary_sha256,
            "review_sha256": validation.review_sha256,
            "evidence_digest": validation.evidence.digest,
            "rendered_note_sha256": validation.rendered_note_sha256,
            "rendered_html_sha256": hashlib.sha256(validation.rendered_html_bytes).hexdigest(),
            "checks": [
                "summary_schema",
                "review_schema",
                "run_binding",
                "evidence_binding",
                "locator_membership",
                "resolved_render_chinese_prose",
            ],
            "blockers": [],
        }
        snapshot_bytes["validation.json"] = canonical_json_bytes(validation_payload)
        for name, content in snapshot_bytes.items():
            atomic_write_bytes(staging / name, content, anchor=staging_anchor)

        specs = {
            "summary.json": ("summary_snapshot", "application/json"),
            "review.json": ("review_snapshot", "application/json"),
            "evidence.json": ("evidence_manifest_snapshot", "application/json"),
            "validation.json": ("review_validation", "application/json"),
            "note.md": ("review_note_markdown", "text/markdown"),
            "note.html": ("review_note_html", "text/html"),
        }
        validate_directory_anchor(staging_anchor)
        refs = {
            name: _artifact_ref(
                validation.run_dir,
                staging / name,
                package_dir / name,
                role,
                media_type,
            )
            for name, (role, media_type) in specs.items()
        }
        validate_directory_anchor(staging_anchor)
        gate = GateState(
            status="passed",
            evaluated_at=rfc3339_utc(),
            checks=(
                "summary_schema",
                "review_schema",
                "run_binding",
                "evidence_binding",
                "locator_membership",
                "resolved_render_chinese_prose",
            ),
            blockers=(),
        )
        review_package = PaperReaderReviewPackage(
            schema_version="paper_reader.review-package.v2",
            review_package_id=package_id,
            run_id=validation.loaded_run.run.run_id,
            created_at=rfc3339_utc(),
            summary=refs["summary.json"],
            review=refs["review.json"],
            evidence_manifest=refs["evidence.json"],
            summary_sha256=validation.summary_sha256,
            review_sha256=validation.review_sha256,
            evidence_digest=validation.evidence.digest,
            artifacts=tuple(refs.values()),
            gate=gate,
        )
        staged_package_path = staging / "review-package.json"
        atomic_write_bytes(
            staged_package_path,
            canonical_json_bytes(review_package),
            anchor=staging_anchor,
        )
        package_path = package_dir / "review-package.json"
        package_ref = _artifact_ref(
            validation.run_dir,
            staged_package_path,
            package_path,
            "review_package",
            "application/json",
        )
        updated_run = _reviewed_run(validation, package_ref, gate)
        try:
            enforce_projected_run_size(
                validation.run_dir,
                max_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                staging_dir=staging,
                replacements={
                    validation.loaded_run.manifest_path: canonical_json_bytes(updated_run),
                },
                retained_replacement_paths=(validation.loaded_run.manifest_path,),
            )
        except RunSizeLimitError as exc:
            raise ReviewSealError(
                "run_size_limit_exceeded",
                str(exc),
                data={
                    "run_size_bytes": exc.actual_bytes,
                    "max_bytes": exc.max_bytes,
                },
            ) from exc
        staging_snapshot = tree_snapshot_from_bytes(
            {
                **snapshot_bytes,
                "review-package.json": canonical_json_bytes(review_package),
            }
        )
        try:
            published = atomic_publish_tree(
                staging,
                package_dir,
                anchor=validation.loaded_run.run_directory_anchor,
                expected_staging_anchor=staging_anchor,
                expected_tree_snapshot=staging_snapshot,
                hold_open_relative_file="review-package.json",
            )
            if not isinstance(published, OwnedPublishedTree):
                raise ReviewSealError(
                    "review_seal_failed",
                    "immutable review publication did not retain its held identity",
                    data={"run_id": validation.loaded_run.run.run_id},
                )
        except Exception as exc:
            raise ReviewSealError(
                "review_seal_failed",
                f"immutable review package publication failed: {package_dir}: {exc}",
                data={"run_id": validation.loaded_run.run.run_id},
            ) from exc
        try:
            with HeldExactTreeGuard(
                published_tree=published,
                expected_tree=staging_snapshot,
                expected_held_bytes=canonical_json_bytes(review_package),
                label="review package",
            ) as published_guard:
                published_guard.verify()
                cas_update_run(
                    validation.loaded_run,
                    updated_run,
                    finalization_guards=(published_guard,),
                )
                published_guard.verify()
        except Exception as exc:
            raise ReviewSealError(
                "review_status_update_failed",
                f"review package is durable but run binding failed: {exc}",
                data={"run_id": validation.loaded_run.run.run_id},
            ) from exc
        return SealedReview(
            run_dir=validation.run_dir,
            package_dir=package_dir,
            review_package=review_package,
            package_digest=package_ref.sha256,
        )
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
    "ReviewSealError",
    "ReviewValidation",
    "SealedReview",
    "seal_review_run",
    "validate_review_run",
]
