from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterator
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from paper_reader_batch.v2_contracts import (
    EVENT_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    BatchEvent,
    BatchManifest,
    BatchState,
    EventCommandResultSnapshot,
    EventData,
    FinishedData,
    LocalPrepareResult,
    WorkerResult,
    WriteClaimedData,
    WriteReconciledData,
    WriteRetriedData,
    WriteStartedData,
    WriteWrittenData,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    canonical_json_bytes,
    canonical_sha256,
    entry_exists,
    list_directory,
    locked_file,
    normalized_absolute_path,
    open_directory_fd,
    publish_bytes_no_replace,
    promote_bytes_no_replace,
    read_bytes,
    read_json_bytes,
    read_locked_bytes,
    replace_bytes_atomic,
    sha256_bytes,
    unlink_internal_regular,
    utc_now,
    validate_locked_path,
)
from paper_reader_batch.v2_manifest import load_manifest
from paper_reader_batch.v2_receipts import FaultHook, RequestOutcome, validate_request_id
from paper_reader_batch.v2_reducer import apply_event, initial_state


@dataclass(frozen=True)
class PendingEvent:
    path: Path
    raw: bytes
    event: BatchEvent


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
    pending_event: PendingEvent | None = None
    incomplete_event_writes: tuple[str, ...] = ()
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

_EVENT_NAME = re.compile(r"^(?P<sequence>\d{20})\.json$")
_EVENT_TEMP_NAME = re.compile(
    r"^\.(?P<target>\d{20}\.json)\.(?P<digest>[0-9a-f]{64})\.tmp$"
)
_EVENT_WRITING_NAME = re.compile(r"^\.(?P<target>\d{20}\.json)\.[0-9a-f]{32}\.writing$")
_STATE_TEMP_NAME = re.compile(r"^\.state\.json\.(?P<digest>[0-9a-f]{64})\.tmp$")
_STATE_WRITING_NAME = re.compile(r"^\.state\.json\.[0-9a-f]{32}\.writing$")


def _command_for_kind(kind: str) -> str:
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


def _load_event(path: Path, *, expected_sequence: int, manifest_sha256: str, previous_sha256: str | None) -> BatchEvent:
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
        event.command != _command_for_kind(event.data.kind)
        or event.command_result.command != event.command
        or event.command_result.request_id != event.request_id
    ):
        raise BatchRuntimeError("journal_corrupt", f"event command/result identity mismatch at {path}")
    return event


def _validate_identity_history(events: list[BatchEvent]) -> None:
    claim_ids: set[str] = set()
    attempt_ids: set[str] = set()
    lease_tokens: set[str] = set()
    reservations: dict[str, tuple[str, str]] = {}
    authorization_sha256s: set[str] = set()
    authorization_nonces: set[str] = set()
    for event in events:
        data = event.data
        if data.kind in {"worker.claimed", "local_prepare.claimed"}:
            for assignment in data.assignments:
                reservation = (assignment.lane, assignment.item_id)
                reserved = reservations.get(assignment.attempt_id) == reservation
                if (
                    assignment.claim_id in claim_ids
                    or (assignment.attempt_id in attempt_ids and not reserved)
                    or assignment.lease_token_sha256 in lease_tokens
                ):
                    raise BatchRuntimeError(
                        "journal_corrupt",
                        "claim, attempt, or lease token identity is reused in journal history",
                    )
                if reserved:
                    del reservations[assignment.attempt_id]
                claim_ids.add(assignment.claim_id)
                attempt_ids.add(assignment.attempt_id)
                lease_tokens.add(assignment.lease_token_sha256)
        elif data.kind == "worker.retried":
            if data.next_attempt_id in attempt_ids:
                raise BatchRuntimeError("journal_corrupt", "retry-bound attempt identity is reused")
            attempt_ids.add(data.next_attempt_id)
            reservations[data.next_attempt_id] = ("worker", data.item_id)
        elif isinstance(data, WriteClaimedData):
            reservation = ("write", data.item_id)
            reserved = reservations.get(data.write_attempt_id) == reservation
            if (
                data.claim_id in claim_ids
                or (data.write_attempt_id in attempt_ids and not reserved)
                or data.lease_token_sha256 in lease_tokens
            ):
                raise BatchRuntimeError(
                    "journal_corrupt",
                    "write claim, attempt, or lease token identity is reused in journal history",
                )
            if reserved:
                del reservations[data.write_attempt_id]
            claim_ids.add(data.claim_id)
            attempt_ids.add(data.write_attempt_id)
            lease_tokens.add(data.lease_token_sha256)
        elif isinstance(data, WriteRetriedData):
            if data.next_write_attempt_id in attempt_ids:
                raise BatchRuntimeError("journal_corrupt", "write retry-bound attempt identity is reused")
            attempt_ids.add(data.next_write_attempt_id)
            reservations[data.next_write_attempt_id] = ("write", data.item_id)
        elif isinstance(data, WriteStartedData):
            if (
                data.authorization_sha256 in authorization_sha256s
                or data.authorization_nonce_sha256 in authorization_nonces
            ):
                raise BatchRuntimeError("journal_corrupt", "write authorization or nonce is reused")
            authorization_sha256s.add(data.authorization_sha256)
            authorization_nonces.add(data.authorization_nonce_sha256)


def _load_events(
    run_dir: Path,
    manifest_sha256: str,
) -> tuple[list[BatchEvent], PendingEvent | None, tuple[str, ...]]:
    events_dir = run_dir / "events"
    try:
        names = list_directory(events_dir)
    except BatchRuntimeError as exc:
        raise BatchRuntimeError("journal_corrupt", f"event directory is unavailable: {events_dir}") from exc
    event_names = [name for name in names if _EVENT_NAME.fullmatch(name)]
    temp_names = [name for name in names if _EVENT_TEMP_NAME.fullmatch(name)]
    writing_names = [name for name in names if _EVENT_WRITING_NAME.fullmatch(name)]
    if len(event_names) + len(temp_names) + len(writing_names) != len(names):
        raise BatchRuntimeError("journal_corrupt", "event journal contains an unknown entry")
    if not event_names:
        raise BatchRuntimeError("journal_corrupt", "event journal is empty")
    if len(temp_names) > 1:
        raise BatchRuntimeError("journal_corrupt", "event journal has multiple pending next events")
    expected_names = [f"{sequence:020d}.json" for sequence in range(1, len(event_names) + 1)]
    if event_names != expected_names:
        raise BatchRuntimeError("journal_corrupt", "event journal has a gap, duplicate, or invalid filename")
    events: list[BatchEvent] = []
    previous: str | None = None
    request_ids: set[str] = set()
    event_ids: set[str] = set()
    for sequence, name in enumerate(event_names, start=1):
        event = _load_event(
            events_dir / name,
            expected_sequence=sequence,
            manifest_sha256=manifest_sha256,
            previous_sha256=previous,
        )
        if event.request_id in request_ids:
            raise BatchRuntimeError("journal_corrupt", f"request id appears in more than one event: {event.request_id}")
        if event.event_id in event_ids:
            raise BatchRuntimeError("journal_corrupt", f"event id appears more than once: {event.event_id}")
        request_ids.add(event.request_id)
        event_ids.add(event.event_id)
        events.append(event)
        previous = event.event_sha256
    _validate_identity_history(events)

    def validate_pending_event(staged: BatchEvent, raw: bytes, *, label: str) -> None:
        if (
            raw != canonical_json_bytes(staged)
            or staged.sequence != len(events) + 1
            or staged.previous_event_sha256 != previous
            or staged.manifest_sha256 != manifest_sha256
            or staged.event_sha256 != _event_digest(staged)
            or staged.command != _command_for_kind(staged.data.kind)
            or staged.command_result.command != staged.command
            or staged.command_result.request_id != staged.request_id
            or staged.request_id in request_ids
            or staged.event_id in event_ids
        ):
            raise BatchRuntimeError("journal_corrupt", f"{label} is not a valid next event")
        _validate_identity_history([*events, staged])

    next_name = f"{len(events) + 1:020d}.json"
    pending: PendingEvent | None = None
    for name in temp_names:
        matched = _EVENT_TEMP_NAME.fullmatch(name)
        assert matched is not None
        if matched.group("target") != next_name:
            raise BatchRuntimeError("journal_corrupt", f"event staging file targets a non-next sequence: {name}")
        raw = read_bytes(events_dir / name, code="journal_corrupt")
        if sha256_bytes(raw) != matched.group("digest"):
            raise BatchRuntimeError("journal_corrupt", f"event staging digest mismatch: {name}")
        try:
            payload = json.loads(raw)
            staged = BatchEvent.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
            raise BatchRuntimeError("journal_corrupt", f"event staging file is invalid: {name}") from exc
        validate_pending_event(staged, raw, label=f"event staging file {name}")
        pending = PendingEvent(path=events_dir / name, raw=raw, event=staged)
    incomplete_writes: list[str] = []
    for name in writing_names:
        matched = _EVENT_WRITING_NAME.fullmatch(name)
        assert matched is not None
        target_sequence = int(matched.group("target")[:20])
        if target_sequence > len(events) + 1:
            raise BatchRuntimeError(
                "journal_corrupt",
                f"event writing entry targets a future sequence: {name}",
            )
        raw = read_bytes(events_dir / name, code="journal_corrupt")
        try:
            payload = json.loads(raw)
            staged = BatchEvent.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
            incomplete_writes.append(name)
            continue
        if matched.group("target") != next_name:
            raise BatchRuntimeError("journal_corrupt", f"complete event writing file is not a valid next event: {name}")
        validate_pending_event(staged, raw, label=f"complete event writing file {name}")
        if pending is not None:
            raise BatchRuntimeError("journal_corrupt", "event journal has multiple durable pending next events")
        pending = PendingEvent(path=events_dir / name, raw=raw, event=staged)
    return events, pending, tuple(incomplete_writes)


def _validate_finished_payload(
    manifest: BatchManifest,
    event: BatchEvent,
    raw: bytes,
    *,
    full_external: bool,
) -> None:
    from paper_reader_batch.v2_artifacts import (
        validate_local_prepare_result_artifacts,
        validate_worker_result_artifacts,
    )

    data = event.data
    if not isinstance(data, FinishedData):
        return
    lane = "worker" if data.kind == "worker.finished" else "local-prepare"
    if sha256_bytes(raw) != data.result_sha256:
        raise BatchRuntimeError("journal_corrupt", "finish result digest does not match event")
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
        resolved_key = validate_worker_result_artifacts(
            manifest,
            result,
            allow_mutable_run=not full_external,
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
) -> None:
    data = event.data
    if not isinstance(data, FinishedData):
        return
    lane = "worker" if data.kind == "worker.finished" else "local-prepare"
    result_path = run_dir / "results" / lane / f"{data.result_sha256}.json"
    raw = read_bytes(result_path, code="journal_corrupt")
    _validate_finished_payload(manifest, event, raw, full_external=full_external)


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
        _validate_finished_event_result(run_dir, manifest, event)
        prior = _validation_view(
            run_dir=run_dir,
            run_dir_identity=run_dir_identity,
            manifest=manifest,
            manifest_raw=manifest_raw,
            manifest_sha256=manifest_sha256,
            events=prefix,
            state=state,
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
    raw = read_bytes(path, code="snapshot_invalid")
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


def _load_state_staging(run_dir: Path, state: BatchState) -> tuple[str | None, tuple[str, ...]]:
    names = list_directory(run_dir)
    pending = [name for name in names if _STATE_TEMP_NAME.fullmatch(name)]
    writing = tuple(name for name in names if _STATE_WRITING_NAME.fullmatch(name))
    if len(pending) > 1:
        raise BatchRuntimeError("journal_corrupt", "run has multiple pending state replacements")
    if pending:
        name = pending[0]
        match = _STATE_TEMP_NAME.fullmatch(name)
        assert match is not None
        raw = read_bytes(run_dir / name, code="snapshot_invalid")
        expected = canonical_json_bytes(state)
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
) -> RunView:
    manifest, manifest_raw, manifest_sha256 = load_manifest(
        root / "manifest.json",
        validate_sources=False,
        drift_context=True,
    )
    events, pending_event, incomplete_event_writes = _load_events(root, manifest_sha256)
    state = _replay_with_result_validation(
        root,
        run_dir_identity,
        manifest,
        manifest_raw,
        manifest_sha256,
        events,
    )
    if pending_event is not None:
        _validate_finished_event_result(root, manifest, pending_event.event, full_external=True)
        prior = _validation_view(
            run_dir=root,
            run_dir_identity=run_dir_identity,
            manifest=manifest,
            manifest_raw=manifest_raw,
            manifest_sha256=manifest_sha256,
            events=events,
            state=state,
        )
        _validate_write_event_result(prior, pending_event.event, committed=True)
        apply_event(state, manifest, pending_event.event)
    snapshot_raw, snapshot, snapshot_status = _load_snapshot(root)
    if snapshot_status == "loaded":
        snapshot_status = "current" if snapshot == state else "stale"
    state_pending_write, incomplete_state_writes = _load_state_staging(root, state)
    lock_path = root / ".run.lock"
    if held_lease_secret is None:
        try:
            lease_secret = read_bytes(lock_path, code="lease_secret_missing")
        except BatchRuntimeError as exc:
            if exc.code in {"lease_secret_missing", "storage_missing"}:
                raise BatchRuntimeError("lease_secret_missing", f"run lease secret is missing: {lock_path}") from exc
            raise
    else:
        lease_secret = held_lease_secret
    if len(lease_secret) != 32 or sha256_bytes(lease_secret) != state.lease_secret_sha256:
        raise BatchRuntimeError("lease_secret_mismatch", "run lease secret does not match initialized journal")
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
        pending_event=pending_event,
        incomplete_event_writes=incomplete_event_writes,
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
        )


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


@contextmanager
def locked_run(
    run_dir: Path,
    *,
    expected_manifest_sha256: str | None = None,
    expected_run_dir_identity: tuple[int, int] | None = None,
    incoming_request_id: str | None = None,
    incoming_command: str | None = None,
    incoming_fingerprint: str | None = None,
) -> Iterator[RunView]:
    preflight = load_run_view(run_dir)
    if incoming_request_id is not None:
        for event in preflight.events:
            if event.request_id == incoming_request_id and (
                event.command != incoming_command or event.request_fingerprint != incoming_fingerprint
            ):
                raise BatchRuntimeError(
                    "idempotency_conflict",
                    "request id is already bound to a different journal operation or input",
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
            if incoming_request_id is not None:
                for event in current.events:
                    if event.request_id == incoming_request_id and (
                        event.command != incoming_command
                        or event.request_fingerprint != incoming_fingerprint
                    ):
                        raise BatchRuntimeError(
                            "idempotency_conflict",
                            "request id is already bound to a different journal operation or input",
                        )
            if current.pending_event is not None:
                pending_event = current.pending_event.event
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
                promote_bytes_no_replace(
                    pending.path,
                    pending.path.parent / f"{pending.event.sequence:020d}.json",
                    pending.raw,
                )
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
                    unlink_internal_regular(current.run_dir / "events" / name)
                current = load_run_view(
                    preflight.run_dir,
                    held_lease_secret=lease_secret,
                    lock_descriptor=descriptor,
                    lock_ancestor_descriptors=ancestor_descriptors,
                )
            if current.state_pending_write is not None:
                if current.snapshot_status == "current":
                    unlink_internal_regular(current.run_dir / current.state_pending_write)
                else:
                    _persist_snapshot(current, current.state)
                current = load_run_view(
                    preflight.run_dir,
                    held_lease_secret=lease_secret,
                    lock_descriptor=descriptor,
                    lock_ancestor_descriptors=ancestor_descriptors,
                )
            if current.incomplete_state_writes:
                for name in current.incomplete_state_writes:
                    if _STATE_WRITING_NAME.fullmatch(name) is None:
                        raise BatchRuntimeError("journal_corrupt", "invalid internal state writing name")
                    unlink_internal_regular(current.run_dir / name)
                current = load_run_view(
                    preflight.run_dir,
                    held_lease_secret=lease_secret,
                    lock_descriptor=descriptor,
                    lock_ancestor_descriptors=ancestor_descriptors,
                )
            yield current
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
    fault: FaultHook | None = None,
) -> RequestOutcome:
    canonical_request_id = validate_request_id(request_id)
    with locked_run(
        run_dir,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_run_dir_identity=expected_run_dir_identity,
        incoming_request_id=canonical_request_id,
        incoming_command=command,
        incoming_fingerprint=request_fingerprint,
    ) as view:
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
            result = reconstruct(view, event)
            if canonical_sha256(result) != event.command_result.semantic_result_sha256:
                raise BatchRuntimeError("journal_corrupt", "replayed semantic result does not match event binding")
            if view.snapshot_status != "current":
                _persist_snapshot(view, view.state, fault=fault)
            return RequestOutcome(result=result, replayed=True)

        if view.snapshot_status != "current" and command != "run.recover":
            _persist_snapshot(view, view.state, fault=fault)
            view = load_run_view(
                view.run_dir,
                held_lease_secret=view.lease_secret,
                lock_descriptor=view.lock_descriptor,
            )

        authoritative_time = occurred_at or utc_now()
        try:
            current_time = datetime.fromisoformat(authoritative_time[:-1] + "+00:00")
            previous_time = datetime.fromisoformat(view.events[-1].occurred_at[:-1] + "+00:00")
        except (ValueError, IndexError) as exc:
            raise BatchRuntimeError("invalid_timestamp", "transaction timestamp is invalid") from exc
        if not authoritative_time.endswith("Z") or current_time < previous_time:
            raise BatchRuntimeError(
                "nonmonotonic_time",
                "transaction time must not precede the latest authoritative journal event",
            )
        transition = propose(view, authoritative_time)
        sequence = view.state.next_sequence
        event_base = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": sequence,
            "event_id": str(uuid4()),
            "occurred_at": authoritative_time,
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
            publish_bytes_no_replace(publication_path, publication.content, allow_existing_exact=True)
            if fault is not None:
                fault("after_result")
        event_path = view.run_dir / "events" / f"{sequence:020d}.json"
        if fault is not None:
            fault("before_event")
        validate_locked_path(lock_path, view.lock_descriptor)
        try:
            publish_bytes_no_replace(event_path, canonical_json_bytes(event), fault=fault)
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
    if view.snapshot_raw is None:
        publish_bytes_no_replace(state_path, state_bytes, fault=fault)
    else:
        replace_bytes_atomic(state_path, state_bytes, expected_current=view.snapshot_raw, fault=fault)


def status_result(view: RunView) -> dict[str, JsonValue]:
    return {
        "run_dir": str(view.run_dir),
        "manifest_sha256": view.manifest_sha256,
        "snapshot_status": view.snapshot_status,
        "pending_event_sequence": view.pending_event.event.sequence if view.pending_event is not None else None,
        "incomplete_event_writes": list(view.incomplete_event_writes),
        "state_pending_write": view.state_pending_write,
        "incomplete_state_writes": list(view.incomplete_state_writes),
        "state": view.state.model_dump(mode="json"),
    }
