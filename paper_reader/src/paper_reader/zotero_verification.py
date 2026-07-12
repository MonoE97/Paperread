from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from pydantic import ValidationError

from paper_reader.candidate_integrity import LocalPublicationError, verify_artifact_ref
from paper_reader.contracts import (
    ArtifactRef,
    GateBlocker,
    GateState,
    PaperReaderRun,
    PaperReaderVerification,
)
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.run_lock import locked_v2_run
from paper_reader.run_size import RunSizeLimitError, enforce_projected_run_size
from paper_reader.storage import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    create_anchored_directory,
    new_random_id,
    new_uuid,
    remove_anchored_tree,
    rfc3339_utc,
    tree_snapshot_from_bytes,
    validate_directory_anchor,
)
from paper_reader.v2_loader import LoadedRun
from paper_reader.zotero_authorization_loader import (
    LoadedAuthorization,
    ZoteroAuthorizationBindingError,
    authorization_manifest_path,
    inspect_authorization_target,
    load_bound_authorization,
)
from paper_reader.zotero_artifact_paths import (
    DeterministicArtifactPaths,
    UnsafeZoteroArtifactPathError,
    anchored_artifact_publication,
    inspect_deterministic_artifact_paths,
)
from paper_reader.zotero_candidate import _artifact_ref
from paper_reader.zotero_lock import locked_zotero_parent
from paper_reader.zotero_note_validation import NoteEvaluation, evaluate_note_snapshot
from paper_reader.zotero_read import LocalApiZoteroReadProvider, ZoteroReadProvider


_PORTABLE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$")


class ZoteroVerificationError(LocalPublicationError):
    pass


def _verification_artifact_paths(
    run_dir: Path,
    authorization_id: str,
    note_key: str,
    *,
    allow_existing_sidecar: bool,
    allow_existing_main: bool,
) -> DeterministicArtifactPaths:
    try:
        return inspect_deterministic_artifact_paths(
            run_dir,
            root_name="verifications",
            parent_parts=(authorization_id,),
            stem=note_key,
            allow_existing_sidecar=allow_existing_sidecar,
            allow_existing_main=allow_existing_main,
        )
    except UnsafeZoteroArtifactPathError as exc:
        raise ZoteroVerificationError(exc.code, str(exc), data=exc.data) from exc


@dataclass(frozen=True, slots=True)
class VerifiedZoteroNote:
    run_dir: Path
    verification_path: Path
    verification_dir: Path
    verification: PaperReaderVerification
    authorization_digest: str
    replayed: bool = False


def _verification_gate(evaluation: NoteEvaluation) -> GateState:
    blockers = tuple(
        GateBlocker(
            code=f"verification_{check.name}",
            message=check.message or f"verification check failed: {check.name}",
        )
        for check in evaluation.checks
        if not check.passed
    )
    return GateState(
        status="passed" if evaluation.verified else "blocked",
        evaluated_at=rfc3339_utc(),
        checks=tuple(check.name for check in evaluation.checks),
        blockers=blockers,
    )


def _validate_verification_record(
    run_dir: Path,
    path: Path,
    raw: bytes,
    *,
    bound: LoadedAuthorization,
    note_key: str,
    loaded: LoadedRun,
) -> PaperReaderVerification:
    try:
        verification = PaperReaderVerification.model_validate_json(raw)
    except ValidationError as exc:
        raise ZoteroVerificationError(
            "verification_tampered",
            f"verification failed strict validation: {exc}",
        ) from exc
    expected_path = (
        run_dir
        / "verifications"
        / bound.authorization.authorization_id
        / f"{note_key}.json"
    )
    if (
        path.resolve(strict=False) != expected_path.resolve(strict=False)
        or canonical_json_bytes(verification) != raw
        or verification.run_id != bound.authorization.run_id
        or verification.authorization_digest != bound.authorization_digest
        or verification.target != bound.authorization.target
        or verification.note_key != note_key
    ):
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification main artifact identity or binding changed",
        )
    sidecar_dir = expected_path.with_suffix("")
    roles: set[str] = set()
    for artifact in verification.artifacts:
        try:
            artifact_path, _artifact_bytes = verify_artifact_ref(
                run_dir,
                artifact,
                anchor=loaded.run_directory_anchor,
            )
        except LocalPublicationError as exc:
            raise ZoteroVerificationError(
                "verification_tampered",
                f"verification member changed: {artifact.path}: {exc}",
            ) from exc
        if not artifact_path.is_relative_to(sidecar_dir) or artifact.role in roles:
            raise ZoteroVerificationError(
                "verification_tampered",
                "verification sidecar membership is not closed and unique",
            )
        roles.add(artifact.role)
    if roles != {"authorization_snapshot", "zotero_note_readback", "verification_checks"}:
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification sidecar artifact roles changed",
        )
    if (
        verification.authorization not in verification.artifacts
        or verification.note_snapshot not in verification.artifacts
        or verification.checks_snapshot not in verification.artifacts
    ):
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification refs are not members of the immutable sidecar",
        )
    return verification


def _existing_verification(
    loaded: LoadedRun,
    *,
    bound: LoadedAuthorization,
    note_key: str,
) -> VerifiedZoteroNote | None:
    run_dir = loaded.manifest_path.parent
    artifact_paths = _verification_artifact_paths(
        run_dir,
        bound.authorization.authorization_id,
        note_key,
        allow_existing_sidecar=True,
        allow_existing_main=True,
    )
    verification_path = artifact_paths.main
    verification_dir = artifact_paths.sidecar
    recovery_record = verification_dir / "record.json"
    if not verification_path.exists() and not recovery_record.exists():
        return None
    try:
        raw = (
            verification_path.read_bytes()
            if verification_path.exists()
            else recovery_record.read_bytes()
        )
    except OSError as exc:
        raise ZoteroVerificationError(
            "verification_tampered",
            f"verification commit candidate is unreadable: {exc}",
        ) from exc
    verification = _validate_verification_record(
        run_dir,
        verification_path,
        raw,
        bound=bound,
        note_key=note_key,
        loaded=loaded,
    )
    if not verification_path.exists():
        try:
            with anchored_artifact_publication(
                artifact_paths,
                staging_dir=None,
                allow_existing_sidecar=True,
                allow_existing_main=False,
                expected_run_anchor=loaded.run_directory_anchor,
            ) as publication:
                publication.publish_main(recovery_record, expected_bytes=raw)
        except UnsafeZoteroArtifactPathError as exc:
            raise ZoteroVerificationError(exc.code, str(exc), data=exc.data) from exc
        except Exception as exc:
            raise ZoteroVerificationError(
                "verification_recovery_failed",
                f"failed to restore exact verification commit marker: {exc}",
            ) from exc
    relative = verification_path.relative_to(run_dir).as_posix()
    refs = [
        item
        for item in loaded.run.artifacts
        if item.role == "zotero_verification" and item.path == relative
    ]
    if len(refs) > 1:
        raise ZoteroVerificationError(
            "verification_tampered",
            "run binds the deterministic verification more than once",
        )
    if refs:
        try:
            path, bound_raw = verify_artifact_ref(
                run_dir,
                refs[0],
                anchor=loaded.run_directory_anchor,
            )
        except LocalPublicationError as exc:
            raise ZoteroVerificationError(
                "verification_tampered",
                f"bound verification failed integrity validation: {exc}",
            ) from exc
        if path != verification_path.resolve(strict=True) or bound_raw != raw:
            raise ZoteroVerificationError(
                "verification_tampered",
                "run-bound verification differs from the deterministic main artifact",
            )
    else:
        verification_ref = _artifact_ref(
            run_dir,
            verification_path,
            verification_path,
            "zotero_verification",
            "application/json",
        )
        updated_run = _updated_run(
            loaded.run,
            verification_ref=verification_ref,
            gate=verification.gate,
            verified=verification.verified,
        )
        try:
            atomic_write_json(
                loaded.manifest_path,
                updated_run,
                anchor=loaded.run_directory_anchor,
            )
        except Exception as exc:
            raise ZoteroVerificationError(
                "verification_status_update_failed",
                f"verification main artifact is durable but run binding failed: {exc}",
            ) from exc
    return VerifiedZoteroNote(
        run_dir=run_dir,
        verification_path=verification_path,
        verification_dir=verification_dir,
        verification=verification,
        authorization_digest=bound.authorization_digest,
        replayed=True,
    )


def _updated_run(
    run: PaperReaderRun,
    *,
    verification_ref: ArtifactRef,
    gate: GateState,
    verified: bool,
) -> PaperReaderRun:
    status = "published" if verified else ("published" if run.status == "published" else "blocked")
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=run.run_id,
        created_at=run.created_at,
        source=run.source,
        target=run.target,
        status=status,
        artifacts=(*run.artifacts, verification_ref),
        gate=gate if run.status != "published" or verified else run.gate,
        live_preflight=run.live_preflight,
    )


def publish_verification_locked(
    loaded: LoadedRun,
    bound: LoadedAuthorization,
    *,
    note_key: str,
    snapshot: dict[str, Any],
    snapshot_bytes: bytes,
) -> VerifiedZoteroNote:
    run_dir = bound.run_dir
    evaluation = evaluate_note_snapshot(
        snapshot,
        authorization=bound.authorization,
        note_key=note_key,
    )
    verification_id = new_random_id("verification")
    artifact_paths = _verification_artifact_paths(
        run_dir,
        bound.authorization.authorization_id,
        note_key,
        allow_existing_sidecar=False,
        allow_existing_main=False,
    )
    verification_path = artifact_paths.main
    verification_dir = artifact_paths.sidecar
    staging = run_dir / f".{verification_id}.{new_uuid()}.staging"
    run_anchor = loaded.run_directory_anchor
    if run_anchor is None:
        raise ZoteroVerificationError(
            "run_directory_changed",
            "verification requires a locked run directory anchor",
        )
    staging_anchor = create_anchored_directory(run_anchor, staging)
    try:
        staged_sidecar = staging / "sidecar"
        checks_payload = {
            "format": "paper_reader.verification-checks.v2-internal",
            "authorization_digest": bound.authorization_digest,
            "note_key": note_key,
            "checks": [item.model_dump(mode="json") for item in evaluation.checks],
        }
        files = {
            "authorization.json": bound.authorization_bytes,
            "note.json": snapshot_bytes,
            "checks.json": canonical_json_bytes(checks_payload),
        }
        for name, content in files.items():
            atomic_write_bytes(
                staged_sidecar / name,
                content,
                anchor=staging_anchor,
            )
        specs = {
            "authorization.json": ("authorization_snapshot", "application/json"),
            "note.json": ("zotero_note_readback", "application/json"),
            "checks.json": ("verification_checks", "application/json"),
        }
        validate_directory_anchor(staging_anchor)
        refs = {
            name: _artifact_ref(
                run_dir,
                staged_sidecar / name,
                verification_dir / name,
                role,
                media,
            )
            for name, (role, media) in specs.items()
        }
        validate_directory_anchor(staging_anchor)
        gate = _verification_gate(evaluation)
        verification = PaperReaderVerification(
            schema_version="paper_reader.verification.v2",
            verification_id=verification_id,
            run_id=loaded.run.run_id,
            created_at=rfc3339_utc(),
            authorization=refs["authorization.json"],
            authorization_digest=bound.authorization_digest,
            target=bound.authorization.target,
            note_key=note_key,
            verified=evaluation.verified,
            content_sha256=evaluation.content_sha256,
            content_length=evaluation.content_length,
            checks=evaluation.checks,
            note_snapshot=refs["note.json"],
            checks_snapshot=refs["checks.json"],
            artifacts=tuple(refs.values()),
            gate=gate,
        )
        verification_bytes = canonical_json_bytes(verification)
        atomic_write_bytes(
            staged_sidecar / "record.json",
            verification_bytes,
            anchor=staging_anchor,
        )
        staged_verification_path = staging / f"{note_key}.json"
        atomic_write_bytes(
            staged_verification_path,
            verification_bytes,
            anchor=staging_anchor,
        )
        sidecar_snapshot = tree_snapshot_from_bytes(
            {**files, "record.json": verification_bytes}
        )
        verification_ref = _artifact_ref(
            run_dir,
            staged_verification_path,
            verification_path,
            "zotero_verification",
            "application/json",
        )
        updated_run = _updated_run(
            loaded.run,
            verification_ref=verification_ref,
            gate=gate,
            verified=evaluation.verified,
        )
        try:
            enforce_projected_run_size(
                run_dir,
                max_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                staging_dir=staging,
                replacements={loaded.manifest_path: canonical_json_bytes(updated_run)},
            )
        except RunSizeLimitError as exc:
            raise ZoteroVerificationError(
                "run_size_limit_exceeded",
                str(exc),
                data={"run_size_bytes": exc.actual_bytes, "max_bytes": exc.max_bytes},
            ) from exc
        publication_phase = "sidecar"
        try:
            with anchored_artifact_publication(
                artifact_paths,
                staging_dir=staging,
                allow_existing_sidecar=False,
                allow_existing_main=False,
                expected_run_anchor=loaded.run_directory_anchor,
                expected_staging_anchor=staging_anchor,
                expected_sidecar_snapshot=sidecar_snapshot,
            ) as publication:
                publication.publish_sidecar(staged_sidecar)
                publication_phase = "main"
                publication.publish_main(
                    staged_verification_path,
                    expected_bytes=verification_bytes,
                )
        except UnsafeZoteroArtifactPathError as exc:
            raise ZoteroVerificationError(exc.code, str(exc), data=exc.data) from exc
        except Exception as exc:
            raise ZoteroVerificationError(
                "verification_publication_failed",
                (
                    f"immutable verification {publication_phase} publication failed: "
                    f"{verification_path}: {exc}"
                ),
            ) from exc
        try:
            atomic_write_json(
                loaded.manifest_path,
                updated_run,
                anchor=loaded.run_directory_anchor,
            )
        except Exception as exc:
            raise ZoteroVerificationError(
                "verification_status_update_failed",
                f"verification tree is durable but run binding failed: {exc}",
            ) from exc
        return VerifiedZoteroNote(
            run_dir=run_dir,
            verification_path=verification_path,
            verification_dir=verification_dir,
            verification=verification,
            authorization_digest=bound.authorization_digest,
            replayed=False,
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


def verify_zotero_authorization(
    authorization_input: Path,
    *,
    note_key: str,
    provider: ZoteroReadProvider | None = None,
) -> VerifiedZoteroNote:
    if not _PORTABLE_IDENTIFIER_RE.fullmatch(note_key):
        raise ZoteroVerificationError("invalid_note_key", "note_key is not a valid identifier")
    try:
        authorization_path, inspected, run_dir = inspect_authorization_target(
            authorization_input
        )
    except ZoteroAuthorizationBindingError as exc:
        raise ZoteroVerificationError(exc.code, str(exc), data=exc.data) from exc
    _verification_artifact_paths(
        run_dir,
        inspected.authorization_id,
        note_key,
        allow_existing_sidecar=True,
        allow_existing_main=True,
    )
    resolved_provider = provider or LocalApiZoteroReadProvider()
    with locked_zotero_parent(run_dir, inspected.target.parent_key):
        with locked_v2_run(run_dir) as loaded:
            try:
                bound = load_bound_authorization(loaded, authorization_path)
            except ZoteroAuthorizationBindingError as exc:
                raise ZoteroVerificationError(exc.code, str(exc), data=exc.data) from exc
            replay = _existing_verification(
                loaded,
                bound=bound,
                note_key=note_key,
            )
            if replay is not None:
                return replay
            _verification_artifact_paths(
                run_dir,
                bound.authorization.authorization_id,
                note_key,
                allow_existing_sidecar=False,
                allow_existing_main=False,
            )
            try:
                snapshot = resolved_provider.get_note(note_key)
            except Exception as exc:
                raise ZoteroVerificationError(
                    "zotero_read_failed",
                    f"read-only Zotero note readback failed: {exc}",
                ) from exc
            if not isinstance(snapshot, dict):
                raise ZoteroVerificationError(
                    "invalid_note_snapshot",
                    "read-only Zotero provider returned a non-object note snapshot",
                )
            try:
                snapshot_bytes = canonical_json_bytes(snapshot)
            except (TypeError, ValueError) as exc:
                raise ZoteroVerificationError(
                    "invalid_note_snapshot",
                    f"Zotero note snapshot is not canonicalizable: {exc}",
                ) from exc
            return publish_verification_locked(
                loaded,
                bound,
                note_key=note_key,
                snapshot=snapshot,
                snapshot_bytes=snapshot_bytes,
            )


__all__ = [
    "LoadedAuthorization",
    "NoteEvaluation",
    "VerifiedZoteroNote",
    "ZoteroVerificationError",
    "authorization_manifest_path",
    "evaluate_note_snapshot",
    "load_bound_authorization",
    "publish_verification_locked",
    "verify_zotero_authorization",
]
