from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterator
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import JsonValue, ValidationError

from paper_reader_batch.v2_contracts import (
    EVENT_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    BatchEvent,
    BatchManifest,
    BatchState,
    ClaimedData,
    EventCommandResultSnapshot,
    EventData,
    FinishedData,
    LeaseMutationData,
    LocalPrepareCoordinationReservedData,
    LocalPrepareResult,
    RequestAbortedData,
    RunRecoveredData,
    WorkerResult,
    WriteClaimedData,
    WriteLeaseMutationData,
    WriteReconciledData,
    WriteRetriedData,
    WriteStartedData,
    WriteUncertainData,
    WriteWrittenData,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    MAX_JSON_ARTIFACT_BYTES,
    active_transition_targets,
    canonical_json_bytes,
    canonical_sha256,
    entry_exists,
    internal_zero_tombstone,
    list_directory,
    list_mutable_directory,
    locked_file,
    normalized_absolute_path,
    open_directory_fd,
    publish_bytes_no_replace,
    promote_bytes_no_replace,
    read_active_transition_owner,
    read_bytes,
    read_committed_transitions,
    read_json_bytes,
    read_locked_bytes,
    read_pending_swap,
    replace_bytes_atomic,
    sha256_bytes,
    utc_now,
    validate_locked_path,
    zero_exact_staging,
)
from paper_reader_batch.v2_manifest import load_manifest
from paper_reader_batch.v2_receipts import FaultHook, RequestOutcome, validate_request_id
from paper_reader_batch.v2_reducer import apply_event, initial_state


@dataclass(frozen=True)
class PendingEvent:
    path: Path
    raw: bytes
    event: BatchEvent
    aborting: bool = False
    proposal_path: Path | None = None


@dataclass(frozen=True)
class AbortedResidue:
    path: Path
    raw: bytes
    proposed_event_sha256: str


@dataclass(frozen=True)
class IncompleteEventWrite:
    path: Path
    raw: bytes


@dataclass(frozen=True)
class RunView:
    run_dir: Path
    run_dir_identity: tuple[int, int]
    manifest: BatchManifest
    manifest_raw: bytes
    manifest_sha256: str
    events: list[BatchEvent]
    state: BatchState
    snapshot_raw: bytes | None
    snapshot_status: str
    lease_secret: bytes
    committed_event_bytes: int = 0
    event_directory_entries: int = 0
    pending_event: PendingEvent | None = None
    aborted_events: tuple[BatchEvent, ...] = ()
    aborted_residues: tuple[AbortedResidue, ...] = ()
    incomplete_event_writes: tuple[str, ...] = ()
    incomplete_event_residues: tuple[IncompleteEventWrite, ...] = ()
    state_pending_write: str | None = None
    incomplete_state_writes: tuple[str, ...] = ()
    lock_descriptor: int | None = None
    lock_ancestor_descriptors: tuple[int, ...] = ()


@dataclass(frozen=True)
class ResultPublication:
    path: Path
    content: bytes


@dataclass(frozen=True)
class ProposedTransition:
    data: EventData
    result: dict[str, JsonValue]
    publication: ResultPublication | None = None


Proposal = Callable[[RunView, str], ProposedTransition]
Reconstructor = Callable[[RunView, BatchEvent], dict[str, JsonValue]]
PreRecoveryValidator = Callable[[RunView], None]
ReplayValidator = Callable[[RunView, BatchEvent], None]
CommitValidator = Callable[[RunView], None]
FinalFreshnessValidator = Callable[[RunView, str], None]
ClosureValidator = Callable[[], None]
CommitGuardFactory = Callable[
    [RunView, BatchEvent | None],
    AbstractContextManager[ClosureValidator],
]


def _parse_freshness_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except (ValueError, IndexError) as exc:
        raise BatchRuntimeError("invalid_timestamp", "lease freshness timestamp is invalid") from exc
    if not value.endswith("Z"):
        raise BatchRuntimeError("invalid_timestamp", "lease freshness timestamp must use UTC Z form")
    return parsed


def _validate_transition_temporal_freshness(
    view: RunView,
    data: EventData,
    *,
    effective_now: str,
) -> None:
    """Fail closed when a lease expires before its event is durably published."""

    current = _parse_freshness_time(effective_now)

    def require_future(expires_at: str, *, write: bool = False) -> None:
        if _parse_freshness_time(expires_at) <= current:
            raise BatchRuntimeError(
                "write_lease_expired" if write else "lease_expired",
                "lease expired before the journal transition could be committed",
            )

    def require_expired(expires_at: str, *, write: bool = False) -> None:
        if _parse_freshness_time(expires_at) > current:
            raise BatchRuntimeError(
                "write_lease_active" if write else "lease_active",
                "lease-expiry transition was proposed before the lease expired",
            )

    if isinstance(data, ClaimedData):
        for assignment in data.assignments:
            require_future(assignment.expires_at)
        return
    if isinstance(data, WriteClaimedData):
        require_future(data.expires_at, write=True)
        return

    item_id = getattr(data, "item_id", None)
    item = next(
        (candidate for candidate in view.state.items if candidate.item_id == item_id),
        None,
    )

    if isinstance(data, LocalPrepareCoordinationReservedData):
        if item is None or item.local_prepare_lease is None:
            raise BatchRuntimeError("journal_corrupt", "coordination reservation lost its local lease")
        require_future(item.local_prepare_lease.expires_at)
        return

    if isinstance(data, LeaseMutationData):
        if item is None:
            raise BatchRuntimeError("journal_corrupt", "lease transition refers to an unknown item")
        lease = item.worker_lease if data.kind.startswith("worker.") else item.local_prepare_lease
        if lease is None:
            raise BatchRuntimeError("journal_corrupt", "lease transition lost its active lease")
        if data.kind.endswith("lease_expired"):
            require_expired(lease.expires_at)
        else:
            require_future(lease.expires_at)
            if data.kind.endswith("renewed"):
                if data.expires_at is None:
                    raise BatchRuntimeError("journal_corrupt", "lease renewal lacks its new expiry")
                require_future(data.expires_at)
        return

    if isinstance(data, FinishedData):
        if item is None:
            raise BatchRuntimeError("journal_corrupt", "finish transition refers to an unknown item")
        lease = item.worker_lease if data.kind == "worker.finished" else item.local_prepare_lease
        if lease is None:
            raise BatchRuntimeError("journal_corrupt", "finish transition lost its active lease")
        require_future(lease.expires_at)
        return

    if isinstance(data, WriteLeaseMutationData):
        if item is None or item.write_lease is None:
            raise BatchRuntimeError("journal_corrupt", "write lease transition lost its active lease")
        if data.kind == "write.lease_expired":
            require_expired(item.write_lease.expires_at, write=True)
        else:
            require_future(item.write_lease.expires_at, write=True)
            if data.kind == "write.renewed":
                if data.expires_at is None:
                    raise BatchRuntimeError("journal_corrupt", "write renewal lacks its new expiry")
                require_future(data.expires_at, write=True)
        return

    if isinstance(data, (WriteStartedData, WriteWrittenData)):
        if item is None or item.write_lease is None:
            raise BatchRuntimeError("journal_corrupt", "write transition lost its active lease")
        require_future(item.write_lease.expires_at, write=True)
        return

    if isinstance(data, WriteUncertainData):
        if item is None or item.write_lease is None:
            raise BatchRuntimeError("journal_corrupt", "write uncertainty transition lost its active lease")
        if data.kind == "write.lease_expired_uncertain":
            require_expired(item.write_lease.expires_at, write=True)
        else:
            require_future(item.write_lease.expires_at, write=True)
        return

    if isinstance(data, RunRecoveredData):
        for resumed in data.resumed_local_prepare_leases:
            require_future(resumed.expires_at)

_EVENT_NAME = re.compile(r"^(?P<sequence>\d{20})\.json$")
_EVENT_TEMP_NAME = re.compile(
    r"^\.(?P<target>\d{20}\.json)\.(?P<digest>[0-9a-f]{64})\.tmp$"
)
_EVENT_WRITING_NAME = re.compile(r"^\.(?P<target>\d{20}\.json)\.[0-9a-f]{32}\.writing$")
_EVENT_ABORTED_NAME = re.compile(
    r"^\.aborted\.(?P<request_id>[0-9a-f-]{36})\.(?P<digest>[0-9a-f]{64})\.json$"
)
_STATE_TEMP_NAME = re.compile(r"^\.state\.json\.(?P<digest>[0-9a-f]{64})\.tmp$")
_STATE_WRITING_NAME = re.compile(r"^\.state\.json\.[0-9a-f]{32}\.writing$")
_RUN_REPLACE_TARGETS = frozenset({"state.json", "batch-report.json", "batch-report.md"})
_MAX_EVENT_DIRECTORY_ENTRIES = 200_000
_MAX_COMMITTED_EVENT_BYTES = 4 * MAX_JSON_ARTIFACT_BYTES


def _run_transition_targets(run_dir: Path) -> frozenset[str]:
    return _RUN_REPLACE_TARGETS


def _event_transition_targets(events_dir: Path) -> frozenset[str]:
    return frozenset()


def _command_for_data(data: EventData) -> str:
    if isinstance(data, RequestAbortedData):
        return "journal.abort"
    kind = data.kind
    mapping = {
        "run.initialized": "run.init",
        "run.recovered": "run.recover",
        "worker.claimed": "worker.claim",
        "worker.renewed": "worker.renew",
        "worker.released": "worker.release",
        "worker.lease_expired": "run.recover",
        "worker.finished": "worker.finish",
        "worker.retried": "worker.retry",
        "local_prepare.claimed": "local-prepare.claim",
        "local_prepare.coordination_reserved": "local-prepare.run.reserve",
        "local_prepare.renewed": "local-prepare.renew",
        "local_prepare.released": "local-prepare.release",
        "local_prepare.lease_expired": "run.recover",
        "local_prepare.finished": "local-prepare.finish",
        "write.claimed": "write.claim",
        "write.renewed": "write.renew",
        "write.released": "write.release",
        "write.lease_expired": "run.recover",
        "write.started": "write.begin",
        "write.written": "write.commit",
        "write.marked_uncertain": "write.mark-uncertain",
        "write.lease_expired_uncertain": "run.recover",
        "write.reconciled": "write.reconcile",
        "write.retried": "write.retry",
    }
    try:
        return mapping[kind]
    except KeyError as exc:  # strict event union should make this unreachable
        raise BatchRuntimeError("journal_corrupt", f"event data kind has no command binding: {kind}") from exc


def _event_digest(event: BatchEvent) -> str:
    payload = event.model_dump(mode="json")
    payload.pop("event_sha256")
    return canonical_sha256(payload)


def _aborted_marker(proposed: BatchEvent) -> BatchEvent:
    proposed_raw = canonical_json_bytes(proposed)
    data = RequestAbortedData(
        aborted_request_id=proposed.request_id,
        aborted_command=proposed.command,
        aborted_request_fingerprint=proposed.request_fingerprint,
        proposed_event_sha256=proposed.event_sha256,
        proposed_event_canonical_json=proposed_raw.decode("utf-8"),
    )
    marker_request_id = str(
        uuid5(
            NAMESPACE_URL,
            f"paper-reader-batch-abort-request:{proposed.event_sha256}",
        )
    )
    semantic_result = {
        "aborted_request_id": proposed.request_id,
        "proposed_event_sha256": proposed.event_sha256,
        "status": "request_aborted",
    }
    event_base = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "sequence": proposed.sequence,
        "event_id": str(
            uuid5(
                NAMESPACE_URL,
                f"paper-reader-batch-request-aborted:{proposed.event_sha256}",
            )
        ),
        "occurred_at": proposed.occurred_at,
        "request_id": marker_request_id,
        "command": "journal.abort",
        "request_fingerprint": canonical_sha256(data),
        "manifest_sha256": proposed.manifest_sha256,
        "previous_event_sha256": proposed.previous_event_sha256,
        "data": data.model_dump(mode="json"),
        "command_result": EventCommandResultSnapshot(
            schema_version="paper_reader_batch.command-result.v2",
            command="journal.abort",
            request_id=marker_request_id,
            semantic_result_sha256=canonical_sha256(semantic_result),
        ).model_dump(mode="json"),
    }
    return BatchEvent(
        **event_base,
        event_sha256=canonical_sha256(event_base),
    )


def _proposal_from_aborted_marker(marker: BatchEvent) -> BatchEvent:
    data = marker.data
    if not isinstance(data, RequestAbortedData):
        raise BatchRuntimeError("journal_corrupt", "event is not a request-aborted marker")
    try:
        proposed_raw = data.proposed_event_canonical_json.encode("utf-8")
        payload = json.loads(
            proposed_raw,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        proposed = BatchEvent.model_validate(payload)
    except (UnicodeEncodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise BatchRuntimeError(
            "journal_corrupt",
            "request-aborted marker embeds an invalid proposal",
        ) from exc
    if (
        isinstance(proposed.data, RequestAbortedData)
        or proposed_raw != canonical_json_bytes(proposed)
        or proposed.event_sha256 != data.proposed_event_sha256
        or proposed.event_sha256 != _event_digest(proposed)
        or proposed.command != _command_for_data(proposed.data)
        or proposed.command_result.command != proposed.command
        or proposed.command_result.request_id != proposed.request_id
        or proposed.request_id != data.aborted_request_id
        or proposed.command != data.aborted_command
        or proposed.request_fingerprint != data.aborted_request_fingerprint
        or marker != _aborted_marker(proposed)
    ):
        raise BatchRuntimeError(
            "journal_corrupt",
            "request-aborted marker does not exactly bind its rejected proposal",
        )
    return proposed


def _load_event(
    path: Path,
    *,
    expected_sequence: int,
    manifest_sha256: str,
    previous_sha256: str | None,
) -> tuple[BatchEvent, int]:
    raw, payload = read_json_bytes(path, code="journal_corrupt")
    if not isinstance(payload, dict) or payload.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise BatchRuntimeError(
            "unsupported_run_schema",
            f"event schema must be exactly {EVENT_SCHEMA_VERSION}: {path}",
        )
    try:
        event = BatchEvent.model_validate(payload)
    except ValidationError as exc:
        raise BatchRuntimeError("journal_corrupt", f"event failed strict validation: {path}") from exc
    if raw != canonical_json_bytes(event):
        raise BatchRuntimeError("journal_corrupt", f"event is not canonical JSON: {path}")
    if event.sequence != expected_sequence:
        raise BatchRuntimeError("journal_corrupt", f"event sequence mismatch at {path}")
    if event.manifest_sha256 != manifest_sha256:
        raise BatchRuntimeError("manifest_drift", f"event manifest hash mismatch at {path}")
    if event.previous_event_sha256 != previous_sha256:
        raise BatchRuntimeError("journal_corrupt", f"event previous hash mismatch at {path}")
    if _event_digest(event) != event.event_sha256:
        raise BatchRuntimeError("journal_corrupt", f"event canonical hash mismatch at {path}")
    if (
        event.command != _command_for_data(event.data)
        or event.command_result.command != event.command
        or event.command_result.request_id != event.request_id
    ):
        raise BatchRuntimeError("journal_corrupt", f"event command/result identity mismatch at {path}")
    if isinstance(event.data, RequestAbortedData):
        _proposal_from_aborted_marker(event)
    return event, len(raw)


@dataclass
class _IdentityHistoryState:
    claim_ids: set[str]
    attempt_ids: set[str]
    lease_tokens: set[str]
    reservations: dict[str, tuple[str, str]]
    authorization_sha256s: set[str]
    authorization_nonces: set[str]


def _new_identity_history_state() -> _IdentityHistoryState:
    return _IdentityHistoryState(
        claim_ids=set(),
        attempt_ids=set(),
        lease_tokens=set(),
        reservations={},
        authorization_sha256s=set(),
        authorization_nonces=set(),
    )


def _advance_identity_history(
    history: _IdentityHistoryState,
    event: BatchEvent,
    *,
    commit: bool = True,
) -> None:
    data = event.data
    if data.kind in {"worker.claimed", "local_prepare.claimed"}:
        new_claim_ids: set[str] = set()
        new_attempt_ids: set[str] = set()
        new_lease_tokens: set[str] = set()
        consumed_reservations: set[str] = set()
        for assignment in data.assignments:
            reservation = (assignment.lane, assignment.item_id)
            reserved = history.reservations.get(assignment.attempt_id) == reservation
            if (
                assignment.claim_id in history.claim_ids
                or assignment.claim_id in new_claim_ids
                or assignment.attempt_id in new_attempt_ids
                or (assignment.attempt_id in history.attempt_ids and not reserved)
                or assignment.lease_token_sha256 in history.lease_tokens
                or assignment.lease_token_sha256 in new_lease_tokens
            ):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "claim, attempt, or lease token identity is reused in journal history",
                )
            if reserved:
                consumed_reservations.add(assignment.attempt_id)
            new_claim_ids.add(assignment.claim_id)
            new_attempt_ids.add(assignment.attempt_id)
            new_lease_tokens.add(assignment.lease_token_sha256)
        if commit:
            for attempt_id in consumed_reservations:
                del history.reservations[attempt_id]
            history.claim_ids.update(new_claim_ids)
            history.attempt_ids.update(new_attempt_ids)
            history.lease_tokens.update(new_lease_tokens)
    elif data.kind == "worker.retried":
        if data.next_attempt_id in history.attempt_ids:
            raise BatchRuntimeError("journal_corrupt", "retry-bound attempt identity is reused")
        if commit:
            history.attempt_ids.add(data.next_attempt_id)
            history.reservations[data.next_attempt_id] = ("worker", data.item_id)
    elif isinstance(data, WriteClaimedData):
        reservation = ("write", data.item_id)
        reserved = history.reservations.get(data.write_attempt_id) == reservation
        if (
            data.claim_id in history.claim_ids
            or (data.write_attempt_id in history.attempt_ids and not reserved)
            or data.lease_token_sha256 in history.lease_tokens
        ):
            raise BatchRuntimeError(
                "journal_corrupt",
                "write claim, attempt, or lease token identity is reused in journal history",
            )
        if commit:
            if reserved:
                del history.reservations[data.write_attempt_id]
            history.claim_ids.add(data.claim_id)
            history.attempt_ids.add(data.write_attempt_id)
            history.lease_tokens.add(data.lease_token_sha256)
    elif isinstance(data, WriteRetriedData):
        if data.next_write_attempt_id in history.attempt_ids:
            raise BatchRuntimeError("journal_corrupt", "write retry-bound attempt identity is reused")
        if commit:
            history.attempt_ids.add(data.next_write_attempt_id)
            history.reservations[data.next_write_attempt_id] = ("write", data.item_id)
    elif isinstance(data, WriteStartedData):
        if (
            data.authorization_sha256 in history.authorization_sha256s
            or data.authorization_nonce_sha256 in history.authorization_nonces
        ):
            raise BatchRuntimeError("journal_corrupt", "write authorization or nonce is reused")
        if commit:
            history.authorization_sha256s.add(data.authorization_sha256)
            history.authorization_nonces.add(data.authorization_nonce_sha256)


def _validate_identity_history(events: list[BatchEvent]) -> None:
    history = _new_identity_history_state()
    for event in events:
        _advance_identity_history(history, event)


def _load_events(
    run_dir: Path,
    manifest: BatchManifest,
    manifest_sha256: str,
) -> tuple[
    list[BatchEvent],
    PendingEvent | None,
    tuple[BatchEvent, ...],
    tuple[AbortedResidue, ...],
    tuple[IncompleteEventWrite, ...],
    int,
    int,
]:
    events_dir = run_dir / "events"
    try:
        names = list_mutable_directory(
            events_dir,
            replace_targets=_event_transition_targets(events_dir),
            max_entries=_MAX_EVENT_DIRECTORY_ENTRIES,
        )
    except BatchRuntimeError as exc:
        if exc.code == "resource_limit":
            raise
        raise BatchRuntimeError("journal_corrupt", f"event directory is unavailable: {events_dir}") from exc
    if len(names) > _MAX_EVENT_DIRECTORY_ENTRIES:
        raise BatchRuntimeError("resource_limit", "event journal directory has too many entries")
    event_directory_entries = len(names) + int(
        entry_exists(events_dir / ".transitions")
    )
    zero_staging = [
        name
        for name in names
        if (
            _EVENT_WRITING_NAME.fullmatch(name)
            or _EVENT_TEMP_NAME.fullmatch(name)
            or _EVENT_ABORTED_NAME.fullmatch(name)
        )
        and internal_zero_tombstone(events_dir / name)
    ]
    if len(zero_staging) > 100_000:
        raise BatchRuntimeError("resource_limit", "event journal has too many logical tombstones")
    names = [name for name in names if name not in set(zero_staging)]
    event_names = [name for name in names if _EVENT_NAME.fullmatch(name)]
    temp_names = [name for name in names if _EVENT_TEMP_NAME.fullmatch(name)]
    writing_names = [name for name in names if _EVENT_WRITING_NAME.fullmatch(name)]
    aborted_names = [name for name in names if _EVENT_ABORTED_NAME.fullmatch(name)]
    if len(temp_names) + len(writing_names) + len(aborted_names) > 100_000:
        raise BatchRuntimeError("resource_limit", "event journal has too many staging or auxiliary entries")
    if len(aborted_names) > 100_000:
        raise BatchRuntimeError("resource_limit", "event journal has too many aborted request receipts")
    if (
        len(event_names)
        + len(temp_names)
        + len(writing_names)
        + len(aborted_names)
        != len(names)
    ):
        raise BatchRuntimeError("journal_corrupt", "event journal contains an unknown entry")
    if not event_names:
        raise BatchRuntimeError("journal_corrupt", "event journal is empty")
    expected_names = [f"{sequence:020d}.json" for sequence in range(1, len(event_names) + 1)]
    if event_names != expected_names:
        raise BatchRuntimeError("journal_corrupt", "event journal has a gap, duplicate, or invalid filename")
    events: list[BatchEvent] = []
    previous: str | None = None
    request_ids: set[str] = set()
    event_ids: set[str] = set()
    committed_event_bytes = 0
    for sequence, name in enumerate(event_names, start=1):
        event, event_size = _load_event(
            events_dir / name,
            expected_sequence=sequence,
            manifest_sha256=manifest_sha256,
            previous_sha256=previous,
        )
        committed_event_bytes += event_size
        if committed_event_bytes > _MAX_COMMITTED_EVENT_BYTES:
            raise BatchRuntimeError(
                "resource_limit",
                "committed event journal exceeds its aggregate byte limit",
            )
        if event.request_id in request_ids:
            raise BatchRuntimeError("journal_corrupt", f"request id appears in more than one event: {event.request_id}")
        if event.event_id in event_ids:
            raise BatchRuntimeError("journal_corrupt", f"event id appears more than once: {event.event_id}")
        request_ids.add(event.request_id)
        event_ids.add(event.event_id)
        events.append(event)
        previous = event.event_sha256
    committed_abort_proposals: dict[str, tuple[BatchEvent, BatchEvent]] = {}
    committed_abort_by_sequence: dict[int, BatchEvent] = {}
    aborted: list[BatchEvent] = []
    identity_history = _new_identity_history_state()
    prefix_state: BatchState | None = None
    for event in events:
        if isinstance(event.data, RequestAbortedData):
            if prefix_state is None:
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "request-aborted marker cannot replace the initializing event",
                )
            proposed = _proposal_from_aborted_marker(event)
            # The rejected proposal must have been a valid transition against
            # the exact committed prefix. Validate its domain identities with
            # an event-local overlay so aborted identities are not burned.
            _advance_identity_history(identity_history, proposed, commit=False)
            apply_event(prefix_state, manifest, proposed)
            if (
                proposed.event_sha256 in committed_abort_proposals
                or event.sequence in committed_abort_by_sequence
                or proposed.request_id in request_ids
                or proposed.event_id in event_ids
            ):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "request-aborted marker reuses a committed proposal identity",
                )
            committed_abort_proposals[proposed.event_sha256] = (event, proposed)
            committed_abort_by_sequence[event.sequence] = proposed
            request_ids.add(proposed.request_id)
            event_ids.add(proposed.event_id)
            aborted.append(proposed)

        if prefix_state is None:
            prefix_state = initial_state(manifest, event)
        else:
            prefix_state = apply_event(prefix_state, manifest, event)
        _advance_identity_history(identity_history, event)
    aborted_aggregate = 0
    aborted_residues: list[AbortedResidue] = []
    for name in aborted_names:
        matched = _EVENT_ABORTED_NAME.fullmatch(name)
        assert matched is not None
        raw = read_bytes(
            events_dir / name,
            code="journal_corrupt",
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
        )
        aborted_aggregate += len(raw)
        if aborted_aggregate > 64 * 1024 * 1024:
            raise BatchRuntimeError("resource_limit", "aborted event receipts exceed their aggregate limit")
        try:
            payload = json.loads(
                raw,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            event = BatchEvent.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, ValidationError) as exc:
            raise BatchRuntimeError("journal_corrupt", f"aborted event receipt is invalid: {name}") from exc
        if (
            raw != canonical_json_bytes(event)
            or event.request_id != matched.group("request_id")
            or event.event_sha256 != matched.group("digest")
            or event.event_sha256 != _event_digest(event)
            or event.command != _command_for_data(event.data)
            or event.command_result.command != event.command
            or event.command_result.request_id != event.request_id
            or event.manifest_sha256 != manifest_sha256
        ):
            raise BatchRuntimeError("journal_corrupt", f"aborted event receipt binding is invalid: {name}")
        if event.sequence < 2 or event.sequence > len(events) + 1:
            raise BatchRuntimeError("journal_corrupt", f"aborted event receipt sequence is invalid: {name}")
        prior_event = events[event.sequence - 2]
        if event.previous_event_sha256 != prior_event.event_sha256:
            raise BatchRuntimeError(
                "journal_corrupt",
                f"aborted event receipt is not bound to its committed journal prefix: {name}",
            )
        committed = committed_abort_proposals.get(event.event_sha256)
        if committed is not None:
            _marker, proposed = committed
            if event != proposed:
                raise BatchRuntimeError(
                    "journal_corrupt",
                    f"aborted event receipt differs from its committed marker: {name}",
                )
            aborted_residues.append(
                AbortedResidue(
                    path=events_dir / name,
                    raw=raw,
                    proposed_event_sha256=proposed.event_sha256,
                )
            )
            continue
        raise BatchRuntimeError(
            "journal_corrupt",
            f"aborted sidecar has no committed request-aborted marker: {name}",
        )

    def validate_pending_event(staged: BatchEvent, raw: bytes, *, label: str) -> None:
        if (
            raw != canonical_json_bytes(staged)
            or staged.sequence != len(events) + 1
            or staged.previous_event_sha256 != previous
            or staged.manifest_sha256 != manifest_sha256
            or staged.event_sha256 != _event_digest(staged)
            or staged.command != _command_for_data(staged.data)
            or staged.command_result.command != staged.command
            or staged.command_result.request_id != staged.request_id
            or staged.request_id in request_ids
            or staged.event_id in event_ids
        ):
            raise BatchRuntimeError("journal_corrupt", f"{label} is not a valid next event")
        if isinstance(staged.data, RequestAbortedData):
            _proposal_from_aborted_marker(staged)
        assert prefix_state is not None
        _advance_identity_history(identity_history, staged, commit=False)
        apply_event(prefix_state, manifest, staged)

    next_name = f"{len(events) + 1:020d}.json"
    next_sequence = len(events) + 1
    staging_aggregate = 0
    staging_raw_cache: dict[str, bytes] = {}

    def read_staging(name: str) -> bytes:
        nonlocal staging_aggregate
        cached = staging_raw_cache.get(name)
        if cached is not None:
            return cached
        raw = read_bytes(
            events_dir / name,
            code="journal_corrupt",
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
        )
        staging_aggregate += len(raw)
        if staging_aggregate > 2 * MAX_JSON_ARTIFACT_BYTES:
            raise BatchRuntimeError(
                "resource_limit",
                "event staging and residue payloads exceed their aggregate limit",
            )
        staging_raw_cache[name] = raw
        return raw

    def parse_staging(raw: bytes, *, label: str) -> BatchEvent:
        try:
            payload = json.loads(
                raw,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            return BatchEvent.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, ValidationError) as exc:
            raise BatchRuntimeError("journal_corrupt", f"{label} is invalid") from exc

    remaining_temp_names: list[str] = []
    for name in temp_names:
        matched = _EVENT_TEMP_NAME.fullmatch(name)
        assert matched is not None
        target_sequence = int(matched.group("target")[:20])
        raw = read_staging(name)
        if sha256_bytes(raw) != matched.group("digest"):
            raise BatchRuntimeError("journal_corrupt", f"event staging digest mismatch: {name}")
        if target_sequence < next_sequence:
            proposed = committed_abort_by_sequence.get(target_sequence)
            if proposed is None or raw != canonical_json_bytes(proposed):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    f"event staging file targets a committed non-abort sequence: {name}",
                )
            aborted_residues.append(
                AbortedResidue(
                    path=events_dir / name,
                    raw=raw,
                    proposed_event_sha256=proposed.event_sha256,
                )
            )
            continue
        if target_sequence > next_sequence:
            raise BatchRuntimeError("journal_corrupt", f"event staging file targets a future sequence: {name}")
        remaining_temp_names.append(name)

    remaining_writing_names: list[str] = []
    for name in writing_names:
        matched = _EVENT_WRITING_NAME.fullmatch(name)
        assert matched is not None
        target_sequence = int(matched.group("target")[:20])
        if target_sequence < next_sequence:
            raw = read_staging(name)
            proposed = committed_abort_by_sequence.get(target_sequence)
            if proposed is None or raw != canonical_json_bytes(proposed):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    f"event writing file targets a committed non-abort sequence: {name}",
                )
            aborted_residues.append(
                AbortedResidue(
                    path=events_dir / name,
                    raw=raw,
                    proposed_event_sha256=proposed.event_sha256,
                )
            )
            continue
        if target_sequence > next_sequence:
            raise BatchRuntimeError(
                "journal_corrupt",
                f"event writing entry targets a future sequence: {name}",
            )
        remaining_writing_names.append(name)

    if len(remaining_temp_names) + len(remaining_writing_names) > 2:
        raise BatchRuntimeError("journal_corrupt", "event journal has too many pending next-event attempts")

    candidates: list[PendingEvent] = []
    for name in remaining_temp_names:
        matched = _EVENT_TEMP_NAME.fullmatch(name)
        assert matched is not None and matched.group("target") == next_name
        raw = read_staging(name)
        staged = parse_staging(raw, label=f"event staging file {name}")
        validate_pending_event(staged, raw, label=f"event staging file {name}")
        candidate_path = events_dir / name
        candidates.append(
            PendingEvent(
                path=candidate_path,
                raw=raw,
                event=staged,
                proposal_path=(
                    None
                    if isinstance(staged.data, RequestAbortedData)
                    else candidate_path
                ),
            )
        )

    incomplete_writes: list[IncompleteEventWrite] = []
    partial_writes: list[tuple[str, bytes]] = []
    for name in remaining_writing_names:
        matched = _EVENT_WRITING_NAME.fullmatch(name)
        assert matched is not None
        assert matched.group("target") == next_name
        raw = read_staging(name)
        try:
            staged = parse_staging(raw, label=f"complete event writing file {name}")
        except BatchRuntimeError:
            partial_writes.append((name, raw))
            continue
        validate_pending_event(staged, raw, label=f"complete event writing file {name}")
        candidate_path = events_dir / name
        candidates.append(
            PendingEvent(
                path=candidate_path,
                raw=raw,
                event=staged,
                proposal_path=(
                    None
                    if isinstance(staged.data, RequestAbortedData)
                    else candidate_path
                ),
            )
        )

    abort_candidates = [
        candidate
        for candidate in candidates
        if isinstance(candidate.event.data, RequestAbortedData)
    ]
    proposal_candidates = [
        candidate
        for candidate in candidates
        if not isinstance(candidate.event.data, RequestAbortedData)
    ]
    if len(abort_candidates) > 1 or len(proposal_candidates) > 1:
        raise BatchRuntimeError("journal_corrupt", "event journal has conflicting pending next events")

    pending: PendingEvent | None = None
    if abort_candidates:
        marker_candidate = abort_candidates[0]
        proposed = _proposal_from_aborted_marker(marker_candidate.event)
        assert prefix_state is not None
        _advance_identity_history(identity_history, proposed, commit=False)
        apply_event(prefix_state, manifest, proposed)
        if (
            proposed.event_sha256 in committed_abort_proposals
            or proposed.request_id in request_ids
            or proposed.event_id in event_ids
        ):
            raise BatchRuntimeError(
                "journal_corrupt",
                "pending request-aborted marker reuses a committed proposal identity",
            )
        if proposal_candidates and proposal_candidates[0].event != proposed:
            raise BatchRuntimeError(
                "journal_corrupt",
                "pending request-aborted marker differs from its co-staged proposal",
            )
        if partial_writes:
            raise BatchRuntimeError(
                "journal_corrupt",
                "complete abort marker is paired with an unexpected partial event write",
            )
        pending = PendingEvent(
            path=(
                proposal_candidates[0].path
                if proposal_candidates
                else marker_candidate.path
            ),
            raw=canonical_json_bytes(proposed),
            event=proposed,
            aborting=True,
            proposal_path=(
                proposal_candidates[0].path
                if proposal_candidates
                else None
            ),
        )
    elif proposal_candidates:
        proposal_candidate = proposal_candidates[0]
        if partial_writes:
            if len(partial_writes) != 1:
                raise BatchRuntimeError("journal_corrupt", "event journal has multiple partial abort attempts")
            partial_name, partial_raw = partial_writes[0]
            expected_abort_raw = canonical_json_bytes(_aborted_marker(proposal_candidate.event))
            if not expected_abort_raw.startswith(partial_raw):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    f"partial event write is not the proposal-bound abort marker: {partial_name}",
                )
            pending = PendingEvent(
                path=proposal_candidate.path,
                raw=proposal_candidate.raw,
                event=proposal_candidate.event,
                aborting=True,
                proposal_path=proposal_candidate.path,
            )
        else:
            pending = proposal_candidate
    else:
        incomplete_writes.extend(
            IncompleteEventWrite(path=events_dir / name, raw=raw)
            for name, raw in partial_writes
        )
    if pending is not None:
        pending_commit_bytes = max(
            len(pending.raw),
            len(canonical_json_bytes(_aborted_marker(pending.event))),
        )
        if (
            committed_event_bytes + pending_commit_bytes
            > _MAX_COMMITTED_EVENT_BYTES
        ):
            raise BatchRuntimeError(
                "resource_limit",
                "pending event or its abort marker exceeds committed journal byte headroom",
            )
        if (
            not pending.aborting
            and event_directory_entries + 1 > _MAX_EVENT_DIRECTORY_ENTRIES
        ):
            raise BatchRuntimeError(
                "resource_limit",
                "pending event lacks directory headroom for a durable abort marker",
            )
    return (
        events,
        pending,
        tuple(aborted),
        tuple(aborted_residues),
        tuple(incomplete_writes),
        committed_event_bytes,
        event_directory_entries,
    )


def _validate_finished_payload(
    manifest: BatchManifest,
    event: BatchEvent,
    raw: bytes,
    *,
    full_external: bool,
    prior_view: RunView | None = None,
) -> None:
    from paper_reader_batch.v2_artifacts import (
        validate_local_prepare_result_artifacts,
        validate_worker_result_artifacts,
    )

    data = event.data
    if not isinstance(data, FinishedData):
        return
    lane = "worker" if data.kind == "worker.finished" else "local-prepare"
    try:
        payload = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise BatchRuntimeError("journal_corrupt", "finish result is invalid JSON") from exc
    model_type = WorkerResult if lane == "worker" else LocalPrepareResult
    expected_version = (
        "paper_reader_batch.worker-result.v2"
        if lane == "worker"
        else "paper_reader_batch.local-prepare-result.v2"
    )
    if not isinstance(payload, dict) or payload.get("schema_version") != expected_version:
        raise BatchRuntimeError("unsupported_run_schema", "finish result has unsupported schema")
    if sha256_bytes(raw) != data.result_sha256:
        raise BatchRuntimeError("journal_corrupt", "finish result digest does not match event")
    try:
        result = model_type.model_validate(payload)
    except ValidationError as exc:
        raise BatchRuntimeError("journal_corrupt", "finish result fails strict validation") from exc
    if raw != canonical_json_bytes(result):
        raise BatchRuntimeError("journal_corrupt", "finish result is not canonical JSON")
    manifest_item = next((item for item in manifest.items if item.item_id == data.item_id), None)
    if manifest_item is None:
        raise BatchRuntimeError("journal_corrupt", "finish result references unknown item")
    if (
        result.manifest_sha256 != event.manifest_sha256
        or result.item_id != data.item_id
        or result.worker_id != data.actor_id
        or result.claim_id != data.claim_id
        or result.attempt_id != data.attempt_id
        or result.attempt_number != data.attempt_number
        or result.lease_token_sha256 != data.lease_token_sha256
        or result.status != data.status
        or (result.error.code if result.error else None) != data.failure_code
        or (result.error.message if result.error else None) != data.failure_message
    ):
        raise BatchRuntimeError("journal_corrupt", "finish result identity does not match event/manifest")
    if lane == "worker":
        prepared_local_result = None
        if prior_view is not None:
            from paper_reader_batch.v2_worker import _load_prepared_local_result

            prior_item = next(
                (item for item in prior_view.state.items if item.item_id == data.item_id),
                None,
            )
            if prior_item is None:
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "worker finish has no event-prior reducer item",
                )
            prepared_local_result = _load_prepared_local_result(
                prior_view,
                item=prior_item,
                manifest_item=manifest_item,
            )
        resolved_key = validate_worker_result_artifacts(
            manifest,
            result,
            allow_mutable_run=not full_external,
            prepared_local_result=prepared_local_result,
        )
        candidate_sha = result.candidate.sha256 if result.candidate is not None else None
        if candidate_sha != data.candidate_sha256 or resolved_key != data.resolved_zotero_item_key:
            raise BatchRuntimeError("journal_corrupt", "worker success identities do not match event")
    else:
        validate_local_prepare_result_artifacts(
            manifest,
            result,
            allow_mutable_run=not full_external,
        )


def _validate_finished_event_result(
    run_dir: Path,
    manifest: BatchManifest,
    event: BatchEvent,
    *,
    full_external: bool = False,
    prior_view: RunView | None = None,
) -> None:
    data = event.data
    if not isinstance(data, FinishedData):
        return
    lane = "worker" if data.kind == "worker.finished" else "local-prepare"
    result_path = run_dir / "results" / lane / f"{data.result_sha256}.json"
    raw = read_bytes(
        result_path,
        code="journal_corrupt",
        max_bytes=MAX_JSON_ARTIFACT_BYTES,
    )
    _validate_finished_payload(
        manifest,
        event,
        raw,
        full_external=full_external,
        prior_view=prior_view,
    )


def _validation_view(
    *,
    run_dir: Path,
    run_dir_identity: tuple[int, int],
    manifest: BatchManifest,
    manifest_raw: bytes,
    manifest_sha256: str,
    events: list[BatchEvent],
    state: BatchState,
) -> RunView:
    return RunView(
        run_dir=run_dir,
        run_dir_identity=run_dir_identity,
        manifest=manifest,
        manifest_raw=manifest_raw,
        manifest_sha256=manifest_sha256,
        events=list(events),
        state=state,
        snapshot_raw=None,
        snapshot_status="validation",
        lease_secret=b"",
    )


def _validate_write_event_result(
    view: RunView,
    event: BatchEvent,
    *,
    raw: bytes | None = None,
    committed: bool,
) -> None:
    data = event.data
    if isinstance(data, WriteStartedData):
        from paper_reader_batch.v2_write import validate_write_started_artifacts

        try:
            validate_write_started_artifacts(view, data)
        except BatchRuntimeError as exc:
            if not committed or exc.code == "unsupported_run_schema":
                raise
            raise BatchRuntimeError(
                "journal_corrupt",
                "committed write.started no longer closes over its authorization artifacts",
            ) from exc
        return
    if isinstance(data, WriteWrittenData):
        lane = "write"
        digest = data.result_sha256
        from paper_reader_batch.v2_write import validate_write_result_payload

        validator = validate_write_result_payload
    elif isinstance(data, WriteReconciledData):
        lane = "reconcile"
        digest = data.reconciliation_sha256
        from paper_reader_batch.v2_write import validate_reconciliation_result_payload

        validator = validate_reconciliation_result_payload
    else:
        return
    payload = raw
    if payload is None:
        payload = read_bytes(
            view.run_dir / "results" / lane / f"{digest}.json",
            code="journal_corrupt",
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
        )
    try:
        validator(view, data, payload)
    except BatchRuntimeError as exc:
        if not committed or exc.code == "unsupported_run_schema":
            raise
        raise BatchRuntimeError(
            "journal_corrupt",
            f"committed {lane} result no longer closes over its event and external artifacts",
        ) from exc


def _replay_with_result_validation(
    run_dir: Path,
    run_dir_identity: tuple[int, int],
    manifest: BatchManifest,
    manifest_raw: bytes,
    manifest_sha256: str,
    events: list[BatchEvent],
) -> BatchState:
    if not events:
        raise BatchRuntimeError("journal_corrupt", "journal is empty")
    state = initial_state(manifest, events[0])
    prefix = [events[0]]
    for event in events[1:]:
        prior = _validation_view(
            run_dir=run_dir,
            run_dir_identity=run_dir_identity,
            manifest=manifest,
            manifest_raw=manifest_raw,
            manifest_sha256=manifest_sha256,
            events=prefix,
            state=state,
        )
        _validate_finished_event_result(
            run_dir,
            manifest,
            event,
            prior_view=prior,
        )
        _validate_write_event_result(prior, event, committed=True)
        state = apply_event(state, manifest, event)
        prefix.append(event)
    return state


def _load_snapshot(run_dir: Path) -> tuple[bytes | None, BatchState | None, str]:
    path = run_dir / "state.json"
    try:
        exists = entry_exists(path)
    except BatchRuntimeError as exc:
        raise BatchRuntimeError("journal_corrupt", f"snapshot path is unsafe: {path}") from exc
    if not exists:
        return None, None, "missing"
    raw = read_bytes(
        path,
        code="snapshot_invalid",
        max_bytes=MAX_JSON_ARTIFACT_BYTES,
    )
    try:
        payload = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return raw, None, "invalid"
    if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA_VERSION:
        raise BatchRuntimeError(
            "unsupported_run_schema",
            f"state schema must be exactly {STATE_SCHEMA_VERSION}: {path}",
        )
    try:
        snapshot = BatchState.model_validate(payload)
    except ValidationError:
        return raw, None, "invalid"
    if raw != canonical_json_bytes(snapshot):
        return raw, snapshot, "noncanonical"
    return raw, snapshot, "loaded"


def _supported_snapshot_repair_source(raw: bytes) -> bool:
    """Return whether the normal loader would permit replacing these bytes."""

    try:
        payload = json.loads(
            raw,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return True
    return isinstance(payload, dict) and payload.get("schema_version") == STATE_SCHEMA_VERSION


def _state_transition_id_from_identity(
    state: BatchState,
    *,
    previous_sha256: str,
    previous_size: int,
    desired_raw: bytes,
    manifest_sha256: str,
    lease_secret: bytes,
) -> str:
    # A snapshot may need repair without advancing the journal. Binding the
    # source endpoint prevents that repair from colliding with the earlier
    # transition that first published the same destination state. The MAC
    # lets owner-only recovery authenticate a retired source after its bytes
    # have already been durably removed.
    material = canonical_json_bytes(
        {
            "schema_version": "paper_reader_batch.state-transition.v2-internal",
            "manifest_sha256": manifest_sha256,
            "latest_event_sha256": state.latest_event_sha256,
            "previous_sha256": previous_sha256,
            "previous_size": previous_size,
            "desired_sha256": sha256_bytes(desired_raw),
            "desired_size": len(desired_raw),
        }
    )
    signature = hmac.new(lease_secret, material, hashlib.sha256).hexdigest()
    return f"state:{state.latest_event_sha256}:{previous_sha256}:{previous_size}:{signature}"


def _state_transition_id(
    state: BatchState,
    previous_raw: bytes,
    *,
    desired_raw: bytes,
    manifest_sha256: str,
    lease_secret: bytes,
) -> str:
    return _state_transition_id_from_identity(
        state,
        previous_sha256=sha256_bytes(previous_raw),
        previous_size=len(previous_raw),
        desired_raw=desired_raw,
        manifest_sha256=manifest_sha256,
        lease_secret=lease_secret,
    )


def _load_state_staging(
    run_dir: Path,
    state: BatchState,
    *,
    allow_pending_swap: bool = False,
) -> tuple[str | None, tuple[str, ...]]:
    names = list_mutable_directory(
        run_dir,
        replace_targets=_run_transition_targets(run_dir),
        allow_pending_swaps=allow_pending_swap,
        allowed_pending_transition_targets={"batch-report.json", "batch-report.md"},
    )
    pending = [name for name in names if _STATE_TEMP_NAME.fullmatch(name)]
    zero_writing = [
        name
        for name in names
        if _STATE_WRITING_NAME.fullmatch(name)
        and internal_zero_tombstone(run_dir / name)
    ]
    if len(zero_writing) > 64:
        raise BatchRuntimeError("resource_limit", "run has too many logical state tombstones")
    names = [name for name in names if name not in set(zero_writing)]
    writing = tuple(name for name in names if _STATE_WRITING_NAME.fullmatch(name))
    if len(pending) > 1:
        raise BatchRuntimeError("journal_corrupt", "run has multiple pending state replacements")
    if pending:
        name = pending[0]
        match = _STATE_TEMP_NAME.fullmatch(name)
        assert match is not None
        expected = canonical_json_bytes(state)
        raw = read_bytes(
            run_dir / name,
            code="snapshot_invalid",
            max_bytes=len(expected),
        )
        if sha256_bytes(raw) != match.group("digest") or raw != expected:
            raise BatchRuntimeError("snapshot_invalid", "pending state replacement does not match journal replay")
        return name, writing
    return None, writing


def _load_bound_run_view(
    root: Path,
    *,
    run_dir_identity: tuple[int, int],
    held_lease_secret: bytes | None = None,
    lock_descriptor: int | None = None,
    lock_ancestor_descriptors: tuple[int, ...] = (),
    ignore_report_swaps: bool = False,
    allow_pending_state_swap: bool = False,
) -> RunView:
    manifest, manifest_raw, manifest_sha256 = load_manifest(
        root / "manifest.json",
        validate_sources=False,
        drift_context=True,
    )
    (
        events,
        pending_event,
        aborted_events,
        aborted_residues,
        incomplete_event_residues,
        committed_event_bytes,
        event_directory_entries,
    ) = _load_events(
        root,
        manifest,
        manifest_sha256,
    )
    state = _replay_with_result_validation(
        root,
        run_dir_identity,
        manifest,
        manifest_raw,
        manifest_sha256,
        events,
    )
    if pending_event is not None:
        # Pending storage is a provisional request receipt, not journal truth.
        # Prove only that its strict payload could follow the committed prefix;
        # event-bound external closure checks run under the exact request lock,
        # where a rejected proposal can be durably retired instead of bricking
        # every read of the run.
        apply_event(state, manifest, pending_event.event)
    if lock_descriptor is not None and not allow_pending_state_swap:
        active_targets = active_transition_targets(
            root,
            replace_targets=_run_transition_targets(root),
        )
        if "state.json" in active_targets:
            pending_swap = read_pending_swap(
                root / "state.json",
                max_bytes=MAX_JSON_ARTIFACT_BYTES,
                replace_targets=_run_transition_targets(root),
            )
            committed = read_committed_transitions(
                root / "state.json",
                max_bytes=MAX_JSON_ARTIFACT_BYTES,
                replace_targets=_run_transition_targets(root),
            )
            desired_raw = canonical_json_bytes(state)

            def prefix_snapshots() -> list[bytes]:
                snapshots: list[bytes] = []
                prefix_state = initial_state(manifest, events[0])
                snapshots.append(canonical_json_bytes(prefix_state))
                for prefix_event in events[1:]:
                    prefix_state = apply_event(prefix_state, manifest, prefix_event)
                    snapshots.append(canonical_json_bytes(prefix_state))
                return snapshots

            prefixes = prefix_snapshots()

            def is_prefix(raw: bytes) -> bool:
                return raw in prefixes

            def is_allowed_previous(raw: bytes) -> bool:
                return is_prefix(raw) or _supported_snapshot_repair_source(raw)

            validate_locked_path(root / ".run.lock", lock_descriptor)
            transition_secret = (
                held_lease_secret
                if held_lease_secret is not None
                else read_locked_bytes(lock_descriptor)
            )
            previous_raw: bytes | None
            previous_sha256: str
            previous_size: int
            if pending_swap is not None:
                public_raw, slot_raw = pending_swap
                if slot_raw != desired_raw or not is_allowed_previous(public_raw):
                    raise BatchRuntimeError("unsafe_storage", "pending state transition is not bound to journal replay")
                previous_raw = public_raw
            elif committed:
                public_raw, retired_raw, _transition_name = committed[0]
                if public_raw != desired_raw or not is_allowed_previous(retired_raw):
                    raise BatchRuntimeError("unsafe_storage", "committed state transition is not bound to journal replay")
                previous_raw = retired_raw
            else:
                public_raw = read_bytes(
                    root / "state.json",
                    code="snapshot_invalid",
                    max_bytes=MAX_JSON_ARTIFACT_BYTES,
                )
                if not is_allowed_previous(public_raw):
                    raise BatchRuntimeError("unsafe_storage", "owner-only state transition has a non-prefix public snapshot")
                if public_raw == desired_raw:
                    owner_raw = read_active_transition_owner(
                        root / "state.json",
                        replace_targets=_run_transition_targets(root),
                    )
                    if owner_raw is None:
                        raise BatchRuntimeError(
                            "unsafe_storage",
                            "completed state transition has no exact active owner",
                        )
                    try:
                        owner = json.loads(owner_raw)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise BatchRuntimeError(
                            "unsafe_storage",
                            "completed state transition owner is invalid",
                        ) from exc
                    if (
                        owner.get("new_sha256") != sha256_bytes(desired_raw)
                        or owner.get("new_size") != len(desired_raw)
                        or re.fullmatch(r"[0-9a-f]{64}", str(owner.get("old_sha256"))) is None
                        or type(owner.get("old_size")) is not int
                        or owner["old_size"] < 0
                    ):
                        raise BatchRuntimeError(
                            "unsafe_storage",
                            "completed state transition owner differs from journal truth",
                        )
                    owner_transition_id = _state_transition_id_from_identity(
                        state,
                        previous_sha256=owner["old_sha256"],
                        previous_size=owner["old_size"],
                        desired_raw=desired_raw,
                        manifest_sha256=manifest_sha256,
                        lease_secret=transition_secret,
                    )
                    if owner.get("transition_id") != owner_transition_id:
                        raise BatchRuntimeError(
                            "unsafe_storage",
                            "completed state transition owner authentication failed",
                        )
                    old_matches = [
                        raw
                        for raw in prefixes
                        if sha256_bytes(raw) == owner.get("old_sha256")
                        and len(raw) == owner.get("old_size")
                    ]
                    if len(old_matches) > 1:
                        raise BatchRuntimeError(
                            "unsafe_storage",
                            "completed state transition old endpoint is ambiguous",
                        )
                    previous_raw = old_matches[0] if old_matches else None
                    previous_sha256 = owner["old_sha256"]
                    previous_size = owner["old_size"]
                else:
                    previous_raw = public_raw
            if previous_raw is not None:
                previous_sha256 = sha256_bytes(previous_raw)
                previous_size = len(previous_raw)
            transition_id = _state_transition_id_from_identity(
                state,
                previous_sha256=previous_sha256,
                previous_size=previous_size,
                desired_raw=desired_raw,
                manifest_sha256=manifest_sha256,
                lease_secret=transition_secret,
            )
            replace_bytes_atomic(
                root / "state.json",
                desired_raw,
                expected_current=previous_raw,
                expected_current_sha256=previous_sha256,
                expected_current_size=previous_size,
                transition_id=transition_id,
                allowed_transition_targets=_run_transition_targets(root),
            )
            validate_locked_path(root / ".run.lock", lock_descriptor)
    snapshot_raw, snapshot, snapshot_status = _load_snapshot(root)
    if snapshot_status == "loaded":
        snapshot_status = "current" if snapshot == state else "stale"
    state_pending_write, incomplete_state_writes = _load_state_staging(
        root,
        state,
        allow_pending_swap=allow_pending_state_swap,
    )
    lock_path = root / ".run.lock"
    if held_lease_secret is None:
        try:
            lease_secret = read_bytes(
                lock_path,
                code="lease_secret_missing",
                max_bytes=32,
            )
        except BatchRuntimeError as exc:
            if exc.code in {"lease_secret_missing", "storage_missing"}:
                raise BatchRuntimeError("lease_secret_missing", f"run lease secret is missing: {lock_path}") from exc
            raise
    else:
        lease_secret = held_lease_secret
    if len(lease_secret) != 32 or sha256_bytes(lease_secret) != state.lease_secret_sha256:
        raise BatchRuntimeError("lease_secret_mismatch", "run lease secret does not match initialized journal")
    if not ignore_report_swaps:
        active_report_targets = active_transition_targets(
            root,
            replace_targets=_run_transition_targets(root),
        )
        for report_name, limit in (
            ("batch-report.json", MAX_JSON_ARTIFACT_BYTES),
            ("batch-report.md", 512 * 1024 * 1024),
        ):
            if report_name in active_report_targets or read_pending_swap(
                root / report_name,
                max_bytes=limit,
                replace_targets=_run_transition_targets(root),
            ) is not None:
                raise BatchRuntimeError(
                    "storage_recovery_required",
                    f"batch report swap requires locked report recovery: {root / report_name}",
                )
    return RunView(
        run_dir=root,
        run_dir_identity=run_dir_identity,
        manifest=manifest,
        manifest_raw=manifest_raw,
        manifest_sha256=manifest_sha256,
        events=events,
        state=state,
        snapshot_raw=snapshot_raw,
        snapshot_status=snapshot_status,
        lease_secret=lease_secret,
        committed_event_bytes=committed_event_bytes,
        event_directory_entries=event_directory_entries,
        pending_event=pending_event,
        aborted_events=aborted_events,
        aborted_residues=aborted_residues,
        incomplete_event_writes=tuple(
            residue.path.name for residue in incomplete_event_residues
        ),
        incomplete_event_residues=incomplete_event_residues,
        state_pending_write=state_pending_write,
        incomplete_state_writes=incomplete_state_writes,
        lock_descriptor=lock_descriptor,
        lock_ancestor_descriptors=lock_ancestor_descriptors,
    )


def load_run_view(
    run_dir: Path,
    *,
    held_lease_secret: bytes | None = None,
    lock_descriptor: int | None = None,
    lock_ancestor_descriptors: tuple[int, ...] = (),
    ignore_report_swaps: bool = False,
    allow_pending_state_swap: bool = False,
) -> RunView:
    root = normalized_absolute_path(run_dir)
    with open_directory_fd(root, create=False) as (run_descriptor, bound_root):
        metadata = os.fstat(run_descriptor)
        return _load_bound_run_view(
            bound_root,
            run_dir_identity=(metadata.st_dev, metadata.st_ino),
            held_lease_secret=held_lease_secret,
            lock_descriptor=lock_descriptor,
            lock_ancestor_descriptors=lock_ancestor_descriptors,
            ignore_report_swaps=ignore_report_swaps,
            allow_pending_state_swap=allow_pending_state_swap,
        )


def load_run_view_for_mutation(run_dir: Path) -> RunView:
    """Read journal truth for mutation without changing a pending snapshot."""
    return load_run_view(run_dir, allow_pending_state_swap=True)


def _require_transaction_preflight(
    view: RunView,
    *,
    expected_manifest_sha256: str,
    expected_run_dir_identity: tuple[int, int],
) -> None:
    if view.manifest_sha256 != expected_manifest_sha256:
        raise BatchRuntimeError(
            "manifest_drift",
            "run manifest differs from the caller preflight inside the transaction lock",
        )
    if view.run_dir_identity != expected_run_dir_identity:
        raise BatchRuntimeError(
            "run_identity_drift",
            "run directory identity differs from the caller preflight inside the transaction lock",
        )


def _request_events(view: RunView) -> tuple[BatchEvent, ...]:
    return (
        tuple(view.events)
        + tuple(
            event
            for event in view.aborted_events
            if not isinstance(event.data, RequestAbortedData)
        )
        + (() if view.pending_event is None else (view.pending_event.event,))
    )


def load_request_preflight(
    run_dir: Path,
    *,
    request_id: str,
    command: str,
) -> tuple[RunView, str, BatchEvent | None]:
    """Load journal truth and resolve one request binding without recovery."""
    canonical_request_id = validate_request_id(request_id)
    view = load_run_view_for_mutation(run_dir)
    matches = tuple(
        event
        for event in _request_events(view)
        if event.request_id == canonical_request_id
    )
    if len(matches) > 1:
        raise BatchRuntimeError(
            "journal_corrupt",
            "request id is bound by more than one journal event",
        )
    event = matches[0] if matches else None
    if event is not None and event.command != command:
        raise BatchRuntimeError(
            "idempotency_conflict",
            "request id is already bound to a different journal operation",
        )
    return view, canonical_request_id, event


def _validate_request_identity(
    view: RunView,
    *,
    request_id: str | None,
    command: str | None,
    fingerprint: str | None,
) -> None:
    if request_id is None:
        return
    for event in _request_events(view):
        if event.request_id == request_id and (
            event.command != command or event.request_fingerprint != fingerprint
        ):
            raise BatchRuntimeError(
                "idempotency_conflict",
                "request id is already bound to a different journal operation or input",
            )


def _retire_pending_event(
    pending: PendingEvent,
    *,
    lock_path: Path,
    lock_descriptor: int,
) -> None:
    """Commit a no-op abort marker before treating the proposal as inert residue."""

    validate_locked_path(lock_path, lock_descriptor)
    marker = _aborted_marker(pending.event)
    marker_path = pending.path.parent / f"{marker.sequence:020d}.json"
    publish_bytes_no_replace(
        marker_path,
        canonical_json_bytes(marker),
        allow_existing_exact=True,
        guard=lambda: validate_locked_path(lock_path, lock_descriptor),
    )
    validate_locked_path(lock_path, lock_descriptor)
    if pending.proposal_path is not None and entry_exists(pending.proposal_path):
        zero_exact_staging(
            pending.proposal_path,
            pending.raw,
            guard=lambda: validate_locked_path(lock_path, lock_descriptor),
        )
        validate_locked_path(lock_path, lock_descriptor)


def _clear_aborted_residues(
    residues: tuple[AbortedResidue, ...],
    *,
    lock_path: Path,
    lock_descriptor: int,
) -> None:
    for residue in residues:
        zero_exact_staging(
            residue.path,
            residue.raw,
            guard=lambda: validate_locked_path(lock_path, lock_descriptor),
        )
    validate_locked_path(lock_path, lock_descriptor)


def _clear_incomplete_event_writes(
    residues: tuple[IncompleteEventWrite, ...],
    *,
    lock_path: Path,
    lock_descriptor: int,
) -> None:
    """Retire opaque next-sequence writes only after full live closure."""

    for residue in residues:
        zero_exact_staging(
            residue.path,
            residue.raw,
            guard=lambda: validate_locked_path(lock_path, lock_descriptor),
        )
    validate_locked_path(lock_path, lock_descriptor)


@contextmanager
def locked_run(
    run_dir: Path,
    *,
    expected_manifest_sha256: str | None = None,
    expected_run_dir_identity: tuple[int, int] | None = None,
    incoming_request_id: str | None = None,
    incoming_command: str | None = None,
    incoming_fingerprint: str | None = None,
    pre_recovery_validate: PreRecoveryValidator | None = None,
    pre_mutation_validate: PreRecoveryValidator | None = None,
    event_promotion_guard: ClosureValidator | None = None,
    allow_unrelated_residue_cleanup: bool = False,
) -> Iterator[RunView]:
    if allow_unrelated_residue_cleanup and (
        pre_recovery_validate is None or pre_mutation_validate is None
    ):
        raise ValueError(
            "unrelated residue cleanup requires both pre-recovery and pre-mutation validation"
        )
    # This first view replays journal truth in memory but deliberately leaves
    # event/state publication transitions untouched. Domain identity must be
    # rejected before acquiring a path that can perform recovery writes.
    preflight = load_run_view_for_mutation(run_dir)
    _validate_request_identity(
        preflight,
        request_id=incoming_request_id,
        command=incoming_command,
        fingerprint=incoming_fingerprint,
    )
    try:
        lock_path = preflight.run_dir / ".run.lock"
        inherited_lock_descriptors: list[int] = []
        with locked_file(
            lock_path,
            create=False,
            inherited_lock_descriptors=inherited_lock_descriptors,
        ) as descriptor:
            ancestor_descriptors = tuple(inherited_lock_descriptors)
            lease_secret = read_locked_bytes(descriptor)
            validate_locked_path(lock_path, descriptor)
            # Rebuild the authoritative journal-truth view while holding the
            # lock, but do not recover pending event/state storage yet.
            pre_recovery = load_run_view(
                preflight.run_dir,
                held_lease_secret=lease_secret,
                lock_descriptor=descriptor,
                lock_ancestor_descriptors=ancestor_descriptors,
                allow_pending_state_swap=True,
            )
            _require_transaction_preflight(
                pre_recovery,
                expected_manifest_sha256=(
                    preflight.manifest_sha256
                    if expected_manifest_sha256 is None
                    else expected_manifest_sha256
                ),
                expected_run_dir_identity=(
                    preflight.run_dir_identity
                    if expected_run_dir_identity is None
                    else expected_run_dir_identity
                ),
            )
            _validate_request_identity(
                pre_recovery,
                request_id=incoming_request_id,
                command=incoming_command,
                fingerprint=incoming_fingerprint,
            )
            exact_aborted_origin = next(
                (
                    event
                    for event in pre_recovery.aborted_events
                    if event.request_id == incoming_request_id
                    and event.command == incoming_command
                    and event.request_fingerprint == incoming_fingerprint
                ),
                None,
            )
            # An exact replay of the durably aborted request may retire its
            # marker-bound storage residue before returning request_aborted.
            # Unrelated requests must first pass every domain/source closure
            # check so an invalid request remains physically read-only.
            if pre_recovery.aborted_residues and exact_aborted_origin is not None:
                origin_residues = tuple(
                    residue
                    for residue in pre_recovery.aborted_residues
                    if residue.proposed_event_sha256
                    == exact_aborted_origin.event_sha256
                )
                _clear_aborted_residues(
                    origin_residues,
                    lock_path=lock_path,
                    lock_descriptor=descriptor,
                )
                pre_recovery = load_run_view(
                    preflight.run_dir,
                    held_lease_secret=lease_secret,
                    lock_descriptor=descriptor,
                    lock_ancestor_descriptors=ancestor_descriptors,
                    allow_pending_state_swap=True,
                )
                _require_transaction_preflight(
                    pre_recovery,
                    expected_manifest_sha256=(
                        preflight.manifest_sha256
                        if expected_manifest_sha256 is None
                        else expected_manifest_sha256
                    ),
                    expected_run_dir_identity=(
                        preflight.run_dir_identity
                        if expected_run_dir_identity is None
                        else expected_run_dir_identity
                    ),
                )
                _validate_request_identity(
                    pre_recovery,
                    request_id=incoming_request_id,
                    command=incoming_command,
                    fingerprint=incoming_fingerprint,
                )
            if (
                pre_recovery.pending_event is not None
                and (
                    incoming_request_id is None
                    or pre_recovery.pending_event.event.request_id != incoming_request_id
                )
            ):
                raise BatchRuntimeError(
                    "storage_recovery_required",
                    "pending journal event must be recovered by its exact originating request",
                )
            if pre_recovery.pending_event is not None and pre_recovery.pending_event.aborting:
                _retire_pending_event(
                    pre_recovery.pending_event,
                    lock_path=lock_path,
                    lock_descriptor=descriptor,
                )
                pre_recovery = load_run_view(
                    preflight.run_dir,
                    held_lease_secret=lease_secret,
                    lock_descriptor=descriptor,
                    lock_ancestor_descriptors=ancestor_descriptors,
                    allow_pending_state_swap=True,
                )
                _require_transaction_preflight(
                    pre_recovery,
                    expected_manifest_sha256=(
                        preflight.manifest_sha256
                        if expected_manifest_sha256 is None
                        else expected_manifest_sha256
                    ),
                    expected_run_dir_identity=(
                        preflight.run_dir_identity
                        if expected_run_dir_identity is None
                        else expected_run_dir_identity
                    ),
                )
                _validate_request_identity(
                    pre_recovery,
                    request_id=incoming_request_id,
                    command=incoming_command,
                    fingerprint=incoming_fingerprint,
                )
            try:
                if pre_recovery_validate is not None:
                    pre_recovery_validate(pre_recovery)
                # The first callback may deliberately pause or invoke a fault
                # hook. Rebind live external inputs immediately before any
                # pending event/state publication is recovered.
                if pre_mutation_validate is not None:
                    pre_mutation_validate(pre_recovery)
            except BatchRuntimeError:
                if (
                    pre_recovery.pending_event is not None
                    and incoming_request_id is not None
                    and pre_recovery.pending_event.event.request_id == incoming_request_id
                ):
                    _retire_pending_event(
                        pre_recovery.pending_event,
                        lock_path=lock_path,
                        lock_descriptor=descriptor,
                    )
                raise

            if allow_unrelated_residue_cleanup and (
                pre_recovery.aborted_residues
                or pre_recovery.incomplete_event_residues
            ):
                if pre_recovery.aborted_residues:
                    _clear_aborted_residues(
                        pre_recovery.aborted_residues,
                        lock_path=lock_path,
                        lock_descriptor=descriptor,
                    )
                if pre_recovery.incomplete_event_residues:
                    _clear_incomplete_event_writes(
                        pre_recovery.incomplete_event_residues,
                        lock_path=lock_path,
                        lock_descriptor=descriptor,
                    )
                pre_recovery = load_run_view(
                    preflight.run_dir,
                    held_lease_secret=lease_secret,
                    lock_descriptor=descriptor,
                    lock_ancestor_descriptors=ancestor_descriptors,
                    allow_pending_state_swap=True,
                )
                _require_transaction_preflight(
                    pre_recovery,
                    expected_manifest_sha256=(
                        preflight.manifest_sha256
                        if expected_manifest_sha256 is None
                        else expected_manifest_sha256
                    ),
                    expected_run_dir_identity=(
                        preflight.run_dir_identity
                        if expected_run_dir_identity is None
                        else expected_run_dir_identity
                    ),
                )
                _validate_request_identity(
                    pre_recovery,
                    request_id=incoming_request_id,
                    command=incoming_command,
                    fingerprint=incoming_fingerprint,
                )

            # Only a fully validated request may cross into durable recovery.
            current = load_run_view(
                preflight.run_dir,
                held_lease_secret=lease_secret,
                lock_descriptor=descriptor,
                lock_ancestor_descriptors=ancestor_descriptors,
            )
            _require_transaction_preflight(
                current,
                expected_manifest_sha256=(
                    preflight.manifest_sha256
                    if expected_manifest_sha256 is None
                    else expected_manifest_sha256
                ),
                expected_run_dir_identity=(
                    preflight.run_dir_identity
                    if expected_run_dir_identity is None
                    else expected_run_dir_identity
                ),
            )
            _validate_request_identity(
                current,
                request_id=incoming_request_id,
                command=incoming_command,
                fingerprint=incoming_fingerprint,
            )
            if current.pending_event is not None:
                pending_event = current.pending_event.event
                if (
                    incoming_request_id is None
                    or pending_event.request_id != incoming_request_id
                ):
                    raise BatchRuntimeError(
                        "storage_recovery_required",
                        "pending journal event must be recovered by its exact originating request",
                    )
                if (
                    incoming_request_id is not None
                    and pending_event.request_id == incoming_request_id
                    and (
                        pending_event.command != incoming_command
                        or pending_event.request_fingerprint != incoming_fingerprint
                    )
                ):
                    raise BatchRuntimeError(
                        "idempotency_conflict",
                        "request id is already bound to a different pending journal operation or input",
                    )
                validate_locked_path(lock_path, descriptor)
                pending = current.pending_event
                pending_target = (
                    pending.path.parent / f"{pending.event.sequence:020d}.json"
                )

                def pending_precommit_guard() -> None:
                    validate_locked_path(lock_path, descriptor)
                    if not entry_exists(pending_target) and event_promotion_guard is not None:
                        event_promotion_guard()

                try:
                    promote_bytes_no_replace(
                        pending.path,
                        pending_target,
                        pending.raw,
                        guard=pending_precommit_guard,
                    )
                except BatchRuntimeError:
                    if (
                        not entry_exists(pending_target)
                        and pending.event.request_id == incoming_request_id
                    ):
                        _retire_pending_event(
                            pending,
                            lock_path=lock_path,
                            lock_descriptor=descriptor,
                        )
                    raise
                current = load_run_view(
                    preflight.run_dir,
                    held_lease_secret=lease_secret,
                    lock_descriptor=descriptor,
                    lock_ancestor_descriptors=ancestor_descriptors,
                )
                _persist_snapshot(current, current.state)
                current = load_run_view(
                    preflight.run_dir,
                    held_lease_secret=lease_secret,
                    lock_descriptor=descriptor,
                    lock_ancestor_descriptors=ancestor_descriptors,
                )
            if current.incomplete_event_writes:
                next_sequence = current.state.next_sequence
                for name in current.incomplete_event_writes:
                    matched = _EVENT_WRITING_NAME.fullmatch(name)
                    if matched is None:
                        raise BatchRuntimeError("journal_corrupt", "invalid internal event writing name")
                    target_sequence = int(matched.group("target")[:20])
                    if target_sequence > next_sequence:
                        raise BatchRuntimeError("journal_corrupt", "event writing entry targets a future sequence")
            if current.state_pending_write is not None:
                if current.snapshot_status != "current":
                    _persist_snapshot(current, current.state)
            if current.incomplete_state_writes:
                for name in current.incomplete_state_writes:
                    if _STATE_WRITING_NAME.fullmatch(name) is None:
                        raise BatchRuntimeError("journal_corrupt", "invalid internal state writing name")
            try:
                yield current
            except BatchRuntimeError:
                (
                    _events,
                    pending,
                    _aborted,
                    _residues,
                    _incomplete,
                    _committed_event_bytes,
                    _event_directory_entries,
                ) = _load_events(
                    preflight.run_dir,
                    preflight.manifest,
                    preflight.manifest_sha256,
                )
                if (
                    pending is not None
                    and incoming_request_id is not None
                    and pending.event.request_id == incoming_request_id
                ):
                    _retire_pending_event(
                        pending,
                        lock_path=lock_path,
                        lock_descriptor=descriptor,
                    )
                raise
    except BatchRuntimeError as exc:
        if exc.code == "storage_missing":
            raise BatchRuntimeError("lease_secret_missing", "run lease secret disappeared before lock") from exc
        raise


def append_transaction(
    run_dir: Path,
    *,
    expected_manifest_sha256: str,
    expected_run_dir_identity: tuple[int, int],
    request_id: str,
    command: str,
    request_fingerprint: str,
    occurred_at: str | None,
    propose: Proposal,
    reconstruct: Reconstructor,
    replay_validate: ReplayValidator | None = None,
    commit_validate: CommitValidator | None = None,
    commit_guard: CommitGuardFactory | None = None,
    final_freshness_validate: FinalFreshnessValidator | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    canonical_request_id = validate_request_id(request_id)
    # Fix the transaction clock once. A validation pass and the eventual
    # commit must never observe different implicit times.
    # Explicit test/replay clocks are fixed by the caller.  Production time is
    # sampled lazily only after the authoritative run lock is held; otherwise
    # time spent waiting for the lock can consume a lease before its event is
    # even proposed.
    authoritative_time = occurred_at
    validated_transition: ProposedTransition | None = None
    validated_state_sha256: str | None = None
    deferred_proposal_error: BatchRuntimeError | None = None
    commit_stack = ExitStack()
    guarded_validate: ClosureValidator = lambda: None
    freshness_view: RunView | None = None
    freshness_data: EventData | None = None

    def mutation_guard() -> None:
        guarded_validate()
        if freshness_data is None:
            return
        if freshness_view is None:
            raise BatchRuntimeError(
                "journal_corrupt",
                "transaction freshness guard has no authoritative run view",
            )
        effective_now = occurred_at if occurred_at is not None else utc_now()
        _validate_transition_temporal_freshness(
            freshness_view,
            freshness_data,
            effective_now=effective_now,
        )
        if final_freshness_validate is not None:
            final_freshness_validate(freshness_view, effective_now)

    def transaction_time() -> str:
        nonlocal authoritative_time
        if authoritative_time is None:
            authoritative_time = utc_now()
        return authoritative_time

    def validate_time(view: RunView) -> None:
        fixed_time = transaction_time()
        try:
            current_time = datetime.fromisoformat(fixed_time[:-1] + "+00:00")
            previous_time = datetime.fromisoformat(
                view.events[-1].occurred_at[:-1] + "+00:00"
            )
        except (ValueError, IndexError) as exc:
            raise BatchRuntimeError("invalid_timestamp", "transaction timestamp is invalid") from exc
        if not fixed_time.endswith("Z") or current_time < previous_time:
            raise BatchRuntimeError(
                "nonmonotonic_time",
                "transaction time must not precede the latest authoritative journal event",
            )

    def validate_before_recovery(view: RunView) -> None:
        nonlocal validated_transition, validated_state_sha256, deferred_proposal_error, guarded_validate, freshness_view, freshness_data
        matches = [
            event
            for event in _request_events(view)
            if event.request_id == canonical_request_id
        ]
        if len(matches) > 1:
            raise BatchRuntimeError(
                "journal_corrupt",
                "request id is bound by more than one journal event",
            )
        if matches:
            event = matches[0]
            if any(
                aborted.event_sha256 == event.event_sha256
                for aborted in view.aborted_events
            ):
                raise BatchRuntimeError(
                    "request_aborted",
                    "request was durably rejected before journal commit; use a new request id",
                )
            if replay_validate is not None:
                replay_validate(view, event)
            # Exact replay must validate the event-bound semantic result before
            # a pending event or state transition is promoted.
            result = reconstruct(view, event)
            if canonical_sha256(result) != event.command_result.semantic_result_sha256:
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "replayed semantic result does not match event binding",
                )
            if (
                view.pending_event is not None
                and view.pending_event.event.event_sha256 == event.event_sha256
            ):
                prior_state = initial_state(view.manifest, view.events[0])
                for prior_event in view.events[1:]:
                    prior_state = apply_event(prior_state, view.manifest, prior_event)
                prior_view = replace(view, state=prior_state, pending_event=None)
                if commit_guard is not None:
                    guarded_validate = commit_stack.enter_context(
                        commit_guard(prior_view, event)
                    )
                closure_validate = guarded_validate

                def validate_pending_semantics() -> None:
                    closure_validate()
                    if replay_validate is not None:
                        replay_validate(prior_view, event)

                guarded_validate = validate_pending_semantics
                guarded_validate()
                freshness_view = prior_view
                freshness_data = event.data
            return
        validate_time(view)
        # Proposal validation is deliberately given a read-only view. Helpers
        # key pending coordination recovery off lock_descriptor; clearing the
        # descriptor makes any such recovery fail closed instead of mutating.
        validation_view = replace(
            view,
            lock_descriptor=None,
            lock_ancestor_descriptors=(),
        )
        validated_state_sha256 = canonical_sha256(view.state)
        try:
            validated_transition = propose(validation_view, transaction_time())
        except BatchRuntimeError as exc:
            # A durable pending event is already authoritative. A later claim
            # may therefore have no remaining work, but it is still allowed to
            # forward-complete that exact prior event before reporting the
            # state-gate result. Identity/source errors remain immediate and
            # cannot cross into recovery.
            if view.pending_event is None or exc.code != "no_available_work":
                raise
            deferred_proposal_error = exc
        if validated_transition is not None and commit_guard is not None:
            guarded_validate = commit_stack.enter_context(commit_guard(view, None))
            guarded_validate()
        freshness_view = view
        freshness_data = None if validated_transition is None else validated_transition.data
        if fault is not None:
            fault("after_pre_recovery_validation")
        mutation_guard()

    def validate_live_before_recovery(view: RunView) -> None:
        nonlocal freshness_view, freshness_data
        freshness_view = view
        matches = [
            event
            for event in _request_events(view)
            if event.request_id == canonical_request_id
        ]
        if len(matches) > 1:
            raise BatchRuntimeError(
                "journal_corrupt",
                "request id is bound by more than one journal event",
            )
        if matches:
            event = matches[0]
            if any(
                aborted.event_sha256 == event.event_sha256
                for aborted in view.aborted_events
            ):
                raise BatchRuntimeError(
                    "request_aborted",
                    "request was durably rejected before journal commit; use a new request id",
                )
            if replay_validate is not None:
                replay_validate(view, event)
            if (
                view.pending_event is not None
                and view.pending_event.event.event_sha256 == event.event_sha256
            ):
                prior_state = initial_state(view.manifest, view.events[0])
                for prior_event in view.events[1:]:
                    prior_state = apply_event(prior_state, view.manifest, prior_event)
                freshness_view = replace(view, state=prior_state, pending_event=None)
                freshness_data = event.data
            mutation_guard()
            return
        if deferred_proposal_error is not None:
            if validated_state_sha256 != canonical_sha256(view.state):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "journal truth changed after deferred proposal validation",
                )
            if commit_validate is not None:
                commit_validate(view)
            return
        if validated_transition is None or validated_state_sha256 is None:
            raise BatchRuntimeError(
                "journal_corrupt",
                "transaction proposal was not validated before recovery",
            )
        if validated_state_sha256 != canonical_sha256(view.state):
            raise BatchRuntimeError(
                "journal_corrupt",
                "journal truth changed after proposal validation",
            )
        if commit_validate is not None:
            commit_validate(view)
        mutation_guard()

    with commit_stack, locked_run(
            run_dir,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_run_dir_identity=expected_run_dir_identity,
            incoming_request_id=canonical_request_id,
            incoming_command=command,
            incoming_fingerprint=request_fingerprint,
            pre_recovery_validate=validate_before_recovery,
            pre_mutation_validate=validate_live_before_recovery,
            event_promotion_guard=mutation_guard,
            allow_unrelated_residue_cleanup=True,
        ) as view:
        freshness_view = view
        if view.lock_descriptor is None:  # pragma: no cover - locked_run always supplies it
            raise BatchRuntimeError("storage_error", "journal transaction lacks held lock descriptor")
        lock_path = view.run_dir / ".run.lock"
        for event in view.events:
            if event.request_id != canonical_request_id:
                continue
            if event.command != command or event.request_fingerprint != request_fingerprint:
                raise BatchRuntimeError(
                    "idempotency_conflict",
                    "request id is already bound to another journal operation or input",
                )
            # Recovery can take an arbitrary amount of time. Rebind live
            # inputs after it and before repairing the snapshot or returning a
            # replayed result.
            if replay_validate is not None:
                replay_validate(view, event)
            result = reconstruct(view, event)
            if canonical_sha256(result) != event.command_result.semantic_result_sha256:
                raise BatchRuntimeError("journal_corrupt", "replayed semantic result does not match event binding")
            if view.snapshot_status != "current":
                _persist_snapshot(view, view.state, fault=fault)
            return RequestOutcome(result=result, replayed=True)

        if deferred_proposal_error is not None:
            if validated_state_sha256 != canonical_sha256(view.state):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "journal truth changed before deferred proposal result",
                )
            raise deferred_proposal_error
        if validated_transition is None or validated_state_sha256 is None:
            raise BatchRuntimeError(
                "journal_corrupt",
                "transaction proposal was not validated before recovery",
            )
        if validated_state_sha256 != canonical_sha256(view.state):
            raise BatchRuntimeError(
                "journal_corrupt",
                "journal truth changed between proposal validation and commit",
            )
        transition = validated_transition
        if commit_validate is not None:
            commit_validate(view)
        if fault is not None:
            fault("after_commit_validation")

        mutation_guard()

        if view.snapshot_status != "current" and command != "run.recover":
            _persist_snapshot(view, view.state, fault=fault)
            view = load_run_view(
                view.run_dir,
                held_lease_secret=view.lease_secret,
                lock_descriptor=view.lock_descriptor,
            )
            freshness_view = view

        validate_time(view)
        fixed_time = transaction_time()
        sequence = view.state.next_sequence
        event_base = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": sequence,
            "event_id": str(uuid4()),
            "occurred_at": fixed_time,
            "request_id": canonical_request_id,
            "command": command,
            "request_fingerprint": request_fingerprint,
            "manifest_sha256": view.manifest_sha256,
            "previous_event_sha256": view.state.latest_event_sha256,
            "data": transition.data.model_dump(mode="json"),
            "command_result": EventCommandResultSnapshot(
                schema_version="paper_reader_batch.command-result.v2",
                command=command,
                request_id=canonical_request_id,
                semantic_result_sha256=canonical_sha256(transition.result),
            ).model_dump(mode="json"),
        }
        event = BatchEvent(**event_base, event_sha256=canonical_sha256(event_base))
        event_raw = canonical_json_bytes(event)
        abort_marker_raw = canonical_json_bytes(_aborted_marker(event))
        if (
            len(event_raw) > MAX_JSON_ARTIFACT_BYTES
            or len(abort_marker_raw) > MAX_JSON_ARTIFACT_BYTES
        ):
            raise BatchRuntimeError(
                "resource_limit",
                "event or its durable request-aborted marker exceeds the JSON artifact limit",
            )
        if (
            view.committed_event_bytes
            + max(len(event_raw), len(abort_marker_raw))
            > _MAX_COMMITTED_EVENT_BYTES
        ):
            raise BatchRuntimeError(
                "resource_limit",
                "event or its durable request-aborted marker would exceed the committed journal byte limit",
            )
        if (
            view.event_directory_entries + 2
            > _MAX_EVENT_DIRECTORY_ENTRIES
        ):
            raise BatchRuntimeError(
                "resource_limit",
                "event journal lacks directory headroom for proposal and abort-marker recovery",
            )
        expected_lane: str | None = None
        expected_digest: str | None = None
        if isinstance(event.data, FinishedData):
            expected_lane = "worker" if event.data.kind == "worker.finished" else "local-prepare"
            expected_digest = event.data.result_sha256
        elif isinstance(event.data, WriteWrittenData):
            expected_lane = "write"
            expected_digest = event.data.result_sha256
        elif isinstance(event.data, WriteReconciledData):
            expected_lane = "reconcile"
            expected_digest = event.data.reconciliation_sha256
        if expected_lane is not None:
            if transition.publication is None:
                raise BatchRuntimeError(
                    "invalid_transition",
                    f"{event.data.kind} requires its exact result publication",
                )
            expected_path = view.run_dir / "results" / expected_lane / f"{expected_digest}.json"
            if normalized_absolute_path(transition.publication.path) != expected_path:
                raise BatchRuntimeError(
                    "invalid_result_path",
                    f"{event.data.kind} result publication lane/path differs from event",
                )
        elif transition.publication is not None:
            raise BatchRuntimeError("invalid_transition", "this event type cannot publish a result")
        if isinstance(event.data, FinishedData):
            assert transition.publication is not None
            _validate_finished_payload(
                view.manifest,
                event,
                transition.publication.content,
                full_external=True,
            )
        elif isinstance(event.data, (WriteWrittenData, WriteReconciledData)):
            assert transition.publication is not None
            _validate_write_event_result(
                view,
                event,
                raw=transition.publication.content,
                committed=False,
            )
        elif isinstance(event.data, WriteStartedData):
            _validate_write_event_result(view, event, committed=False)
        mutation_guard()
        _validate_identity_history([*view.events, event])
        updated_state = apply_event(view.state, view.manifest, event)
        if transition.publication is not None:
            publication = transition.publication
            publication_path = normalized_absolute_path(publication.path)
            publication_digest = sha256_bytes(publication.content)
            try:
                relative = publication_path.relative_to(view.run_dir)
            except ValueError as exc:
                raise BatchRuntimeError("invalid_result_path", "result publication is outside the run") from exc
            if (
                len(relative.parts) != 3
                or relative.parts[0] != "results"
                or relative.parts[1] not in {"worker", "local-prepare", "write", "reconcile"}
                or relative.parts[2] != f"{publication_digest}.json"
                or relative.parts[1] != expected_lane
                or publication_digest != expected_digest
            ):
                raise BatchRuntimeError(
                    "invalid_result_path",
                    "result must use its event-bound results/<lane>/<sha256>.json path",
                )
            try:
                payload = json.loads(publication.content)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BatchRuntimeError("invalid_result", "result publication is not JSON") from exc
            if canonical_json_bytes(payload) != publication.content:
                raise BatchRuntimeError("invalid_result", "result publication must use canonical JSON bytes")
            validate_locked_path(lock_path, view.lock_descriptor)
            publish_bytes_no_replace(
                publication_path,
                publication.content,
                allow_existing_exact=True,
                guard=mutation_guard,
            )
            if fault is not None:
                fault("after_result")
            mutation_guard()
        event_path = view.run_dir / "events" / f"{sequence:020d}.json"

        def event_storage_guard() -> None:
            validate_locked_path(lock_path, view.lock_descriptor)

        def event_semantic_precommit_guard() -> None:
            if commit_validate is not None:
                commit_validate(view)
            mutation_guard()

        if fault is not None:
            fault("before_event")
        mutation_guard()
        validate_locked_path(lock_path, view.lock_descriptor)
        try:
            publish_bytes_no_replace(
                event_path,
                event_raw,
                fault=fault,
                guard=event_storage_guard,
                precommit_guard=event_semantic_precommit_guard,
            )
        except BatchRuntimeError as exc:
            if exc.code == "output_conflict":
                raise BatchRuntimeError("journal_corrupt", f"event sequence was occupied concurrently: {event_path}") from exc
            raise
        if fault is not None:
            fault("after_event")
        if fault is not None:
            fault("before_snapshot")
        _persist_snapshot(view, updated_state, fault=fault)
        if fault is not None:
            fault("after_snapshot")
        return RequestOutcome(result=transition.result, replayed=False)


def _persist_snapshot(view: RunView, state: BatchState, *, fault: FaultHook | None = None) -> None:
    if view.lock_descriptor is None:
        raise BatchRuntimeError("storage_error", "snapshot mutation requires the held run lock")
    validate_locked_path(view.run_dir / ".run.lock", view.lock_descriptor)
    state_path = view.run_dir / "state.json"
    state_bytes = canonical_json_bytes(state)
    if view.snapshot_raw == state_bytes:
        return
    if view.snapshot_raw is None:
        publish_bytes_no_replace(state_path, state_bytes, fault=fault)
    else:
        replace_bytes_atomic(
            state_path,
            state_bytes,
            expected_current=view.snapshot_raw,
            transition_id=_state_transition_id(
                state,
                view.snapshot_raw,
                desired_raw=state_bytes,
                manifest_sha256=view.manifest_sha256,
                lease_secret=view.lease_secret,
            ),
            allowed_transition_targets=_run_transition_targets(view.run_dir),
            fault=fault,
        )


def status_result(view: RunView) -> dict[str, JsonValue]:
    from paper_reader_batch.v2_next_actions import derive_next_actions

    return {
        "run_dir": str(view.run_dir),
        "manifest_sha256": view.manifest_sha256,
        "snapshot_status": view.snapshot_status,
        "pending_event_sequence": view.pending_event.event.sequence if view.pending_event is not None else None,
        "incomplete_event_writes": list(view.incomplete_event_writes),
        "state_pending_write": view.state_pending_write,
        "incomplete_state_writes": list(view.incomplete_state_writes),
        "state": view.state.model_dump(mode="json"),
        "next_actions": derive_next_actions(view),
    }
