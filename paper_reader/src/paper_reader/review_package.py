from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
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
from paper_reader.summary_lint import lint_rendered_markdown, lint_summary
from paper_reader.v2_loader import LoadedRun, load_v2_run


@dataclass(frozen=True, slots=True)
class ReviewValidation:
    loaded_run: LoadedRun
    run_dir: Path
    summary: PaperReaderSummary | None
    review: PaperReaderReview | None
    evidence: BoundEvidence | None
    summary_path: Path
    review_path: Path
    summary_sha256: str | None
    review_sha256: str | None
    rendered_note: str | None
    rendered_html: str | None
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
        data: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.blockers = blockers
        self.data = data or {}


def _blocker(code: str, message: str, artifact_path: str | None = None) -> GateBlocker:
    return GateBlocker(code=code, message=message, artifact_path=artifact_path)


def _load_summary(path: Path, blockers: list[GateBlocker]) -> PaperReaderSummary | None:
    try:
        return PaperReaderSummary.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        blockers.append(_blocker("summary_missing", "summary.json is required", "summary.json"))
    except (OSError, ValidationError) as exc:
        blockers.append(
            _blocker(
                "invalid_summary_schema",
                f"strict paper_reader.summary.v2 validation failed: {exc}",
                "summary.json",
            )
        )
    return None


def _load_review(path: Path, blockers: list[GateBlocker]) -> PaperReaderReview | None:
    try:
        return PaperReaderReview.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        blockers.append(_blocker("review_missing", "review.json is required", "review.json"))
    except (OSError, ValidationError) as exc:
        blockers.append(
            _blocker(
                "invalid_review_schema",
                f"strict paper_reader.review.v2 validation failed: {exc}",
                "review.json",
            )
        )
    return None


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


def validate_review_run(run_path: Path) -> ReviewValidation:
    loaded = load_v2_run(run_path)
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    summary_path = run_dir / "summary.json"
    review_path = run_dir / "review.json"
    blockers: list[GateBlocker] = []
    summary = _load_summary(summary_path, blockers)
    review = _load_review(review_path, blockers)
    summary_sha256 = canonical_json_sha256(summary) if summary is not None else None
    review_sha256 = canonical_json_sha256(review) if review is not None else None
    evidence: BoundEvidence | None = None
    rendered_note: str | None = None
    rendered_html: str | None = None
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
            metadata_path = evidence.artifacts_by_role["metadata"][0]
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be an object")
            rendered_note = render_note(
                metadata,
                summary.model_dump(mode="json"),
                generated_date=loaded.run.created_at[:10],
            )
            rendered_html = render_note_html(rendered_note)
            rendered_note_sha256 = hashlib.sha256(rendered_note.encode("utf-8")).hexdigest()
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
        summary_sha256=summary_sha256,
        review_sha256=review_sha256,
        rendered_note=rendered_note,
        rendered_html=rendered_html,
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
    validation = validate_review_run(run_path)
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
        or validation.summary_sha256 is None
        or validation.review_sha256 is None
        or validation.rendered_note is None
        or validation.rendered_html is None
        or validation.rendered_note_sha256 is None
    ):
        raise ReviewSealError("review_blocked", "validated review inputs are incomplete")

    package_id = new_random_id("review-package")
    package_dir = validation.run_dir / "reviews" / package_id
    staging = validation.run_dir / f".{package_id}.{new_uuid()}.staging"
    staging.mkdir()
    try:
        snapshot_bytes = {
            "summary.json": validation.summary_path.read_bytes(),
            "review.json": validation.review_path.read_bytes(),
            "evidence.json": validation.evidence.manifest_bytes,
            "note.md": validation.rendered_note.encode("utf-8"),
            "note.html": validation.rendered_html.encode("utf-8"),
        }
        validation_payload = {
            "format": "paper_reader.review-validation.v2-internal",
            "run_id": validation.loaded_run.run.run_id,
            "summary_sha256": validation.summary_sha256,
            "review_sha256": validation.review_sha256,
            "evidence_digest": validation.evidence.digest,
            "rendered_note_sha256": validation.rendered_note_sha256,
            "rendered_html_sha256": hashlib.sha256(
                validation.rendered_html.encode("utf-8")
            ).hexdigest(),
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
            (staging / name).write_bytes(content)

        specs = {
            "summary.json": ("summary_snapshot", "application/json"),
            "review.json": ("review_snapshot", "application/json"),
            "evidence.json": ("evidence_manifest_snapshot", "application/json"),
            "validation.json": ("review_validation", "application/json"),
            "note.md": ("review_note_markdown", "text/markdown"),
            "note.html": ("review_note_html", "text/html"),
        }
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
        (staging / "review-package.json").write_bytes(canonical_json_bytes(review_package))
        try:
            atomic_publish_tree(staging, package_dir)
        except Exception as exc:
            raise ReviewSealError(
                "review_seal_failed",
                f"immutable review package publication failed: {package_dir}: {exc}",
                data={"run_id": validation.loaded_run.run.run_id},
            ) from exc
        package_path = package_dir / "review-package.json"
        package_ref = ArtifactRef(
            role="review_package",
            path=package_path.relative_to(validation.run_dir).as_posix(),
            sha256=sha256_file(package_path),
            size_bytes=package_path.stat().st_size,
            media_type="application/json",
        )
        atomic_write_json(
            validation.loaded_run.manifest_path,
            _reviewed_run(validation, package_ref, gate),
        )
        return SealedReview(
            run_dir=validation.run_dir,
            package_dir=package_dir,
            review_package=review_package,
            package_digest=package_ref.sha256,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)


__all__ = [
    "ReviewSealError",
    "ReviewValidation",
    "SealedReview",
    "seal_review_run",
    "validate_review_run",
]
