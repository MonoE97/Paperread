from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

import fitz

from paper_reader.contracts import (
    ArtifactRef,
    GateState,
    LocalSourceIdentity,
    PaperReaderRun,
)
from paper_reader.evidence_figures import (
    IncompleteFigureEvidenceError,
    prepare_figure_artifacts,
)
from paper_reader.evidence_manifest import (
    EvidenceManifest,
    EvidenceManifestError,
    build_evidence_manifest,
    validate_evidence_manifest_membership,
)
from paper_reader.pdf_extract import ExtractedTextLimitError, extract_pdf
from paper_reader.pdf_workflow import build_pdf_metadata
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.run_lock import locked_v2_run
from paper_reader.run_size import (
    RunSizeLimitError,
    enforce_projected_run_size,
    projected_run_size,
)
from paper_reader.storage import (
    atomic_publish_tree,
    atomic_write_json,
    canonical_json_bytes,
    fingerprint_resolved_source,
    new_random_id,
    new_uuid,
    sha256_file,
)
from paper_reader.v2_loader import LoadedRun
from paper_reader.workflow import (
    build_context_markdown,
    build_section_context_markdown,
)


@dataclass(frozen=True, slots=True)
class PreparedEvidence:
    run_dir: Path
    evidence_dir: Path
    evidence_manifest: EvidenceManifest
    evidence_digest: str


class EvidenceBundleError(ValueError):
    def __init__(self, code: str, message: str, *, data: dict[str, str | int] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


def _run_root(loaded: LoadedRun) -> Path:
    return loaded.manifest_path.resolve(strict=True).parent


def _verify_local_source(loaded: LoadedRun) -> LocalSourceIdentity:
    source = loaded.run.source
    if not isinstance(source, LocalSourceIdentity):
        raise EvidenceBundleError(
            "not_implemented",
            "run prepare is not implemented for Zotero-backed V2 runs",
            data={"run_id": loaded.run.run_id},
        )
    try:
        actual = fingerprint_resolved_source(Path(source.resolved_path))
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceBundleError(
            "source_changed",
            f"local PDF source cannot be revalidated: {source.resolved_path}: {exc}",
            data={"run_id": loaded.run.run_id, "source_pdf": source.resolved_path},
        ) from exc
    expected_identity = (
        source.resolved_path,
        source.sha256,
        source.size_bytes,
        source.device,
        source.inode,
    )
    actual_identity = (
        actual.resolved_path,
        actual.sha256,
        actual.size_bytes,
        actual.device,
        actual.inode,
    )
    if actual_identity != expected_identity:
        raise EvidenceBundleError(
            "source_changed",
            f"local PDF source no longer matches the initialized fingerprint: {source.resolved_path}",
            data={"run_id": loaded.run.run_id, "source_pdf": source.resolved_path},
        )
    return source


def _artifact_ref(run_dir: Path, path: Path, role: str, media_type: str) -> ArtifactRef:
    return ArtifactRef(
        role=role,
        path=path.relative_to(run_dir).as_posix(),
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        media_type=media_type,
    )


def _secondary_sources(metadata: dict) -> dict:
    return {
        "item_key": "",
        "title": str(metadata.get("title", "")),
        "usage_boundary": "cross-check only; must not be cited in evidence_summary",
        "sources": [],
        "warnings": [],
    }


def _page_count(source_path: Path) -> int:
    try:
        with fitz.open(source_path) as document:
            return document.page_count
    except Exception as exc:
        raise EvidenceBundleError(
            "invalid_local_pdf",
            f"local PDF cannot be opened for preparation: {source_path}: {exc}",
            data={"source_pdf": str(source_path)},
        ) from exc


def _updated_run(loaded: LoadedRun, evidence_ref: ArtifactRef) -> PaperReaderRun:
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=loaded.run.run_id,
        created_at=loaded.run.created_at,
        source=loaded.run.source,
        target=loaded.run.target,
        status="prepared",
        artifacts=(*loaded.run.artifacts, evidence_ref),
        gate=GateState(status="not_evaluated"),
        live_preflight=loaded.run.live_preflight,
    )


def prepare_local_evidence(
    run_path: Path,
    *,
    preview_pages: int | None = None,
    figure_limit: int | None = None,
) -> PreparedEvidence:
    with locked_v2_run(run_path) as loaded:
        return _prepare_local_evidence_locked(
            loaded,
            preview_pages=preview_pages,
            figure_limit=figure_limit,
        )


def _prepare_local_evidence_locked(
    loaded: LoadedRun,
    *,
    preview_pages: int | None,
    figure_limit: int | None,
) -> PreparedEvidence:
    run_dir = _run_root(loaded)
    source = _verify_local_source(loaded)
    resolved_figure_limit = (
        V2_RESOURCE_POLICY.figure_default_limit if figure_limit is None else figure_limit
    )
    if not 0 <= resolved_figure_limit <= V2_RESOURCE_POLICY.figure_hard_limit:
        raise EvidenceBundleError(
            "figure_limit_exceeded",
            f"figure limit must be between 0 and {V2_RESOURCE_POLICY.figure_hard_limit}",
            data={"figure_limit": resolved_figure_limit},
        )

    source_path = Path(source.resolved_path)
    page_count = _page_count(source_path)
    if page_count > V2_RESOURCE_POLICY.pdf_max_pages:
        raise EvidenceBundleError(
            "pdf_page_limit_exceeded",
            f"PDF has {page_count} pages; limit is {V2_RESOURCE_POLICY.pdf_max_pages}",
            data={"page_count": page_count, "max_pages": V2_RESOURCE_POLICY.pdf_max_pages},
        )

    try:
        extraction = extract_pdf(
            source_path,
            max_pages=preview_pages,
            max_chars=V2_RESOURCE_POLICY.extracted_text_max_chars,
        )
    except ExtractedTextLimitError as exc:
        raise EvidenceBundleError(
            "extracted_text_limit_exceeded",
            f"extracted text exceeded {exc.max_chars} characters during page iteration",
            data={
                "extracted_chars": exc.actual_chars,
                "max_chars": exc.max_chars,
            },
        ) from exc
    extracted_chars = len(str(extraction.get("text", "")))
    if extracted_chars > V2_RESOURCE_POLICY.extracted_text_max_chars:
        raise EvidenceBundleError(
            "extracted_text_limit_exceeded",
            f"extracted text has {extracted_chars} characters; limit is {V2_RESOURCE_POLICY.extracted_text_max_chars}",
            data={
                "extracted_chars": extracted_chars,
                "max_chars": V2_RESOURCE_POLICY.extracted_text_max_chars,
            },
        )

    metadata = build_pdf_metadata(source_path)
    complete = preview_pages is None
    evidence_id = new_random_id("evidence")
    staging = run_dir / f".{evidence_id}.{new_uuid()}.staging"
    staging.mkdir()
    try:
        files_to_write = {
            "metadata.json": canonical_json_bytes(metadata),
            "extract.json": canonical_json_bytes(extraction),
            "context.md": build_context_markdown(metadata, extraction).encode("utf-8"),
            "section_context.md": build_section_context_markdown(metadata, extraction).encode("utf-8"),
            "secondary_sources.json": canonical_json_bytes(_secondary_sources(metadata)),
        }
        for name, content in files_to_write.items():
            (staging / name).write_bytes(content)

        future_dir = run_dir / "evidence" / evidence_id
        artifact_specs: list[tuple[Path, Path, str, str]] = [
            (staging / "metadata.json", future_dir / "metadata.json", "metadata", "application/json"),
            (staging / "extract.json", future_dir / "extract.json", "extract", "application/json"),
            (staging / "context.md", future_dir / "context.md", "context", "text/markdown"),
            (
                staging / "section_context.md",
                future_dir / "section_context.md",
                "section_context",
                "text/markdown",
            ),
            (
                staging / "secondary_sources.json",
                future_dir / "secondary_sources.json",
                "secondary_sources",
                "application/json",
            ),
        ]
        try:
            prepared_figures = prepare_figure_artifacts(
                source_path=source_path,
                staging=staging,
                future_dir=future_dir,
                run_dir=run_dir,
                figure_limit=resolved_figure_limit,
                preview_pages=preview_pages,
                complete=complete,
            )
        except IncompleteFigureEvidenceError as exc:
            raise EvidenceBundleError(
                "figure_extraction_failed",
                f"figure extraction failed for incomplete preview evidence: {exc}",
                data={"run_id": loaded.run.run_id},
            ) from exc
        artifact_specs.extend(prepared_figures.artifacts)
        files = tuple(
            _artifact_ref(run_dir, staged_path, role, media_type).model_copy(
                update={"path": future_path.relative_to(run_dir).as_posix()}
            )
            for staged_path, future_path, role, media_type in artifact_specs
        )
        manifest = build_evidence_manifest(
            evidence_id=evidence_id,
            run_id=loaded.run.run_id,
            source_sha256=source.sha256,
            complete=complete,
            preview_pages=preview_pages,
            files=files,
            extraction=extraction,
            figure_limit=resolved_figure_limit,
            run_size_bytes=0,
            figures=prepared_figures.members,
            degraded=prepared_figures.degraded,
            figure_check=prepared_figures.extraction_check,
            figure_resource_checks=prepared_figures.resource_checks,
        )
        manifest_path = staging / "evidence.json"
        manifest_ref: ArtifactRef | None = None
        updated_run: PaperReaderRun | None = None
        predicted_size: int | None = None
        for _attempt in range(8):
            manifest_path.write_bytes(canonical_json_bytes(manifest))
            manifest_ref = _artifact_ref(
                run_dir,
                manifest_path,
                "evidence_manifest",
                "application/json",
            ).model_copy(
                update={"path": (future_dir / "evidence.json").relative_to(run_dir).as_posix()}
            )
            updated_run = _updated_run(loaded, manifest_ref)
            next_size = projected_run_size(
                run_dir,
                staging_dir=staging,
                replacements={
                    loaded.manifest_path: canonical_json_bytes(updated_run),
                },
            )
            current_size = next(
                int(item.actual)
                for item in manifest.resource_checks
                if item.name == "run_size_bytes"
            )
            if next_size == current_size:
                predicted_size = next_size
                break
            checks = tuple(
                item.model_copy(update={"actual": next_size})
                if item.name == "run_size_bytes"
                else item
                for item in manifest.resource_checks
            )
            manifest = manifest.model_copy(update={"resource_checks": checks})
        if predicted_size is None or manifest_ref is None or updated_run is None:
            raise EvidenceBundleError(
                "run_size_accounting_failed",
                "prepared run size accounting did not converge",
                data={"run_id": loaded.run.run_id},
            )
        try:
            validate_evidence_manifest_membership(
                manifest,
                run_dir=run_dir,
                bundle_dir=staging,
                manifest_bundle_dir=future_dir,
            )
        except EvidenceManifestError as exc:
            raise EvidenceBundleError(
                exc.code,
                str(exc),
                data={"run_id": loaded.run.run_id},
            ) from exc
        try:
            enforce_projected_run_size(
                run_dir,
                max_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                staging_dir=staging,
                replacements={
                    loaded.manifest_path: canonical_json_bytes(updated_run),
                },
            )
        except RunSizeLimitError as exc:
            raise EvidenceBundleError(
                "run_size_limit_exceeded",
                str(exc),
                data={
                    "run_size_bytes": exc.actual_bytes,
                    "max_bytes": exc.max_bytes,
                },
            ) from exc

        destination = run_dir / "evidence" / evidence_id
        try:
            atomic_publish_tree(staging, destination)
        except Exception as exc:
            raise EvidenceBundleError(
                "evidence_publication_failed",
                f"immutable evidence publication failed: {destination}: {exc}",
                data={"run_id": loaded.run.run_id, "evidence_dir": str(destination)},
            ) from exc
        published_manifest = destination / "evidence.json"
        try:
            atomic_write_json(loaded.manifest_path, updated_run)
        except Exception as exc:
            raise EvidenceBundleError(
                "evidence_status_update_failed",
                f"evidence tree is durable but run binding failed: {exc}",
                data={
                    "run_id": loaded.run.run_id,
                    "evidence_dir": str(destination),
                },
            ) from exc
        return PreparedEvidence(
            run_dir=run_dir,
            evidence_dir=destination,
            evidence_manifest=manifest,
            evidence_digest=hashlib.sha256(published_manifest.read_bytes()).hexdigest(),
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)


__all__ = [
    "EvidenceBundleError",
    "EvidenceManifest",
    "PreparedEvidence",
    "prepare_local_evidence",
]
