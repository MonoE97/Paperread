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
    PaperReaderReconciliation,
    PaperReaderRun,
    PaperReaderVerification,
)
from paper_reader.note_hash import note_html_sha256
from paper_reader.raw_schema import require_raw_schema_version
from paper_reader.resource_policy import V2_RESOURCE_POLICY
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
from paper_reader.zotero_candidate import _artifact_ref, _note_child_view
from paper_reader.zotero_lock import ZoteroLockError, locked_zotero_parent
from paper_reader.zotero_note_validation import evaluate_note_snapshot
from paper_reader.zotero_read import LocalApiZoteroReadProvider, ZoteroReadProvider
from paper_reader.zotero_verification import _verification_gate


_PORTABLE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$")
_RECONCILIATION_MEMBER_BY_ROLE = {
    "authorization_snapshot": "authorization.json",
    "verification_checks": "checks.json",
    "zotero_children_snapshot": "children.json",
    "zotero_note_readback": "note.json",
    "reconciliation_verification": "verification.json",
}


class ZoteroReconciliationError(LocalPublicationError):
    pass


def _read_closed_reconciliation_sidecar(
    loaded: LoadedRun,
    reconciliation_path: Path,
    *,
    expected_roles: set[str],
) -> dict[str, bytes]:
    anchor = loaded.run_directory_anchor
    if anchor is None:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation sidecar validation requires a locked run anchor",
        )
    sidecar = reconciliation_path.with_suffix("")
    expected_names = tuple(
        sorted(
            (
                *(
                    _RECONCILIATION_MEMBER_BY_ROLE[role]
                    for role in expected_roles
                ),
                "record.json",
            )
        )
    )
    try:
        with open_anchored_directory(anchor, sidecar) as sidecar_anchor:
            before_names = tuple(sorted(os.listdir(sidecar_anchor.descriptor)))
            if before_names != expected_names:
                raise ZoteroReconciliationError(
                    "reconciliation_tampered",
                    "reconciliation sidecar membership is not the exact closed set",
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
                for name in expected_names
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
                raise ZoteroReconciliationError(
                    "reconciliation_tampered",
                    "reconciliation sidecar changed while it was inspected",
                )
            validate_directory_anchor(sidecar_anchor)
        validate_directory_anchor(anchor)
    except ZoteroReconciliationError:
        raise
    except (OSError, ValueError) as exc:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation sidecar cannot be inspected safely",
        ) from exc
    return members


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
        raise ZoteroReconciliationError(exc.code, str(exc)) from exc


def _reconciliation_artifact_paths(
    run_dir: Path,
    authorization_id: str,
    *,
    allow_existing_sidecar: bool,
    allow_existing_main: bool,
) -> DeterministicArtifactPaths:
    try:
        return inspect_deterministic_artifact_paths(
            run_dir,
            root_name="reconciliations",
            parent_parts=(),
            stem=authorization_id,
            allow_existing_sidecar=allow_existing_sidecar,
            allow_existing_main=allow_existing_main,
        )
    except UnsafeZoteroArtifactPathError as exc:
        raise ZoteroReconciliationError(exc.code, str(exc), data=exc.data) from exc


@dataclass(frozen=True, slots=True)
class ReconciledZoteroWrite:
    run_dir: Path
    reconciliation_path: Path
    reconciliation_dir: Path
    reconciliation: PaperReaderReconciliation
    authorization_digest: str
    replayed: bool = False


def _validate_reconciliation_record(
    run_dir: Path,
    path: Path,
    raw: bytes,
    *,
    bound: LoadedAuthorization,
    loaded: LoadedRun,
) -> PaperReaderReconciliation:
    require_raw_schema_version(
        raw,
        expected="paper_reader.reconciliation.v2",
        artifact_path=path,
    )
    try:
        reconciliation = PaperReaderReconciliation.model_validate_json(raw)
    except ValidationError as exc:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            f"reconciliation failed strict validation: {exc}",
        ) from exc
    expected_path = (
        run_dir
        / "reconciliations"
        / f"{bound.authorization.authorization_id}.json"
    )
    if (
        path.resolve(strict=False) != expected_path.resolve(strict=False)
        or canonical_json_bytes(reconciliation) != raw
        or reconciliation.run_id != bound.authorization.run_id
        or reconciliation.authorization_digest != bound.authorization_digest
        or reconciliation.target != bound.authorization.target
    ):
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation main artifact identity or binding changed",
        )
    sidecar_dir = expected_path.with_suffix("")
    required = {"authorization_snapshot", "zotero_children_snapshot"}
    if reconciliation.verification is not None:
        required |= {
            "zotero_note_readback",
            "verification_checks",
            "reconciliation_verification",
        }
    sidecar_members = _read_closed_reconciliation_sidecar(
        loaded,
        expected_path,
        expected_roles=required,
    )
    if sidecar_members["record.json"] != raw:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation sidecar record differs from its main commit marker",
        )
    refs_by_role: dict[str, ArtifactRef] = {}
    for artifact in reconciliation.artifacts:
        expected_filename = _RECONCILIATION_MEMBER_BY_ROLE.get(artifact.role)
        if expected_filename is None or artifact.role in refs_by_role:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                "reconciliation sidecar artifact roles changed",
            )
        try:
            artifact_path, artifact_bytes = verify_artifact_ref(
                run_dir,
                artifact,
                anchor=loaded.run_directory_anchor,
            )
        except LocalPublicationError as exc:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                f"reconciliation member changed: {artifact.path}: {exc}",
            ) from exc
        if (
            artifact_path != sidecar_dir / expected_filename
            or artifact_bytes != sidecar_members[expected_filename]
        ):
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                "reconciliation refs do not bind the exact closed sidecar members",
            )
        refs_by_role[artifact.role] = artifact
    if set(refs_by_role) != required:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation sidecar artifact roles changed",
        )
    if (
        reconciliation.authorization
        != refs_by_role["authorization_snapshot"]
        or reconciliation.children_snapshot
        != refs_by_role["zotero_children_snapshot"]
        or reconciliation.verification
        != refs_by_role.get("reconciliation_verification")
    ):
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation named refs do not match their exact immutable sidecar roles",
        )
    if sidecar_members["authorization.json"] != bound.authorization_bytes:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation authorization snapshot differs from the bound authorization",
        )
    try:
        children = json.loads(sidecar_members["children.json"])
        if (
            not isinstance(children, list)
            or not all(isinstance(item, dict) for item in children)
            or canonical_json_bytes(children) != sidecar_members["children.json"]
        ):
            raise ValueError("children snapshot is not one canonical object array")
        matched_note_keys = _locate_exact_matches(children, bound=bound)
    except (TypeError, ValueError) as exc:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            f"reconciliation children snapshot cannot be reevaluated: {exc}",
        ) from exc
    match_count = len(matched_note_keys)
    if match_count == 0:
        expected_outcome = "not_found"
        expected_retry = True
        expected_gate = _location_gate(expected_outcome, match_count=0)
    elif match_count > 1:
        expected_outcome = "ambiguous"
        expected_retry = False
        expected_gate = _location_gate(expected_outcome, match_count=match_count)
    else:
        if reconciliation.verification is None:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                "unique reconciliation match is missing its full verification",
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
                note_key=matched_note_keys[0],
            )
            verification = PaperReaderVerification.model_validate_json(
                sidecar_members["verification.json"]
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                f"reconciliation verification cannot be reevaluated: {exc}",
            ) from exc
        expected_checks = canonical_json_bytes(
            {
                "format": "paper_reader.verification-checks.v2-internal",
                "authorization_digest": bound.authorization_digest,
                "note_key": matched_note_keys[0],
                "checks": [item.model_dump(mode="json") for item in evaluation.checks],
            }
        )
        expected_gate = _verification_gate(evaluation)
        outer_note_ref = refs_by_role["zotero_note_readback"]
        outer_checks_ref = refs_by_role["verification_checks"]
        outer_verification_ref = refs_by_role["reconciliation_verification"]
        expected_inner_artifacts = (
            refs_by_role["authorization_snapshot"],
            outer_note_ref,
            outer_checks_ref,
        )
        if (
            canonical_json_bytes(verification)
            != sidecar_members["verification.json"]
            or sidecar_members["checks.json"] != expected_checks
            or verification.run_id != bound.authorization.run_id
            or verification.authorization_digest != bound.authorization_digest
            or verification.target != bound.authorization.target
            or verification.note_key != matched_note_keys[0]
            or verification.verified != evaluation.verified
            or verification.content_sha256 != evaluation.content_sha256
            or verification.content_length != evaluation.content_length
            or verification.checks != evaluation.checks
            or verification.authorization
            != refs_by_role["authorization_snapshot"]
            or verification.note_snapshot != outer_note_ref
            or verification.checks_snapshot != outer_checks_ref
            or verification.artifacts != expected_inner_artifacts
            or reconciliation.verification != outer_verification_ref
            or verification.gate.status != expected_gate.status
            or verification.gate.checks != expected_gate.checks
            or verification.gate.blockers != expected_gate.blockers
        ):
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                "reconciliation verification fields do not match immutable snapshots",
            )
        expected_outcome = "verified" if evaluation.verified else "blocked"
        expected_retry = False
    if (
        reconciliation.match_count != match_count
        or reconciliation.matched_note_keys != matched_note_keys
        or reconciliation.outcome != expected_outcome
        or reconciliation.retry_confirmation_required != expected_retry
        or reconciliation.gate.status != expected_gate.status
        or reconciliation.gate.checks != expected_gate.checks
        or reconciliation.gate.blockers != expected_gate.blockers
        or (match_count == 1) != (reconciliation.verification is not None)
    ):
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation outcome does not match its immutable snapshots",
        )
    return reconciliation


def _existing_reconciliation(
    loaded: LoadedRun,
    *,
    bound: LoadedAuthorization,
) -> ReconciledZoteroWrite | None:
    run_dir = loaded.manifest_path.parent
    artifact_paths = _reconciliation_artifact_paths(
        run_dir,
        bound.authorization.authorization_id,
        allow_existing_sidecar=True,
        allow_existing_main=True,
    )
    reconciliation_path = artifact_paths.main
    reconciliation_dir = artifact_paths.sidecar
    recovery_record = reconciliation_dir / "record.json"
    anchor = loaded.run_directory_anchor
    if anchor is None:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation recovery requires a locked run anchor",
        )
    try:
        main_exists = anchored_entry_exists(anchor, reconciliation_path)
        record_exists = anchored_entry_exists(anchor, recovery_record)
        sidecar_exists = anchored_entry_exists(anchor, reconciliation_dir)
        if not main_exists and not record_exists:
            if sidecar_exists:
                raise ZoteroReconciliationError(
                    "reconciliation_tampered",
                    "reconciliation sidecar exists without its record commit marker",
                )
            return None
        raw = read_anchored_bytes(
            anchor,
            reconciliation_path if main_exists else recovery_record,
            max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
        )
    except ZoteroReconciliationError:
        raise
    except (OSError, ValueError) as exc:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation commit candidate cannot be inspected safely",
        ) from exc
    reconciliation = _validate_reconciliation_record(
        run_dir,
        reconciliation_path,
        raw,
        bound=bound,
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
            raise ZoteroReconciliationError(exc.code, str(exc), data=exc.data) from exc
        except Exception as exc:
            raise ZoteroReconciliationError(
                "reconciliation_recovery_failed",
                f"failed to restore exact reconciliation commit marker: {exc}",
            ) from exc
        recovered_members = _read_closed_reconciliation_sidecar(
            loaded,
            reconciliation_path,
            expected_roles={artifact.role for artifact in reconciliation.artifacts},
        )
        try:
            recovered_main = read_anchored_bytes(
                anchor,
                reconciliation_path,
                expected_size=len(raw),
                max_bytes=len(raw),
            )
        except (OSError, ValueError) as exc:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                "recovered reconciliation main cannot be inspected safely",
            ) from exc
        if recovered_members["record.json"] != raw or recovered_main != raw:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                "recovered reconciliation tree differs from the validated record",
            )
    relative = reconciliation_path.relative_to(run_dir).as_posix()
    refs = [
        item
        for item in loaded.run.artifacts
        if item.role == "zotero_reconciliation" and item.path == relative
    ]
    if len(refs) > 1:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "run binds the deterministic reconciliation more than once",
        )
    if refs:
        try:
            path, bound_raw = verify_artifact_ref(
                run_dir,
                refs[0],
                anchor=loaded.run_directory_anchor,
            )
        except LocalPublicationError as exc:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                f"bound reconciliation failed integrity validation: {exc}",
            ) from exc
        if path != reconciliation_path.resolve(strict=True) or bound_raw != raw:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                "run-bound reconciliation differs from the deterministic main artifact",
            )
    else:
        reconciliation_ref = ArtifactRef(
            role="zotero_reconciliation",
            path=relative,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            media_type="application/json",
        )
        try:
            terminal_members = _read_closed_reconciliation_sidecar(
                loaded,
                reconciliation_path,
                expected_roles={artifact.role for artifact in reconciliation.artifacts},
            )
            with open_bound_authorization_guard(
                loaded,
                bound,
            ) as authorization_guard, open_terminal_artifact_guard(
                anchor,
                main_path=reconciliation_path,
                main_bytes=raw,
                sidecar_path=reconciliation_dir,
                sidecar_snapshot=tree_snapshot_from_bytes(terminal_members),
                label="recovered reconciliation terminal",
            ) as terminal_guard:
                revalidated = _validate_reconciliation_record(
                    run_dir,
                    reconciliation_path,
                    raw,
                    bound=bound,
                    loaded=loaded,
                )
                if revalidated != reconciliation:
                    raise ZoteroReconciliationError(
                        "reconciliation_tampered",
                        "recovered reconciliation changed between validation and binding",
                    )
                updated_run = _updated_run(
                    loaded.run,
                    reconciliation_ref=reconciliation_ref,
                    gate=revalidated.gate,
                    verified=revalidated.outcome == "verified",
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
            raise ZoteroReconciliationError(
                "reconciliation_status_update_failed",
                f"reconciliation main artifact is durable but run binding failed: {exc}",
            ) from exc
    return ReconciledZoteroWrite(
        run_dir=run_dir,
        reconciliation_path=reconciliation_path,
        reconciliation_dir=reconciliation_dir,
        reconciliation=reconciliation,
        authorization_digest=bound.authorization_digest,
        replayed=True,
    )


def _captured_children(
    provider: ZoteroReadProvider,
    *,
    parent_key: str,
) -> tuple[list[dict[str, Any]], bytes]:
    try:
        children = provider.get_children(parent_key)
    except Exception as exc:
        raise ZoteroReconciliationError(
            "zotero_read_failed",
            "read-only Zotero reconciliation children read failed",
        ) from exc
    if not isinstance(children, list) or not all(isinstance(item, dict) for item in children):
        raise ZoteroReconciliationError(
            "invalid_live_snapshot",
            "read-only Zotero provider returned invalid children",
        )
    try:
        return children, canonical_json_bytes(children)
    except (TypeError, ValueError) as exc:
        raise ZoteroReconciliationError(
            "invalid_live_snapshot",
            f"Zotero children snapshot is not canonicalizable: {exc}",
        ) from exc


def _locate_exact_matches(
    children: list[dict[str, Any]],
    *,
    bound: LoadedAuthorization,
) -> tuple[str, ...]:
    matches: list[str] = []
    for child in children:
        view = _note_child_view(child)
        if view is None:
            continue
        key, parent_key, title = view
        data = child.get("data")
        assert isinstance(data, dict)
        note_html = str(data.get("note", ""))
        if (
            parent_key == bound.authorization.target.parent_key
            and title == bound.authorization.note_title
            and note_html_sha256(note_html) == bound.authorization.content_sha256
        ):
            if not _PORTABLE_IDENTIFIER_RE.fullmatch(key):
                raise ZoteroReconciliationError(
                    "invalid_live_snapshot",
                    "matching Zotero note key is not a portable identifier",
                )
            matches.append(key)
    return tuple(matches)


def _location_gate(outcome: str, *, match_count: int) -> GateState:
    if outcome == "not_found":
        blocker = GateBlocker(
            code="reconciliation_not_found",
            message="no exact parent+title+canonical-hash note was found; retry needs explicit confirmation",
        )
    else:
        blocker = GateBlocker(
            code="reconciliation_ambiguous",
            message=f"{match_count} exact parent+title+canonical-hash notes were found",
        )
    return GateState(
        status="blocked",
        evaluated_at=rfc3339_utc(),
        checks=("exact_parent_title_hash_locator",),
        blockers=(blocker,),
    )


def _updated_run(
    run: PaperReaderRun,
    *,
    reconciliation_ref: ArtifactRef,
    gate: GateState,
    verified: bool,
) -> PaperReaderRun:
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=run.run_id,
        created_at=run.created_at,
        source=run.source,
        target=run.target,
        status="published" if verified else "blocked",
        artifacts=(*run.artifacts, reconciliation_ref),
        gate=gate,
        live_preflight=run.live_preflight,
    )


def _publish_reconciliation_locked(
    loaded: LoadedRun,
    bound: LoadedAuthorization,
    *,
    provider: ZoteroReadProvider,
    children: list[dict[str, Any]],
    children_bytes: bytes,
    matched_note_keys: tuple[str, ...],
) -> ReconciledZoteroWrite:
    run_dir = bound.run_dir
    match_count = len(matched_note_keys)
    if match_count == 0:
        outcome = "not_found"
        retry_confirmation_required = True
        gate = _location_gate(outcome, match_count=0)
        note_snapshot = None
        note_bytes = None
        evaluation = None
    elif match_count > 1:
        outcome = "ambiguous"
        retry_confirmation_required = False
        gate = _location_gate(outcome, match_count=match_count)
        note_snapshot = None
        note_bytes = None
        evaluation = None
    else:
        note_key = matched_note_keys[0]
        try:
            note_snapshot = provider.get_note(note_key)
        except Exception as exc:
            raise ZoteroReconciliationError(
                "zotero_read_failed",
                "read-only Zotero reconciliation note read failed",
            ) from exc
        if not isinstance(note_snapshot, dict):
            raise ZoteroReconciliationError(
                "invalid_note_snapshot",
                "read-only Zotero provider returned a non-object note snapshot",
            )
        try:
            note_bytes = canonical_json_bytes(note_snapshot)
        except (TypeError, ValueError) as exc:
            raise ZoteroReconciliationError(
                "invalid_note_snapshot",
                f"Zotero note readback is not canonicalizable: {exc}",
            ) from exc
        evaluation = evaluate_note_snapshot(
            note_snapshot,
            authorization=bound.authorization,
            note_key=note_key,
        )
        outcome = "verified" if evaluation.verified else "blocked"
        retry_confirmation_required = False
        gate = _verification_gate(evaluation)

    reconciliation_id = new_random_id("reconciliation")
    artifact_paths = _reconciliation_artifact_paths(
        run_dir,
        bound.authorization.authorization_id,
        allow_existing_sidecar=False,
        allow_existing_main=False,
    )
    reconciliation_path = artifact_paths.main
    reconciliation_dir = artifact_paths.sidecar
    staging = run_dir / f".{reconciliation_id}.{new_uuid()}.staging"
    run_anchor = loaded.run_directory_anchor
    if run_anchor is None:
        raise ZoteroReconciliationError(
            "run_directory_changed",
            "reconciliation requires a locked run directory anchor",
        )
    staging_anchor = create_anchored_directory(run_anchor, staging)
    try:
        staged_sidecar = staging / "sidecar"
        files = {
            "authorization.json": bound.authorization_bytes,
            "children.json": children_bytes,
        }
        if note_bytes is not None and evaluation is not None:
            checks_payload = {
                "format": "paper_reader.verification-checks.v2-internal",
                "authorization_digest": bound.authorization_digest,
                "note_key": matched_note_keys[0],
                "checks": [item.model_dump(mode="json") for item in evaluation.checks],
            }
            files["note.json"] = note_bytes
            files["checks.json"] = canonical_json_bytes(checks_payload)
        for name, content in files.items():
            atomic_write_bytes(
                staged_sidecar / name,
                content,
                anchor=staging_anchor,
            )
        specs = {
            "authorization.json": ("authorization_snapshot", "application/json"),
            "children.json": ("zotero_children_snapshot", "application/json"),
        }
        if note_bytes is not None:
            specs["note.json"] = ("zotero_note_readback", "application/json")
            specs["checks.json"] = ("verification_checks", "application/json")
        validate_directory_anchor(staging_anchor)
        refs = {
            name: _artifact_ref(
                run_dir,
                staged_sidecar / name,
                reconciliation_dir / name,
                role,
                media,
            )
            for name, (role, media) in specs.items()
        }
        validate_directory_anchor(staging_anchor)

        verification_ref: ArtifactRef | None = None
        verification_bytes: bytes | None = None
        if note_bytes is not None and evaluation is not None:
            verification_id = new_random_id("verification")
            verification = PaperReaderVerification(
                schema_version="paper_reader.verification.v2",
                verification_id=verification_id,
                run_id=loaded.run.run_id,
                created_at=rfc3339_utc(),
                authorization=refs["authorization.json"],
                authorization_digest=bound.authorization_digest,
                target=bound.authorization.target,
                note_key=matched_note_keys[0],
                verified=evaluation.verified,
                content_sha256=evaluation.content_sha256,
                content_length=evaluation.content_length,
                checks=evaluation.checks,
                note_snapshot=refs["note.json"],
                checks_snapshot=refs["checks.json"],
                artifacts=(
                    refs["authorization.json"],
                    refs["note.json"],
                    refs["checks.json"],
                ),
                gate=gate,
            )
            staged_verification_path = staged_sidecar / "verification.json"
            verification_bytes = canonical_json_bytes(verification)
            atomic_write_bytes(
                staged_verification_path,
                verification_bytes,
                anchor=staging_anchor,
            )
            verification_ref = _artifact_ref(
                run_dir,
                staged_verification_path,
                reconciliation_dir / "verification.json",
                "reconciliation_verification",
                "application/json",
            )
            refs["verification.json"] = verification_ref

        reconciliation = PaperReaderReconciliation(
            schema_version="paper_reader.reconciliation.v2",
            reconciliation_id=reconciliation_id,
            run_id=loaded.run.run_id,
            created_at=rfc3339_utc(),
            authorization=refs["authorization.json"],
            authorization_digest=bound.authorization_digest,
            target=bound.authorization.target,
            outcome=outcome,
            match_count=match_count,
            matched_note_keys=matched_note_keys,
            children_snapshot=refs["children.json"],
            verification=verification_ref,
            retry_confirmation_required=retry_confirmation_required,
            artifacts=tuple(refs.values()),
            gate=gate,
        )
        reconciliation_bytes = canonical_json_bytes(reconciliation)
        atomic_write_bytes(
            staged_sidecar / "record.json",
            reconciliation_bytes,
            anchor=staging_anchor,
        )
        staged_reconciliation_path = staging / f"{bound.authorization.authorization_id}.json"
        atomic_write_bytes(
            staged_reconciliation_path,
            reconciliation_bytes,
            anchor=staging_anchor,
        )
        sidecar_files = {**files, "record.json": reconciliation_bytes}
        if verification_bytes is not None:
            sidecar_files["verification.json"] = verification_bytes
        sidecar_snapshot = tree_snapshot_from_bytes(sidecar_files)
        reconciliation_ref = _artifact_ref(
            run_dir,
            staged_reconciliation_path,
            reconciliation_path,
            "zotero_reconciliation",
            "application/json",
        )
        updated_run = _updated_run(
            loaded.run,
            reconciliation_ref=reconciliation_ref,
            gate=gate,
            verified=outcome == "verified",
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
            raise ZoteroReconciliationError(
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
                    staged_reconciliation_path,
                    expected_bytes=reconciliation_bytes,
                )
        except UnsafeZoteroArtifactPathError as exc:
            raise ZoteroReconciliationError(exc.code, str(exc), data=exc.data) from exc
        except Exception as exc:
            raise ZoteroReconciliationError(
                "reconciliation_publication_failed",
                (
                    f"immutable reconciliation {publication_phase} publication failed: "
                    f"{reconciliation_path}: {exc}"
                ),
            ) from exc
        try:
            with open_bound_authorization_guard(
                loaded,
                bound,
            ) as authorization_guard, open_terminal_artifact_guard(
                run_anchor,
                main_path=reconciliation_path,
                main_bytes=reconciliation_bytes,
                sidecar_path=reconciliation_dir,
                sidecar_snapshot=sidecar_snapshot,
                label="reconciliation terminal",
            ) as terminal_guard:
                cas_update_run(
                    loaded,
                    updated_run,
                    finalization_guards=(authorization_guard, terminal_guard),
                )
                terminal_guard.verify()
                authorization_guard.verify()
        except Exception as exc:
            raise ZoteroReconciliationError(
                "reconciliation_status_update_failed",
                f"reconciliation tree is durable but run binding failed: {exc}",
            ) from exc
        return ReconciledZoteroWrite(
            run_dir=run_dir,
            reconciliation_path=reconciliation_path,
            reconciliation_dir=reconciliation_dir,
            reconciliation=reconciliation,
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


def _preflight_reconciliation_authorization(
    authorization_input: Path,
) -> InspectedAuthorization:
    try:
        return preflight_authorization_schema_versions(authorization_input)
    except ZoteroAuthorizationBindingError as exc:
        raise ZoteroReconciliationError(exc.code, str(exc), data=exc.data) from exc


def _preflight_existing_reconciliation_schema(
    inspection: InspectedAuthorization,
) -> None:
    artifact_paths = _reconciliation_artifact_paths(
        inspection.run_dir,
        inspection.authorization.authorization_id,
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
            raise ZoteroReconciliationError(
                "run_directory_changed",
                "reconciliation run changed during terminal schema preflight",
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
                raise ZoteroReconciliationError(
                    "reconciliation_tampered",
                    "reconciliation terminal cannot be inspected before lock acquisition",
                ) from exc
            require_raw_schema_version(
                raw,
                expected="paper_reader.reconciliation.v2",
                artifact_path=selected_path,
            )
        validate_directory_anchor(anchor)


def _refresh_reconciliation_inspection(
    authorization_input: Path,
    previous: InspectedAuthorization,
) -> InspectedAuthorization:
    refreshed = _preflight_reconciliation_authorization(authorization_input)
    if (
        refreshed.run_dir != previous.run_dir
        or refreshed.run_directory_device != previous.run_directory_device
        or refreshed.run_directory_inode != previous.run_directory_inode
        or refreshed.skill_root != previous.skill_root
        or refreshed.skill_root_device != previous.skill_root_device
        or refreshed.skill_root_inode != previous.skill_root_inode
    ):
        raise ZoteroReconciliationError(
            "run_directory_changed",
            "authorization run or skill root changed during reconciliation retry",
        )
    if (
        refreshed.authorization_path != previous.authorization_path
        or refreshed.authorization_bytes != previous.authorization_bytes
        or refreshed.authorization != previous.authorization
        or refreshed.expected_artifacts != previous.expected_artifacts
    ):
        raise ZoteroReconciliationError(
            "authorization_tampered",
            "authorization changed during reconciliation retry",
        )
    return refreshed


def reconcile_zotero_authorization(
    authorization_input: Path,
    *,
    provider: ZoteroReadProvider | None = None,
) -> ReconciledZoteroWrite:
    inspection = _preflight_reconciliation_authorization(authorization_input)
    _preflight_existing_reconciliation_schema(inspection)
    resolved_provider = provider or LocalApiZoteroReadProvider()
    try:
        return _reconcile_zotero_authorization_from_inspection(
            inspection,
            provider=resolved_provider,
        )
    except (RunLoadError, ZoteroReconciliationError) as exc:
        if exc.code not in {"run_manifest_changed", "run_artifact_changed"}:
            raise
    refreshed = _refresh_reconciliation_inspection(
        authorization_input,
        inspection,
    )
    return _reconcile_zotero_authorization_from_inspection(
        refreshed,
        provider=resolved_provider,
    )


def _reconcile_zotero_authorization_from_inspection(
    inspection: InspectedAuthorization,
    *,
    provider: ZoteroReadProvider,
) -> ReconciledZoteroWrite:
    authorization_path = inspection.authorization_path
    inspected = inspection.authorization
    run_dir = inspection.run_dir
    _reconciliation_artifact_paths(
        run_dir,
        inspected.authorization_id,
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
                raise ZoteroReconciliationError(
                    "authorization_tampered",
                    "authorization run directory changed after read-only preflight",
                )
            if loaded.manifest_bytes != inspection.run_manifest_bytes:
                raise ZoteroReconciliationError(
                    "run_directory_changed",
                    "authorization run manifest changed after read-only preflight",
                )
            try:
                bound = load_bound_authorization(loaded, authorization_path)
            except ZoteroAuthorizationBindingError as exc:
                raise ZoteroReconciliationError(exc.code, str(exc), data=exc.data) from exc
            if bound.authorization_bytes != inspection.authorization_bytes:
                raise ZoteroReconciliationError(
                    "authorization_tampered",
                    "authorization changed after read-only preflight",
                )
            replay = _existing_reconciliation(
                loaded,
                bound=bound,
            )
            if replay is not None:
                return replay
            _reconciliation_artifact_paths(
                run_dir,
                bound.authorization.authorization_id,
                allow_existing_sidecar=False,
                allow_existing_main=False,
            )
            children, children_bytes = _captured_children(
                provider,
                parent_key=bound.authorization.target.parent_key,
            )
            matched_note_keys = _locate_exact_matches(children, bound=bound)
            return _publish_reconciliation_locked(
                loaded,
                bound,
                provider=provider,
                children=children,
                children_bytes=children_bytes,
                matched_note_keys=matched_note_keys,
            )


__all__ = [
    "ReconciledZoteroWrite",
    "ZoteroReconciliationError",
    "reconcile_zotero_authorization",
]
