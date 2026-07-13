from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from paper_reader.contracts import (
    Identifier,
    Rfc3339Utc,
    Sha256,
    StrictContractModel,
)
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.storage import (
    DirectoryAnchorLike,
    anchored_entry_exists,
    atomic_publish_tree,
    atomic_write_bytes,
    canonical_json_bytes,
    create_anchored_directory,
    new_uuid,
    open_anchored_directory,
    read_anchored_bytes,
    remove_anchored_tree,
    rfc3339_utc,
    snapshot_directory_fd,
    tree_snapshot_from_bytes,
    validate_directory_anchor,
)
from paper_reader.zotero_lock import LockedZoteroParent


LEDGER_DIRECTORY_NAME = ".zotero-authorization-reservations"
INDEX_DIRECTORY_NAME = ".zotero-authorization-reservation-index"
RESERVATION_SCHEMA = "paper_reader.zotero-authorization-reservation.v2"
RESERVATION_WITNESS_SCHEMA = "paper_reader.zotero-authorization-reservation-witness.v2"
MAX_RESERVATION_TTL_SECONDS = 300
RECORD_FILENAME = "record.json"
WITNESS_FILENAME = "witness.json"
STAGING_DIRECTORY_NAME = ".staging"


class ZoteroAuthorizationReservationCore(StrictContractModel):
    schema_version: Literal["paper_reader.zotero-authorization-reservation.v2"]
    authorization_id: Identifier
    authorization_digest: Sha256
    run_id: Identifier
    candidate_digest: Sha256
    parent_key: Identifier
    note_title: str
    created_at: Rfc3339Utc
    expires_at: Rfc3339Utc
    ttl_seconds: Annotated[int, Field(gt=0, le=MAX_RESERVATION_TTL_SECONDS)]


class ZoteroAuthorizationReservation(ZoteroAuthorizationReservationCore):
    reservation_id: Identifier
    reservation_digest: Sha256


class ZoteroAuthorizationReservationWitness(StrictContractModel):
    schema_version: Literal[
        "paper_reader.zotero-authorization-reservation-witness.v2"
    ]
    reservation_id: Identifier
    reservation_digest: Sha256


class ZoteroAuthorizationReservationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        data: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


@dataclass(frozen=True, slots=True)
class AppendedAuthorizationReservation:
    path: Path
    witness_path: Path
    reservation: ZoteroAuthorizationReservation


@dataclass(frozen=True, slots=True)
class _ValidatedReservationRecord:
    reservation: ZoteroAuthorizationReservation
    record_bytes: bytes
    witness_bytes: bytes


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _require_aware_utc(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ZoteroAuthorizationReservationError(
            "invalid_authorization_time",
            "authorization reservation time must be timezone-aware",
        )
    return now.astimezone(timezone.utc)


def _parent_directory_name(parent_key: str) -> str:
    return hashlib.sha256(parent_key.encode("utf-8")).hexdigest()


def _reservation_core(
    reservation: ZoteroAuthorizationReservation,
) -> ZoteroAuthorizationReservationCore:
    return ZoteroAuthorizationReservationCore.model_validate(
        reservation.model_dump(
            mode="python",
            exclude={"reservation_id", "reservation_digest"},
        )
    )


def _core_digest(core: ZoteroAuthorizationReservationCore) -> str:
    return hashlib.sha256(canonical_json_bytes(core)).hexdigest()


def _reservation_identity(digest: str) -> str:
    return f"reservation_{digest}"


def _commitment_paths(
    locked: LockedZoteroParent,
    root_name: str,
) -> tuple[Path, Path]:
    root = locked.skill_root / root_name
    parent_root = root / _parent_directory_name(locked.parent_key)
    return root, parent_root


def _open_or_create_directory(
    anchor: DirectoryAnchorLike,
    path: Path,
    *,
    mode: int,
):
    try:
        return create_anchored_directory(anchor, path, mode=mode)
    except FileExistsError:
        return open_anchored_directory(anchor, path)


def _tampered(message: str, *, cause: BaseException | None = None):
    error = ZoteroAuthorizationReservationError(
        "authorization_reservation_tampered",
        message,
    )
    if cause is not None:
        raise error from cause
    raise error


def _validated_records(
    parent_anchor: DirectoryAnchorLike,
    *,
    parent_key: str,
) -> tuple[_ValidatedReservationRecord, ...]:
    try:
        before_parent = os.fstat(parent_anchor.descriptor)
        names = sorted(os.listdir(parent_anchor.descriptor))
    except OSError as exc:
        _tampered("authorization reservation ledger cannot be listed safely", cause=exc)

    records: list[_ValidatedReservationRecord] = []
    try:
        before_snapshot = snapshot_directory_fd(
            parent_anchor.descriptor,
            max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
            max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
            max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
        )
    except (OSError, ValueError) as exc:
        _tampered("authorization reservation ledger cannot be snapshotted", cause=exc)
    for name in names:
        if not name.startswith("reservation_"):
            _tampered("authorization reservation ledger contains an unknown entry")
        try:
            metadata = os.stat(
                name,
                dir_fd=parent_anchor.descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            _tampered("authorization reservation ledger entry is unsafe", cause=exc)
        if not stat.S_ISDIR(metadata.st_mode):
            _tampered("authorization reservation ledger contains an unsafe entry")
        entry_path = parent_anchor.path / name
        try:
            with open_anchored_directory(parent_anchor, entry_path) as entry_anchor:
                if sorted(os.listdir(entry_anchor.descriptor)) != [
                    RECORD_FILENAME,
                    WITNESS_FILENAME,
                ]:
                    _tampered(
                        "authorization reservation record/witness membership changed"
                    )
                record_path = entry_path / RECORD_FILENAME
                witness_path = entry_path / WITNESS_FILENAME
                raw = read_anchored_bytes(
                    entry_anchor,
                    record_path,
                    max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                )
                witness_raw = read_anchored_bytes(
                    entry_anchor,
                    witness_path,
                    max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
                )
                reservation = ZoteroAuthorizationReservation.model_validate_json(raw)
                witness = ZoteroAuthorizationReservationWitness.model_validate_json(
                    witness_raw
                )
                if sorted(os.listdir(entry_anchor.descriptor)) != [
                    RECORD_FILENAME,
                    WITNESS_FILENAME,
                ]:
                    _tampered(
                        "authorization reservation record/witness membership changed"
                    )
                validate_directory_anchor(entry_anchor)
        except ZoteroAuthorizationReservationError:
            raise
        except (OSError, ValueError, ValidationError) as exc:
            _tampered("authorization reservation is unreadable or invalid", cause=exc)
        try:
            created_at = _parse_utc(reservation.created_at)
            expires_at = _parse_utc(reservation.expires_at)
            core = _reservation_core(reservation)
            digest = _core_digest(core)
        except (ValueError, ValidationError) as exc:
            _tampered("authorization reservation binding is invalid", cause=exc)
        expected_id = _reservation_identity(digest)
        expected_witness = ZoteroAuthorizationReservationWitness(
            schema_version=RESERVATION_WITNESS_SCHEMA,
            reservation_id=expected_id,
            reservation_digest=digest,
        )
        if (
            raw != canonical_json_bytes(reservation)
            or witness_raw != canonical_json_bytes(witness)
            or witness != expected_witness
            or reservation.reservation_id != expected_id
            or reservation.reservation_digest != digest
            or name != expected_id
            or reservation.parent_key != parent_key
            or expires_at != created_at + timedelta(seconds=reservation.ttl_seconds)
        ):
            _tampered("authorization reservation binding is inconsistent")
        records.append(
            _ValidatedReservationRecord(
                reservation=reservation,
                record_bytes=raw,
                witness_bytes=witness_raw,
            )
        )
    try:
        after_names = sorted(os.listdir(parent_anchor.descriptor))
        after_parent = os.fstat(parent_anchor.descriptor)
        after_snapshot = snapshot_directory_fd(
            parent_anchor.descriptor,
            max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
            max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
            max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
        )
        validate_directory_anchor(parent_anchor)
    except (OSError, ValueError) as exc:
        _tampered("authorization reservation ledger changed while scanned", cause=exc)
    if (
        after_names != names
        or after_snapshot != before_snapshot
        or (before_parent.st_dev, before_parent.st_ino, before_parent.st_mtime_ns)
        != (after_parent.st_dev, after_parent.st_ino, after_parent.st_mtime_ns)
    ):
        _tampered("authorization reservation ledger changed while scanned")
    return tuple(records)


def _load_commitment_records(
    locked: LockedZoteroParent,
    *,
    root_name: str,
) -> dict[str, _ValidatedReservationRecord]:
    root, parent_root = _commitment_paths(locked, root_name)
    root_anchor = locked.root_anchor
    if not anchored_entry_exists(root_anchor, root):
        locked.validate()
        return {}
    with open_anchored_directory(root_anchor, root) as commitment_anchor:
        if not anchored_entry_exists(commitment_anchor, parent_root):
            validate_directory_anchor(commitment_anchor)
            locked.validate()
            return {}
        with open_anchored_directory(commitment_anchor, parent_root) as parent_anchor:
            records = _validated_records(
                parent_anchor,
                parent_key=locked.parent_key,
            )
        validate_directory_anchor(commitment_anchor)
    locked.validate()
    return {record.reservation.reservation_id: record for record in records}


def _witness_for(
    reservation: ZoteroAuthorizationReservation,
) -> ZoteroAuthorizationReservationWitness:
    return ZoteroAuthorizationReservationWitness(
        schema_version=RESERVATION_WITNESS_SCHEMA,
        reservation_id=reservation.reservation_id,
        reservation_digest=reservation.reservation_digest,
    )


def _publish_commitment_tree(
    locked: LockedZoteroParent,
    *,
    root_name: str,
    reservation: ZoteroAuthorizationReservation,
) -> tuple[Path, Path]:
    root, parent_root = _commitment_paths(locked, root_name)
    raw = canonical_json_bytes(reservation)
    witness_raw = canonical_json_bytes(_witness_for(reservation))
    with _open_or_create_directory(
        locked.root_anchor,
        root,
        mode=0o700,
    ) as commitment_anchor:
        staging_root = root / STAGING_DIRECTORY_NAME
        with _open_or_create_directory(
            commitment_anchor,
            staging_root,
            mode=0o700,
        ) as staging_root_anchor:
            with _open_or_create_directory(
                commitment_anchor,
                parent_root,
                mode=0o700,
            ) as parent_anchor:
                existing = {
                    item.reservation.reservation_id: item
                    for item in _validated_records(
                        parent_anchor,
                        parent_key=locked.parent_key,
                    )
                }
                prior = existing.get(reservation.reservation_id)
                if prior is not None:
                    if prior.record_bytes != raw or prior.witness_bytes != witness_raw:
                        _tampered("authorization reservation commitments conflict")
                    validate_directory_anchor(parent_anchor)
                    locked.validate()
                    return (
                        parent_root / reservation.reservation_id / RECORD_FILENAME,
                        parent_root / reservation.reservation_id / WITNESS_FILENAME,
                    )

                staging = (
                    staging_root
                    / f".{reservation.reservation_id}.{new_uuid()}.staging"
                )
                staging_anchor = create_anchored_directory(
                    staging_root_anchor,
                    staging,
                    mode=0o700,
                )
                try:
                    atomic_write_bytes(
                        staging / RECORD_FILENAME,
                        raw,
                        anchor=staging_anchor,
                    )
                    atomic_write_bytes(
                        staging / WITNESS_FILENAME,
                        witness_raw,
                        anchor=staging_anchor,
                    )
                    entry_path = parent_root / reservation.reservation_id
                    validate_directory_anchor(parent_anchor)
                    atomic_publish_tree(
                        staging,
                        entry_path,
                        anchor=locked.root_anchor,
                        expected_staging_anchor=staging_anchor,
                        expected_tree_snapshot=tree_snapshot_from_bytes(
                            {
                                RECORD_FILENAME: raw,
                                WITNESS_FILENAME: witness_raw,
                            }
                        ),
                    )
                finally:
                    try:
                        remove_anchored_tree(
                            locked.root_anchor,
                            staging,
                            expected=staging_anchor,
                        )
                    finally:
                        staging_anchor.close()
                validate_directory_anchor(parent_anchor)
                committed = {
                    item.reservation.reservation_id: item
                    for item in _validated_records(
                        parent_anchor,
                        parent_key=locked.parent_key,
                    )
                }.get(reservation.reservation_id)
                if (
                    committed is None
                    or committed.record_bytes != raw
                    or committed.witness_bytes != witness_raw
                ):
                    _tampered("authorization reservation commitment was not durable")
    locked.validate()
    return (
        parent_root / reservation.reservation_id / RECORD_FILENAME,
        parent_root / reservation.reservation_id / WITNESS_FILENAME,
    )


def _commitments_match(
    first: _ValidatedReservationRecord,
    second: _ValidatedReservationRecord,
) -> bool:
    return (
        first.reservation == second.reservation
        and first.record_bytes == second.record_bytes
        and first.witness_bytes == second.witness_bytes
    )


def load_authorization_reservations(
    locked: LockedZoteroParent,
) -> tuple[ZoteroAuthorizationReservation, ...]:
    locked.validate()
    try:
        ledger = _load_commitment_records(
            locked,
            root_name=LEDGER_DIRECTORY_NAME,
        )
        index = _load_commitment_records(
            locked,
            root_name=INDEX_DIRECTORY_NAME,
        )
    except ZoteroAuthorizationReservationError:
        raise
    except (OSError, ValueError) as exc:
        _tampered("authorization reservation commitments are unsafe", cause=exc)
    for reservation_id in ledger.keys() & index.keys():
        if not _commitments_match(ledger[reservation_id], index[reservation_id]):
            _tampered("authorization reservation ledger/index commitments conflict")

    try:
        for reservation_id in sorted(index.keys() - ledger.keys()):
            _publish_commitment_tree(
                locked,
                root_name=LEDGER_DIRECTORY_NAME,
                reservation=index[reservation_id].reservation,
            )
        for reservation_id in sorted(ledger.keys() - index.keys()):
            _publish_commitment_tree(
                locked,
                root_name=INDEX_DIRECTORY_NAME,
                reservation=ledger[reservation_id].reservation,
            )
    except ZoteroAuthorizationReservationError:
        raise
    except (OSError, ValueError) as exc:
        raise ZoteroAuthorizationReservationError(
            "authorization_reservation_failed",
            "authorization reservation commitments could not be recovered safely",
        ) from exc

    try:
        repaired_ledger = _load_commitment_records(
            locked,
            root_name=LEDGER_DIRECTORY_NAME,
        )
        repaired_index = _load_commitment_records(
            locked,
            root_name=INDEX_DIRECTORY_NAME,
        )
    except ZoteroAuthorizationReservationError:
        raise
    except (OSError, ValueError) as exc:
        _tampered("authorization reservation commitments changed after recovery", cause=exc)
    if repaired_ledger.keys() != repaired_index.keys() or any(
        not _commitments_match(repaired_ledger[key], repaired_index[key])
        for key in repaired_ledger
    ):
        _tampered("authorization reservation ledger/index parity is inconsistent")
    locked.validate()
    return tuple(
        repaired_ledger[key].reservation
        for key in sorted(repaired_ledger)
    )


def active_authorization_reservations(
    records: tuple[ZoteroAuthorizationReservation, ...],
    *,
    note_title: str,
    now: datetime,
) -> tuple[ZoteroAuthorizationReservation, ...]:
    instant = _require_aware_utc(now)
    return tuple(
        reservation
        for reservation in records
        if reservation.note_title == note_title
        and _parse_utc(reservation.expires_at) > instant
    )


def _active_error(
    reservation: ZoteroAuthorizationReservation,
) -> ZoteroAuthorizationReservationError:
    return ZoteroAuthorizationReservationError(
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


def reject_active_authorization_reservation(
    locked: LockedZoteroParent,
    *,
    note_title: str,
    now: datetime,
) -> None:
    matches = active_authorization_reservations(
        load_authorization_reservations(locked),
        note_title=note_title,
        now=now,
    )
    if len(matches) > 1:
        raise ZoteroAuthorizationReservationError(
            "authorization_reservation_tampered",
            "multiple active reservations bind the same parent and exact title",
        )
    if matches:
        raise _active_error(matches[0])


def append_authorization_reservation(
    locked: LockedZoteroParent,
    *,
    authorization_id: str,
    authorization_digest: str,
    run_id: str,
    candidate_digest: str,
    note_title: str,
    now: datetime,
    ttl_seconds: int,
) -> AppendedAuthorizationReservation:
    instant = _require_aware_utc(now)
    try:
        core = ZoteroAuthorizationReservationCore(
            schema_version=RESERVATION_SCHEMA,
            authorization_id=authorization_id,
            authorization_digest=authorization_digest,
            run_id=run_id,
            candidate_digest=candidate_digest,
            parent_key=locked.parent_key,
            note_title=note_title,
            created_at=rfc3339_utc(instant),
            expires_at=rfc3339_utc(instant + timedelta(seconds=ttl_seconds)),
            ttl_seconds=ttl_seconds,
        )
        digest = _core_digest(core)
        reservation_id = _reservation_identity(digest)
        reservation = ZoteroAuthorizationReservation(
            **core.model_dump(mode="python"),
            reservation_id=reservation_id,
            reservation_digest=digest,
        )
    except ValidationError as exc:
        raise ZoteroAuthorizationReservationError(
            "invalid_authorization_reservation",
            "authorization reservation cannot be formed",
        ) from exc

    locked.validate()
    try:
        existing_records = load_authorization_reservations(locked)
        for existing in existing_records:
            if (
                existing.note_title == note_title
                and _parse_utc(existing.expires_at) > instant
            ):
                raise _active_error(existing)
        path, witness_path = _publish_commitment_tree(
            locked,
            root_name=LEDGER_DIRECTORY_NAME,
            reservation=reservation,
        )
        _publish_commitment_tree(
            locked,
            root_name=INDEX_DIRECTORY_NAME,
            reservation=reservation,
        )
        committed = load_authorization_reservations(locked)
        if [item for item in committed if item.reservation_id == reservation_id] != [
            reservation
        ]:
            _tampered("authorization reservation pair was not committed exactly once")
        locked.validate()
    except ZoteroAuthorizationReservationError:
        raise
    except FileExistsError as exc:
        raise ZoteroAuthorizationReservationError(
            "authorization_reservation_conflict",
            "authorization reservation already exists",
        ) from exc
    except (OSError, ValueError) as exc:
        raise ZoteroAuthorizationReservationError(
            "authorization_reservation_failed",
            "authorization reservation could not be committed safely",
        ) from exc
    return AppendedAuthorizationReservation(
        path=path,
        witness_path=witness_path,
        reservation=reservation,
    )


__all__ = [
    "AppendedAuthorizationReservation",
    "INDEX_DIRECTORY_NAME",
    "LEDGER_DIRECTORY_NAME",
    "MAX_RESERVATION_TTL_SECONDS",
    "RESERVATION_SCHEMA",
    "RESERVATION_WITNESS_SCHEMA",
    "RECORD_FILENAME",
    "STAGING_DIRECTORY_NAME",
    "WITNESS_FILENAME",
    "ZoteroAuthorizationReservation",
    "ZoteroAuthorizationReservationError",
    "active_authorization_reservations",
    "append_authorization_reservation",
    "load_authorization_reservations",
    "reject_active_authorization_reservation",
]
