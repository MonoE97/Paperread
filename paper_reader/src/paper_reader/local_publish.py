from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass, replace
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
    PaperReaderReviewPackage,
    PaperReaderRun,
    ZoteroPublicationTarget,
    ZoteroSourceIdentity,
)
from paper_reader.evidence_manifest import (
    BoundEvidence,
    EvidenceManifestError,
    load_bound_evidence,
)
from paper_reader.note_hash import canonicalize_note_html_for_hash, note_html_sha256
from paper_reader.raw_schema import require_raw_schema_version
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.run_lock import ExpectedRunArtifact, locked_v2_run
from paper_reader.run_size import RunSizeLimitError, enforce_projected_run_size
from paper_reader.secondary_evidence import (
    SecondaryEvidenceError,
    open_bound_source_closure_guard,
)
from paper_reader.storage import (
    HeldResolvedSourceGuard,
    ImmutableTreeSnapshot,
    OwnedDirectoryAnchor,
    OwnedPublishedFile,
    PublishConflictError,
    TreeSnapshotLimitError,
    UnsafeStoragePathError,
    anchored_entry_exists,
    atomic_write_json,
    cas_update_run,
    canonical_json_bytes,
    open_anchored_regular_file,
    open_anchored_directory,
    open_resolved_source_guard,
    publish_bytes_no_replace,
    read_anchored_bytes,
    safe_relative_artifact_path,
    snapshot_anchored_tree,
    snapshot_directory_fd,
    stat_anchored_entry,
    tree_snapshot_from_hashes,
    validate_directory_anchor,
)
from paper_reader.v2_loader import (
    DirectoryAnchor,
    LoadedRun,
    RunLoadError,
    _load_v2_run_from_anchor,
    load_v2_run,
)


_BASE_CANDIDATE_MEMBER_BY_ROLE = {
    "run_snapshot": "run.json",
    "source_snapshot": "source.json",
    "evidence_manifest_snapshot": "evidence.json",
    "summary_snapshot": "summary.json",
    "review_snapshot": "review.json",
    "review_package_snapshot": "review-package.json",
    "review_validation": "validation.json",
    "note_markdown": "note.md",
    "note_html": "note.html",
}
_ZOTERO_CANDIDATE_MEMBER_BY_ROLE = {
    "raw_discovery_bundle_snapshot": "discovery.raw.json",
    "zotero_parent_snapshot": "parent.json",
    "zotero_children_snapshot": "children.json",
}


@dataclass(frozen=True, slots=True)
class PublishedLocalCandidate:
    run_dir: Path
    candidate_path: Path
    target_path: Path
    receipt_path: Path
    candidate_digest: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class LocalCandidatePublicationPreflight:
    run_dir: Path
    run_device: int
    run_inode: int
    run_manifest_sha256: str
    candidate_path: Path
    candidate_bytes: bytes
    candidate_digest: str


@dataclass(slots=True)
class _CandidateTreeGuard:
    anchor: OwnedDirectoryAnchor
    expected: ImmutableTreeSnapshot
    failure_code: str = "candidate_tampered"
    label: str = "candidate tree"

    def close(self) -> None:
        self.anchor.close()

    def __enter__(self) -> _CandidateTreeGuard:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def verify(self) -> None:
        try:
            validate_directory_anchor(self.anchor)
            observed = snapshot_directory_fd(
                self.anchor.descriptor,
                max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
                max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
            )
            validate_directory_anchor(self.anchor)
        except (OSError, ValueError) as exc:
            raise LocalPublicationError(
                self.failure_code,
                f"{self.label} identity became uncertain: {exc}",
            ) from exc
        if observed != self.expected:
            raise LocalPublicationError(
                self.failure_code,
                f"{self.label} changed during local publication",
            )


def _open_original_evidence_tree_guard(
    loaded: LoadedRun,
    evidence: BoundEvidence,
) -> _CandidateTreeGuard:
    run_anchor = loaded.run_directory_anchor
    if run_anchor is None:
        raise LocalPublicationError(
            "run_directory_changed",
            "original evidence guard requires a locked run directory anchor",
        )
    bundle_dir = evidence.manifest_path.parent
    members = {
        evidence.manifest_path.relative_to(bundle_dir).as_posix(): (
            len(evidence.manifest_bytes),
            hashlib.sha256(evidence.manifest_bytes).hexdigest(),
        )
    }
    for artifacts in evidence.artifacts_by_role.values():
        for artifact in artifacts:
            relative = artifact.path.relative_to(bundle_dir).as_posix()
            if relative in members:
                raise LocalPublicationError(
                    "evidence_artifact_hash_mismatch",
                    "original evidence bundle contains duplicate member paths",
                )
            members[relative] = (
                len(artifact.raw_bytes),
                hashlib.sha256(artifact.raw_bytes).hexdigest(),
            )
    try:
        expected = tree_snapshot_from_hashes(members)
        anchor = open_anchored_directory(run_anchor, bundle_dir)
    except (OSError, ValueError) as exc:
        raise LocalPublicationError(
            "evidence_artifact_hash_mismatch",
            f"original evidence bundle cannot be held safely: {exc}",
        ) from exc
    guard = _CandidateTreeGuard(
        anchor=anchor,
        expected=expected,
        failure_code="evidence_artifact_hash_mismatch",
        label="original evidence bundle",
    )
    try:
        guard.verify()
    except BaseException:
        guard.close()
        raise
    return guard


def _verify_held_source(guard: HeldResolvedSourceGuard) -> None:
    try:
        guard.verify()
    except (OSError, ValueError) as exc:
        raise LocalPublicationError(
            "source_changed",
            f"local PDF source changed during publication: {exc}",
        ) from exc


@dataclass(slots=True)
class _CommittedFileGuard:
    path: Path
    anchor: DirectoryAnchor
    descriptor: int
    identity: tuple[int, int, int, int, int, int]
    expected: bytes
    expected_sha256: str
    failure_code: str
    anchor_failure_code: str
    label: str

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> _CommittedFileGuard:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def verify(self) -> None:
        try:
            validate_directory_anchor(self.anchor)
        except UnsafeStoragePathError as exc:
            raise LocalPublicationError(
                self.anchor_failure_code,
                f"{self.label} anchor identity became uncertain: {self.path}: {exc}",
                data={"artifact_path": str(self.path)},
            ) from exc
        try:
            opened_before = os.fstat(self.descriptor)
            named_before = stat_anchored_entry(self.anchor, self.path)
            chunks: list[bytes] = []
            offset = 0
            limit = len(self.expected)
            while offset <= limit:
                chunk = os.pread(
                    self.descriptor,
                    min(1024 * 1024, limit - offset + 1),
                    offset,
                )
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
                if offset > limit:
                    break
            opened_after = os.fstat(self.descriptor)
            named_after = stat_anchored_entry(self.anchor, self.path)
        except (OSError, UnsafeStoragePathError) as exc:
            try:
                validate_directory_anchor(self.anchor)
            except UnsafeStoragePathError as anchor_exc:
                raise LocalPublicationError(
                    self.anchor_failure_code,
                    f"{self.label} anchor changed during finalization: "
                    f"{self.path}: {anchor_exc}",
                    data={"artifact_path": str(self.path)},
                ) from anchor_exc
            raise LocalPublicationError(
                self.failure_code,
                f"committed {self.label} identity became uncertain: {self.path}: {exc}",
                data={"artifact_path": str(self.path)},
            ) from exc
        try:
            validate_directory_anchor(self.anchor)
        except UnsafeStoragePathError as exc:
            raise LocalPublicationError(
                self.anchor_failure_code,
                f"{self.label} anchor changed during finalization: {self.path}: {exc}",
                data={"artifact_path": str(self.path)},
            ) from exc
        identities = {
            (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                metadata.st_nlink,
            )
            for metadata in (opened_before, named_before, opened_after, named_after)
        }
        raw = b"".join(chunks)
        if (
            identities != {self.identity}
            or not all(
                stat.S_ISREG(metadata.st_mode)
                for metadata in (opened_before, named_before, opened_after, named_after)
            )
            or self.identity[5] != 1
            or raw != self.expected
            or hashlib.sha256(raw).hexdigest() != self.expected_sha256
        ):
            raise LocalPublicationError(
                self.failure_code,
                f"committed {self.label} changed before publication finalized: {self.path}",
                data={"artifact_path": str(self.path)},
            )


def _preflight_review_package_snapshot_schema(
    run_dir: Path,
    candidate_dir: Path,
    candidate: PaperReaderCandidate,
    *,
    anchor: DirectoryAnchor,
) -> tuple[ArtifactRef, Path, bytes]:
    package_refs = tuple(
        artifact
        for artifact in candidate.artifacts
        if artifact.role == "review_package_snapshot"
    )
    if len(package_refs) != 1 or candidate.sealed_review != package_refs[0]:
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate must bind exactly one sealed review package snapshot",
        )
    package_ref = package_refs[0]
    try:
        package_relative = safe_relative_artifact_path(package_ref.path)
        package_path = run_dir / package_relative
        if not package_path.is_relative_to(candidate_dir):
            raise LocalPublicationError(
                "candidate_tampered",
                "candidate review package snapshot escapes its immutable tree",
            )
        package_bytes = read_anchored_bytes(
            anchor,
            package_path,
            max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
        )
    except LocalPublicationError:
        raise
    except (OSError, ValueError) as exc:
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate review package snapshot cannot be inspected safely",
        ) from exc
    require_raw_schema_version(
        package_bytes,
        expected="paper_reader.review-package.v2",
        artifact_path=package_path,
    )
    return package_ref, package_path, package_bytes


def revalidate_candidate_original_evidence(
    loaded: LoadedRun,
    candidate: PaperReaderCandidate,
    verified: dict[str, tuple[tuple[Path, bytes], ...]],
) -> BoundEvidence:
    review_package_path, review_package_bytes = verified["review_package_snapshot"][0]
    require_raw_schema_version(
        review_package_bytes,
        expected="paper_reader.review-package.v2",
        artifact_path=review_package_path,
    )
    try:
        review_package = PaperReaderReviewPackage.model_validate_json(
            review_package_bytes
        )
    except ValidationError as exc:
        raise LocalPublicationError(
            "candidate_tampered",
            f"strict review package snapshot validation failed: {exc}",
        ) from exc
    _evidence_path, evidence_bytes = verified["evidence_manifest_snapshot"][0]
    if (
        canonical_json_bytes(review_package) != review_package_bytes
        or review_package.run_id != candidate.run_id
        or hashlib.sha256(evidence_bytes).hexdigest()
        != review_package.evidence_digest
    ):
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate review package/evidence snapshot binding mismatch",
        )
    try:
        return load_bound_evidence(loaded, review_package.evidence_digest)
    except EvidenceManifestError as exc:
        raise LocalPublicationError(exc.code, str(exc)) from exc


def _validate_candidate_run_snapshot_plan_binding(
    loaded: LoadedRun,
    candidate: PaperReaderCandidate,
    verified: dict[str, list[tuple[Path, bytes]]],
) -> None:
    snapshot_path, snapshot_bytes = verified["run_snapshot"][0]
    require_raw_schema_version(
        snapshot_bytes,
        expected="paper_reader.run.v2",
        artifact_path=snapshot_path,
    )
    try:
        snapshot = PaperReaderRun.model_validate_json(snapshot_bytes)
    except ValidationError as exc:
        raise LocalPublicationError(
            "candidate_tampered",
            f"strict candidate run snapshot validation failed: {exc}",
        ) from exc
    if (
        canonical_json_bytes(snapshot) != snapshot_bytes
        or snapshot.run_id != candidate.run_id
        or snapshot.source != candidate.source
        or candidate.source != loaded.run.source
    ):
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate run snapshot identity differs from the current run",
        )
    snapshot_plan_refs = tuple(
        artifact
        for artifact in snapshot.artifacts
        if artifact.role == "secondary_source_plan"
    )
    current_plan_refs = tuple(
        artifact
        for artifact in loaded.run.artifacts
        if artifact.role == "secondary_source_plan"
    )
    if snapshot_plan_refs != current_plan_refs:
        raise LocalPublicationError(
            "secondary_plan_mismatch",
            "candidate run snapshot does not retain the current secondary source plan ref",
        )
    source = loaded.run.source
    if isinstance(source, LocalSourceIdentity):
        expected_source_roles = (("source_snapshot", None),)
    else:
        expected_source_roles = (
            ("raw_discovery_bundle", source.raw_discovery_bundle),
            ("normalized_source", source.normalized_source),
        )
    for role, embedded_ref in expected_source_roles:
        current_refs = tuple(
            artifact for artifact in loaded.run.artifacts if artifact.role == role
        )
        snapshot_refs = tuple(
            artifact for artifact in snapshot.artifacts if artifact.role == role
        )
        if (
            len(current_refs) != 1
            or snapshot_refs != current_refs
            or (embedded_ref is not None and current_refs != (embedded_ref,))
        ):
            raise LocalPublicationError(
                "secondary_plan_mismatch",
                "candidate run snapshot does not retain an exact singleton source closure",
            )


def _load_candidate(
    candidate_input: Path,
    *,
    loaded_run: LoadedRun | None = None,
    require_local: bool = True,
) -> tuple[LoadedRun, Path, PaperReaderCandidate, str, dict[str, tuple[tuple[Path, bytes], ...]]]:
    requested = candidate_manifest_path(candidate_input)
    if requested.is_symlink() or requested.parent.is_symlink():
        raise LocalPublicationError("candidate_tampered", "candidate path must not use symlinks")
    candidate_path = Path(os.path.abspath(requested))
    candidate_dir = candidate_path.parent
    if candidate_path.name != "candidate.json" or candidate_dir.parent.name != "candidates":
        raise LocalPublicationError("candidate_tampered", "candidate left its run candidates directory")
    run_dir = candidate_dir.parent.parent
    loaded = loaded_run or load_v2_run(run_dir)
    if loaded.manifest_path.parent != run_dir:
        raise LocalPublicationError("candidate_tampered", "candidate run directory binding mismatch")
    anchor = loaded.run_directory_anchor
    if anchor is None:
        with DirectoryAnchor.open(run_dir, manifest_path=loaded.manifest_path) as opened_anchor:
            if (opened_anchor.device, opened_anchor.inode) != (
                loaded.run_directory_device,
                loaded.run_directory_inode,
            ):
                raise LocalPublicationError(
                    "candidate_tampered",
                    "candidate run directory changed before it was validated",
                )
            anchored = replace(loaded, run_directory_anchor=opened_anchor)
            result = _load_candidate(
                candidate_path,
                loaded_run=anchored,
                require_local=require_local,
            )
        return loaded, *result[1:]

    try:
        before_snapshot = snapshot_anchored_tree(
            anchor,
            candidate_dir,
            max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
            max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
            max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
        )
        raw = read_anchored_bytes(
            anchor,
            candidate_path,
            max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
        )
    except TreeSnapshotLimitError as exc:
        code = (
            "run_size_limit_exceeded"
            if exc.limit_name == "max_total_bytes"
            else "candidate_tampered"
        )
        raise LocalPublicationError(
            code,
            str(exc),
            data={"max_bytes": exc.maximum},
        ) from exc
    except (OSError, ValueError) as exc:
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate tree cannot be read as an anchored immutable tree",
        ) from exc
    require_raw_schema_version(
        raw,
        expected="paper_reader.candidate.v2",
        artifact_path=candidate_path,
    )
    try:
        candidate = PaperReaderCandidate.model_validate_json(raw)
    except ValidationError as exc:
        raise LocalPublicationError("candidate_tampered", f"strict candidate validation failed: {exc}") from exc
    if canonical_json_bytes(candidate) != raw:
        raise LocalPublicationError("candidate_tampered", "candidate.json is not canonical JSON")
    if candidate_dir != run_dir / "candidates" / candidate.candidate_id:
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate directory must be candidates/<candidate_id>",
        )
    _preflight_review_package_snapshot_schema(
        run_dir,
        candidate_dir,
        candidate,
        anchor=anchor,
    )
    digest = candidate_core_digest(candidate)
    relative = candidate_path.relative_to(run_dir).as_posix()
    refs = [item for item in loaded.run.artifacts if item.role == "candidate" and item.path == relative]
    if len(refs) != 1:
        raise LocalPublicationError("candidate_not_bound", "run does not bind this candidate")
    if refs[0].sha256 != digest or refs[0].size_bytes != len(raw) or hashlib.sha256(raw).hexdigest() != digest:
        raise LocalPublicationError("candidate_tampered", "candidate core digest or size mismatch")
    if candidate.run_id != loaded.run.run_id:
        raise LocalPublicationError("candidate_tampered", "candidate run_id mismatch")
    if candidate.source != loaded.run.source:
        raise LocalPublicationError("candidate_tampered", "candidate source binding mismatch")
    if candidate.gate.status != "write_ready" or candidate.gate.blockers:
        raise LocalPublicationError("candidate_not_ready", "candidate gate is not write_ready")
    if require_local and (
        not isinstance(candidate.source, LocalSourceIdentity)
        or not isinstance(candidate.target, LocalPublicationTarget)
    ):
        raise LocalPublicationError("not_implemented", "local publish accepts only local candidates")

    verified: dict[str, list[tuple[Path, bytes]]] = {}
    expected_member_by_role = dict(_BASE_CANDIDATE_MEMBER_BY_ROLE)
    if isinstance(candidate.source, ZoteroSourceIdentity) and isinstance(
        candidate.target, ZoteroPublicationTarget
    ):
        expected_member_by_role.update(_ZOTERO_CANDIDATE_MEMBER_BY_ROLE)
    expected_members = {
        "candidate.json": (len(raw), hashlib.sha256(raw).hexdigest()),
    }
    for artifact in candidate.artifacts:
        try:
            path, content = verify_artifact_ref(
                run_dir,
                artifact,
                anchor=anchor,
            )
        except LocalPublicationError as exc:
            raise LocalPublicationError("candidate_tampered", str(exc)) from exc
        if not path.is_relative_to(candidate_dir):
            raise LocalPublicationError("candidate_tampered", "candidate artifact escapes its tree")
        relative_member = path.relative_to(candidate_dir).as_posix()
        expected_member = expected_member_by_role.get(artifact.role)
        if expected_member is None or relative_member != expected_member:
            raise LocalPublicationError(
                "candidate_tampered",
                "candidate artifact role does not use its fixed sidecar path",
            )
        if relative_member in expected_members:
            raise LocalPublicationError(
                "candidate_tampered",
                "candidate artifact paths must be unique and must not replace candidate.json",
            )
        expected_members[relative_member] = (artifact.size_bytes, artifact.sha256)
        verified.setdefault(artifact.role, []).append((path, content))
    if set(verified) != set(expected_member_by_role) or any(
        len(items) != 1 for items in verified.values()
    ):
        raise LocalPublicationError("candidate_tampered", "candidate artifact membership is incomplete")
    _validate_candidate_run_snapshot_plan_binding(loaded, candidate, verified)
    if candidate.evidence_manifest not in candidate.artifacts or candidate.sealed_review not in candidate.artifacts:
        raise LocalPublicationError("candidate_tampered", "candidate gate refs are not artifact members")
    revalidate_candidate_original_evidence(
        loaded,
        candidate,
        {role: tuple(items) for role, items in verified.items()},
    )
    if isinstance(candidate.target, LocalPublicationTarget):
        _note_path, note_bytes = verified["note_markdown"][0]
        if (
            hashlib.sha256(note_bytes).hexdigest() != candidate.content_sha256
            or len(note_bytes) != candidate.content_length
            or markdown_note_title(note_bytes) != candidate.note_title
        ):
            raise LocalPublicationError("candidate_tampered", "candidate Markdown binding mismatch")
    elif isinstance(candidate.target, ZoteroPublicationTarget):
        _html_path, html_bytes = verified["note_html"][0]
        try:
            html = html_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LocalPublicationError("candidate_tampered", "candidate HTML is not UTF-8") from exc
        if (
            note_html_sha256(html) != candidate.content_sha256
            or len(canonicalize_note_html_for_hash(html)) != candidate.content_length
            or candidate.live_preflight is None
            or candidate.live_preflight.parent_snapshot not in candidate.artifacts
            or candidate.live_preflight.children_snapshot not in candidate.artifacts
        ):
            raise LocalPublicationError("candidate_tampered", "candidate HTML/live binding mismatch")
    else:  # pragma: no cover - strict target discriminator makes this unreachable
        raise LocalPublicationError("candidate_tampered", "candidate target type is invalid")
    try:
        after_snapshot = snapshot_anchored_tree(
            anchor,
            candidate_dir,
            max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
            max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
            max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
        )
        expected_snapshot = tree_snapshot_from_hashes(expected_members)
    except TreeSnapshotLimitError as exc:
        code = (
            "run_size_limit_exceeded"
            if exc.limit_name == "max_total_bytes"
            else "candidate_tampered"
        )
        raise LocalPublicationError(
            code,
            str(exc),
            data={"max_bytes": exc.maximum},
        ) from exc
    except (OSError, ValueError) as exc:
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate tree cannot be revalidated as an anchored immutable tree",
        ) from exc
    if before_snapshot != after_snapshot:
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate tree changed while it was validated",
        )
    if after_snapshot != expected_snapshot:
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate tree contains files or directories not bound by candidate.json",
        )
    return loaded, candidate_path, candidate, digest, {
        role: tuple(items) for role, items in verified.items()
    }


def _preflight_candidate_review_package_snapshot(
    loaded: LoadedRun,
    *,
    anchor: DirectoryAnchor,
    candidate_path: Path,
    candidate_bytes: bytes,
) -> PaperReaderCandidate:
    try:
        candidate = PaperReaderCandidate.model_validate_json(candidate_bytes)
    except ValidationError as exc:
        raise LocalPublicationError(
            "candidate_tampered",
            f"strict candidate validation failed: {exc}",
        ) from exc
    if canonical_json_bytes(candidate) != candidate_bytes:
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate.json is not canonical JSON",
        )
    package_ref, preflight_package_path, preflight_package_bytes = (
        _preflight_review_package_snapshot_schema(
            loaded.manifest_path.parent,
            candidate_path.parent,
            candidate,
            anchor=anchor,
        )
    )
    package_path, package_bytes = verify_artifact_ref(
        loaded.manifest_path.parent,
        package_ref,
        anchor=anchor,
    )
    if (
        package_path != preflight_package_path
        or package_bytes != preflight_package_bytes
    ):
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate review package snapshot changed during schema preflight",
        )
    return candidate


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


def _read_stable_regular_file(
    path: Path,
    *,
    conflict_code: str,
    anchor: DirectoryAnchor | None = None,
    expected_size: int | None = None,
    max_bytes: int | None = None,
) -> bytes:
    if anchor is not None:
        try:
            return read_anchored_bytes(
                anchor,
                path,
                expected_size=expected_size,
                max_bytes=max_bytes,
            )
        except UnsafeStoragePathError as exc:
            try:
                validate_directory_anchor(anchor)
            except UnsafeStoragePathError as anchor_exc:
                raise LocalPublicationError(
                    "run_directory_changed",
                    f"anchored publication directory changed: {path}: {anchor_exc}",
                ) from anchor_exc
            raise LocalPublicationError(
                conflict_code,
                f"publication path is unsafe: {path}: {exc}",
            ) from exc
        except OSError as exc:
            raise LocalPublicationError(
                conflict_code,
                f"publication path changed while it was verified: {path}: {exc}",
            ) from exc
    try:
        before_path = os.lstat(path)
    except OSError as exc:
        raise LocalPublicationError(
            conflict_code,
            f"publication state is unreadable: {path}: {exc}",
        ) from exc
    if (
        stat.S_ISLNK(before_path.st_mode)
        or not stat.S_ISREG(before_path.st_mode)
        or before_path.st_nlink != 1
    ):
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
        (
            before_path.st_dev,
            before_path.st_ino,
            before_path.st_size,
            before_path.st_mtime_ns,
            before_path.st_ctime_ns,
            before_path.st_nlink,
        ),
        (
            before_fd.st_dev,
            before_fd.st_ino,
            before_fd.st_size,
            before_fd.st_mtime_ns,
            before_fd.st_ctime_ns,
            before_fd.st_nlink,
        ),
        (
            after_fd.st_dev,
            after_fd.st_ino,
            after_fd.st_size,
            after_fd.st_mtime_ns,
            after_fd.st_ctime_ns,
            after_fd.st_nlink,
        ),
        (
            after_path.st_dev,
            after_path.st_ino,
            after_path.st_size,
            after_path.st_mtime_ns,
            after_path.st_ctime_ns,
            after_path.st_nlink,
        ),
    }
    if (
        len(identities) != 1
        or not stat.S_ISREG(after_path.st_mode)
        or after_path.st_nlink != 1
    ):
        raise LocalPublicationError(
            conflict_code,
            f"publication path changed while it was verified: {path}",
        )
    return raw


def _guard_committed_file(
    path: Path,
    expected: bytes,
    expected_sha256: str,
    *,
    anchor: DirectoryAnchor,
    failure_code: str,
    anchor_failure_code: str,
    label: str,
    missing_ok: bool = False,
) -> _CommittedFileGuard | None:
    try:
        owned = open_anchored_regular_file(
            anchor,
            path,
            expected_size=len(expected),
        )
    except FileNotFoundError as exc:
        if missing_ok:
            try:
                validate_directory_anchor(anchor)
            except UnsafeStoragePathError as anchor_exc:
                raise LocalPublicationError(
                    anchor_failure_code,
                    f"committed {label} anchor changed: {path}: {anchor_exc}",
                    data={"artifact_path": str(path)},
                ) from anchor_exc
            return None
        raise LocalPublicationError(
            failure_code,
            f"committed {label} disappeared before finalization: {path}",
            data={"artifact_path": str(path)},
        ) from exc
    except (OSError, UnsafeStoragePathError) as exc:
        try:
            validate_directory_anchor(anchor)
        except UnsafeStoragePathError as anchor_exc:
            raise LocalPublicationError(
                anchor_failure_code,
                f"committed {label} anchor changed: {path}: {anchor_exc}",
                data={"artifact_path": str(path)},
            ) from anchor_exc
        raise LocalPublicationError(
            failure_code,
            f"committed {label} cannot be held for finalization: {path}: {exc}",
            data={"artifact_path": str(path)},
        ) from exc
    return _guard_storage_published_file(
        owned,
        path,
        expected,
        expected_sha256,
        anchor=anchor,
        failure_code=failure_code,
        anchor_failure_code=anchor_failure_code,
        label=label,
    )


def _guard_storage_published_file(
    published: OwnedPublishedFile,
    path: Path,
    expected: bytes,
    expected_sha256: str,
    *,
    anchor: DirectoryAnchor,
    failure_code: str,
    anchor_failure_code: str,
    label: str,
) -> _CommittedFileGuard:
    if Path(os.path.abspath(published.path)) != Path(os.path.abspath(path)):
        published.close()
        raise LocalPublicationError(
            failure_code,
            f"storage publication returned a handle for a different {label}",
            data={"artifact_path": str(path)},
        )
    descriptor = published.detach_descriptor()
    guard = _CommittedFileGuard(
        path=path,
        anchor=anchor,
        descriptor=descriptor,
        identity=published.identity,
        expected=expected,
        expected_sha256=expected_sha256,
        failure_code=failure_code,
        anchor_failure_code=anchor_failure_code,
        label=label,
    )
    try:
        guard.verify()
    except BaseException:
        guard.close()
        raise
    return guard


def _publish_or_recover_target(
    target_path: Path,
    note_bytes: bytes,
    content_sha256: str,
    *,
    anchor: DirectoryAnchor,
) -> _CommittedFileGuard:
    existing = _guard_committed_file(
        target_path,
        note_bytes,
        content_sha256,
        anchor=anchor,
        failure_code="publish_conflict",
        anchor_failure_code="invalid_local_target",
        label="local target",
        missing_ok=True,
    )
    if existing is not None:
        return existing
    try:
        published = publish_bytes_no_replace(
            note_bytes,
            target_path,
            anchor=anchor,
            hold_open=True,
        )
    except (PublishConflictError, FileExistsError):
        conflict = _guard_committed_file(
            target_path,
            note_bytes,
            content_sha256,
            anchor=anchor,
            failure_code="publish_conflict",
            anchor_failure_code="invalid_local_target",
            label="local target",
            missing_ok=True,
        )
        if conflict is None:
            raise LocalPublicationError(
                "publish_failed",
                f"conflicting local target disappeared before it could be guarded: {target_path}",
                data={"target_path": str(target_path)},
            )
        return conflict
    except Exception as exc:
        recovered = _guard_committed_file(
            target_path,
            note_bytes,
            content_sha256,
            anchor=anchor,
            failure_code="publish_conflict",
            anchor_failure_code="invalid_local_target",
            label="local target",
            missing_ok=True,
        )
        if recovered is not None:
            return recovered
        raise LocalPublicationError(
            "publish_failed",
            f"atomic local publication failed before commit: {target_path}: {exc}",
            data={"target_path": str(target_path)},
        ) from exc
    else:
        if not isinstance(published, OwnedPublishedFile):
            raise LocalPublicationError(
                "publication_recovery_required",
                "storage publication did not return its held target descriptor",
                data={"target_path": str(target_path)},
            )
        return _guard_storage_published_file(
            published,
            target_path,
            note_bytes,
            content_sha256,
            anchor=anchor,
            failure_code="publication_recovery_required",
            anchor_failure_code="invalid_local_target",
            label="local target",
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
    run_anchor: DirectoryAnchor,
    target_anchor: DirectoryAnchor,
) -> _CommittedFileGuard:
    if anchored_entry_exists(run_anchor, intent_path):
        existing = _guard_committed_file(
            intent_path,
            intent_bytes,
            hashlib.sha256(intent_bytes).hexdigest(),
            anchor=run_anchor,
            failure_code="publication_identity_conflict",
            anchor_failure_code="run_directory_changed",
            label="local publication intent",
        )
        assert existing is not None
        existing.failure_code = "publication_recovery_required"
        return existing

    if anchored_entry_exists(target_anchor, target_path):
        raise LocalPublicationError(
            "publish_conflict",
            f"fixed local target predates this run publication intent: {target_path}",
            data={"target_path": str(target_path)},
        )
    try:
        published = publish_bytes_no_replace(
            intent_bytes,
            intent_path,
            anchor=run_anchor,
            hold_open=True,
        )
    except (PublishConflictError, FileExistsError):
        conflict = _guard_committed_file(
            intent_path,
            intent_bytes,
            hashlib.sha256(intent_bytes).hexdigest(),
            anchor=run_anchor,
            failure_code="publication_identity_conflict",
            anchor_failure_code="run_directory_changed",
            label="local publication intent",
            missing_ok=True,
        )
        if conflict is None:
            raise LocalPublicationError(
                "publication_intent_failed",
                f"conflicting publication intent disappeared: {intent_path}",
            )
        conflict.failure_code = "publication_recovery_required"
        return conflict
    except Exception as exc:
        recovered = _guard_committed_file(
            intent_path,
            intent_bytes,
            hashlib.sha256(intent_bytes).hexdigest(),
            anchor=run_anchor,
            failure_code="publication_identity_conflict",
            anchor_failure_code="run_directory_changed",
            label="local publication intent",
            missing_ok=True,
        )
        if recovered is not None:
            recovered.failure_code = "publication_recovery_required"
            return recovered
        raise LocalPublicationError(
            "publication_intent_failed",
            f"atomic publication intent commit failed: {intent_path}: {exc}",
        ) from exc
    if not isinstance(published, OwnedPublishedFile):
        raise LocalPublicationError(
            "publication_recovery_required",
            "storage publication did not return its held intent descriptor",
        )
    return _guard_storage_published_file(
        published,
        intent_path,
        intent_bytes,
        hashlib.sha256(intent_bytes).hexdigest(),
        anchor=run_anchor,
        failure_code="publication_recovery_required",
        anchor_failure_code="run_directory_changed",
        label="local publication intent",
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
    run_anchor: DirectoryAnchor,
) -> tuple[Path, ArtifactRef, _CommittedFileGuard]:
    receipt_bytes, receipt_path, receipt_ref = _receipt_bytes_and_path(
        run_dir=run_dir,
        candidate_path=candidate_path,
        candidate=candidate,
        candidate_digest=candidate_digest,
        intent_ref=intent_ref,
    )
    if anchored_entry_exists(run_anchor, receipt_path):
        guard = _guard_committed_file(
            receipt_path,
            receipt_bytes,
            receipt_ref.sha256,
            anchor=run_anchor,
            failure_code="receipt_conflict",
            anchor_failure_code="run_directory_changed",
            label="local publication receipt",
        )
        assert guard is not None
        guard.failure_code = "publication_recovery_required"
    else:
        try:
            published = publish_bytes_no_replace(
                receipt_bytes,
                receipt_path,
                anchor=run_anchor,
                hold_open=True,
            )
        except (PublishConflictError, FileExistsError):
            guard = _guard_committed_file(
                receipt_path,
                receipt_bytes,
                receipt_ref.sha256,
                anchor=run_anchor,
                failure_code="receipt_conflict",
                anchor_failure_code="run_directory_changed",
                label="local publication receipt",
            )
            assert guard is not None
            guard.failure_code = "publication_recovery_required"
        except Exception as exc:
            recovered = _guard_committed_file(
                receipt_path,
                receipt_bytes,
                receipt_ref.sha256,
                anchor=run_anchor,
                failure_code="receipt_conflict",
                anchor_failure_code="run_directory_changed",
                label="local publication receipt",
                missing_ok=True,
            )
            if recovered is None:
                raise
            guard = recovered
            guard.failure_code = "publication_recovery_required"
        else:
            if not isinstance(published, OwnedPublishedFile):
                raise LocalPublicationError(
                    "publication_recovery_required",
                    "storage publication did not return its held receipt descriptor",
                )
            guard = _guard_storage_published_file(
                published,
                receipt_path,
                receipt_bytes,
                receipt_ref.sha256,
                anchor=run_anchor,
                failure_code="publication_recovery_required",
                anchor_failure_code="run_directory_changed",
                label="local publication receipt",
            )
    return receipt_path, receipt_ref, guard


def _lexical_candidate_manifest_path(candidate_input: Path) -> Path:
    requested = Path(candidate_input).expanduser()
    try:
        metadata = os.lstat(requested)
    except FileNotFoundError:
        metadata = None
    if (
        metadata is not None
        and stat.S_ISDIR(metadata.st_mode)
        or metadata is None
        and requested.suffix.lower() != ".json"
    ):
        requested = requested / "candidate.json"
    return Path(os.path.abspath(requested))


def _preflight_local_candidate(
    candidate_input: Path,
) -> LocalCandidatePublicationPreflight:
    candidate_path = _lexical_candidate_manifest_path(candidate_input)
    candidate_dir = candidate_path.parent
    if (
        candidate_path.name != "candidate.json"
        or candidate_dir.parent.name != "candidates"
    ):
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate must use candidates/<candidate_id>/candidate.json",
        )
    run_dir = candidate_dir.parent.parent
    manifest_path = run_dir / "run.json"
    try:
        anchor_context = DirectoryAnchor.open(
            run_dir,
            manifest_path=manifest_path,
        )
    except RunLoadError:
        raise
    with anchor_context as anchor:
        loaded = _load_v2_run_from_anchor(
            anchor,
            manifest_name="run.json",
            manifest_path=manifest_path,
        )
        try:
            raw = read_anchored_bytes(
                anchor,
                candidate_path,
                max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            )
        except (OSError, ValueError) as exc:
            raise LocalPublicationError(
                "candidate_tampered",
                "candidate must be a stable single-link file inside its run",
            ) from exc
        require_raw_schema_version(
            raw,
            expected="paper_reader.candidate.v2",
            artifact_path=candidate_path,
        )
        _preflight_candidate_review_package_snapshot(
            loaded,
            anchor=anchor,
            candidate_path=candidate_path,
            candidate_bytes=raw,
        )
        validate_directory_anchor(anchor)
    return LocalCandidatePublicationPreflight(
        run_dir=run_dir,
        run_device=loaded.run_directory_device,
        run_inode=loaded.run_directory_inode,
        run_manifest_sha256=loaded.manifest_sha256,
        candidate_path=candidate_path,
        candidate_bytes=raw,
        candidate_digest=hashlib.sha256(raw).hexdigest(),
    )


def _candidate_run_dir(candidate_input: Path) -> Path:
    """Compatibility helper for internal callers; publication uses the full preflight."""

    try:
        return _preflight_local_candidate(candidate_input).run_dir
    except OSError as exc:
        raise LocalPublicationError(
            "candidate_unreadable",
            "candidate cannot be inspected safely",
        ) from exc


def publish_local_candidate(candidate_input: Path) -> PublishedLocalCandidate:
    preflight = _preflight_local_candidate(candidate_input)
    try:
        return _publish_local_candidate_from_preflight(candidate_input, preflight)
    except RunLoadError as exc:
        if exc.code not in {"run_manifest_changed", "run_artifact_changed"}:
            raise
    refreshed = _preflight_local_candidate(candidate_input)
    if (
        refreshed.run_dir != preflight.run_dir
        or refreshed.run_device != preflight.run_device
        or refreshed.run_inode != preflight.run_inode
    ):
        raise RunLoadError(
            "run_directory_changed",
            f"run directory changed during local publication retry: {refreshed.run_dir}",
            manifest_path=refreshed.run_dir / "run.json",
        )
    if (
        refreshed.candidate_path != preflight.candidate_path
        or refreshed.candidate_bytes != preflight.candidate_bytes
        or refreshed.candidate_digest != preflight.candidate_digest
    ):
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate changed during local publication retry",
        )
    return _publish_local_candidate_from_preflight(candidate_input, refreshed)


def _publish_local_candidate_from_preflight(
    candidate_input: Path,
    preflight: LocalCandidatePublicationPreflight,
) -> PublishedLocalCandidate:
    with locked_v2_run(
        preflight.run_dir,
        expected_run_path=preflight.run_dir,
        expected_run_device=preflight.run_device,
        expected_run_inode=preflight.run_inode,
        expected_run_manifest_sha256=preflight.run_manifest_sha256,
        expected_artifacts=(
            ExpectedRunArtifact(
                path=preflight.candidate_path.relative_to(preflight.run_dir).as_posix(),
                sha256=preflight.candidate_digest,
            ),
        ),
    ) as loaded:
        return _publish_local_candidate_locked(
            candidate_input,
            loaded,
            preflight=preflight,
        )


def _publish_local_candidate_locked(
    candidate_input: Path,
    locked_run: LoadedRun,
    *,
    preflight: LocalCandidatePublicationPreflight | None = None,
) -> PublishedLocalCandidate:
    loaded, candidate_path, candidate, digest, verified = _load_candidate(
        candidate_input,
        loaded_run=locked_run,
    )
    if preflight is not None and (
        candidate_path != preflight.candidate_path
        or canonical_json_bytes(candidate) != preflight.candidate_bytes
        or digest != preflight.candidate_digest
    ):
        raise LocalPublicationError(
            "candidate_tampered",
            "candidate changed after its read-only publication preflight",
        )
    source = candidate.source
    target = candidate.target
    assert isinstance(source, LocalSourceIdentity)
    assert isinstance(target, LocalPublicationTarget)
    verify_local_source(source)
    original_evidence = revalidate_candidate_original_evidence(
        loaded,
        candidate,
        verified,
    )
    target_path = validate_local_target_location(target, source)
    _note_path, note_bytes = verified["note_markdown"][0]
    run_dir = loaded.manifest_path.parent
    run_anchor = loaded.run_directory_anchor
    if run_anchor is None:
        raise LocalPublicationError(
            "run_directory_changed",
            "local publication requires a locked run directory anchor",
        )
    expected_candidate_members = {
        "candidate.json": (
            len(canonical_json_bytes(candidate)),
            hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
        )
    }
    for artifact in candidate.artifacts:
        artifact_path = run_dir / artifact.path
        expected_candidate_members[
            artifact_path.relative_to(candidate_path.parent).as_posix()
        ] = (artifact.size_bytes, artifact.sha256)
    expected_candidate_snapshot = tree_snapshot_from_hashes(
        expected_candidate_members
    )
    try:
        source_guard_context = open_resolved_source_guard(
            source.resolved_path,
            max_bytes=V2_RESOURCE_POLICY.local_pdf_max_bytes,
            expected_sha256=source.sha256,
            expected_size=source.size_bytes,
            expected_device=source.device,
            expected_inode=source.inode,
        )
    except (OSError, ValueError) as exc:
        raise LocalPublicationError(
            "source_changed",
            f"local PDF source cannot be held for publication: {exc}",
        ) from exc
    try:
        intent_bytes, intent_path, intent_ref = _intent_bytes_and_path(
            run_dir=run_dir,
            candidate=candidate,
            candidate_digest=digest,
        )
        (
            receipt_bytes,
            projected_receipt_path,
            projected_receipt_ref,
        ) = _receipt_bytes_and_path(
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
                retained_replacement_paths=(loaded.manifest_path,),
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
        target_anchor_context = DirectoryAnchor.open(
            target_path.parent,
            manifest_path=target_path,
        )
    except BaseException as exc:
        source_guard_context.close()
        if isinstance(exc, RunLoadError):
            raise LocalPublicationError("invalid_local_target", str(exc)) from exc
        raise
    with ExitStack() as stack:
        source_guard = stack.enter_context(source_guard_context)
        try:
            source_closure_guard = stack.enter_context(
                open_bound_source_closure_guard(loaded)
            )
            source_closure_guard.verify()
        except SecondaryEvidenceError as exc:
            raise LocalPublicationError(exc.code, str(exc)) from exc
        except UnsafeStoragePathError as exc:
            raise LocalPublicationError(
                "secondary_plan_mismatch",
                f"source closure changed before local publication: {exc}",
            ) from exc
        try:
            candidate_tree_guard = stack.enter_context(
                _CandidateTreeGuard(
                    anchor=open_anchored_directory(run_anchor, candidate_path.parent),
                    expected=expected_candidate_snapshot,
                )
            )
        except (OSError, ValueError) as exc:
            raise LocalPublicationError(
                "candidate_tampered",
                f"candidate tree cannot be held for publication: {exc}",
            ) from exc
        evidence_tree_guard = stack.enter_context(
            _open_original_evidence_tree_guard(loaded, original_evidence)
        )
        target_anchor = stack.enter_context(target_anchor_context)
        _verify_held_source(source_guard)
        source_closure_guard.verify()
        candidate_tree_guard.verify()
        evidence_tree_guard.verify()
        if (
            target_anchor.device != target.parent_device
            or target_anchor.inode != target.parent_inode
        ):
            raise LocalPublicationError(
                "invalid_local_target",
                "local target parent identity changed",
            )
        validate_directory_anchor(run_anchor)
        intent_guard = _publish_or_verify_intent(
            intent_bytes=intent_bytes,
            intent_path=intent_path,
            target_path=target_path,
            run_anchor=run_anchor,
            target_anchor=target_anchor,
        )
        with intent_guard:
            _verify_held_source(source_guard)
            source_closure_guard.verify()
            candidate_tree_guard.verify()
            evidence_tree_guard.verify()
            target_guard = _publish_or_recover_target(
                target_path,
                note_bytes,
                candidate.content_sha256,
                anchor=target_anchor,
            )
            with target_guard:
                try:
                    _verify_held_source(source_guard)
                    source_closure_guard.verify()
                    candidate_tree_guard.verify()
                    evidence_tree_guard.verify()
                    intent_guard.verify()
                    target_guard.verify()
                    receipt_path, receipt_ref, receipt_guard = _publish_or_verify_receipt(
                        run_dir=run_dir,
                        candidate_path=candidate_path,
                        candidate=candidate,
                        candidate_digest=digest,
                        intent_ref=intent_ref,
                        run_anchor=run_anchor,
                    )
                except LocalPublicationError:
                    raise
                except Exception as exc:
                    raise LocalPublicationError(
                        "publication_recovery_required",
                        f"target is committed but local receipt is incomplete: {exc}",
                        data={"target_path": str(target_path)},
                    ) from exc
                with receipt_guard:
                    try:
                        _verify_held_source(source_guard)
                        source_closure_guard.verify()
                        candidate_tree_guard.verify()
                        evidence_tree_guard.verify()
                        intent_guard.verify()
                        target_guard.verify()
                        receipt_guard.verify()
                        updated_run = _updated_run(
                            loaded,
                            intent_ref,
                            receipt_ref,
                            candidate.gate,
                        )
                        expected_run_bytes = canonical_json_bytes(updated_run)
                        if updated_run != loaded.run:
                            written_run = cas_update_run(
                                loaded,
                                updated_run,
                                hold_new=True,
                                finalization_guards=(
                                    source_guard,
                                    source_closure_guard,
                                    candidate_tree_guard,
                                    evidence_tree_guard,
                                    intent_guard,
                                    target_guard,
                                    receipt_guard,
                                ),
                            )
                            if not isinstance(written_run, OwnedPublishedFile):
                                raise LocalPublicationError(
                                    "publication_recovery_required",
                                    "run compare-and-swap lost its held new identity",
                                )
                            run_guard = _guard_storage_published_file(
                                written_run,
                                loaded.manifest_path,
                                expected_run_bytes,
                                hashlib.sha256(expected_run_bytes).hexdigest(),
                                anchor=run_anchor,
                                failure_code="publication_recovery_required",
                                anchor_failure_code="run_directory_changed",
                                label="published run manifest",
                            )
                        else:
                            run_guard = _guard_committed_file(
                                loaded.manifest_path,
                                expected_run_bytes,
                                hashlib.sha256(expected_run_bytes).hexdigest(),
                                anchor=run_anchor,
                                failure_code="publication_recovery_required",
                                anchor_failure_code="run_directory_changed",
                                label="published run manifest",
                            )
                        assert run_guard is not None
                        with run_guard:
                            _verify_held_source(source_guard)
                            source_closure_guard.verify()
                            candidate_tree_guard.verify()
                            evidence_tree_guard.verify()
                            intent_guard.verify()
                            target_guard.verify()
                            receipt_guard.verify()
                            run_guard.verify()
                            revalidate_candidate_original_evidence(
                                loaded,
                                candidate,
                                verified,
                            )
                            evidence_tree_guard.verify()
                            _verify_held_source(source_guard)
                            source_closure_guard.verify()
                            candidate_tree_guard.verify()
                            evidence_tree_guard.verify()
                            intent_guard.verify()
                            target_guard.verify()
                            receipt_guard.verify()
                            run_guard.verify()
                    except Exception as exc:
                        if isinstance(exc, LocalPublicationError):
                            raise
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
