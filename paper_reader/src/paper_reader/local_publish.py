from __future__ import annotations

import hashlib
import json
import os
import stat
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
    PaperReaderRun,
    ZoteroPublicationTarget,
    ZoteroSourceIdentity,
)
from paper_reader.note_hash import canonicalize_note_html_for_hash, note_html_sha256
from paper_reader.raw_schema import require_raw_schema_version
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.run_lock import ExpectedRunArtifact, locked_v2_run
from paper_reader.run_size import RunSizeLimitError, enforce_projected_run_size
from paper_reader.storage import (
    PublishConflictError,
    UnsafeStoragePathError,
    anchored_entry_exists,
    atomic_write_json,
    canonical_json_bytes,
    publish_bytes_no_replace,
    read_anchored_bytes,
    snapshot_anchored_tree,
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
        before_snapshot = snapshot_anchored_tree(anchor, candidate_dir)
        raw = read_anchored_bytes(anchor, candidate_path)
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
    if require_local and (
        not isinstance(candidate.source, LocalSourceIdentity)
        or not isinstance(candidate.target, LocalPublicationTarget)
    ):
        raise LocalPublicationError("not_implemented", "local publish accepts only local candidates")

    verified: dict[str, list[tuple[Path, bytes]]] = {}
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
        if relative_member in expected_members:
            raise LocalPublicationError(
                "candidate_tampered",
                "candidate artifact paths must be unique and must not replace candidate.json",
            )
        expected_members[relative_member] = (artifact.size_bytes, artifact.sha256)
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
    if isinstance(candidate.source, ZoteroSourceIdentity) and isinstance(
        candidate.target, ZoteroPublicationTarget
    ):
        required.update(
            {
                "raw_discovery_bundle_snapshot",
                "zotero_parent_snapshot",
                "zotero_children_snapshot",
            }
        )
    if any(len(verified.get(role, [])) != 1 for role in required):
        raise LocalPublicationError("candidate_tampered", "candidate artifact membership is incomplete")
    if candidate.evidence_manifest not in candidate.artifacts or candidate.sealed_review not in candidate.artifacts:
        raise LocalPublicationError("candidate_tampered", "candidate gate refs are not artifact members")
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
        after_snapshot = snapshot_anchored_tree(anchor, candidate_dir)
        expected_snapshot = tree_snapshot_from_hashes(expected_members)
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
) -> bytes:
    if anchor is not None:
        try:
            return read_anchored_bytes(anchor, path)
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
        (before_path.st_dev, before_path.st_ino, before_path.st_size, before_path.st_mtime_ns, before_path.st_nlink),
        (before_fd.st_dev, before_fd.st_ino, before_fd.st_size, before_fd.st_mtime_ns, before_fd.st_nlink),
        (after_fd.st_dev, after_fd.st_ino, after_fd.st_size, after_fd.st_mtime_ns, after_fd.st_nlink),
        (after_path.st_dev, after_path.st_ino, after_path.st_size, after_path.st_mtime_ns, after_path.st_nlink),
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


def _verify_exact_target(
    target_path: Path,
    expected: bytes,
    expected_sha256: str,
    *,
    anchor: DirectoryAnchor,
) -> bool:
    if not anchored_entry_exists(anchor, target_path):
        return False
    raw = _read_stable_regular_file(
        target_path,
        conflict_code="publish_conflict",
        anchor=anchor,
    )
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
    *,
    anchor: DirectoryAnchor,
) -> None:
    if _verify_exact_target(target_path, note_bytes, content_sha256, anchor=anchor):
        return
    try:
        publish_bytes_no_replace(note_bytes, target_path, anchor=anchor)
    except (PublishConflictError, FileExistsError):
        _verify_exact_target(target_path, note_bytes, content_sha256, anchor=anchor)
    except Exception as exc:
        if _verify_exact_target(target_path, note_bytes, content_sha256, anchor=anchor):
            return
        raise LocalPublicationError(
            "publish_failed",
            f"atomic local publication failed before commit: {target_path}: {exc}",
            data={"target_path": str(target_path)},
        ) from exc
    if not _verify_exact_target(target_path, note_bytes, content_sha256, anchor=anchor):
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
    run_anchor: DirectoryAnchor,
    target_anchor: DirectoryAnchor,
) -> None:
    if anchored_entry_exists(run_anchor, intent_path):
        actual = _read_stable_regular_file(
            intent_path,
            conflict_code="publication_identity_conflict",
            anchor=run_anchor,
        )
        if actual != intent_bytes:
            raise LocalPublicationError(
                "publication_identity_conflict",
                f"run publication intent belongs to another candidate: {intent_path}",
            )
        return

    if anchored_entry_exists(target_anchor, target_path):
        raise LocalPublicationError(
            "publish_conflict",
            f"fixed local target predates this run publication intent: {target_path}",
            data={"target_path": str(target_path)},
        )
    try:
        publish_bytes_no_replace(intent_bytes, intent_path, anchor=run_anchor)
    except (PublishConflictError, FileExistsError):
        actual = _read_stable_regular_file(
            intent_path,
            conflict_code="publication_identity_conflict",
            anchor=run_anchor,
        )
        if actual != intent_bytes:
            raise LocalPublicationError(
                "publication_identity_conflict",
                f"run publication intent belongs to another candidate: {intent_path}",
            )
    except Exception as exc:
        if anchored_entry_exists(run_anchor, intent_path):
            actual = _read_stable_regular_file(
                intent_path,
                conflict_code="publication_identity_conflict",
                anchor=run_anchor,
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
        anchor=run_anchor,
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
    run_anchor: DirectoryAnchor,
) -> tuple[Path, ArtifactRef]:
    receipt_bytes, receipt_path, receipt_ref = _receipt_bytes_and_path(
        run_dir=run_dir,
        candidate_path=candidate_path,
        candidate=candidate,
        candidate_digest=candidate_digest,
        intent_ref=intent_ref,
    )
    if anchored_entry_exists(run_anchor, receipt_path):
        actual = _read_stable_regular_file(
            receipt_path,
            conflict_code="receipt_conflict",
            anchor=run_anchor,
        )
        if actual != receipt_bytes:
            raise LocalPublicationError(
                "receipt_conflict",
                f"deterministic local receipt contains different bytes: {receipt_path}",
            )
    else:
        try:
            publish_bytes_no_replace(receipt_bytes, receipt_path, anchor=run_anchor)
        except (PublishConflictError, FileExistsError):
            actual = _read_stable_regular_file(
                receipt_path,
                conflict_code="receipt_conflict",
                anchor=run_anchor,
            )
            if actual != receipt_bytes:
                raise LocalPublicationError(
                    "receipt_conflict",
                    f"deterministic local receipt contains different bytes: {receipt_path}",
                )
        except Exception as exc:
            if anchored_entry_exists(run_anchor, receipt_path):
                actual = _read_stable_regular_file(
                    receipt_path,
                    conflict_code="receipt_conflict",
                    anchor=run_anchor,
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
            raw = read_anchored_bytes(anchor, candidate_path)
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
    target_path = validate_local_target_location(target, source)
    _note_path, note_bytes = verified["note_markdown"][0]
    run_dir = loaded.manifest_path.parent
    run_anchor = loaded.run_directory_anchor
    if run_anchor is None:
        raise LocalPublicationError(
            "run_directory_changed",
            "local publication requires a locked run directory anchor",
        )
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
    try:
        target_anchor_context = DirectoryAnchor.open(
            target_path.parent,
            manifest_path=target_path,
        )
    except RunLoadError as exc:
        raise LocalPublicationError("invalid_local_target", str(exc)) from exc
    with target_anchor_context as target_anchor:
        if (
            target_anchor.device != target.parent_device
            or target_anchor.inode != target.parent_inode
        ):
            raise LocalPublicationError(
                "invalid_local_target",
                "local target parent identity changed",
            )
        validate_directory_anchor(run_anchor)
        _publish_or_verify_intent(
            intent_bytes=intent_bytes,
            intent_path=intent_path,
            target_path=target_path,
            run_anchor=run_anchor,
            target_anchor=target_anchor,
        )
        _publish_or_recover_target(
            target_path,
            note_bytes,
            candidate.content_sha256,
            anchor=target_anchor,
        )

        try:
            receipt_path, receipt_ref = _publish_or_verify_receipt(
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
        try:
            updated_run = _updated_run(loaded, intent_ref, receipt_ref, candidate.gate)
            if updated_run != loaded.run:
                atomic_write_json(
                    loaded.manifest_path,
                    updated_run,
                    anchor=run_anchor,
                )
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
