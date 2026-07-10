from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
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
    authorization_manifest_path,
    inspect_authorization_target,
    load_bound_authorization,
)
from paper_reader.zotero_candidate import _artifact_ref
from paper_reader.zotero_lock import locked_zotero_parent
from paper_reader.zotero_note_validation import NoteEvaluation, evaluate_note_snapshot
from paper_reader.zotero_read import LocalApiZoteroReadProvider, ZoteroReadProvider


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class ZoteroVerificationError(LocalPublicationError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedZoteroNote:
    run_dir: Path
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


def _existing_verification(
    loaded: LoadedRun,
    *,
    authorization_digest: str,
    note_key: str,
) -> VerifiedZoteroNote | None:
    run_dir = loaded.manifest_path.resolve(strict=True).parent
    for ref in (item for item in loaded.run.artifacts if item.role == "zotero_verification"):
        try:
            path, raw = verify_artifact_ref(run_dir, ref)
            verification = PaperReaderVerification.model_validate_json(raw)
        except (LocalPublicationError, ValidationError) as exc:
            raise ZoteroVerificationError(
                "verification_tampered",
                f"bound verification failed integrity validation: {ref.path}: {exc}",
            ) from exc
        if canonical_json_bytes(verification) != raw:
            raise ZoteroVerificationError(
                "verification_tampered",
                f"bound verification is not canonical: {ref.path}",
            )
        if (
            verification.authorization_digest == authorization_digest
            and verification.note_key == note_key
        ):
            for artifact in verification.artifacts:
                try:
                    artifact_path, _artifact_bytes = verify_artifact_ref(run_dir, artifact)
                except LocalPublicationError as exc:
                    raise ZoteroVerificationError(
                        "verification_tampered",
                        f"bound verification member changed: {artifact.path}: {exc}",
                    ) from exc
                if not artifact_path.is_relative_to(path.parent):
                    raise ZoteroVerificationError(
                        "verification_tampered",
                        "bound verification member escapes its immutable tree",
                    )
            return VerifiedZoteroNote(
                run_dir=run_dir,
                verification_dir=path.parent,
                verification=verification,
                authorization_digest=authorization_digest,
                replayed=True,
            )
    return None


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
    verification_dir = run_dir / "verifications" / verification_id
    staging = run_dir / f".{verification_id}.{new_uuid()}.staging"
    staging.mkdir()
    try:
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
            (staging / name).write_bytes(content)
        specs = {
            "authorization.json": ("authorization_snapshot", "application/json"),
            "note.json": ("zotero_note_readback", "application/json"),
            "checks.json": ("verification_checks", "application/json"),
        }
        refs = {
            name: _artifact_ref(
                run_dir,
                staging / name,
                verification_dir / name,
                role,
                media,
            )
            for name, (role, media) in specs.items()
        }
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
        staged_verification_path = staging / "verification.json"
        staged_verification_path.write_bytes(verification_bytes)
        verification_path = verification_dir / "verification.json"
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
        try:
            atomic_publish_tree(staging, verification_dir)
        except Exception as exc:
            raise ZoteroVerificationError(
                "verification_publication_failed",
                f"immutable verification publication failed: {verification_dir}: {exc}",
            ) from exc
        try:
            atomic_write_json(loaded.manifest_path, updated_run)
        except Exception as exc:
            raise ZoteroVerificationError(
                "verification_status_update_failed",
                f"verification tree is durable but run binding failed: {exc}",
            ) from exc
        return VerifiedZoteroNote(
            run_dir=run_dir,
            verification_dir=verification_dir,
            verification=verification,
            authorization_digest=bound.authorization_digest,
            replayed=False,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def verify_zotero_authorization(
    authorization_input: Path,
    *,
    note_key: str,
    provider: ZoteroReadProvider | None = None,
) -> VerifiedZoteroNote:
    if not _IDENTIFIER_RE.fullmatch(note_key):
        raise ZoteroVerificationError("invalid_note_key", "note_key is not a valid identifier")
    try:
        authorization_path, inspected, run_dir = inspect_authorization_target(
            authorization_input
        )
    except ZoteroAuthorizationBindingError as exc:
        raise ZoteroVerificationError(exc.code, str(exc), data=exc.data) from exc
    resolved_provider = provider or LocalApiZoteroReadProvider()
    with locked_zotero_parent(run_dir, inspected.target.parent_key):
        with locked_v2_run(run_dir) as loaded:
            try:
                bound = load_bound_authorization(loaded, authorization_path)
            except ZoteroAuthorizationBindingError as exc:
                raise ZoteroVerificationError(exc.code, str(exc), data=exc.data) from exc
            replay = _existing_verification(
                loaded,
                authorization_digest=bound.authorization_digest,
                note_key=note_key,
            )
            if replay is not None:
                return replay
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
