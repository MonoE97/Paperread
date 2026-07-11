from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable
from uuid import UUID, uuid5

from pydantic import ValidationError

from paper_reader_batch.v2_contracts import (
    BatchEvent,
    BatchManifest,
    BatchState,
    ClaimedData,
    FinishedData,
    LeaseState,
    LeaseMutationData,
    LOCAL_PREPARE_COORDINATION_UUID_NAME,
    LocalPrepareCoordinationReservedData,
    PdfManifestItem,
    RetriedData,
    RunInitializedData,
    RunRecoveredData,
    ResumedLocalPrepareLease,
    StateItem,
    STATE_SCHEMA_VERSION,
    WriteClaimedData,
    WriteLeaseMutationData,
    WriteLeaseState,
    WriteReconciledData,
    WriteRetriedData,
    WriteStartedData,
    WriteUncertainData,
    WriteWrittenData,
    ZoteroItemManifestItem,
    ZoteroTitleManifestItem,
)
from paper_reader_batch.v2_errors import BatchRuntimeError


MAX_TASK_LEASE_SECONDS = 3600
MAX_WRITE_LEASE_SECONDS = 300


def _corrupt(message: str) -> BatchRuntimeError:
    return BatchRuntimeError("journal_corrupt", message)


def _manifest_item(manifest: BatchManifest, item_id: str):
    for item in manifest.items:
        if item.item_id == item_id:
            return item
    raise _corrupt(f"event references unknown item: {item_id}")


def _state_item_index(state: BatchState, item_id: str) -> int:
    for index, item in enumerate(state.items):
        if item.item_id == item_id:
            return index
    raise _corrupt(f"state is missing manifest item: {item_id}")


def _initial_items(manifest: BatchManifest) -> list[StateItem]:
    return [
        StateItem(
            item_id=item.item_id,
            input_type=item.input_type,
            expected_output=item.expected_output,
            local_prepare_status="queued" if isinstance(item, PdfManifestItem) else "not_applicable",
            write_status="not_applicable" if isinstance(item, PdfManifestItem) else "awaiting_candidate",
        )
        for item in manifest.items
    ]


def _status(items: Iterable[StateItem]) -> str:
    values = list(items)
    if any(item.write_status == "uncertain" for item in values):
        return "write_uncertain"
    if any(
        item.worker_status == "claimed"
        or item.local_prepare_status == "claimed"
        or item.write_status in {"claimed", "started"}
        for item in values
    ):
        return "running"
    if any(
        item.worker_status in {"failed", "blocked"}
        or item.local_prepare_status in {"failed", "blocked"}
        or item.write_status in {"blocked", "retry_confirmation_required"}
        for item in values
    ):
        return "needs_attention"
    if any(item.write_status == "queued" for item in values):
        return "awaiting_write"
    if any(item.worker_status == "queued" or item.local_prepare_status == "queued" for item in values):
        return "ready"
    return "succeeded"


def _with_items(state: BatchState, items: list[StateItem], event: BatchEvent) -> BatchState:
    candidate = state.model_copy(
        update={
            "items": items,
            "updated_at": event.occurred_at,
            "next_sequence": event.sequence + 1,
            "latest_event_sha256": event.event_sha256,
            "batch_status": _status(items),
        }
    )
    try:
        return BatchState.model_validate(candidate.model_dump(mode="json"))
    except ValidationError as exc:
        raise _corrupt("reducer produced state that violates the strict state contract") from exc


def initial_state(manifest: BatchManifest, event: BatchEvent) -> BatchState:
    if event.sequence != 1 or not isinstance(event.data, RunInitializedData):
        raise _corrupt("journal must begin with run.initialized at sequence 1")
    if event.data.manifest_id != manifest.manifest_id:
        raise _corrupt("run.initialized manifest id does not match manifest")
    items = _initial_items(manifest)
    return BatchState(
        schema_version=STATE_SCHEMA_VERSION,
        manifest_id=manifest.manifest_id,
        manifest_sha256=event.manifest_sha256,
        lease_secret_sha256=event.data.lease_secret_sha256,
        initialized_at=event.data.initialized_at,
        updated_at=event.occurred_at,
        next_sequence=2,
        latest_event_sha256=event.event_sha256,
        batch_status=_status(items),
        items=items,
    )


def _apply_claim(state: BatchState, manifest: BatchManifest, event: BatchEvent, data: ClaimedData) -> BatchState:
    items = list(state.items)
    active_claims = sum(
        item.worker_status == "claimed" or item.local_prepare_status == "claimed"
        for item in state.items
    )
    if active_claims + len(data.assignments) > manifest.default_concurrency:
        raise _corrupt("claim event exceeds manifest concurrency")
    for assignment in data.assignments:
        issued_at = _timestamp(assignment.issued_at)
        expires_at = _timestamp(assignment.expires_at)
        if (
            assignment.issued_at != event.occurred_at
            or expires_at <= issued_at
            or expires_at - issued_at > timedelta(seconds=MAX_TASK_LEASE_SECONDS)
        ):
            raise _corrupt("claim event has non-authoritative lease times")
        manifest_item = _manifest_item(manifest, assignment.item_id)
        if assignment.source != manifest_item.source:
            raise _corrupt(f"claim source identity does not match manifest: {assignment.item_id}")
        index = _state_item_index(state, assignment.item_id)
        item = items[index]
        if item.worker_status == "claimed" or item.local_prepare_status == "claimed":
            raise _corrupt(f"cross-lane or duplicate active claim: {assignment.item_id}")
        lease = LeaseState(
            lane=assignment.lane,
            actor_id=assignment.actor_id,
            claim_id=assignment.claim_id,
            attempt_id=assignment.attempt_id,
            attempt_number=assignment.attempt_number,
            lease_token_sha256=assignment.lease_token_sha256,
            issued_at=assignment.issued_at,
            expires_at=assignment.expires_at,
        )
        if assignment.lane == "worker":
            if data.kind != "worker.claimed" or item.worker_status != "queued":
                raise _corrupt(f"illegal worker claim transition: {assignment.item_id}")
            if assignment.attempt_number != item.worker_attempt_count + 1:
                raise _corrupt(f"worker attempt number is not monotonic: {assignment.item_id}")
            if item.worker_pending_attempt_id is not None and assignment.attempt_id != item.worker_pending_attempt_id:
                raise _corrupt(f"worker claim does not consume the retry-bound attempt: {assignment.item_id}")
            items[index] = item.model_copy(
                update={
                    "worker_status": "claimed",
                    "worker_attempt_count": assignment.attempt_number,
                    "worker_lease": lease,
                    "worker_result_sha256": None,
                    "worker_last_actor_id": assignment.actor_id,
                    "worker_last_claim_id": assignment.claim_id,
                    "worker_last_attempt_id": assignment.attempt_id,
                    "worker_last_lease_token_sha256": assignment.lease_token_sha256,
                    "worker_pending_attempt_id": None,
                    "worker_failure_code": None,
                    "worker_failure_message": None,
                }
            )
        else:
            if data.kind != "local_prepare.claimed" or item.local_prepare_status not in {"queued", "failed", "blocked"}:
                raise _corrupt(f"illegal local prepare claim transition: {assignment.item_id}")
            if not isinstance(manifest_item, PdfManifestItem):
                raise _corrupt(f"local prepare claim targets a non-PDF item: {assignment.item_id}")
            if assignment.attempt_number != item.local_prepare_attempt_count + 1:
                raise _corrupt(f"local prepare attempt number is not monotonic: {assignment.item_id}")
            items[index] = item.model_copy(
                update={
                    "local_prepare_status": "claimed",
                    "local_prepare_attempt_count": assignment.attempt_number,
                    "local_prepare_lease": lease,
                    "local_prepare_result_sha256": None,
                    "local_prepare_last_actor_id": assignment.actor_id,
                    "local_prepare_last_claim_id": assignment.claim_id,
                    "local_prepare_last_attempt_id": assignment.attempt_id,
                    "local_prepare_last_lease_token_sha256": assignment.lease_token_sha256,
                    "local_prepare_coordination_request_id": None,
                    "local_prepare_coordination_fingerprint": None,
                    "local_prepare_coordination_device": None,
                    "local_prepare_coordination_inode": None,
                    "local_prepare_failure_code": None,
                    "local_prepare_failure_message": None,
                }
            )
    return _with_items(state, items, event)


def _same_lease(lease, data) -> bool:
    return bool(
        lease
        and lease.actor_id == data.actor_id
        and lease.claim_id == data.claim_id
        and lease.attempt_id == data.attempt_id
        and lease.attempt_number == data.attempt_number
        and lease.lease_token_sha256 == data.lease_token_sha256
    )


def _same_write_lease(lease: WriteLeaseState | None, data) -> bool:
    return bool(
        lease
        and lease.writer_id == data.writer_id
        and lease.claim_id == data.claim_id
        and lease.write_attempt_id == data.write_attempt_id
        and lease.attempt_number == data.attempt_number
        and lease.lease_token_sha256 == data.lease_token_sha256
        and lease.candidate_sha256 == data.candidate_sha256
    )


def _same_last_write_identity(item: StateItem, data) -> bool:
    return bool(
        item.write_last_writer_id == data.writer_id
        and item.write_last_claim_id == data.claim_id
        and item.write_last_attempt_id == data.write_attempt_id
        and item.write_attempt_count == data.attempt_number
        and item.write_last_lease_token_sha256 == data.lease_token_sha256
        and item.candidate_sha256 == data.candidate_sha256
    )


def _timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:  # strict contract should already prevent this
        raise _corrupt(f"invalid event timestamp: {value}") from exc


def _apply_local_prepare_coordination_reserved(
    state: BatchState,
    event: BatchEvent,
    data: LocalPrepareCoordinationReservedData,
) -> BatchState:
    items = list(state.items)
    index = _state_item_index(state, data.item_id)
    item = items[index]
    lease = item.local_prepare_lease
    if item.local_prepare_status != "claimed" or not _same_lease(lease, data):
        raise _corrupt(f"coordination reservation references inactive local lease: {data.item_id}")
    expected_request_id = str(uuid5(UUID(data.coordinator_request_id), LOCAL_PREPARE_COORDINATION_UUID_NAME))
    if event.request_id != expected_request_id:
        raise _corrupt(f"coordination reservation request id is not derived canonically: {data.item_id}")
    if not (_timestamp(lease.issued_at) <= _timestamp(event.occurred_at) < _timestamp(lease.expires_at)):
        raise _corrupt(f"coordination reservation occurred outside its lease: {data.item_id}")
    if item.local_prepare_coordination_request_id is not None:
        raise _corrupt(f"local attempt has more than one coordination reservation: {data.item_id}")
    items[index] = item.model_copy(
        update={
            "local_prepare_coordination_request_id": data.coordinator_request_id,
            "local_prepare_coordination_fingerprint": data.coordinator_request_fingerprint,
            "local_prepare_coordination_device": data.request_dir_device,
            "local_prepare_coordination_inode": data.request_dir_inode,
        }
    )
    return _with_items(state, items, event)


def _apply_lease_mutation(state: BatchState, event: BatchEvent, data: LeaseMutationData) -> BatchState:
    items = list(state.items)
    index = _state_item_index(state, data.item_id)
    item = items[index]
    lane = "worker" if data.kind.startswith("worker.") else "local_prepare"
    lease = item.worker_lease if lane == "worker" else item.local_prepare_lease
    if not _same_lease(lease, data):
        raise _corrupt(f"lease mutation identity mismatch: {data.item_id}")
    if data.kind.endswith("renewed"):
        if data.issued_at is None or data.expires_at is None:
            raise _corrupt("renew event is missing lease times")
        if (
            data.issued_at != event.occurred_at
            or _timestamp(event.occurred_at) >= _timestamp(lease.expires_at)
            or _timestamp(data.issued_at) < _timestamp(lease.issued_at)
            or _timestamp(data.expires_at) <= _timestamp(data.issued_at)
            or _timestamp(data.expires_at) <= _timestamp(lease.expires_at)
            or _timestamp(data.expires_at) - _timestamp(data.issued_at)
            > timedelta(seconds=MAX_TASK_LEASE_SECONDS)
        ):
            raise _corrupt("renew event has non-authoritative or non-extending lease times")
        renewed = lease.model_copy(update={"issued_at": data.issued_at, "expires_at": data.expires_at})
        update = {f"{lane}_lease": renewed}
    else:
        if data.issued_at is not None or data.expires_at is not None:
            raise _corrupt("release/expiry event must not carry replacement lease times")
        if data.kind.endswith("lease_expired"):
            if _timestamp(event.occurred_at) < _timestamp(lease.expires_at):
                raise _corrupt("lease_expired event occurred before authoritative expiry")
        elif _timestamp(event.occurred_at) >= _timestamp(lease.expires_at):
            raise _corrupt("release event occurred after authoritative expiry")
        update = {f"{lane}_lease": None}
        update[f"{lane}_status"] = "queued"
        if lane == "local_prepare":
            update.update(
                {
                    "local_prepare_coordination_request_id": None,
                    "local_prepare_coordination_fingerprint": None,
                    "local_prepare_coordination_device": None,
                    "local_prepare_coordination_inode": None,
                }
            )
    items[index] = item.model_copy(update=update)
    return _with_items(state, items, event)


def _apply_finished(state: BatchState, manifest: BatchManifest, event: BatchEvent, data: FinishedData) -> BatchState:
    items = list(state.items)
    index = _state_item_index(state, data.item_id)
    item = items[index]
    lane = "worker" if data.kind == "worker.finished" else "local_prepare"
    lease = item.worker_lease if lane == "worker" else item.local_prepare_lease
    if not _same_lease(lease, data):
        raise _corrupt(f"finish identity mismatch: {data.item_id}")
    if _timestamp(event.occurred_at) >= _timestamp(lease.expires_at):
        raise _corrupt(f"finish event occurred after lease expiry: {data.item_id}")
    has_failure = data.failure_code is not None or data.failure_message is not None
    if data.status in {"failed", "blocked"}:
        if not has_failure or data.resolved_zotero_item_key is not None or data.candidate_sha256 is not None:
            raise _corrupt("failed/blocked finish requires a typed failure and forbids success identities")
    elif has_failure:
        raise _corrupt("successful/prepared finish forbids failure fields")
    if lane == "worker":
        status = data.status
        if status == "prepared":
            raise _corrupt("worker result cannot use prepared status")
        manifest_item = _manifest_item(manifest, data.item_id)
        if status == "succeeded":
            if data.candidate_sha256 is None:
                raise _corrupt("successful worker finish requires candidate digest")
            if isinstance(manifest_item, PdfManifestItem):
                if data.resolved_zotero_item_key is not None:
                    raise _corrupt("local PDF worker finish cannot resolve a Zotero item")
            elif data.resolved_zotero_item_key is None:
                raise _corrupt("Zotero worker finish requires exact resolved parent key")
        update = {
            "worker_status": status,
            "worker_lease": None,
            "worker_result_sha256": data.result_sha256,
            "resolved_zotero_item_key": data.resolved_zotero_item_key,
            "candidate_sha256": data.candidate_sha256,
            "worker_failure_code": data.failure_code,
            "worker_failure_message": data.failure_message,
        }
        if status == "succeeded":
            if isinstance(manifest_item, PdfManifestItem):
                update["local_prepare_status"] = "prepared"
            else:
                update["write_status"] = "queued" if manifest.write_policy == "zotero_write" else "prepared_only"
        items[index] = item.model_copy(update=update)
    else:
        if data.status not in {"prepared", "failed", "blocked"}:
            raise _corrupt("local prepare finish has invalid status")
        items[index] = item.model_copy(
            update={
                "local_prepare_status": data.status,
                "local_prepare_lease": None,
                "local_prepare_result_sha256": data.result_sha256,
                "local_prepare_failure_code": data.failure_code,
                "local_prepare_failure_message": data.failure_message,
            }
        )
    return _with_items(state, items, event)


def _apply_write_claimed(
    state: BatchState,
    manifest: BatchManifest,
    event: BatchEvent,
    data: WriteClaimedData,
) -> BatchState:
    if manifest.write_policy != "zotero_write":
        raise _corrupt("write claim is forbidden by manifest write policy")
    manifest_item = _manifest_item(manifest, data.item_id)
    if isinstance(manifest_item, PdfManifestItem):
        raise _corrupt(f"write claim targets a local PDF item: {data.item_id}")
    if any(item.write_status in {"claimed", "started", "uncertain"} for item in state.items):
        raise _corrupt("write claim violates the serial write lane")

    index = _state_item_index(state, data.item_id)
    items = list(state.items)
    item = items[index]
    issued_at = _timestamp(data.issued_at)
    expires_at = _timestamp(data.expires_at)
    parent_identity_matches = (
        isinstance(manifest_item, ZoteroItemManifestItem)
        and item.resolved_zotero_item_key == manifest_item.source.item_key
    ) or (
        isinstance(manifest_item, ZoteroTitleManifestItem)
        and (
            manifest_item.source.resolved_item_key is None
            or item.resolved_zotero_item_key == manifest_item.source.resolved_item_key
        )
    )
    if (
        item.write_status != "queued"
        or item.write_lease is not None
        or item.worker_status != "succeeded"
        or item.resolved_zotero_item_key is None
        or not parent_identity_matches
        or item.candidate_sha256 is None
        or data.candidate_sha256 != item.candidate_sha256
        or data.attempt_number != item.write_attempt_count + 1
        or data.issued_at != event.occurred_at
        or expires_at <= issued_at
        or expires_at - issued_at > timedelta(seconds=MAX_WRITE_LEASE_SECONDS)
    ):
        raise _corrupt(f"illegal or non-authoritative write claim: {data.item_id}")
    if item.write_pending_attempt_id is not None and data.write_attempt_id != item.write_pending_attempt_id:
        raise _corrupt(f"write claim does not consume the retry-bound attempt: {data.item_id}")
    if item.write_started_event_sha256 is not None or item.authorization_sha256 is not None:
        raise _corrupt(f"queued write retains an active authorization: {data.item_id}")
    if (
        data.claim_id == item.write_last_claim_id
        or data.write_attempt_id == item.write_last_attempt_id
        or data.lease_token_sha256 == item.write_last_lease_token_sha256
    ):
        raise _corrupt(f"write claim reuses the previous attempt identity: {data.item_id}")
    for other in state.items:
        if other.item_id == data.item_id:
            continue
        if (
            data.claim_id == other.write_last_claim_id
            or data.write_attempt_id == other.write_last_attempt_id
            or data.lease_token_sha256 == other.write_last_lease_token_sha256
        ):
            raise _corrupt(f"write claim reuses another item's attempt identity: {data.item_id}")

    lease = WriteLeaseState(
        writer_id=data.writer_id,
        claim_id=data.claim_id,
        write_attempt_id=data.write_attempt_id,
        attempt_number=data.attempt_number,
        lease_token_sha256=data.lease_token_sha256,
        issued_at=data.issued_at,
        expires_at=data.expires_at,
        candidate_sha256=data.candidate_sha256,
    )
    items[index] = item.model_copy(
        update={
            "write_status": "claimed",
            "write_attempt_count": data.attempt_number,
            "write_lease": lease,
            "write_last_writer_id": data.writer_id,
            "write_last_claim_id": data.claim_id,
            "write_last_attempt_id": data.write_attempt_id,
            "write_last_lease_token_sha256": data.lease_token_sha256,
            "write_pending_attempt_id": None,
            "write_result_sha256": None,
            "reconciliation_sha256": None,
            "write_failure_code": None,
            "write_failure_message": None,
        }
    )
    return _with_items(state, items, event)


def _apply_write_lease_mutation(
    state: BatchState,
    event: BatchEvent,
    data: WriteLeaseMutationData,
) -> BatchState:
    index = _state_item_index(state, data.item_id)
    items = list(state.items)
    item = items[index]
    lease = item.write_lease
    allowed_statuses = {"claimed", "started"} if data.kind == "write.renewed" else {"claimed"}
    if item.write_status not in allowed_statuses or not _same_write_lease(lease, data):
        raise _corrupt(f"write lease mutation identity mismatch: {data.item_id}")
    assert lease is not None
    occurred_at = _timestamp(event.occurred_at)
    authoritative_expiry = _timestamp(lease.expires_at)

    if data.kind == "write.renewed":
        if data.issued_at is None or data.expires_at is None:
            raise _corrupt("write renew event is missing lease times")
        issued_at = _timestamp(data.issued_at)
        expires_at = _timestamp(data.expires_at)
        if (
            data.issued_at != event.occurred_at
            or occurred_at >= authoritative_expiry
            or issued_at < _timestamp(lease.issued_at)
            or expires_at <= issued_at
            or expires_at <= authoritative_expiry
            or expires_at - issued_at > timedelta(seconds=MAX_WRITE_LEASE_SECONDS)
        ):
            raise _corrupt("write renew event has non-authoritative lease times")
        update = {
            "write_lease": lease.model_copy(
                update={"issued_at": data.issued_at, "expires_at": data.expires_at}
            )
        }
    else:
        if data.issued_at is not None or data.expires_at is not None:
            raise _corrupt("write release/expiry event must not carry replacement lease times")
        if data.kind == "write.lease_expired":
            if occurred_at < authoritative_expiry:
                raise _corrupt("write lease expiry event occurred before authoritative expiry")
        elif occurred_at >= authoritative_expiry:
            raise _corrupt("write release event occurred after authoritative expiry")
        update = {"write_status": "queued", "write_lease": None}
    items[index] = item.model_copy(update=update)
    return _with_items(state, items, event)


def _apply_write_started(
    state: BatchState,
    event: BatchEvent,
    data: WriteStartedData,
) -> BatchState:
    index = _state_item_index(state, data.item_id)
    items = list(state.items)
    item = items[index]
    lease = item.write_lease
    if item.write_status != "claimed" or not _same_write_lease(lease, data):
        raise _corrupt(f"write start identity mismatch: {data.item_id}")
    assert lease is not None
    if (
        data.started_at != event.occurred_at
        or not (_timestamp(lease.issued_at) <= _timestamp(event.occurred_at) < _timestamp(lease.expires_at))
        or data.external_claim_id != data.claim_id
        or item.authorization_sha256 is not None
        or item.authorization_nonce_sha256 is not None
        or item.external_claim_id is not None
        or data.authorization_sha256 == item.write_last_authorization_sha256
        or data.authorization_nonce_sha256 == item.write_last_authorization_nonce_sha256
    ):
        raise _corrupt(f"write start has stale authorization or non-authoritative time: {data.item_id}")
    for other in state.items:
        if other.item_id == data.item_id:
            continue
        if (
            data.authorization_sha256 == other.write_last_authorization_sha256
            or data.authorization_nonce_sha256 == other.write_last_authorization_nonce_sha256
        ):
            raise _corrupt(f"write start reuses another item's authorization: {data.item_id}")

    items[index] = item.model_copy(
        update={
            "write_status": "started",
            "write_started_event_sha256": event.event_sha256,
            "authorization_sha256": data.authorization_sha256,
            "authorization_nonce_sha256": data.authorization_nonce_sha256,
            "external_claim_id": data.external_claim_id,
            "write_last_authorization_sha256": data.authorization_sha256,
            "write_last_authorization_nonce_sha256": data.authorization_nonce_sha256,
            "write_last_external_claim_id": data.external_claim_id,
            "write_failure_code": None,
            "write_failure_message": None,
        }
    )
    return _with_items(state, items, event)


def _apply_write_written(
    state: BatchState,
    manifest: BatchManifest,
    event: BatchEvent,
    data: WriteWrittenData,
) -> BatchState:
    index = _state_item_index(state, data.item_id)
    items = list(state.items)
    item = items[index]
    lease = item.write_lease
    manifest_item = _manifest_item(manifest, data.item_id)
    if item.write_status != "started" or not _same_write_lease(lease, data):
        raise _corrupt(f"write commit identity mismatch: {data.item_id}")
    assert lease is not None
    if (
        _timestamp(event.occurred_at) >= _timestamp(lease.expires_at)
        or data.authorization_sha256 != item.authorization_sha256
        or isinstance(manifest_item, PdfManifestItem)
        or data.parent_key != item.resolved_zotero_item_key
    ):
        raise _corrupt(f"write commit has stale authorization, parent, or lease: {data.item_id}")
    items[index] = item.model_copy(
        update={
            "write_status": "written",
            "write_lease": None,
            "write_result_sha256": data.result_sha256,
            "reconciliation_sha256": None,
            "write_failure_code": None,
            "write_failure_message": None,
        }
    )
    return _with_items(state, items, event)


def _apply_write_uncertain(
    state: BatchState,
    event: BatchEvent,
    data: WriteUncertainData,
) -> BatchState:
    index = _state_item_index(state, data.item_id)
    items = list(state.items)
    item = items[index]
    lease = item.write_lease
    if item.write_status != "started" or not _same_write_lease(lease, data):
        raise _corrupt(f"write uncertain identity mismatch: {data.item_id}")
    assert lease is not None
    if data.authorization_sha256 != item.authorization_sha256:
        raise _corrupt(f"write uncertain authorization mismatch: {data.item_id}")
    occurred_at = _timestamp(event.occurred_at)
    expires_at = _timestamp(lease.expires_at)
    if data.kind == "write.lease_expired_uncertain":
        if occurred_at < expires_at:
            raise _corrupt("started write expiry event occurred before authoritative expiry")
    elif occurred_at >= expires_at:
        raise _corrupt("active write uncertain event occurred after authoritative expiry")
    items[index] = item.model_copy(
        update={
            "write_status": "uncertain",
            "write_lease": None,
            "write_failure_code": "write_outcome_uncertain",
            "write_failure_message": data.reason,
        }
    )
    return _with_items(state, items, event)


def _apply_write_reconciled(
    state: BatchState,
    event: BatchEvent,
    data: WriteReconciledData,
) -> BatchState:
    index = _state_item_index(state, data.item_id)
    items = list(state.items)
    item = items[index]
    if (
        item.write_status != "uncertain"
        or item.write_lease is not None
        or not _same_last_write_identity(item, data)
        or data.authorization_sha256 != item.authorization_sha256
    ):
        raise _corrupt(f"write reconciliation identity mismatch: {data.item_id}")
    if data.outcome == "verified":
        status = "written"
        failure_code = None
        failure_message = None
    elif data.outcome == "not_found":
        status = "retry_confirmation_required"
        failure_code = "write_not_found"
        failure_message = "read-only reconciliation found no exact note match"
    elif data.outcome == "ambiguous":
        status = "blocked"
        failure_code = "write_reconciliation_ambiguous"
        failure_message = "read-only reconciliation found multiple exact note matches"
    else:
        status = "blocked"
        failure_code = "write_verification_blocked"
        failure_message = "the unique located note failed full verification"
    items[index] = item.model_copy(
        update={
            "write_status": status,
            "reconciliation_sha256": data.reconciliation_sha256,
            "write_failure_code": failure_code,
            "write_failure_message": failure_message,
        }
    )
    return _with_items(state, items, event)


def _apply_write_retried(
    state: BatchState,
    event: BatchEvent,
    data: WriteRetriedData,
) -> BatchState:
    index = _state_item_index(state, data.item_id)
    items = list(state.items)
    item = items[index]
    if (
        item.write_status != "retry_confirmation_required"
        or item.write_lease is not None
        or data.acknowledged_no_match is not True
        or item.write_last_writer_id != data.previous_writer_id
        or item.write_last_claim_id != data.previous_claim_id
        or item.write_last_attempt_id != data.previous_write_attempt_id
        or item.write_attempt_count != data.previous_attempt_number
        or item.write_last_lease_token_sha256 != data.previous_lease_token_sha256
        or item.candidate_sha256 != data.candidate_sha256
        or item.authorization_sha256 != data.authorization_sha256
        or item.authorization_nonce_sha256 != data.previous_authorization_nonce_sha256
        or item.external_claim_id != data.previous_external_claim_id
        or item.reconciliation_sha256 != data.reconciliation_sha256
        or data.next_attempt_number != item.write_attempt_count + 1
        or data.next_write_attempt_id == data.previous_write_attempt_id
    ):
        raise _corrupt(f"write retry does not bind an acknowledged no-match attempt: {data.item_id}")
    for other in state.items:
        if other.item_id == data.item_id:
            continue
        if data.next_write_attempt_id in {
            other.write_last_attempt_id,
            other.write_pending_attempt_id,
        }:
            raise _corrupt(f"write retry reuses another item's attempt identity: {data.item_id}")
    items[index] = item.model_copy(
        update={
            "write_status": "queued",
            "write_pending_attempt_id": data.next_write_attempt_id,
            "write_started_event_sha256": None,
            "authorization_sha256": None,
            "authorization_nonce_sha256": None,
            "external_claim_id": None,
            "write_result_sha256": None,
            "reconciliation_sha256": None,
            "write_failure_code": None,
            "write_failure_message": None,
        }
    )
    return _with_items(state, items, event)


def apply_event(state: BatchState, manifest: BatchManifest, event: BatchEvent) -> BatchState:
    if event.sequence != state.next_sequence or event.previous_event_sha256 != state.latest_event_sha256:
        raise _corrupt("event sequence or previous hash does not match reducer state")
    if _timestamp(event.occurred_at) < _timestamp(state.updated_at):
        raise _corrupt("event timestamp precedes the latest authoritative state")
    data = event.data
    if isinstance(data, ClaimedData):
        return _apply_claim(state, manifest, event, data)
    if isinstance(data, LocalPrepareCoordinationReservedData):
        return _apply_local_prepare_coordination_reserved(state, event, data)
    if isinstance(data, LeaseMutationData):
        return _apply_lease_mutation(state, event, data)
    if isinstance(data, FinishedData):
        return _apply_finished(state, manifest, event, data)
    if isinstance(data, WriteClaimedData):
        return _apply_write_claimed(state, manifest, event, data)
    if isinstance(data, WriteLeaseMutationData):
        return _apply_write_lease_mutation(state, event, data)
    if isinstance(data, WriteStartedData):
        return _apply_write_started(state, event, data)
    if isinstance(data, WriteWrittenData):
        return _apply_write_written(state, manifest, event, data)
    if isinstance(data, WriteUncertainData):
        return _apply_write_uncertain(state, event, data)
    if isinstance(data, WriteReconciledData):
        return _apply_write_reconciled(state, event, data)
    if isinstance(data, WriteRetriedData):
        return _apply_write_retried(state, event, data)
    if isinstance(data, RetriedData):
        items = list(state.items)
        index = _state_item_index(state, data.item_id)
        item = items[index]
        if item.worker_status not in {"failed", "blocked"} or item.worker_lease is not None:
            raise _corrupt(f"illegal worker retry transition: {data.item_id}")
        if (
            item.worker_last_actor_id != data.previous_actor_id
            or item.worker_last_claim_id != data.previous_claim_id
            or item.worker_last_attempt_id != data.previous_attempt_id
            or item.worker_attempt_count != data.previous_attempt_number
            or item.worker_last_lease_token_sha256 != data.previous_lease_token_sha256
            or data.next_attempt_number != item.worker_attempt_count + 1
            or data.next_attempt_id == data.previous_attempt_id
        ):
            raise _corrupt(f"worker retry identity does not bind the failed attempt: {data.item_id}")
        items[index] = item.model_copy(
            update={
                "worker_status": "queued",
                "worker_result_sha256": None,
                "worker_pending_attempt_id": data.next_attempt_id,
                "worker_failure_code": None,
                "worker_failure_message": None,
            }
        )
        return _with_items(state, items, event)
    if isinstance(data, RunRecoveredData):
        items = list(state.items)
        seen: set[tuple[str, str]] = set()
        for recovered in [
            *data.expired_worker_leases,
            *data.expired_local_prepare_leases,
            *data.resumed_local_prepare_leases,
        ]:
            key = (recovered.lane, recovered.item_id)
            if key in seen:
                raise _corrupt(f"recover repeats one lease identity: {recovered.item_id}")
            seen.add(key)
            authoritative_expiry = (
                recovered.previous_expires_at
                if isinstance(recovered, ResumedLocalPrepareLease)
                else recovered.expires_at
            )
            if _timestamp(event.occurred_at) < _timestamp(authoritative_expiry):
                raise _corrupt(f"recover event precedes lease expiry: {recovered.item_id}")
        for recovered in data.expired_worker_leases:
            if recovered.lane != "worker":
                raise _corrupt("worker recovery contains another lane")
            index = _state_item_index(state, recovered.item_id)
            item = items[index]
            if item.worker_status != "claimed" or not _same_lease(item.worker_lease, recovered):
                raise _corrupt(f"recover references inactive worker lease: {recovered.item_id}")
            if item.worker_lease.expires_at != recovered.expires_at:
                raise _corrupt(f"recover worker expiry does not match authoritative lease: {recovered.item_id}")
            items[index] = item.model_copy(update={"worker_status": "queued", "worker_lease": None})
        for recovered in data.expired_local_prepare_leases:
            if recovered.lane != "local_prepare":
                raise _corrupt("local recovery contains another lane")
            index = _state_item_index(state, recovered.item_id)
            item = items[index]
            if item.local_prepare_status != "claimed" or not _same_lease(item.local_prepare_lease, recovered):
                raise _corrupt(f"recover references inactive local lease: {recovered.item_id}")
            if item.local_prepare_lease.expires_at != recovered.expires_at:
                raise _corrupt(f"recover local expiry does not match authoritative lease: {recovered.item_id}")
            items[index] = item.model_copy(
                update={
                    "local_prepare_status": "queued",
                    "local_prepare_lease": None,
                    "local_prepare_coordination_request_id": None,
                    "local_prepare_coordination_fingerprint": None,
                    "local_prepare_coordination_device": None,
                    "local_prepare_coordination_inode": None,
                }
            )
        for resumed in data.resumed_local_prepare_leases:
            index = _state_item_index(state, resumed.item_id)
            item = items[index]
            lease = item.local_prepare_lease
            if item.local_prepare_status != "claimed" or not _same_lease(lease, resumed):
                raise _corrupt(f"recover references inactive local coordination lease: {resumed.item_id}")
            issued_at = _timestamp(resumed.issued_at)
            expires_at = _timestamp(resumed.expires_at)
            if (
                lease.expires_at != resumed.previous_expires_at
                or resumed.issued_at != event.occurred_at
                or expires_at <= issued_at
                or expires_at - issued_at > timedelta(seconds=MAX_TASK_LEASE_SECONDS)
            ):
                raise _corrupt(f"recover local coordination lease times are invalid: {resumed.item_id}")
            items[index] = item.model_copy(
                update={
                    "local_prepare_lease": lease.model_copy(
                        update={"issued_at": resumed.issued_at, "expires_at": resumed.expires_at}
                    )
                }
            )
        return _with_items(state, items, event)
    raise _corrupt(f"event type is not accepted by the Task 5 reducer: {data.kind}")


def replay(manifest: BatchManifest, events: list[BatchEvent]) -> BatchState:
    if not events:
        raise _corrupt("journal is empty")
    state = initial_state(manifest, events[0])
    for event in events[1:]:
        state = apply_event(state, manifest, event)
    return state
