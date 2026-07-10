from __future__ import annotations

import hashlib
import re
import shutil
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
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.run_lock import locked_v2_run
from paper_reader.run_size import RunSizeLimitError, enforce_projected_run_size
from paper_reader.storage import (
    atomic_write_json,
    canonical_json_bytes,
    new_random_id,
    new_uuid,
    rfc3339_utc,
)
from paper_reader.v2_loader import LoadedRun
from paper_reader.zotero_authorization_loader import (
    LoadedAuthorization,
    ZoteroAuthorizationBindingError,
    inspect_authorization_target,
    load_bound_authorization,
)
from paper_reader.zotero_artifact_paths import (
    DeterministicArtifactPaths,
    UnsafeZoteroArtifactPathError,
    anchored_artifact_publication,
    inspect_deterministic_artifact_paths,
)
from paper_reader.zotero_candidate import _artifact_ref, _note_child_view
from paper_reader.zotero_lock import locked_zotero_parent
from paper_reader.zotero_note_validation import evaluate_note_snapshot
from paper_reader.zotero_read import LocalApiZoteroReadProvider, ZoteroReadProvider
from paper_reader.zotero_verification import _verification_gate


_PORTABLE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$")


class ZoteroReconciliationError(LocalPublicationError):
    pass


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
) -> PaperReaderReconciliation:
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
    roles: set[str] = set()
    for artifact in reconciliation.artifacts:
        try:
            artifact_path, _artifact_bytes = verify_artifact_ref(run_dir, artifact)
        except LocalPublicationError as exc:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                f"reconciliation member changed: {artifact.path}: {exc}",
            ) from exc
        if not artifact_path.is_relative_to(sidecar_dir) or artifact.role in roles:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                "reconciliation sidecar membership is not closed and unique",
            )
        roles.add(artifact.role)
    required = {"authorization_snapshot", "zotero_children_snapshot"}
    if reconciliation.verification is not None:
        required |= {
            "zotero_note_readback",
            "verification_checks",
            "reconciliation_verification",
        }
    if roles != required:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation sidecar artifact roles changed",
        )
    if (
        reconciliation.authorization not in reconciliation.artifacts
        or reconciliation.children_snapshot not in reconciliation.artifacts
        or (
            reconciliation.verification is not None
            and reconciliation.verification not in reconciliation.artifacts
        )
    ):
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            "reconciliation refs are not members of the immutable sidecar",
        )
    return reconciliation


def _existing_reconciliation(
    loaded: LoadedRun,
    *,
    bound: LoadedAuthorization,
) -> ReconciledZoteroWrite | None:
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    artifact_paths = _reconciliation_artifact_paths(
        run_dir,
        bound.authorization.authorization_id,
        allow_existing_sidecar=True,
        allow_existing_main=True,
    )
    reconciliation_path = artifact_paths.main
    reconciliation_dir = artifact_paths.sidecar
    recovery_record = reconciliation_dir / "record.json"
    if not reconciliation_path.exists() and not recovery_record.exists():
        return None
    try:
        raw = (
            reconciliation_path.read_bytes()
            if reconciliation_path.exists()
            else recovery_record.read_bytes()
        )
    except OSError as exc:
        raise ZoteroReconciliationError(
            "reconciliation_tampered",
            f"reconciliation commit candidate is unreadable: {exc}",
        ) from exc
    reconciliation = _validate_reconciliation_record(
        run_dir,
        reconciliation_path,
        raw,
        bound=bound,
    )
    if not reconciliation_path.exists():
        try:
            with anchored_artifact_publication(
                artifact_paths,
                staging_dir=None,
                allow_existing_sidecar=True,
                allow_existing_main=False,
            ) as publication:
                publication.publish_main(recovery_record, expected_bytes=raw)
        except UnsafeZoteroArtifactPathError as exc:
            raise ZoteroReconciliationError(exc.code, str(exc), data=exc.data) from exc
        except Exception as exc:
            raise ZoteroReconciliationError(
                "reconciliation_recovery_failed",
                f"failed to restore exact reconciliation commit marker: {exc}",
            ) from exc
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
            path, bound_raw = verify_artifact_ref(run_dir, refs[0])
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
        reconciliation_ref = _artifact_ref(
            run_dir,
            reconciliation_path,
            reconciliation_path,
            "zotero_reconciliation",
            "application/json",
        )
        updated_run = _updated_run(
            loaded.run,
            reconciliation_ref=reconciliation_ref,
            gate=reconciliation.gate,
            verified=reconciliation.outcome == "verified",
        )
        try:
            atomic_write_json(loaded.manifest_path, updated_run)
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
            f"read-only Zotero reconciliation children read failed: {exc}",
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
                f"read-only Zotero reconciliation note read failed: {exc}",
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
    staging.mkdir()
    try:
        staged_sidecar = staging / "sidecar"
        staged_sidecar.mkdir()
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
            (staged_sidecar / name).write_bytes(content)
        specs = {
            "authorization.json": ("authorization_snapshot", "application/json"),
            "children.json": ("zotero_children_snapshot", "application/json"),
        }
        if note_bytes is not None:
            specs["note.json"] = ("zotero_note_readback", "application/json")
            specs["checks.json"] = ("verification_checks", "application/json")
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

        verification_ref: ArtifactRef | None = None
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
            staged_verification_path.write_bytes(canonical_json_bytes(verification))
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
        (staged_sidecar / "record.json").write_bytes(reconciliation_bytes)
        staged_reconciliation_path = staging / f"{bound.authorization.authorization_id}.json"
        staged_reconciliation_path.write_bytes(reconciliation_bytes)
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
            atomic_write_json(loaded.manifest_path, updated_run)
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
        if staging.exists():
            shutil.rmtree(staging)


def reconcile_zotero_authorization(
    authorization_input: Path,
    *,
    provider: ZoteroReadProvider | None = None,
) -> ReconciledZoteroWrite:
    try:
        authorization_path, inspected, run_dir = inspect_authorization_target(
            authorization_input
        )
    except ZoteroAuthorizationBindingError as exc:
        raise ZoteroReconciliationError(exc.code, str(exc), data=exc.data) from exc
    _reconciliation_artifact_paths(
        run_dir,
        inspected.authorization_id,
        allow_existing_sidecar=True,
        allow_existing_main=True,
    )
    resolved_provider = provider or LocalApiZoteroReadProvider()
    with locked_zotero_parent(run_dir, inspected.target.parent_key):
        with locked_v2_run(run_dir) as loaded:
            try:
                bound = load_bound_authorization(loaded, authorization_path)
            except ZoteroAuthorizationBindingError as exc:
                raise ZoteroReconciliationError(exc.code, str(exc), data=exc.data) from exc
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
                resolved_provider,
                parent_key=bound.authorization.target.parent_key,
            )
            matched_note_keys = _locate_exact_matches(children, bound=bound)
            return _publish_reconciliation_locked(
                loaded,
                bound,
                provider=resolved_provider,
                children=children,
                children_bytes=children_bytes,
                matched_note_keys=matched_note_keys,
            )


__all__ = [
    "ReconciledZoteroWrite",
    "ZoteroReconciliationError",
    "reconcile_zotero_authorization",
]
