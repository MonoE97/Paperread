from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
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
from paper_reader.raw_schema import require_raw_schema_version
from paper_reader.run_lock import locked_v2_run
from paper_reader.run_size import RunSizeLimitError, enforce_projected_run_size
from paper_reader.storage import (
    anchored_entry_exists,
    atomic_write_bytes,
    atomic_write_json,
    cas_update_run,
    canonical_json_bytes,
    create_anchored_directory,
    new_random_id,
    new_uuid,
    open_anchored_directory,
    open_terminal_artifact_guard,
    read_anchored_bytes,
    remove_anchored_tree,
    rfc3339_utc,
    snapshot_directory_fd,
    tree_snapshot_from_bytes,
    validate_directory_anchor,
)
from paper_reader.v2_loader import DirectoryAnchor, LoadedRun, RunLoadError
from paper_reader.zotero_authorization_loader import (
    InspectedAuthorization,
    LoadedAuthorization,
    ZoteroAuthorizationBindingError,
    authorization_manifest_path,
    load_bound_authorization,
    open_bound_authorization_guard,
    preflight_authorization_schema_versions,
)
from paper_reader.zotero_artifact_paths import (
    DeterministicArtifactPaths,
    UnsafeZoteroArtifactPathError,
    anchored_artifact_publication,
    inspect_deterministic_artifact_paths,
)
from paper_reader.zotero_candidate import _artifact_ref
from paper_reader.zotero_lock import ZoteroLockError, locked_zotero_parent
from paper_reader.zotero_note_validation import NoteEvaluation, evaluate_note_snapshot
from paper_reader.zotero_read import LocalApiZoteroReadProvider, ZoteroReadProvider


_PORTABLE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$")
_VERIFICATION_MEMBER_BY_ROLE = {
    "authorization_snapshot": "authorization.json",
    "verification_checks": "checks.json",
    "zotero_note_readback": "note.json",
}
_VERIFICATION_SIDECAR_NAMES = tuple(
    sorted((*_VERIFICATION_MEMBER_BY_ROLE.values(), "record.json"))
)


class ZoteroVerificationError(LocalPublicationError):
    pass


@contextmanager
def _locked_parent_for_inspection(inspection: InspectedAuthorization):
    try:
        with locked_zotero_parent(
            inspection.run_dir,
            inspection.authorization.target.parent_key,
            expected_skill_root=inspection.skill_root,
            expected_skill_root_device=inspection.skill_root_device,
            expected_skill_root_inode=inspection.skill_root_inode,
            expected_run_path=inspection.run_dir,
            expected_run_device=inspection.run_directory_device,
            expected_run_inode=inspection.run_directory_inode,
            expected_run_manifest_sha256=inspection.run_manifest_sha256,
            expected_artifacts=inspection.expected_artifacts,
        ) as locked:
            yield locked
    except ZoteroLockError as exc:
        raise ZoteroVerificationError(exc.code, str(exc)) from exc


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


def _read_closed_verification_sidecar(
    loaded: LoadedRun,
    verification_path: Path,
) -> dict[str, bytes]:
    anchor = loaded.run_directory_anchor
    if anchor is None:
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification sidecar validation requires a locked run anchor",
        )
    sidecar = verification_path.with_suffix("")
    try:
        with open_anchored_directory(anchor, sidecar) as sidecar_anchor:
            before_names = tuple(sorted(os.listdir(sidecar_anchor.descriptor)))
            if before_names != _VERIFICATION_SIDECAR_NAMES:
                raise ZoteroVerificationError(
                    "verification_tampered",
                    "verification sidecar membership is not the exact closed set",
                )
            before_snapshot = snapshot_directory_fd(
                sidecar_anchor.descriptor,
                max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
                max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
            )
            members = {
                name: read_anchored_bytes(
                    sidecar_anchor,
                    sidecar / name,
                    max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                )
                for name in _VERIFICATION_SIDECAR_NAMES
            }
            after_names = tuple(sorted(os.listdir(sidecar_anchor.descriptor)))
            after_snapshot = snapshot_directory_fd(
                sidecar_anchor.descriptor,
                max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
                max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
            )
            if after_names != before_names or after_snapshot != before_snapshot:
                raise ZoteroVerificationError(
                    "verification_tampered",
                    "verification sidecar changed while it was inspected",
                )
            validate_directory_anchor(sidecar_anchor)
        validate_directory_anchor(anchor)
    except ZoteroVerificationError:
        raise
    except (OSError, ValueError) as exc:
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification sidecar cannot be inspected safely",
        ) from exc
    return members


def _validate_verification_record(
    run_dir: Path,
    path: Path,
    raw: bytes,
    *,
    bound: LoadedAuthorization,
    note_key: str,
    loaded: LoadedRun,
) -> PaperReaderVerification:
    require_raw_schema_version(
        raw,
        expected="paper_reader.verification.v2",
        artifact_path=path,
    )
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
    sidecar_members = _read_closed_verification_sidecar(loaded, expected_path)
    if sidecar_members["record.json"] != raw:
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification sidecar record differs from its main commit marker",
        )
    refs_by_role: dict[str, ArtifactRef] = {}
    for artifact in verification.artifacts:
        expected_filename = _VERIFICATION_MEMBER_BY_ROLE.get(artifact.role)
        if expected_filename is None or artifact.role in refs_by_role:
            raise ZoteroVerificationError(
                "verification_tampered",
                "verification sidecar artifact roles changed",
            )
        try:
            artifact_path, artifact_bytes = verify_artifact_ref(
                run_dir,
                artifact,
                anchor=loaded.run_directory_anchor,
            )
        except LocalPublicationError as exc:
            raise ZoteroVerificationError(
                "verification_tampered",
                f"verification member changed: {artifact.path}: {exc}",
            ) from exc
        if (
            artifact_path != sidecar_dir / expected_filename
            or artifact_bytes != sidecar_members[expected_filename]
        ):
            raise ZoteroVerificationError(
                "verification_tampered",
                "verification refs do not bind the exact closed sidecar members",
            )
        refs_by_role[artifact.role] = artifact
    if set(refs_by_role) != set(_VERIFICATION_MEMBER_BY_ROLE):
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification sidecar artifact roles changed",
        )
    if (
        verification.authorization
        != refs_by_role["authorization_snapshot"]
        or verification.note_snapshot
        != refs_by_role["zotero_note_readback"]
        or verification.checks_snapshot
        != refs_by_role["verification_checks"]
    ):
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification named refs do not match their exact immutable sidecar roles",
        )
    if sidecar_members["authorization.json"] != bound.authorization_bytes:
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification authorization snapshot differs from the bound authorization",
        )
    try:
        note_snapshot = json.loads(sidecar_members["note.json"])
        if (
            not isinstance(note_snapshot, dict)
            or canonical_json_bytes(note_snapshot) != sidecar_members["note.json"]
        ):
            raise ValueError("note snapshot is not one canonical JSON object")
        evaluation = evaluate_note_snapshot(
            note_snapshot,
            authorization=bound.authorization,
            note_key=note_key,
        )
    except (TypeError, ValueError) as exc:
        raise ZoteroVerificationError(
            "verification_tampered",
            f"verification note snapshot cannot be reevaluated: {exc}",
        ) from exc
    expected_checks_payload = canonical_json_bytes(
        {
            "format": "paper_reader.verification-checks.v2-internal",
            "authorization_digest": bound.authorization_digest,
            "note_key": note_key,
            "checks": [item.model_dump(mode="json") for item in evaluation.checks],
        }
    )
    expected_gate = _verification_gate(evaluation)
    if (
        sidecar_members["checks.json"] != expected_checks_payload
        or verification.verified != evaluation.verified
        or verification.content_sha256 != evaluation.content_sha256
        or verification.content_length != evaluation.content_length
        or verification.checks != evaluation.checks
        or verification.gate.status != expected_gate.status
        or verification.gate.checks != expected_gate.checks
        or verification.gate.blockers != expected_gate.blockers
    ):
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification derived fields do not match its immutable snapshots",
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
    anchor = loaded.run_directory_anchor
    if anchor is None:
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification recovery requires a locked run anchor",
        )
    try:
        main_exists = anchored_entry_exists(anchor, verification_path)
        record_exists = anchored_entry_exists(anchor, recovery_record)
        sidecar_exists = anchored_entry_exists(anchor, verification_dir)
        if not main_exists and not record_exists:
            if sidecar_exists:
                raise ZoteroVerificationError(
                    "verification_tampered",
                    "verification sidecar exists without its record commit marker",
                )
            return None
        raw = read_anchored_bytes(
            anchor,
            verification_path if main_exists else recovery_record,
            max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
        )
    except ZoteroVerificationError:
        raise
    except (OSError, ValueError) as exc:
        raise ZoteroVerificationError(
            "verification_tampered",
            "verification commit candidate cannot be inspected safely",
        ) from exc
    verification = _validate_verification_record(
        run_dir,
        verification_path,
        raw,
        bound=bound,
        note_key=note_key,
        loaded=loaded,
    )
    if not main_exists:
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
        recovered_members = _read_closed_verification_sidecar(loaded, verification_path)
        try:
            recovered_main = read_anchored_bytes(
                anchor,
                verification_path,
                expected_size=len(raw),
                max_bytes=len(raw),
            )
        except (OSError, ValueError) as exc:
            raise ZoteroVerificationError(
                "verification_tampered",
                "recovered verification main cannot be inspected safely",
            ) from exc
        if recovered_members["record.json"] != raw or recovered_main != raw:
            raise ZoteroVerificationError(
                "verification_tampered",
                "recovered verification tree differs from the validated record",
            )
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
        verification_ref = ArtifactRef(
            role="zotero_verification",
            path=relative,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            media_type="application/json",
        )
        try:
            terminal_members = _read_closed_verification_sidecar(
                loaded,
                verification_path,
            )
            with open_bound_authorization_guard(
                loaded,
                bound,
            ) as authorization_guard, open_terminal_artifact_guard(
                anchor,
                main_path=verification_path,
                main_bytes=raw,
                sidecar_path=verification_dir,
                sidecar_snapshot=tree_snapshot_from_bytes(terminal_members),
                label="recovered verification terminal",
            ) as terminal_guard:
                revalidated = _validate_verification_record(
                    run_dir,
                    verification_path,
                    raw,
                    bound=bound,
                    note_key=note_key,
                    loaded=loaded,
                )
                if revalidated != verification:
                    raise ZoteroVerificationError(
                        "verification_tampered",
                        "recovered verification changed between validation and binding",
                    )
                updated_run = _updated_run(
                    loaded.run,
                    verification_ref=verification_ref,
                    gate=revalidated.gate,
                    verified=revalidated.verified,
                )
                authorization_guard.verify()
                terminal_guard.verify()
                cas_update_run(
                    loaded,
                    updated_run,
                    finalization_guards=(authorization_guard, terminal_guard),
                )
                terminal_guard.verify()
                authorization_guard.verify()
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
                retained_replacement_paths=(loaded.manifest_path,),
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
            with open_bound_authorization_guard(
                loaded,
                bound,
            ) as authorization_guard, open_terminal_artifact_guard(
                run_anchor,
                main_path=verification_path,
                main_bytes=verification_bytes,
                sidecar_path=verification_dir,
                sidecar_snapshot=sidecar_snapshot,
                label="verification terminal",
            ) as terminal_guard:
                cas_update_run(
                    loaded,
                    updated_run,
                    finalization_guards=(authorization_guard, terminal_guard),
                )
                terminal_guard.verify()
                authorization_guard.verify()
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


def _preflight_verification_authorization(
    authorization_input: Path,
) -> InspectedAuthorization:
    try:
        return preflight_authorization_schema_versions(authorization_input)
    except ZoteroAuthorizationBindingError as exc:
        raise ZoteroVerificationError(exc.code, str(exc), data=exc.data) from exc


def _preflight_existing_verification_schema(
    inspection: InspectedAuthorization,
    *,
    note_key: str,
) -> None:
    artifact_paths = _verification_artifact_paths(
        inspection.run_dir,
        inspection.authorization.authorization_id,
        note_key,
        allow_existing_sidecar=True,
        allow_existing_main=True,
    )
    record_path = artifact_paths.sidecar / "record.json"
    with DirectoryAnchor.open(
        inspection.run_dir,
        manifest_path=inspection.run_dir / "run.json",
    ) as anchor:
        if (anchor.device, anchor.inode) != (
            inspection.run_directory_device,
            inspection.run_directory_inode,
        ):
            raise ZoteroVerificationError(
                "run_directory_changed",
                "verification run changed during terminal schema preflight",
            )
        main_exists = anchored_entry_exists(anchor, artifact_paths.main)
        record_exists = anchored_entry_exists(anchor, record_path)
        if not main_exists and not record_exists:
            return
        existing_records = tuple(
            path
            for path, exists in (
                (artifact_paths.main, main_exists),
                (record_path, record_exists),
            )
            if exists
        )
        for selected_path in existing_records:
            try:
                raw = read_anchored_bytes(
                    anchor,
                    selected_path,
                    max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                )
            except (OSError, ValueError) as exc:
                raise ZoteroVerificationError(
                    "verification_tampered",
                    "verification terminal cannot be inspected before lock acquisition",
                ) from exc
            require_raw_schema_version(
                raw,
                expected="paper_reader.verification.v2",
                artifact_path=selected_path,
            )
        validate_directory_anchor(anchor)


def _refresh_verification_inspection(
    authorization_input: Path,
    previous: InspectedAuthorization,
) -> InspectedAuthorization:
    refreshed = _preflight_verification_authorization(authorization_input)
    if (
        refreshed.run_dir != previous.run_dir
        or refreshed.run_directory_device != previous.run_directory_device
        or refreshed.run_directory_inode != previous.run_directory_inode
        or refreshed.skill_root != previous.skill_root
        or refreshed.skill_root_device != previous.skill_root_device
        or refreshed.skill_root_inode != previous.skill_root_inode
    ):
        raise ZoteroVerificationError(
            "run_directory_changed",
            "authorization run or skill root changed during verification retry",
        )
    if (
        refreshed.authorization_path != previous.authorization_path
        or refreshed.authorization_bytes != previous.authorization_bytes
        or refreshed.authorization != previous.authorization
        or refreshed.expected_artifacts != previous.expected_artifacts
    ):
        raise ZoteroVerificationError(
            "authorization_tampered",
            "authorization changed during verification retry",
        )
    return refreshed


def verify_zotero_authorization(
    authorization_input: Path,
    *,
    note_key: str,
    provider: ZoteroReadProvider | None = None,
) -> VerifiedZoteroNote:
    if not _PORTABLE_IDENTIFIER_RE.fullmatch(note_key):
        raise ZoteroVerificationError("invalid_note_key", "note_key is not a valid identifier")
    inspection = _preflight_verification_authorization(authorization_input)
    _preflight_existing_verification_schema(inspection, note_key=note_key)
    resolved_provider = provider or LocalApiZoteroReadProvider()
    try:
        return _verify_zotero_authorization_from_inspection(
            inspection,
            note_key=note_key,
            provider=resolved_provider,
        )
    except (RunLoadError, ZoteroVerificationError) as exc:
        if exc.code not in {"run_manifest_changed", "run_artifact_changed"}:
            raise
    refreshed = _refresh_verification_inspection(
        authorization_input,
        inspection,
    )
    return _verify_zotero_authorization_from_inspection(
        refreshed,
        note_key=note_key,
        provider=resolved_provider,
    )


def _verify_zotero_authorization_from_inspection(
    inspection: InspectedAuthorization,
    *,
    note_key: str,
    provider: ZoteroReadProvider,
) -> VerifiedZoteroNote:
    authorization_path = inspection.authorization_path
    inspected = inspection.authorization
    run_dir = inspection.run_dir
    _verification_artifact_paths(
        run_dir,
        inspected.authorization_id,
        note_key,
        allow_existing_sidecar=True,
        allow_existing_main=True,
    )
    with _locked_parent_for_inspection(inspection):
        with locked_v2_run(
            run_dir,
            expected_run_path=inspection.run_dir,
            expected_run_device=inspection.run_directory_device,
            expected_run_inode=inspection.run_directory_inode,
            expected_run_manifest_sha256=inspection.run_manifest_sha256,
            expected_artifacts=inspection.expected_artifacts,
        ) as loaded:
            if (
                loaded.run_directory_device,
                loaded.run_directory_inode,
            ) != (
                inspection.run_directory_device,
                inspection.run_directory_inode,
            ):
                raise ZoteroVerificationError(
                    "authorization_tampered",
                    "authorization run directory changed after read-only preflight",
                )
            if loaded.manifest_bytes != inspection.run_manifest_bytes:
                raise ZoteroVerificationError(
                    "run_directory_changed",
                    "authorization run manifest changed after read-only preflight",
                )
            try:
                bound = load_bound_authorization(loaded, authorization_path)
            except ZoteroAuthorizationBindingError as exc:
                raise ZoteroVerificationError(exc.code, str(exc), data=exc.data) from exc
            if bound.authorization_bytes != inspection.authorization_bytes:
                raise ZoteroVerificationError(
                    "authorization_tampered",
                    "authorization changed after read-only preflight",
                )
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
                snapshot = provider.get_note(note_key)
            except Exception as exc:
                raise ZoteroVerificationError(
                    "zotero_read_failed",
                    "read-only Zotero note readback failed",
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
