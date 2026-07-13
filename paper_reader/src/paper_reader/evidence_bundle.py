from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import fitz

from paper_reader.contracts import (
    ArtifactRef,
    GateState,
    LocalSourceIdentity,
    PaperReaderRun,
    ZoteroSourceIdentity,
)
from paper_reader.candidate_integrity import LocalPublicationError, verify_artifact_ref
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
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    create_anchored_directory,
    new_random_id,
    new_uuid,
    remove_anchored_tree,
    sha256_file,
    tree_snapshot_from_hashes,
    validate_directory_anchor,
)
from paper_reader.secondary_sources import build_secondary_sources
from paper_reader.v2_loader import LoadedRun
from paper_reader.workflow import (
    build_context_markdown,
    build_metadata,
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


@dataclass(frozen=True, slots=True)
class _PreparedSource:
    pdf: LocalSourceIdentity
    metadata: dict
    secondary_sources: dict
    allow_network_figure_source: bool


def _run_root(loaded: LoadedRun) -> Path:
    return loaded.manifest_path.parent


def _pdf_identity(loaded: LoadedRun) -> LocalSourceIdentity:
    source = loaded.run.source
    if isinstance(source, LocalSourceIdentity):
        return source
    if isinstance(source, ZoteroSourceIdentity):
        return source.attachment
    raise EvidenceBundleError("invalid_source_identity", "unknown V2 source identity")


def _anonymous_pdf_path(
    snapshot_handle,
    *,
    run_id: str,
    source_path: str,
) -> Path:
    descriptor = snapshot_handle.fileno()
    opened = os.fstat(descriptor)
    if os.name == "posix":
        if opened.st_nlink != 0:
            raise EvidenceBundleError(
                "secure_pdf_snapshot_unavailable",
                f"anonymous PDF snapshot could not be created for: {source_path}",
                data={"run_id": run_id, "source_pdf": source_path},
            )
        for descriptor_root in (Path("/dev/fd"), Path("/proc/self/fd")):
            candidate = descriptor_root / str(descriptor)
            try:
                candidate_stat = os.stat(candidate)
            except OSError:
                continue
            if (
                candidate_stat.st_ino == opened.st_ino
                and candidate_stat.st_size == opened.st_size
                and candidate_stat.st_nlink == opened.st_nlink
            ):
                return candidate
    elif isinstance(snapshot_handle.name, str):  # pragma: no cover - POSIX runtime
        return Path(snapshot_handle.name)
    raise EvidenceBundleError(
        "secure_pdf_snapshot_unavailable",
        f"delete-on-close PDF snapshot path is unavailable for: {source_path}",
        data={"run_id": run_id, "source_pdf": source_path},
    )


@contextmanager
def _verified_pdf_snapshot(
    source: LocalSourceIdentity,
    *,
    run_id: str,
) -> Iterator[Path]:
    source_path = Path(source.resolved_path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source_path, flags)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceBundleError(
            "source_changed",
            f"local PDF source cannot be revalidated: {source.resolved_path}: {exc}",
            data={"run_id": run_id, "source_pdf": source.resolved_path},
        ) from exc
    with os.fdopen(descriptor, "rb") as source_handle:
        before = os.fstat(source_handle.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino, before.st_size)
            != (source.device, source.inode, source.size_bytes)
        ):
            raise EvidenceBundleError(
                "source_changed",
                f"local PDF source no longer matches the initialized identity: {source.resolved_path}",
                data={"run_id": run_id, "source_pdf": source.resolved_path},
            )
        if before.st_size > V2_RESOURCE_POLICY.local_pdf_max_bytes:
            raise EvidenceBundleError(
                "source_too_large",
                f"local PDF source exceeds {V2_RESOURCE_POLICY.local_pdf_max_bytes} bytes",
                data={
                    "run_id": run_id,
                    "source_pdf": source.resolved_path,
                    "size_bytes": before.st_size,
                    "max_size_bytes": V2_RESOURCE_POLICY.local_pdf_max_bytes,
                },
            )

        with tempfile.TemporaryFile(mode="w+b") as snapshot_handle:
            digest = hashlib.sha256()
            copied = 0
            while chunk := source_handle.read(1024 * 1024):
                copied += len(chunk)
                if copied > source.size_bytes:
                    raise EvidenceBundleError(
                        "source_changed",
                        f"local PDF source grew while it was snapshotted: {source.resolved_path}",
                        data={"run_id": run_id, "source_pdf": source.resolved_path},
                    )
                digest.update(chunk)
                snapshot_handle.write(chunk)
            snapshot_handle.flush()
            os.fsync(snapshot_handle.fileno())

            after = os.fstat(source_handle.fileno())
            before_identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            after_identity = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if before_identity != after_identity:
                raise EvidenceBundleError(
                    "source_changed",
                    f"local PDF source changed while it was snapshotted: {source.resolved_path}",
                    data={"run_id": run_id, "source_pdf": source.resolved_path},
                )
            if copied != source.size_bytes or digest.hexdigest() != source.sha256:
                raise EvidenceBundleError(
                    "source_changed",
                    f"local PDF source no longer matches the initialized fingerprint: {source.resolved_path}",
                    data={"run_id": run_id, "source_pdf": source.resolved_path},
                )
            snapshot_handle.seek(0)
            snapshot_path = _anonymous_pdf_path(
                snapshot_handle,
                run_id=run_id,
                source_path=source.resolved_path,
            )
            yield snapshot_path


def _verify_source(loaded: LoadedRun, *, pdf: LocalSourceIdentity) -> _PreparedSource:
    source = loaded.run.source
    if isinstance(source, LocalSourceIdentity):
        if source != pdf:  # pragma: no cover - internal caller invariant
            raise EvidenceBundleError("invalid_source_identity", "local PDF identity mismatch")
        metadata = build_pdf_metadata(Path(pdf.resolved_path))
        return _PreparedSource(
            pdf=pdf,
            metadata=metadata,
            secondary_sources=_secondary_sources(metadata),
            allow_network_figure_source=False,
        )
    if not isinstance(source, ZoteroSourceIdentity):  # pragma: no cover - strict discriminator
        raise EvidenceBundleError("invalid_source_identity", "unknown V2 source identity")
    if source.attachment != pdf:  # pragma: no cover - internal caller invariant
        raise EvidenceBundleError("invalid_source_identity", "Zotero PDF identity mismatch")
    run_dir = _run_root(loaded)
    try:
        _snapshot_path, snapshot_bytes = verify_artifact_ref(
            run_dir,
            source.normalized_source,
            anchor=loaded.run_directory_anchor,
        )
    except LocalPublicationError as exc:
        raise EvidenceBundleError(
            "source_snapshot_tampered",
            f"normalized Zotero source snapshot failed integrity verification: {exc}",
            data={"run_id": loaded.run.run_id},
        ) from exc
    try:
        snapshot = json.loads(snapshot_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceBundleError(
            "source_snapshot_tampered",
            "normalized Zotero source snapshot is not valid UTF-8 JSON",
            data={"run_id": loaded.run.run_id},
        ) from exc
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("format") != "paper_reader.zotero-source.v2-internal"
        or not isinstance(snapshot.get("selected_item"), dict)
        or not isinstance(snapshot.get("selected_attachment"), dict)
    ):
        raise EvidenceBundleError(
            "source_snapshot_tampered",
            "normalized Zotero source snapshot has an invalid structure",
            data={"run_id": loaded.run.run_id},
        )
    item = snapshot["selected_item"]
    selected_attachment = snapshot["selected_attachment"]
    if (
        str(item.get("key", "")) != source.item_key
        or str(item.get("title", "")) != source.title
        or str(item.get("DOI", "")) != source.doi
        or item.get("version") != source.parent_version
        or str(selected_attachment.get("key", "")) != source.attachment_key
        or str(selected_attachment.get("path", "")) != source.attachment.resolved_path
    ):
        raise EvidenceBundleError(
            "source_snapshot_tampered",
            "normalized Zotero source snapshot does not match run source identity",
            data={"run_id": loaded.run.run_id},
        )
    metadata = build_metadata(item)
    if (
        metadata.get("pdf_attachment_key") != source.attachment_key
        or metadata.get("pdf_path") != source.attachment.resolved_path
    ):
        raise EvidenceBundleError(
            "source_snapshot_tampered",
            "normalized Zotero metadata selects a different primary PDF",
            data={"run_id": loaded.run.run_id},
        )
    return _PreparedSource(
        pdf=pdf,
        metadata=metadata,
        secondary_sources=build_secondary_sources(item),
        allow_network_figure_source=True,
    )


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


def _display_pdf_path(
    value: str,
    *,
    verified_path: Path,
    source_path: Path,
) -> str:
    display = str(source_path)
    aliases = {str(verified_path)}
    if verified_path.name.isdigit():
        aliases.update(
            {
                f"/dev/fd/{verified_path.name}",
                f"/proc/self/fd/{verified_path.name}",
            }
        )
    rendered = value
    for alias in sorted(aliases, key=len, reverse=True):
        rendered = rendered.replace(alias, display)
    return rendered


def _display_evidence_error(
    exc: EvidenceBundleError,
    *,
    verified_path: Path,
    source_path: Path,
) -> EvidenceBundleError:
    data = {
        key: (
            _display_pdf_path(value, verified_path=verified_path, source_path=source_path)
            if isinstance(value, str)
            else value
        )
        for key, value in exc.data.items()
    }
    data["source_pdf"] = str(source_path)
    return EvidenceBundleError(
        exc.code,
        _display_pdf_path(str(exc), verified_path=verified_path, source_path=source_path),
        data=data,
    )


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
    source = _pdf_identity(loaded)
    with _verified_pdf_snapshot(source, run_id=loaded.run.run_id) as verified_pdf_path:
        prepared_source = _verify_source(loaded, pdf=source)
        return _prepare_verified_evidence_locked(
            loaded,
            prepared_source=prepared_source,
            verified_pdf_path=verified_pdf_path,
            preview_pages=preview_pages,
            figure_limit=figure_limit,
        )


def _prepare_verified_evidence_locked(
    loaded: LoadedRun,
    *,
    prepared_source: _PreparedSource,
    verified_pdf_path: Path,
    preview_pages: int | None,
    figure_limit: int | None,
) -> PreparedEvidence:
    run_dir = _run_root(loaded)
    source = prepared_source.pdf
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
    try:
        page_count = _page_count(verified_pdf_path)
    except EvidenceBundleError as exc:
        raise _display_evidence_error(
            exc,
            verified_path=verified_pdf_path,
            source_path=source_path,
        ) from exc
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
            _verified_pdf_path=verified_pdf_path,
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
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceBundleError(
            "invalid_local_pdf",
            "local PDF text extraction failed: "
            + _display_pdf_path(
                str(exc),
                verified_path=verified_pdf_path,
                source_path=source_path,
            ),
            data={
                "run_id": loaded.run.run_id,
                "source_pdf": str(source_path),
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

    metadata = prepared_source.metadata
    complete = preview_pages is None
    evidence_id = new_random_id("evidence")
    staging = run_dir / f".{evidence_id}.{new_uuid()}.staging"
    run_anchor = loaded.run_directory_anchor
    if run_anchor is None:
        raise EvidenceBundleError(
            "run_directory_changed",
            "evidence preparation requires a locked run directory anchor",
        )
    staging_anchor = create_anchored_directory(run_anchor, staging)
    try:
        files_to_write = {
            "metadata.json": canonical_json_bytes(metadata),
            "extract.json": canonical_json_bytes(extraction),
            "context.md": build_context_markdown(metadata, extraction).encode("utf-8"),
            "section_context.md": build_section_context_markdown(metadata, extraction).encode("utf-8"),
            "secondary_sources.json": canonical_json_bytes(prepared_source.secondary_sources),
        }
        for name, content in files_to_write.items():
            atomic_write_bytes(staging / name, content, anchor=staging_anchor)

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
                verified_source_path=verified_pdf_path,
                staging=staging,
                staging_anchor=staging_anchor,
                future_dir=future_dir,
                run_dir=run_dir,
                figure_limit=resolved_figure_limit,
                preview_pages=preview_pages,
                complete=complete,
                allow_network_source=prepared_source.allow_network_figure_source,
            )
        except IncompleteFigureEvidenceError as exc:
            raise EvidenceBundleError(
                "figure_extraction_failed",
                f"figure extraction failed for incomplete preview evidence: {exc}",
                data={"run_id": loaded.run.run_id},
            ) from exc
        artifact_specs.extend(prepared_figures.artifacts)
        validate_directory_anchor(staging_anchor)
        files = tuple(
            _artifact_ref(run_dir, staged_path, role, media_type).model_copy(
                update={"path": future_path.relative_to(run_dir).as_posix()}
            )
            for staged_path, future_path, role, media_type in artifact_specs
        )
        validate_directory_anchor(staging_anchor)
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
            atomic_write_bytes(
                manifest_path,
                canonical_json_bytes(manifest),
                anchor=staging_anchor,
            )
            manifest_ref = _artifact_ref(
                run_dir,
                manifest_path,
                "evidence_manifest",
                "application/json",
            ).model_copy(
                update={"path": (future_dir / "evidence.json").relative_to(run_dir).as_posix()}
            )
            updated_run = _updated_run(loaded, manifest_ref)
            validate_directory_anchor(staging_anchor)
            next_size = projected_run_size(
                run_dir,
                staging_dir=staging,
                replacements={
                    loaded.manifest_path: canonical_json_bytes(updated_run),
                },
            )
            validate_directory_anchor(staging_anchor)
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
            validate_directory_anchor(staging_anchor)
            validate_evidence_manifest_membership(
                manifest,
                run_dir=run_dir,
                bundle_dir=staging,
                manifest_bundle_dir=future_dir,
                anchor=staging_anchor,
            )
            validate_directory_anchor(staging_anchor)
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
        bundle_prefix = PurePosixPath(destination.relative_to(run_dir).as_posix())
        staging_snapshot = tree_snapshot_from_hashes(
            {
                PurePosixPath(ref.path).relative_to(bundle_prefix).as_posix(): (
                    ref.size_bytes,
                    ref.sha256,
                )
                for ref in (*files, manifest_ref)
            }
        )
        try:
            atomic_publish_tree(
                staging,
                destination,
                anchor=loaded.run_directory_anchor,
                expected_staging_anchor=staging_anchor,
                expected_tree_snapshot=staging_snapshot,
            )
        except Exception as exc:
            raise EvidenceBundleError(
                "evidence_publication_failed",
                f"immutable evidence publication failed: {destination}: {exc}",
                data={"run_id": loaded.run.run_id, "evidence_dir": str(destination)},
            ) from exc
        published_manifest = destination / "evidence.json"
        try:
            atomic_write_json(
                loaded.manifest_path,
                updated_run,
                anchor=loaded.run_directory_anchor,
            )
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
            evidence_digest=manifest_ref.sha256,
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
    "EvidenceBundleError",
    "EvidenceManifest",
    "PreparedEvidence",
    "prepare_local_evidence",
]
