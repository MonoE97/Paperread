from __future__ import annotations

import hashlib
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
    atomic_publish_tree,
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
from paper_reader.zotero_candidate import _artifact_ref, _note_child_view
from paper_reader.zotero_lock import locked_zotero_parent
from paper_reader.zotero_note_validation import evaluate_note_snapshot
from paper_reader.zotero_read import LocalApiZoteroReadProvider, ZoteroReadProvider
from paper_reader.zotero_verification import _verification_gate


class ZoteroReconciliationError(LocalPublicationError):
    pass


@dataclass(frozen=True, slots=True)
class ReconciledZoteroWrite:
    run_dir: Path
    reconciliation_dir: Path
    reconciliation: PaperReaderReconciliation
    authorization_digest: str
    replayed: bool = False


def _existing_reconciliation(
    loaded: LoadedRun,
    *,
    authorization_digest: str,
) -> ReconciledZoteroWrite | None:
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    matches: list[tuple[Path, PaperReaderReconciliation]] = []
    for ref in (item for item in loaded.run.artifacts if item.role == "zotero_reconciliation"):
        try:
            path, raw = verify_artifact_ref(run_dir, ref)
            reconciliation = PaperReaderReconciliation.model_validate_json(raw)
        except (LocalPublicationError, ValidationError) as exc:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                f"bound reconciliation failed strict validation: {ref.path}: {exc}",
            ) from exc
        if canonical_json_bytes(reconciliation) != raw:
            raise ZoteroReconciliationError(
                "reconciliation_tampered",
                f"bound reconciliation is not canonical: {ref.path}",
            )
        for artifact in reconciliation.artifacts:
            try:
                artifact_path, _artifact_bytes = verify_artifact_ref(run_dir, artifact)
            except LocalPublicationError as exc:
                raise ZoteroReconciliationError(
                    "reconciliation_tampered",
                    f"bound reconciliation member changed: {artifact.path}: {exc}",
                ) from exc
            if not artifact_path.is_relative_to(path.parent):
                raise ZoteroReconciliationError(
                    "reconciliation_tampered",
                    "bound reconciliation member escapes its immutable tree",
                )
        if reconciliation.authorization_digest == authorization_digest:
            matches.append((path, reconciliation))
    if len(matches) > 1:
        raise ZoteroReconciliationError(
            "reconciliation_conflict",
            "authorization has multiple bound terminal reconciliations",
        )
    if not matches:
        return None
    path, reconciliation = matches[0]
    return ReconciledZoteroWrite(
        run_dir=run_dir,
        reconciliation_dir=path.parent,
        reconciliation=reconciliation,
        authorization_digest=authorization_digest,
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
    reconciliation_dir = run_dir / "reconciliations" / reconciliation_id
    staging = run_dir / f".{reconciliation_id}.{new_uuid()}.staging"
    staging.mkdir()
    try:
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
            (staging / name).write_bytes(content)
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
                staging / name,
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
            staged_verification_path = staging / "verification.json"
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
        staged_reconciliation_path = staging / "reconciliation.json"
        staged_reconciliation_path.write_bytes(canonical_json_bytes(reconciliation))
        reconciliation_path = reconciliation_dir / "reconciliation.json"
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
        try:
            atomic_publish_tree(staging, reconciliation_dir)
        except Exception as exc:
            raise ZoteroReconciliationError(
                "reconciliation_publication_failed",
                f"immutable reconciliation publication failed: {reconciliation_dir}: {exc}",
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
    resolved_provider = provider or LocalApiZoteroReadProvider()
    with locked_zotero_parent(run_dir, inspected.target.parent_key):
        with locked_v2_run(run_dir) as loaded:
            try:
                bound = load_bound_authorization(loaded, authorization_path)
            except ZoteroAuthorizationBindingError as exc:
                raise ZoteroReconciliationError(exc.code, str(exc), data=exc.data) from exc
            replay = _existing_reconciliation(
                loaded,
                authorization_digest=bound.authorization_digest,
            )
            if replay is not None:
                return replay
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
