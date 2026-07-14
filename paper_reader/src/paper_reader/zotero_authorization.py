from __future__ import annotations

import hashlib
import os
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from paper_reader.candidate_integrity import (
    LocalPublicationError,
    candidate_manifest_path,
    verify_artifact_ref,
    verify_local_source,
)
from paper_reader.contracts import (
    ArtifactRef,
    GateState,
    Identifier,
    LivePreflight,
    McpWriteEnvelope,
    PaperReaderCandidate,
    PaperReaderRun,
    PaperReaderWriteAuthorization,
    ZoteroPublicationTarget,
    ZoteroSourceIdentity,
)
from paper_reader.local_publish import (
    _CandidateTreeGuard,
    _candidate_run_dir,
    _load_candidate,
    _open_original_evidence_tree_guard,
    revalidate_candidate_original_evidence,
)
from paper_reader.note import FORBIDDEN_RENDERED_HEADINGS, REQUIRED_SECTIONS
from paper_reader.raw_schema import require_raw_schema_version
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.run_lock import ExpectedRunArtifact, locked_v2_run
from paper_reader.run_size import RunSizeLimitError, enforce_projected_run_size
from paper_reader.storage import (
    HeldExactFileGuard,
    ImmutableTreeSnapshot,
    OwnedDirectoryAnchor,
    OwnedPublishedFile,
    atomic_write_bytes,
    atomic_write_json,
    cas_update_run,
    canonical_json_bytes,
    create_anchored_directory,
    anchored_entry_exists,
    new_random_id,
    new_uuid,
    random_token,
    remove_anchored_tree,
    open_anchored_directory,
    open_anchored_regular_file,
    read_anchored_bytes,
    rfc3339_utc,
    snapshot_directory_fd,
    tree_snapshot_from_bytes,
    tree_snapshot_from_hashes,
    validate_directory_anchor,
)
from paper_reader.zotero_candidate import _artifact_ref, _captured_live_snapshots, _note_child_view
from paper_reader.zotero_lifecycle import parent_fingerprint
from paper_reader.v2_loader import DirectoryAnchor, LoadedRun, RunLoadError, load_v2_run
from paper_reader.zotero_lock import (
    LockedZoteroParent,
    ZoteroLockError,
    locked_zotero_parent,
    skill_root_for_zotero_run,
)
from paper_reader.zotero_read import LocalApiZoteroReadProvider, ZoteroReadProvider
from paper_reader.zotero_authorization_loader import (
    ZoteroAuthorizationBindingError,
    _read_closed_authorization_sidecar,
    load_authorization_artifact,
)
from paper_reader.zotero_artifact_paths import (
    DeterministicArtifactPaths,
    UnsafeZoteroArtifactPathError,
    anchored_artifact_publication,
    inspect_deterministic_artifact_paths,
)
from paper_reader.zotero_authorization_reservations import (
    ZoteroAuthorizationReservation,
    ZoteroAuthorizationReservationError,
    active_authorization_reservations,
    append_authorization_reservation,
    load_authorization_reservations,
)


class ZoteroAuthorizationError(LocalPublicationError):
    pass


_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)


def _authorization_artifact_paths(
    run_dir: Path,
    authorization_id: str,
    *,
    allow_existing_sidecar: bool,
    allow_existing_main: bool,
) -> DeterministicArtifactPaths:
    try:
        return inspect_deterministic_artifact_paths(
            run_dir,
            root_name="authorizations",
            parent_parts=(),
            stem=authorization_id,
            allow_existing_sidecar=allow_existing_sidecar,
            allow_existing_main=allow_existing_main,
        )
    except UnsafeZoteroArtifactPathError as exc:
        raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc


@dataclass(frozen=True, slots=True)
class AuthorizedZoteroWrite:
    run_dir: Path
    authorization_path: Path
    authorization_dir: Path
    authorization: PaperReaderWriteAuthorization
    authorization_digest: str
    write_token: str


@dataclass(frozen=True, slots=True)
class CandidateAuthorizationPreflight:
    candidate_path: Path
    candidate: PaperReaderCandidate
    candidate_digest: str
    run_dir: Path
    run_device: int
    run_inode: int
    run_manifest_bytes: bytes
    run_manifest_sha256: str
    skill_root: Path
    skill_root_device: int
    skill_root_inode: int


@dataclass(slots=True)
class _AuthorizationFinalizationGuard:
    run_anchor: DirectoryAnchor
    main_guard: HeldExactFileGuard
    sidecar_anchor: OwnedDirectoryAnchor
    expected_sidecar_snapshot: ImmutableTreeSnapshot

    def close(self) -> None:
        try:
            self.main_guard.close()
        finally:
            self.sidecar_anchor.close()

    def __enter__(self) -> _AuthorizationFinalizationGuard:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def verify(self) -> None:
        try:
            validate_directory_anchor(self.run_anchor)
            self.main_guard.verify()
            validate_directory_anchor(self.sidecar_anchor)
            observed_sidecar = snapshot_directory_fd(
                self.sidecar_anchor.descriptor,
                max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
                max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
            )
            validate_directory_anchor(self.sidecar_anchor)
            self.main_guard.verify()
            validate_directory_anchor(self.run_anchor)
        except (OSError, ValueError) as exc:
            raise ZoteroAuthorizationError(
                "authorization_tampered",
                "immutable authorization main or sidecar changed during finalization",
            ) from exc
        if observed_sidecar != self.expected_sidecar_snapshot:
            raise ZoteroAuthorizationError(
                "authorization_tampered",
                "immutable authorization sidecar closed set changed during finalization",
            )


def _open_authorization_finalization_guard(
    *,
    run_anchor: DirectoryAnchor,
    authorization_path: Path,
    authorization_dir: Path,
    authorization_bytes: bytes,
    sidecar_snapshot: ImmutableTreeSnapshot,
) -> _AuthorizationFinalizationGuard:
    try:
        sidecar_anchor = open_anchored_directory(run_anchor, authorization_dir)
    except (OSError, ValueError) as exc:
        raise ZoteroAuthorizationError(
            "authorization_tampered",
            "immutable authorization sidecar could not be held for finalization",
        ) from exc
    try:
        main_file = open_anchored_regular_file(
            run_anchor,
            authorization_path,
            expected_size=len(authorization_bytes),
        )
    except (OSError, ValueError) as exc:
        sidecar_anchor.close()
        raise ZoteroAuthorizationError(
            "authorization_tampered",
            "immutable authorization main could not be held for finalization",
        ) from exc
    guard = _AuthorizationFinalizationGuard(
        run_anchor=run_anchor,
        main_guard=HeldExactFileGuard(
            anchor=run_anchor,
            owned_file=main_file,
            expected_bytes=authorization_bytes,
            label="authorization main artifact",
        ),
        sidecar_anchor=sidecar_anchor,
        expected_sidecar_snapshot=sidecar_snapshot,
    )
    try:
        guard.verify()
    except BaseException:
        guard.close()
        raise
    return guard


def _preflight_existing_authorization_schemas(
    loaded: LoadedRun,
    run_anchor: DirectoryAnchor,
) -> None:
    run_dir = loaded.manifest_path.parent
    for ref in (item for item in loaded.run.artifacts if item.role == "write_authorization"):
        path = run_dir / ref.path
        try:
            raw = read_anchored_bytes(
                run_anchor,
                path,
                max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            )
        except (OSError, ValueError) as exc:
            raise ZoteroAuthorizationError(
                "authorization_tampered",
                "existing write authorization is unsafe",
            ) from exc
        require_raw_schema_version(
            raw,
            expected="paper_reader.write-authorization.v2",
            artifact_path=path,
        )

    authorizations_dir = run_dir / "authorizations"
    if not anchored_entry_exists(run_anchor, authorizations_dir):
        validate_directory_anchor(run_anchor)
        return
    try:
        with open_anchored_directory(run_anchor, authorizations_dir) as authorizations_anchor:
            names = sorted(os.listdir(authorizations_anchor.descriptor))
            for name in names:
                metadata = os.stat(
                    name,
                    dir_fd=authorizations_anchor.descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISREG(metadata.st_mode) and name.endswith(".json"):
                    path = authorizations_dir / name
                    raw = read_anchored_bytes(
                        authorizations_anchor,
                        path,
                        max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                    )
                elif stat.S_ISDIR(metadata.st_mode) and name.startswith("authorization_"):
                    sidecar = authorizations_dir / name
                    with open_anchored_directory(
                        authorizations_anchor,
                        sidecar,
                    ) as sidecar_anchor:
                        record_path = sidecar / "record.json"
                        raw = read_anchored_bytes(
                            sidecar_anchor,
                            record_path,
                            max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                        )
                else:
                    raise ZoteroAuthorizationError(
                        "unsafe_artifact_path",
                        "authorization directory contains an unsafe entry",
                    )
                require_raw_schema_version(
                    raw,
                    expected="paper_reader.write-authorization.v2",
                    artifact_path=path if stat.S_ISREG(metadata.st_mode) else record_path,
                )
            if sorted(os.listdir(authorizations_anchor.descriptor)) != names:
                raise ZoteroAuthorizationError(
                    "authorization_tampered",
                    "authorization directory changed during schema preflight",
                )
            validate_directory_anchor(authorizations_anchor)
        validate_directory_anchor(run_anchor)
    except ZoteroAuthorizationError:
        raise
    except RunLoadError:
        raise
    except (OSError, ValueError) as exc:
        raise ZoteroAuthorizationError(
            "unsafe_artifact_path",
            "authorization directory cannot be inspected safely",
        ) from exc


def _candidate_target_without_network(
    candidate_input: Path,
) -> CandidateAuthorizationPreflight:
    requested = candidate_manifest_path(candidate_input)
    candidate_path = Path(os.path.abspath(requested.expanduser()))
    candidate_dir = candidate_path.parent
    if candidate_dir.parent.name != "candidates":
        raise ZoteroAuthorizationError(
            "candidate_tampered",
            "candidate left its run candidates directory",
        )
    run_dir = candidate_dir.parent.parent
    if run_dir.parent.parent.name != "runs":
        loaded = load_v2_run(run_dir)
        try:
            with DirectoryAnchor.open(
                run_dir,
                manifest_path=run_dir / "run.json",
            ) as run_anchor:
                if (
                    run_anchor.device != loaded.run_directory_device
                    or run_anchor.inode != loaded.run_directory_inode
                ):
                    raise ZoteroAuthorizationError(
                        "run_directory_changed",
                        "run directory changed during authorization preflight",
                    )
                raw = read_anchored_bytes(
                    run_anchor,
                    candidate_path,
                    max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                )
                _load_candidate(
                    candidate_path,
                    loaded_run=replace(loaded, run_directory_anchor=run_anchor),
                    require_local=False,
                )
        except ZoteroAuthorizationError:
            raise
        except LocalPublicationError as exc:
            raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
        except (OSError, RunLoadError, ValueError) as exc:
            raise ZoteroAuthorizationError(
                "candidate_unreadable",
                "candidate cannot be inspected safely before authorization",
            ) from exc
        require_raw_schema_version(
            raw,
            expected="paper_reader.candidate.v2",
            artifact_path=candidate_path,
        )
        try:
            candidate = PaperReaderCandidate.model_validate_json(raw)
        except ValidationError as exc:
            raise ZoteroAuthorizationError(
                "candidate_unreadable",
                "candidate cannot be validated before authorization",
            ) from exc
        if not isinstance(candidate.source, ZoteroSourceIdentity) or not isinstance(
            candidate.target,
            ZoteroPublicationTarget,
        ):
            raise ZoteroAuthorizationError(
                "local_candidate_forbidden",
                "local candidates cannot produce Zotero write authorization",
            )
        raise ZoteroAuthorizationError(
            "candidate_tampered",
            "Zotero candidate is outside the installed skill runs directory",
        )

    skill_root = skill_root_for_zotero_run(run_dir)
    try:
        root_anchor = DirectoryAnchor.open(
            skill_root,
            manifest_path=skill_root / ".zotero-parent-locks",
        )
    except RunLoadError as exc:
        raise ZoteroAuthorizationError(
            "run_directory_changed",
            "Zotero skill root cannot be anchored during authorization preflight",
        ) from exc
    with root_anchor:
        skill_root_device = root_anchor.device
        skill_root_inode = root_anchor.inode
        loaded = load_v2_run(run_dir)
        try:
            with DirectoryAnchor.open(
                run_dir,
                manifest_path=run_dir / "run.json",
            ) as run_anchor:
                if (
                    run_anchor.device != loaded.run_directory_device
                    or run_anchor.inode != loaded.run_directory_inode
                ):
                    raise ZoteroAuthorizationError(
                        "run_directory_changed",
                        "run directory changed during authorization preflight",
                    )
                raw = read_anchored_bytes(
                    run_anchor,
                    candidate_path,
                    max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                )
                _load_candidate(
                    candidate_path,
                    loaded_run=replace(loaded, run_directory_anchor=run_anchor),
                    require_local=False,
                )
                _preflight_existing_authorization_schemas(loaded, run_anchor)
                validate_directory_anchor(root_anchor)
        except ZoteroAuthorizationError:
            raise
        except LocalPublicationError as exc:
            raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
        except RunLoadError as exc:
            if exc.code == "unsupported_run_schema":
                raise
            raise ZoteroAuthorizationError(
                "candidate_unreadable",
                "candidate cannot be inspected safely before authorization",
            ) from exc
        except (OSError, ValueError) as exc:
            raise ZoteroAuthorizationError(
                "candidate_unreadable",
                "candidate cannot be inspected safely before authorization",
            ) from exc
    require_raw_schema_version(
        raw,
        expected="paper_reader.candidate.v2",
        artifact_path=candidate_path,
    )
    try:
        candidate = PaperReaderCandidate.model_validate_json(raw)
    except ValidationError as exc:
        raise ZoteroAuthorizationError(
            "candidate_unreadable",
            "candidate cannot be validated before authorization",
        ) from exc
    if not isinstance(candidate.source, ZoteroSourceIdentity) or not isinstance(
        candidate.target,
        ZoteroPublicationTarget,
    ):
        raise ZoteroAuthorizationError(
            "local_candidate_forbidden",
            "local candidates cannot produce Zotero write authorization",
        )
    return CandidateAuthorizationPreflight(
        candidate_path=candidate_path,
        candidate=candidate,
        candidate_digest=hashlib.sha256(raw).hexdigest(),
        run_dir=run_dir,
        run_device=loaded.run_directory_device,
        run_inode=loaded.run_directory_inode,
        run_manifest_bytes=loaded.manifest_bytes,
        run_manifest_sha256=loaded.manifest_sha256,
        skill_root=skill_root,
        skill_root_device=skill_root_device,
        skill_root_inode=skill_root_inode,
    )


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


@contextmanager
def _locked_parent_for_preflight(
    inspected: CandidateAuthorizationPreflight,
):
    expected_candidate = ExpectedRunArtifact(
        path=inspected.candidate_path.relative_to(inspected.run_dir).as_posix(),
        sha256=inspected.candidate_digest,
    )
    try:
        with locked_zotero_parent(
            inspected.run_dir,
            inspected.candidate.target.parent_key,
            expected_skill_root=inspected.skill_root,
            expected_skill_root_device=inspected.skill_root_device,
            expected_skill_root_inode=inspected.skill_root_inode,
            expected_run_path=inspected.run_dir,
            expected_run_device=inspected.run_device,
            expected_run_inode=inspected.run_inode,
            expected_run_manifest_sha256=inspected.run_manifest_sha256,
            expected_artifacts=(expected_candidate,),
        ) as locked:
            yield locked
    except ZoteroLockError as exc:
        raise ZoteroAuthorizationError(exc.code, str(exc)) from exc


def _active_reservation_error(
    reservation: ZoteroAuthorizationReservation,
) -> ZoteroAuthorizationError:
    return ZoteroAuthorizationError(
        "authorization_active",
        "another run already holds an unexpired authorization for this parent and title",
        data={
            "reservation_id": reservation.reservation_id,
            "authorization_id": reservation.authorization_id,
            "authorization_digest": reservation.authorization_digest,
            "candidate_digest": reservation.candidate_digest,
            "run_id": reservation.run_id,
            "expires_at": reservation.expires_at,
        },
    )


def _reject_active_authorization(
    run_dir: Path,
    loaded: LoadedRun,
    *,
    parent_key: str,
    note_title: str,
    now: datetime,
) -> None:
    run = loaded.run
    for ref in (item for item in run.artifacts if item.role == "write_authorization"):
        try:
            authorization_path, raw = verify_artifact_ref(
                run_dir,
                ref,
                anchor=loaded.run_directory_anchor,
            )
        except Exception as exc:
            raise ZoteroAuthorizationError(
                "authorization_tampered",
                "bound authorization failed integrity validation",
            ) from exc
        require_raw_schema_version(
            raw,
            expected="paper_reader.write-authorization.v2",
            artifact_path=authorization_path,
        )
        try:
            authorization = PaperReaderWriteAuthorization.model_validate_json(raw)
        except ValidationError as exc:
            raise ZoteroAuthorizationError(
                "authorization_tampered",
                "bound authorization failed strict validation",
            ) from exc
        if canonical_json_bytes(authorization) != raw:
            raise ZoteroAuthorizationError(
                "authorization_tampered",
                "bound authorization is not canonical",
            )
        if authorization.run_id != run.run_id:
            raise ZoteroAuthorizationError(
                "authorization_tampered",
                "bound authorization run identity mismatch",
            )
        if (
            authorization.target.parent_key == parent_key
            and authorization.target.note_title == note_title
            and authorization.note_title == note_title
            and _parse_utc(authorization.expires_at) > now
        ):
            raise ZoteroAuthorizationError(
                "authorization_active",
                "this candidate already has an unexpired write authorization",
                data={
                    "authorization_id": authorization.authorization_id,
                    "candidate_digest": authorization.candidate_digest,
                    "expires_at": authorization.expires_at,
                },
            )


def _fresh_live_preflight(
    candidate: PaperReaderCandidate,
    provider: ZoteroReadProvider,
) -> tuple[bytes, bytes]:
    try:
        parent, children, parent_bytes, children_bytes = _captured_live_snapshots(
            provider,
            parent_key=candidate.target.parent_key,
        )
    except LocalPublicationError as exc:
        raise ZoteroAuthorizationError(
            exc.code,
            "read-only Zotero authorization preflight failed",
            data=exc.data,
        ) from exc
    try:
        observed_fingerprint = parent_fingerprint(parent)
    except Exception as exc:
        raise ZoteroAuthorizationError(
            "invalid_live_snapshot",
            "fresh Zotero parent snapshot is invalid",
        ) from exc
    parent_key = str(parent.get("key") or (parent.get("data") or {}).get("key") or "").strip()
    matching_keys: list[str] = []
    for child in children:
        view = _note_child_view(child)
        if view is None:
            continue
        key, parent_item, title = view
        if parent_item != candidate.target.parent_key:
            raise ZoteroAuthorizationError(
                "invalid_live_snapshot",
                "fresh Zotero note child has the wrong parent",
            )
        if title == candidate.note_title:
            matching_keys.append(key)
    if (
        parent_key != candidate.target.parent_key
        or observed_fingerprint != candidate.target.parent_fingerprint
        or matching_keys
    ):
        raise ZoteroAuthorizationError(
            "stale_candidate",
            "candidate parent fingerprint or title availability changed; rebuild candidate",
            data={"matching_note_keys": ",".join(matching_keys)},
        )
    return parent_bytes, children_bytes


def _authorization_ref(
    run_dir: Path,
    staged_path: Path,
    future_path: Path,
    role: str,
    media_type: str,
) -> ArtifactRef:
    return _artifact_ref(run_dir, staged_path, future_path, role, media_type)


def _updated_run(
    run: PaperReaderRun,
    *,
    authorization_ref: ArtifactRef,
    preflight: LivePreflight,
    gate: GateState,
) -> PaperReaderRun:
    return PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=run.run_id,
        created_at=run.created_at,
        source=run.source,
        target=run.target,
        status=run.status,
        artifacts=(*run.artifacts, authorization_ref),
        gate=gate,
        live_preflight=preflight,
    )


def _raise_after_authorization_commit_failure(cause: Exception) -> None:
    """Fail closed after the authorization/run exchange is already durable."""

    if isinstance(cause, ZoteroAuthorizationError):
        raise cause
    if isinstance(cause, LocalPublicationError):
        raise ZoteroAuthorizationError(
            cause.code,
            str(cause),
            data=cause.data,
        ) from cause
    raise ZoteroAuthorizationError(
        "authorization_tampered",
        "authorization finalization state changed before token return",
    ) from cause


def _recover_matching_orphan_authorization(
    loaded: LoadedRun,
    *,
    candidate_digest: str,
    candidate_guard: _CandidateTreeGuard,
    evidence_guard: _CandidateTreeGuard,
    reservations: tuple[ZoteroAuthorizationReservation, ...],
    required_reservation: ZoteroAuthorizationReservation | None = None,
) -> bool:
    run_dir = loaded.manifest_path.parent
    authorizations_dir = run_dir / "authorizations"
    run_anchor = loaded.run_directory_anchor
    if run_anchor is None:
        raise ZoteroAuthorizationError(
            "run_directory_changed",
            "orphan recovery requires a locked run directory anchor",
        )
    if not anchored_entry_exists(run_anchor, authorizations_dir):
        validate_directory_anchor(run_anchor)
        return False
    bound_paths = {
        item.path
        for item in loaded.run.artifacts
        if item.role == "write_authorization"
    }
    candidates: list[tuple[Path, bytes]] = []
    try:
        with open_anchored_directory(run_anchor, authorizations_dir) as authorizations_anchor:
            names = sorted(os.listdir(authorizations_anchor.descriptor))
            name_set = set(names)
            for name in names:
                metadata = os.stat(
                    name,
                    dir_fd=authorizations_anchor.descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISREG(metadata.st_mode) and name.endswith(".json"):
                    main_path = authorizations_dir / name
                    relative = main_path.relative_to(run_dir).as_posix()
                    raw = read_anchored_bytes(
                        authorizations_anchor,
                        main_path,
                        max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                    )
                    if relative not in bound_paths:
                        candidates.append((main_path, raw))
                    continue
                if stat.S_ISDIR(metadata.st_mode) and name.startswith("authorization_"):
                    if f"{name}.json" in name_set:
                        continue
                    sidecar_dir = authorizations_dir / name
                    with open_anchored_directory(
                        authorizations_anchor,
                        sidecar_dir,
                    ) as sidecar_anchor:
                        record_path = sidecar_dir / "record.json"
                        raw = read_anchored_bytes(
                            sidecar_anchor,
                            record_path,
                            max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                        )
                    candidates.append((authorizations_dir / f"{name}.json", raw))
                    continue
                raise ZoteroAuthorizationError(
                    "authorization_orphan_invalid",
                    "authorization directory contains an unsafe orphan entry",
                )
            if sorted(os.listdir(authorizations_anchor.descriptor)) != names:
                raise ZoteroAuthorizationError(
                    "authorization_orphan_invalid",
                    "authorization directory changed during orphan recovery",
                )
            validate_directory_anchor(authorizations_anchor)
        validate_directory_anchor(run_anchor)
    except ZoteroAuthorizationError:
        raise
    except (OSError, ValueError) as exc:
        raise ZoteroAuthorizationError(
            "authorization_orphan_invalid",
            "authorization orphans cannot be inspected safely",
        ) from exc

    matches = []
    for main_path, raw in candidates:
        require_raw_schema_version(
            raw,
            expected="paper_reader.write-authorization.v2",
            artifact_path=main_path,
        )
        try:
            preview = PaperReaderWriteAuthorization.model_validate_json(raw)
        except Exception as exc:
            raise ZoteroAuthorizationError(
                "authorization_orphan_invalid",
                "unbound authorization commit candidate is invalid",
            ) from exc
        if preview.run_id != loaded.run.run_id or preview.candidate_digest != candidate_digest:
            continue
        try:
            recovered = load_authorization_artifact(
                loaded,
                main_path,
                require_bound=False,
                raw_override=raw,
            )
        except ZoteroAuthorizationBindingError as exc:
            raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
        exact_reservations = [
            reservation
            for reservation in reservations
            if reservation.authorization_id == recovered.authorization.authorization_id
            and reservation.authorization_digest == recovered.authorization_digest
            and reservation.run_id == recovered.authorization.run_id
            and reservation.candidate_digest == recovered.authorization.candidate_digest
            and reservation.parent_key == recovered.authorization.target.parent_key
            and reservation.note_title == recovered.authorization.note_title
        ]
        if len(exact_reservations) != 1:
            raise ZoteroAuthorizationError(
                "authorization_orphan_invalid",
                "unbound authorization has no unique exact durable reservation",
            )
        if (
            required_reservation is not None
            and exact_reservations[0] != required_reservation
        ):
            continue
        matches.append(recovered)
    if len(matches) > 1:
        raise ZoteroAuthorizationError(
            "authorization_recovery_conflict",
            "multiple exact unbound authorizations match this candidate",
        )
    if not matches:
        return False

    recovered = matches[0]
    if not anchored_entry_exists(run_anchor, recovered.authorization_path):
        recovery_record = recovered.authorization_path.with_suffix("") / "record.json"
        recovery_bytes = read_anchored_bytes(
            run_anchor,
            recovery_record,
            expected_size=len(recovered.authorization_bytes),
            max_bytes=len(recovered.authorization_bytes),
        )
        artifact_paths = _authorization_artifact_paths(
            run_dir,
            recovered.authorization.authorization_id,
            allow_existing_sidecar=True,
            allow_existing_main=False,
        )
        try:
            with anchored_artifact_publication(
                artifact_paths,
                staging_dir=None,
                allow_existing_sidecar=True,
                allow_existing_main=False,
                expected_run_anchor=loaded.run_directory_anchor,
            ) as publication:
                publication.publish_main(
                    recovery_record,
                    expected_bytes=recovery_bytes,
                )
        except UnsafeZoteroArtifactPathError as exc:
            raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
        except Exception as exc:
            raise ZoteroAuthorizationError(
                "authorization_recovery_failed",
                "failed to restore exact authorization commit marker",
            ) from exc
    authorization_ref = ArtifactRef(
        role="write_authorization",
        path=recovered.authorization_path.relative_to(run_dir).as_posix(),
        sha256=hashlib.sha256(recovered.authorization_bytes).hexdigest(),
        size_bytes=len(recovered.authorization_bytes),
        media_type="application/json",
    )
    try:
        sidecar_members, _sidecar_digest = _read_closed_authorization_sidecar(
            run_anchor,
            recovered.authorization_path,
        )
        with _open_authorization_finalization_guard(
            run_anchor=run_anchor,
            authorization_path=recovered.authorization_path,
            authorization_dir=recovered.authorization_path.with_suffix(""),
            authorization_bytes=recovered.authorization_bytes,
            sidecar_snapshot=tree_snapshot_from_bytes(sidecar_members),
        ) as authorization_guard:
            revalidated = load_authorization_artifact(
                loaded,
                recovered.authorization_path,
                require_bound=False,
                raw_override=recovered.authorization_bytes,
            )
            if revalidated != recovered:
                raise ZoteroAuthorizationError(
                    "authorization_tampered",
                    "recovered authorization changed between validation and binding",
                )
            updated_run = _updated_run(
                loaded.run,
                authorization_ref=authorization_ref,
                preflight=revalidated.authorization.live_preflight,
                gate=revalidated.authorization.gate,
            )
            authorization_guard.verify()
            candidate_guard.verify()
            evidence_guard.verify()
            cas_update_run(
                loaded,
                updated_run,
                finalization_guards=(
                    authorization_guard,
                    candidate_guard,
                    evidence_guard,
                ),
            )
            evidence_guard.verify()
            candidate_guard.verify()
            authorization_guard.verify()
    except Exception as exc:
        raise ZoteroAuthorizationError(
            "authorization_status_update_failed",
            "authorization main artifact is durable but run binding failed",
        ) from exc
    raise ZoteroAuthorizationError(
        "authorization_recovered_token_unavailable",
        (
            "the exact orphan authorization was safely bound, but its plaintext write token "
            "cannot be recovered; do not write with this authorization"
        ),
        data={"authorization_path": str(recovered.authorization_path)},
    )


def _validate_authorization_options(
    *,
    ttl_seconds: int,
    external_claim_id: str | None,
    write_attempt_id: str | None,
) -> tuple[str | None, str | None]:
    if (external_claim_id is None) != (write_attempt_id is None):
        raise ZoteroAuthorizationError(
            "invalid_identity_options",
            "external_claim_id and write_attempt_id must appear together",
        )
    if external_claim_id is not None and write_attempt_id is not None:
        try:
            external_claim_id = _IDENTIFIER_ADAPTER.validate_python(
                external_claim_id,
                strict=True,
            )
            write_attempt_id = _IDENTIFIER_ADAPTER.validate_python(
                write_attempt_id,
                strict=True,
            )
        except ValidationError as exc:
            raise ZoteroAuthorizationError(
                "invalid_external_identity",
                "external_claim_id and write_attempt_id must both be valid Identifiers",
            ) from exc
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
        raise ZoteroAuthorizationError(
            "invalid_authorization_ttl",
            "authorization TTL must be between 1 and 300 seconds",
        )
    return external_claim_id, write_attempt_id


def _trusted_utc_wall_clock() -> datetime:
    """Return the process wall clock for authorization security decisions."""

    return datetime.now(timezone.utc)


def _authorization_instant() -> datetime:
    instant = _trusted_utc_wall_clock()
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ZoteroAuthorizationError(
            "invalid_authorization_clock",
            "trusted authorization clock must be timezone-aware",
        )
    return instant.astimezone(timezone.utc)


def _refreshed_candidate_preflight(
    candidate_input: Path,
    previous: CandidateAuthorizationPreflight,
) -> CandidateAuthorizationPreflight:
    refreshed = _candidate_target_without_network(candidate_input)
    if (
        refreshed.run_dir != previous.run_dir
        or refreshed.run_device != previous.run_device
        or refreshed.run_inode != previous.run_inode
        or refreshed.skill_root != previous.skill_root
        or refreshed.skill_root_device != previous.skill_root_device
        or refreshed.skill_root_inode != previous.skill_root_inode
    ):
        raise ZoteroAuthorizationError(
            "run_directory_changed",
            "Zotero run or skill root changed during authorization retry",
        )
    if (
        refreshed.candidate_path != previous.candidate_path
        or refreshed.candidate_digest != previous.candidate_digest
        or refreshed.candidate != previous.candidate
    ):
        raise ZoteroAuthorizationError(
            "candidate_tampered",
            "candidate changed during authorization retry",
        )
    return refreshed


def _authorize_zotero_candidate_once(
    inspected: CandidateAuthorizationPreflight,
    *,
    provider: ZoteroReadProvider | None,
    ttl_seconds: int,
    external_claim_id: str | None,
    write_attempt_id: str | None,
) -> AuthorizedZoteroWrite:
    candidate_path = inspected.candidate_path
    run_dir = inspected.run_dir
    resolved_provider = provider or LocalApiZoteroReadProvider()

    with ExitStack() as finalization_stack, _locked_parent_for_preflight(
        inspected
    ) as parent_lock:
        expected_candidate = ExpectedRunArtifact(
            path=inspected.candidate_path.relative_to(inspected.run_dir).as_posix(),
            sha256=inspected.candidate_digest,
        )
        with locked_v2_run(
            run_dir,
            expected_run_path=inspected.run_dir,
            expected_run_device=inspected.run_device,
            expected_run_inode=inspected.run_inode,
            expected_run_manifest_sha256=inspected.run_manifest_sha256,
            expected_artifacts=(expected_candidate,),
        ) as loaded:
            if (
                loaded.run_directory_device != inspected.run_device
                or loaded.run_directory_inode != inspected.run_inode
            ):
                raise ZoteroAuthorizationError(
                    "run_directory_changed",
                    "run changed after authorization preflight",
                )
            if loaded.manifest_bytes != inspected.run_manifest_bytes:
                raise ZoteroAuthorizationError(
                    "run_directory_changed",
                    "run manifest changed after authorization preflight",
                )
            run_anchor = loaded.run_directory_anchor
            if run_anchor is None:
                raise ZoteroAuthorizationError(
                    "run_directory_changed",
                    "authorization requires a locked run directory anchor",
                )
            _preflight_existing_authorization_schemas(loaded, run_anchor)
            try:
                loaded, candidate_path, candidate, candidate_digest, verified = _load_candidate(
                    candidate_path,
                    loaded_run=loaded,
                    require_local=False,
                )
            except LocalPublicationError as exc:
                raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
            if (
                candidate_path != inspected.candidate_path
                or candidate_digest != inspected.candidate_digest
                or candidate != inspected.candidate
                or candidate.target.parent_key != parent_lock.parent_key
                or candidate.note_title != inspected.candidate.note_title
            ):
                raise ZoteroAuthorizationError(
                    "run_directory_changed",
                    "candidate changed after authorization preflight",
                )
            if not isinstance(candidate.source, ZoteroSourceIdentity) or not isinstance(
                candidate.target, ZoteroPublicationTarget
            ):
                raise ZoteroAuthorizationError(
                    "local_candidate_forbidden",
                    "local candidates cannot produce Zotero write authorization",
                )
            candidate_bytes_for_guard = canonical_json_bytes(candidate)
            expected_candidate_members = {
                "candidate.json": (
                    len(candidate_bytes_for_guard),
                    hashlib.sha256(candidate_bytes_for_guard).hexdigest(),
                )
            }
            for artifact in candidate.artifacts:
                artifact_path = run_dir / artifact.path
                expected_candidate_members[
                    artifact_path.relative_to(candidate_path.parent).as_posix()
                ] = (artifact.size_bytes, artifact.sha256)
            candidate_guard = finalization_stack.enter_context(
                _CandidateTreeGuard(
                    anchor=open_anchored_directory(
                        run_anchor,
                        candidate_path.parent,
                    ),
                    expected=tree_snapshot_from_hashes(
                        expected_candidate_members
                    ),
                )
            )
            candidate_guard.verify()
            try:
                original_evidence = revalidate_candidate_original_evidence(
                    loaded,
                    candidate,
                    verified,
                )
            except LocalPublicationError as exc:
                raise ZoteroAuthorizationError(
                    exc.code,
                    str(exc),
                    data=exc.data,
                ) from exc
            evidence_guard = finalization_stack.enter_context(
                _open_original_evidence_tree_guard(loaded, original_evidence)
            )
            evidence_guard.verify()
            active_instant = _authorization_instant()
            _reject_active_authorization(
                run_dir,
                loaded,
                parent_key=candidate.target.parent_key,
                note_title=candidate.note_title,
                now=active_instant,
            )
            try:
                reservations = load_authorization_reservations(parent_lock)
                active_records = active_authorization_reservations(
                    reservations,
                    note_title=candidate.note_title,
                    now=active_instant,
                )
            except ZoteroAuthorizationReservationError as exc:
                raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
            if len(active_records) > 1:
                raise ZoteroAuthorizationError(
                    "authorization_reservation_tampered",
                    "multiple active reservations bind the same parent and exact title",
                )
            if active_records:
                active = active_records[0]
                if (
                    active.run_id != candidate.run_id
                    or active.candidate_digest != candidate_digest
                ):
                    raise _active_reservation_error(active)
                recovered = _recover_matching_orphan_authorization(
                    loaded,
                    candidate_digest=candidate_digest,
                    candidate_guard=candidate_guard,
                    evidence_guard=evidence_guard,
                    reservations=reservations,
                    required_reservation=active,
                )
                if not recovered:
                    raise _active_reservation_error(active)
            else:
                _recover_matching_orphan_authorization(
                    loaded,
                    candidate_digest=candidate_digest,
                    candidate_guard=candidate_guard,
                    evidence_guard=evidence_guard,
                    reservations=reservations,
                )
            try:
                verify_local_source(candidate.source.attachment)
            except LocalPublicationError as exc:
                raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
            authorization_id = new_random_id("authorization")
            artifact_paths = _authorization_artifact_paths(
                run_dir,
                authorization_id,
                allow_existing_sidecar=False,
                allow_existing_main=False,
            )
            parent_bytes, children_bytes = _fresh_live_preflight(candidate, resolved_provider)
            try:
                revalidate_candidate_original_evidence(loaded, candidate, verified)
            except LocalPublicationError as exc:
                raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
            grant_instant = _authorization_instant()

            if external_claim_id is None:
                resolved_claim_id = new_random_id("direct")
                resolved_attempt_id = new_random_id("direct")
                while resolved_attempt_id == resolved_claim_id:  # defensive; UUID collision is negligible
                    resolved_attempt_id = new_random_id("direct")
            else:
                resolved_claim_id = external_claim_id
                assert write_attempt_id is not None
                resolved_attempt_id = write_attempt_id

            _html_path, html_bytes = verified["note_html"][0]
            content_html = html_bytes.decode("utf-8")
            candidate_bytes = canonical_json_bytes(candidate)
            authorization_dir = artifact_paths.sidecar
            authorization_path = artifact_paths.main
            staging = run_dir / f".{authorization_id}.{new_uuid()}.staging"
            staging_anchor = create_anchored_directory(run_anchor, staging)
            try:
                staged_sidecar = staging / "sidecar"
                files = {
                    "candidate.json": candidate_bytes,
                    "content.html": html_bytes,
                    "parent.json": parent_bytes,
                    "children.json": children_bytes,
                }
                for name, content in files.items():
                    atomic_write_bytes(
                        staged_sidecar / name,
                        content,
                        anchor=staging_anchor,
                    )
                specs = {
                    "candidate.json": ("candidate_snapshot", "application/json"),
                    "content.html": ("authorized_content_html", "text/html"),
                    "parent.json": ("zotero_parent_snapshot", "application/json"),
                    "children.json": ("zotero_children_snapshot", "application/json"),
                }
                validate_directory_anchor(staging_anchor)
                refs = {
                    name: _authorization_ref(
                        run_dir,
                        staged_sidecar / name,
                        authorization_dir / name,
                        role,
                        media,
                    )
                    for name, (role, media) in specs.items()
                }
                validate_directory_anchor(staging_anchor)
                preflight = LivePreflight(
                    preflight_id=new_random_id("preflight"),
                    captured_at=rfc3339_utc(grant_instant),
                    parent_key=candidate.target.parent_key,
                    parent_fingerprint=candidate.target.parent_fingerprint,
                    requested_note_title=candidate.note_title,
                    title_available=True,
                    matching_note_keys=(),
                    parent_snapshot=refs["parent.json"],
                    children_snapshot=refs["children.json"],
                )
                gate = GateState(
                    status="write_ready",
                    evaluated_at=rfc3339_utc(grant_instant),
                    checks=(
                        "candidate_integrity",
                        "source_identity",
                        "parent_fingerprint",
                        "live_title_availability",
                        "canonical_html_binding",
                        "authorization_ttl",
                        "cross_run_title_reservation",
                    ),
                    blockers=(),
                )
                write_token = random_token(32)
                authorization = PaperReaderWriteAuthorization(
                    schema_version="paper_reader.write-authorization.v2",
                    authorization_id=authorization_id,
                    run_id=candidate.run_id,
                    created_at=rfc3339_utc(grant_instant),
                    expires_at=rfc3339_utc(
                        grant_instant + timedelta(seconds=ttl_seconds)
                    ),
                    ttl_seconds=ttl_seconds,
                    candidate=refs["candidate.json"],
                    candidate_digest=candidate_digest,
                    target=candidate.target,
                    note_title=candidate.note_title,
                    tags=candidate.tags,
                    content_html=content_html,
                    content_sha256=candidate.content_sha256,
                    content_length=candidate.content_length,
                    minimum_content_length=candidate.content_length,
                    required_headings=tuple(REQUIRED_SECTIONS),
                    forbidden_headings=tuple(FORBIDDEN_RENDERED_HEADINGS),
                    nonce=random_token(32),
                    token_sha256=hashlib.sha256(write_token.encode("utf-8")).hexdigest(),
                    external_claim_id=resolved_claim_id,
                    write_attempt_id=resolved_attempt_id,
                    mcp_envelope=McpWriteEnvelope(
                        parentKey=candidate.target.parent_key,
                        content=content_html,
                        tags=candidate.tags,
                    ),
                    artifacts=tuple(refs.values()),
                    live_preflight=preflight,
                    gate=gate,
                )
                authorization_bytes = canonical_json_bytes(authorization)
                atomic_write_bytes(
                    staged_sidecar / "record.json",
                    authorization_bytes,
                    anchor=staging_anchor,
                )
                staged_authorization_path = staging / f"{authorization_id}.json"
                atomic_write_bytes(
                    staged_authorization_path,
                    authorization_bytes,
                    anchor=staging_anchor,
                )
                sidecar_snapshot = tree_snapshot_from_bytes(
                    {**files, "record.json": authorization_bytes}
                )
                authorization_ref = _authorization_ref(
                    run_dir,
                    staged_authorization_path,
                    authorization_path,
                    "write_authorization",
                    "application/json",
                )
                updated_run = _updated_run(
                    loaded.run,
                    authorization_ref=authorization_ref,
                    preflight=preflight,
                    gate=gate,
                )
                updated_run_bytes = canonical_json_bytes(updated_run)
                try:
                    enforce_projected_run_size(
                        run_dir,
                        max_bytes=V2_RESOURCE_POLICY.run_max_bytes,
                        staging_dir=staging,
                        replacements={loaded.manifest_path: updated_run_bytes},
                        retained_replacement_paths=(loaded.manifest_path,),
                    )
                except RunSizeLimitError as exc:
                    raise ZoteroAuthorizationError(
                        "run_size_limit_exceeded",
                        str(exc),
                        data={"run_size_bytes": exc.actual_bytes, "max_bytes": exc.max_bytes},
                    ) from exc
                try:
                    append_authorization_reservation(
                        parent_lock,
                        authorization_id=authorization.authorization_id,
                        authorization_digest=authorization_ref.sha256,
                        run_id=authorization.run_id,
                        candidate_digest=authorization.candidate_digest,
                        note_title=authorization.note_title,
                        now=grant_instant,
                        ttl_seconds=authorization.ttl_seconds,
                    )
                except ZoteroAuthorizationReservationError as exc:
                    raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
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
                            staged_authorization_path,
                            expected_bytes=authorization_bytes,
                        )
                except UnsafeZoteroArtifactPathError as exc:
                    raise ZoteroAuthorizationError(exc.code, str(exc), data=exc.data) from exc
                except Exception as exc:
                    raise ZoteroAuthorizationError(
                        "authorization_publication_failed",
                        "immutable authorization publication failed",
                        data={"phase": publication_phase},
                    ) from exc
                with _open_authorization_finalization_guard(
                    run_anchor=run_anchor,
                    authorization_path=authorization_path,
                    authorization_dir=authorization_dir,
                    authorization_bytes=authorization_bytes,
                    sidecar_snapshot=sidecar_snapshot,
                ) as authorization_guard:
                    try:
                        written_run = cas_update_run(
                            loaded,
                            updated_run,
                            hold_new=True,
                            finalization_guards=(
                                authorization_guard,
                                candidate_guard,
                                evidence_guard,
                            ),
                        )
                    except Exception as exc:
                        raise ZoteroAuthorizationError(
                            "authorization_status_update_failed",
                            "authorization tree is durable but run binding failed",
                        ) from exc
                    if not isinstance(written_run, OwnedPublishedFile):
                        raise ZoteroAuthorizationError(
                            "authorization_status_update_failed",
                            "run binding did not retain the updated manifest identity",
                        )
                    with HeldExactFileGuard(
                        anchor=run_anchor,
                        owned_file=written_run,
                        expected_bytes=updated_run_bytes,
                        label="updated authorization run manifest",
                    ) as updated_run_guard:
                        try:
                            updated_run_guard.verify()
                            authorization_guard.verify()
                            candidate_guard.verify()
                            evidence_guard.verify()
                            revalidate_candidate_original_evidence(
                                loaded,
                                candidate,
                                verified,
                            )
                            evidence_guard.verify()
                            candidate_guard.verify()
                            authorization_guard.verify()
                            updated_run_guard.verify()
                        except (LocalPublicationError, OSError, ValueError) as exc:
                            _raise_after_authorization_commit_failure(exc)
                        if _authorization_instant() >= _parse_utc(
                            authorization.expires_at
                        ):
                            raise ZoteroAuthorizationError(
                                "authorization_expired_before_return",
                                (
                                    "authorization expired before its plaintext write "
                                    "token could be returned; do not write with this "
                                    "authorization"
                                ),
                                data={
                                    "authorization_path": str(authorization_path),
                                    "authorization_id": authorization.authorization_id,
                                    "authorization_digest": authorization_ref.sha256,
                                    "expires_at": authorization.expires_at,
                                },
                            )
                        try:
                            evidence_guard.verify()
                            candidate_guard.verify()
                            authorization_guard.verify()
                            updated_run_guard.verify()
                            verify_local_source(candidate.source.attachment)
                            evidence_guard.verify()
                            authorization_guard.verify()
                            candidate_guard.verify()
                            updated_run_guard.verify()
                            evidence_guard.verify()
                        except (LocalPublicationError, OSError, ValueError) as exc:
                            _raise_after_authorization_commit_failure(exc)
                        return AuthorizedZoteroWrite(
                            run_dir=run_dir,
                            authorization_path=authorization_path,
                            authorization_dir=authorization_dir,
                            authorization=authorization,
                            authorization_digest=authorization_ref.sha256,
                            write_token=write_token,
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


def authorize_zotero_candidate(
    candidate_input: Path,
    *,
    provider: ZoteroReadProvider | None = None,
    ttl_seconds: int = 300,
    external_claim_id: str | None = None,
    write_attempt_id: str | None = None,
) -> AuthorizedZoteroWrite:
    external_claim_id, write_attempt_id = _validate_authorization_options(
        ttl_seconds=ttl_seconds,
        external_claim_id=external_claim_id,
        write_attempt_id=write_attempt_id,
    )
    inspected = _candidate_target_without_network(candidate_input)
    for attempt in range(2):
        try:
            return _authorize_zotero_candidate_once(
                inspected,
                provider=provider,
                ttl_seconds=ttl_seconds,
                external_claim_id=external_claim_id,
                write_attempt_id=write_attempt_id,
            )
        except (RunLoadError, ZoteroAuthorizationError) as exc:
            if exc.code not in {
                "run_manifest_changed",
                "run_artifact_changed",
            } or attempt == 1:
                raise
        inspected = _refreshed_candidate_preflight(candidate_input, inspected)
    raise AssertionError("authorization manifest retry loop did not terminate")


__all__ = [
    "AuthorizedZoteroWrite",
    "ZoteroAuthorizationError",
    "authorize_zotero_candidate",
]
