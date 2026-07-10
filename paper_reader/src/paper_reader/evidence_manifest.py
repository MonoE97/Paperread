from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from paper_reader.contracts import ArtifactRef
from paper_reader.evidence import parse_trusted_locator
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.storage import canonical_json_bytes, resolve_artifact_path, rfc3339_utc
from paper_reader.v2_loader import LoadedRun


class StrictEvidenceModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class EvidenceResourceCheck(StrictEvidenceModel):
    name: str
    status: Literal["passed", "degraded", "blocked"]
    actual: int | float | str | bool | None
    limit: int | float | str | None
    message: str | None = None


class EvidenceSectionMember(StrictEvidenceModel):
    title: str
    start_page: int
    end_page: int


class EvidenceTableCandidateMember(StrictEvidenceModel):
    index: int
    page: int
    section: str


class EvidenceFigureMember(StrictEvidenceModel):
    figure_id: str
    page: int
    artifact_path: str


class EvidenceManifest(StrictEvidenceModel):
    format: Literal["paper_reader.evidence.v2-internal"]
    evidence_id: str
    run_id: str
    created_at: str
    source_sha256: str
    complete: bool
    degraded: bool
    preview_pages: int | None
    files: tuple[ArtifactRef, ...]
    pages: tuple[int, ...]
    sections: tuple[EvidenceSectionMember, ...]
    table_candidates: tuple[EvidenceTableCandidateMember, ...]
    figures: tuple[EvidenceFigureMember, ...]
    resource_checks: tuple[EvidenceResourceCheck, ...]


@dataclass(frozen=True, slots=True)
class BoundEvidence:
    manifest: EvidenceManifest
    manifest_ref: ArtifactRef
    manifest_path: Path
    manifest_bytes: bytes
    digest: str
    artifacts_by_role: dict[str, tuple[Path, ...]]


class EvidenceManifestError(ValueError):
    def __init__(self, code: str, message: str, *, artifact_path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.artifact_path = artifact_path


def locator_membership_error(locator: str, manifest: EvidenceManifest) -> str | None:
    parsed = parse_trusted_locator(locator)
    if parsed is None:
        return "noncanonical_locator"
    if parsed.source == "figure_context":
        if not any(item.figure_id == parsed.figure_id for item in manifest.figures):
            return "figure_not_in_evidence"
        return None

    if parsed.page not in manifest.pages:
        return "page_not_in_evidence"
    if parsed.section is None:
        return None
    if not any(
        item.title == parsed.section
        and parsed.page is not None
        and item.start_page <= parsed.page <= item.end_page
        for item in manifest.sections
    ):
        return "section_not_in_evidence"
    if parsed.table_candidate is None:
        return None
    if not any(
        item.index == parsed.table_candidate
        and item.page == parsed.page
        and item.section == parsed.section
        for item in manifest.table_candidates
    ):
        return "table_candidate_not_in_evidence"
    return None


def _reject_symlink_components(run_dir: Path, relative_path: str) -> None:
    current = run_dir
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise EvidenceManifestError(
                "evidence_symlink_forbidden",
                f"immutable evidence artifact must not use symlinks: {relative_path}",
                artifact_path=relative_path,
            )


def _verify_artifact(run_dir: Path, artifact: ArtifactRef) -> tuple[Path, bytes]:
    _reject_symlink_components(run_dir, artifact.path)
    try:
        path = resolve_artifact_path(run_dir, artifact.path)
        raw = path.read_bytes()
        mode = path.stat().st_mode
    except (OSError, ValueError) as exc:
        raise EvidenceManifestError(
            "evidence_artifact_unreadable",
            f"evidence artifact is unreadable: {artifact.path}: {exc}",
            artifact_path=artifact.path,
        ) from exc
    if not stat.S_ISREG(mode):
        raise EvidenceManifestError(
            "evidence_artifact_unreadable",
            f"evidence artifact is not a regular file: {artifact.path}",
            artifact_path=artifact.path,
        )
    if len(raw) != artifact.size_bytes or hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise EvidenceManifestError(
            "evidence_artifact_hash_mismatch",
            f"evidence artifact hash or size mismatch: {artifact.path}",
            artifact_path=artifact.path,
        )
    return path, raw


def _validate_manifest_membership(manifest: EvidenceManifest) -> None:
    pages = set(manifest.pages)
    if len(pages) != len(manifest.pages) or any(page <= 0 for page in pages):
        raise EvidenceManifestError("invalid_evidence_membership", "evidence pages must be unique positive integers")
    section_keys: set[tuple[str, int, int]] = set()
    for section in manifest.sections:
        key = (section.title, section.start_page, section.end_page)
        if key in section_keys or section.start_page > section.end_page:
            raise EvidenceManifestError("invalid_evidence_membership", "evidence section membership is invalid")
        if section.start_page not in pages or section.end_page not in pages:
            raise EvidenceManifestError("invalid_evidence_membership", "evidence section pages are not members")
        section_keys.add(key)
    table_indices: set[int] = set()
    for item in manifest.table_candidates:
        if item.index <= 0 or item.index in table_indices or item.page not in pages:
            raise EvidenceManifestError("invalid_evidence_membership", "table candidate membership is invalid")
        if not any(
            section.title == item.section
            and section.start_page <= item.page <= section.end_page
            for section in manifest.sections
        ):
            raise EvidenceManifestError("invalid_evidence_membership", "table candidate section is not a member")
        table_indices.add(item.index)
    figure_ids: set[str] = set()
    image_paths = {artifact.path for artifact in manifest.files if artifact.role == "figure_image"}
    for item in manifest.figures:
        if item.figure_id in figure_ids or item.page not in pages or item.artifact_path not in image_paths:
            raise EvidenceManifestError("invalid_evidence_membership", "figure membership is invalid")
        figure_ids.add(item.figure_id)


def load_bound_evidence(loaded: LoadedRun, evidence_digest: str) -> BoundEvidence:
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    matching_refs = [
        artifact
        for artifact in loaded.run.artifacts
        if artifact.role == "evidence_manifest" and artifact.sha256 == evidence_digest
    ]
    if len(matching_refs) != 1:
        raise EvidenceManifestError(
            "evidence_not_bound",
            f"run must bind exactly one evidence manifest with digest {evidence_digest}",
        )
    manifest_ref = matching_refs[0]
    manifest_path, manifest_bytes = _verify_artifact(run_dir, manifest_ref)
    try:
        manifest = EvidenceManifest.model_validate_json(manifest_bytes)
    except Exception as exc:
        raise EvidenceManifestError(
            "invalid_evidence_manifest",
            f"strict evidence manifest validation failed: {manifest_ref.path}",
            artifact_path=manifest_ref.path,
        ) from exc
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise EvidenceManifestError(
            "noncanonical_evidence_manifest",
            f"evidence manifest is not canonical JSON: {manifest_ref.path}",
            artifact_path=manifest_ref.path,
        )
    source = loaded.run.source
    if manifest.run_id != loaded.run.run_id or manifest.source_sha256 != source.sha256:
        raise EvidenceManifestError("evidence_binding_mismatch", "evidence manifest run/source binding mismatch")
    if hashlib.sha256(manifest_bytes).hexdigest() != evidence_digest:
        raise EvidenceManifestError("evidence_binding_mismatch", "evidence digest mismatch")

    required_roles = {"metadata", "extract", "context", "section_context", "secondary_sources"}
    roles = [artifact.role for artifact in manifest.files]
    for role in required_roles:
        if roles.count(role) != 1:
            raise EvidenceManifestError(
                "invalid_evidence_manifest",
                f"evidence manifest must bind exactly one {role} artifact",
            )
    manifest_dir = manifest_path.parent
    artifacts_by_role: dict[str, list[Path]] = {}
    for artifact in manifest.files:
        path, _raw = _verify_artifact(run_dir, artifact)
        if not path.is_relative_to(manifest_dir):
            raise EvidenceManifestError(
                "evidence_artifact_outside_bundle",
                f"evidence member is outside its immutable bundle: {artifact.path}",
                artifact_path=artifact.path,
            )
        artifacts_by_role.setdefault(artifact.role, []).append(path)
    _validate_manifest_membership(manifest)
    return BoundEvidence(
        manifest=manifest,
        manifest_ref=manifest_ref,
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        digest=evidence_digest,
        artifacts_by_role={role: tuple(paths) for role, paths in artifacts_by_role.items()},
    )


def build_evidence_manifest(
    *,
    evidence_id: str,
    run_id: str,
    source_sha256: str,
    complete: bool,
    preview_pages: int | None,
    files: tuple[ArtifactRef, ...],
    extraction: dict,
    figure_limit: int,
    run_size_bytes: int,
    figures: tuple[EvidenceFigureMember, ...],
    degraded: bool,
    figure_check: EvidenceResourceCheck,
    figure_resource_checks: tuple[EvidenceResourceCheck, ...],
) -> EvidenceManifest:
    pages = tuple(int(item["page"]) for item in extraction.get("pages", []) if isinstance(item, dict))
    sections = tuple(
        EvidenceSectionMember(
            title=str(item.get("title", "")),
            start_page=int(item.get("start_page", 0)),
            end_page=int(item.get("end_page", 0)),
        )
        for item in extraction.get("sections", [])
        if isinstance(item, dict)
    )
    table_candidates = tuple(
        EvidenceTableCandidateMember(
            index=index,
            page=int(item.get("page", 0)),
            section=str(item.get("section", "")),
        )
        for index, item in enumerate(extraction.get("table_candidates", []), start=1)
        if isinstance(item, dict)
    )
    checks = (
        EvidenceResourceCheck(
            name="pdf_page_count",
            status="passed",
            actual=int(extraction.get("page_count", 0)),
            limit=V2_RESOURCE_POLICY.pdf_max_pages,
        ),
        EvidenceResourceCheck(
            name="extracted_text_chars",
            status="passed",
            actual=len(str(extraction.get("text", ""))),
            limit=V2_RESOURCE_POLICY.extracted_text_max_chars,
        ),
        EvidenceResourceCheck(
            name="figure_limit",
            status="passed",
            actual=figure_limit,
            limit=V2_RESOURCE_POLICY.figure_hard_limit,
        ),
        figure_check,
        *figure_resource_checks,
        EvidenceResourceCheck(
            name="run_size_bytes",
            status="passed",
            actual=run_size_bytes,
            limit=V2_RESOURCE_POLICY.run_max_bytes,
        ),
    )
    return EvidenceManifest(
        format="paper_reader.evidence.v2-internal",
        evidence_id=evidence_id,
        run_id=run_id,
        created_at=rfc3339_utc(),
        source_sha256=source_sha256,
        complete=complete,
        degraded=degraded,
        preview_pages=preview_pages,
        files=files,
        pages=pages,
        sections=sections,
        table_candidates=table_candidates,
        figures=figures,
        resource_checks=checks,
    )


__all__ = [
    "BoundEvidence",
    "EvidenceFigureMember",
    "EvidenceManifest",
    "EvidenceManifestError",
    "EvidenceResourceCheck",
    "EvidenceSectionMember",
    "EvidenceTableCandidateMember",
    "build_evidence_manifest",
    "locator_membership_error",
    "load_bound_evidence",
]
