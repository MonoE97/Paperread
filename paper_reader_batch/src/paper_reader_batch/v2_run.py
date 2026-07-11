from __future__ import annotations

from pathlib import Path
import re
import secrets
import unicodedata
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from paper_reader_batch.v2_contracts import (
    BatchEvent,
    BatchState,
    COMMAND_RESULT_SCHEMA_VERSION,
    EventCommandResultSnapshot,
    PdfManifestItem,
    RecoveredLease,
    ResumedLocalPrepareLease,
    RunInitializedData,
    RunRecoveredData,
    StateItem,
    STATE_SCHEMA_VERSION,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    canonical_json_bytes,
    canonical_sha256,
    ensure_directory,
    entry_exists,
    entry_exists_allow_missing_parent,
    list_directory,
    normalized_absolute_path,
    open_directory_fd,
    publish_bytes_no_replace,
    publish_directory_no_replace,
    read_bytes,
    sha256_bytes,
    utc_now,
    validate_parent_directory,
)
from paper_reader_batch.v2_manifest import load_manifest
from paper_reader_batch.v2_manifest import validate_manifest_sources
from paper_reader_batch.v2_receipts import FaultHook, RequestOutcome, RequestReceiptStore
from paper_reader_batch.v2_reducer import initial_state


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_RECOVERED_LOCAL_PREPARE_LEASE_SECONDS = 900


def _slug(title: str) -> str:
    fragments: list[str] = []
    for char in unicodedata.normalize("NFKC", title):
        if char.isascii() and char.isalnum():
            fragments.append(char.lower())
        elif char.isspace() or char.isascii():
            fragments.append("-")
        elif unicodedata.category(char).startswith(("L", "N")):
            fragments.append(f"u{ord(char):04x}-")
    value = _SLUG_PATTERN.sub("-", "".join(fragments)).strip("-")
    return value or "untitled"


def _event_bytes(event_payload: dict[str, Any]) -> bytes:
    return canonical_json_bytes(event_payload)


def _initial_items(manifest) -> list[StateItem]:
    items: list[StateItem] = []
    for manifest_item in manifest.items:
        local_status = "queued" if isinstance(manifest_item, PdfManifestItem) else "not_applicable"
        write_status = "not_applicable" if isinstance(manifest_item, PdfManifestItem) else "awaiting_candidate"
        items.append(
            StateItem(
                item_id=manifest_item.item_id,
                input_type=manifest_item.input_type,
                expected_output=manifest_item.expected_output,
                local_prepare_status=local_status,
                write_status=write_status,
            )
        )
    return items


def _safe_read_matches(path: Path, expected: bytes) -> bool:
    try:
        return read_bytes(path) == expected
    except BatchRuntimeError as exc:
        if exc.code in {"artifact_unreadable", "storage_missing"}:
            return False
        raise


def initialize_run(
    manifest_path: Path,
    *,
    request_id: str,
    skill_root: Path,
    output: Path | None = None,
    initialized_at: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    manifest, manifest_raw, manifest_sha256 = load_manifest(manifest_path, validate_sources=True)
    root = normalized_absolute_path(skill_root)
    with open_directory_fd(root, create=False):
        pass
    explicit_target = normalized_absolute_path(output) if output is not None else None
    if explicit_target is not None:
        validate_parent_directory(explicit_target)
    timestamp_seed = initialized_at or utc_now()
    fingerprint = canonical_sha256(
        {
            "command": "run.init",
            "manifest_path": str(normalized_absolute_path(manifest_path)),
            "manifest_sha256": manifest_sha256,
            "requested_target": str(explicit_target) if explicit_target is not None else None,
            "initialized_at_override": initialized_at,
        }
    )
    store = RequestReceiptStore(root)

    def target_factory(reserved: set[str]) -> Path:
        if explicit_target is not None:
            candidate = explicit_target
            if str(candidate) in reserved or entry_exists(candidate):
                raise BatchRuntimeError("output_conflict", f"run target is reserved or occupied: {candidate}")
            return candidate
        dated_root = root / "runs" / timestamp_seed[:10]
        stem = _slug(manifest.batch_title)
        suffix = 1
        while True:
            name = stem if suffix == 1 else f"{stem}_v{suffix}"
            candidate = dated_root / name
            if str(candidate) not in reserved and not entry_exists_allow_missing_parent(candidate):
                return candidate
            suffix += 1

    def plan_factory(target: Path) -> dict[str, Any]:
        timestamp = timestamp_seed
        lease_secret = secrets.token_bytes(32)
        lease_secret_sha256 = sha256_bytes(lease_secret)
        semantic_result = {
            "run_dir": str(target),
            "manifest_id": manifest.manifest_id,
            "manifest_sha256": manifest_sha256,
        }
        event_base = {
            "schema_version": "paper_reader_batch.event.v2",
            "sequence": 1,
            "event_id": str(uuid4()),
            "occurred_at": timestamp,
            "request_id": request_id,
            "command": "run.init",
            "request_fingerprint": fingerprint,
            "manifest_sha256": manifest_sha256,
            "previous_event_sha256": None,
            "data": RunInitializedData(
                manifest_id=manifest.manifest_id,
                initialized_at=timestamp,
                lease_secret_sha256=lease_secret_sha256,
            ).model_dump(mode="json"),
            "command_result": EventCommandResultSnapshot(
                schema_version=COMMAND_RESULT_SCHEMA_VERSION,
                command="run.init",
                request_id=request_id,
                semantic_result_sha256=canonical_sha256(semantic_result),
            ).model_dump(mode="json"),
        }
        event_sha256 = canonical_sha256(event_base)
        event = BatchEvent(**event_base, event_sha256=event_sha256)
        state = initial_state(manifest, event)
        return {
            "manifest_sha256": manifest_sha256,
            "lease_secret_hex": lease_secret.hex(),
            "event": event.model_dump(mode="json"),
            "state": state.model_dump(mode="json"),
            "semantic_result": semantic_result,
        }

    def planned_bytes(plan: dict[str, Any]) -> tuple[bytes, bytes, bytes, bytes]:
        event = plan.get("event")
        state = plan.get("state")
        secret_hex = plan.get("lease_secret_hex")
        if not isinstance(event, dict) or not isinstance(state, dict) or not isinstance(secret_hex, str):
            raise BatchRuntimeError("receipt_corrupt", "run initialization receipt plan is invalid")
        try:
            secret = bytes.fromhex(secret_hex)
        except ValueError as exc:
            raise BatchRuntimeError("receipt_corrupt", "run lease secret encoding is invalid") from exc
        if len(secret) != 32:
            raise BatchRuntimeError("receipt_corrupt", "run lease secret must be 256 bits")
        return (
            manifest_raw,
            _event_bytes(event),
            canonical_json_bytes(state),
            secret,
        )

    required_directories = [
        "events",
        "results/worker",
        "results/local-prepare",
        "results/write",
        "results/reconcile",
    ]

    def inspect(target: Path, plan: dict[str, Any]) -> bool:
        if not entry_exists_allow_missing_parent(target):
            return False
        manifest_bytes, event_bytes, state_bytes, secret_bytes = planned_bytes(plan)
        if not all(
            [
                _safe_read_matches(target / "manifest.json", manifest_bytes),
                _safe_read_matches(target / "events" / "00000000000000000001.json", event_bytes),
                _safe_read_matches(target / "state.json", state_bytes),
                _safe_read_matches(target / ".run.lock", secret_bytes),
            ]
        ):
            return False
        for relative in required_directories:
            try:
                with open_directory_fd(target / relative, create=False):
                    pass
            except BatchRuntimeError as exc:
                if exc.code == "storage_missing":
                    return False
                raise
        return True

    def publish(target: Path, plan: dict[str, Any], resuming: bool) -> None:
        manifest_bytes, event_bytes, state_bytes, secret_bytes = planned_bytes(plan)
        target_exists = entry_exists_allow_missing_parent(target)
        if target_exists:
            raise BatchRuntimeError("output_conflict", f"run target was occupied concurrently: {target}")
        ensure_directory(target.parent)
        staging = target.parent / f".{target.name}.{request_id}.staging"
        staging_exists = entry_exists(staging)
        if staging_exists and not resuming:
            raise BatchRuntimeError("output_conflict", f"run staging target is already occupied: {staging}")
        ensure_directory(staging)
        for relative in required_directories:
            ensure_directory(staging / relative)
        publish_bytes_no_replace(staging / "manifest.json", manifest_bytes, allow_existing_exact=resuming)
        publish_bytes_no_replace(staging / ".run.lock", secret_bytes, allow_existing_exact=resuming)
        publish_bytes_no_replace(
            staging / "events" / "00000000000000000001.json",
            event_bytes,
            allow_existing_exact=resuming,
        )
        publish_bytes_no_replace(staging / "state.json", state_bytes, allow_existing_exact=resuming)
        expected_entries = {
            ".run.lock",
            "events",
            "manifest.json",
            "results",
            "state.json",
        }
        if set(list_directory(staging)) != expected_entries:
            raise BatchRuntimeError("unsafe_storage", f"run staging tree has unexpected entries: {staging}")
        if list_directory(staging / "events") != ["00000000000000000001.json"]:
            raise BatchRuntimeError("unsafe_storage", "run staging event directory is not closed-world")
        if set(list_directory(staging / "results")) != {"worker", "local-prepare", "write", "reconcile"}:
            raise BatchRuntimeError("unsafe_storage", "run staging results directory is not closed-world")
        for lane in ["worker", "local-prepare", "write", "reconcile"]:
            if list_directory(staging / "results" / lane):
                raise BatchRuntimeError("unsafe_storage", f"run staging result lane is not empty: {lane}")
        publish_directory_no_replace(staging, target)

    try:
        return store.execute(
            request_id=request_id,
            command="run.init",
            request_fingerprint=fingerprint,
            requested_target=explicit_target,
            target_factory=target_factory,
            plan_factory=plan_factory,
            publish=publish,
            inspect=inspect,
            fault=fault,
        )
    except ValidationError as exc:  # pragma: no cover - strict plan constructors normally catch directly
        raise BatchRuntimeError("invalid_run", "run initialization plan failed strict validation") from exc


def validate_run(run_dir: Path) -> dict[str, Any]:
    from paper_reader_batch.v2_journal import load_run_view

    view = load_run_view(run_dir)
    validate_manifest_sources(view.manifest)
    return {
        "run_dir": str(view.run_dir),
        "manifest_id": view.manifest.manifest_id,
        "manifest_sha256": view.manifest_sha256,
        "event_count": len(view.events),
        "snapshot_status": view.snapshot_status,
        "batch_status": view.state.batch_status,
        "valid": True,
    }


def run_status(run_dir: Path) -> dict[str, Any]:
    from paper_reader_batch.v2_journal import load_run_view, status_result

    return status_result(load_run_view(run_dir))


def recover_run(
    run_dir: Path,
    *,
    request_id: str,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    from datetime import datetime, timedelta

    from paper_reader_batch.v2_journal import ProposedTransition, append_transaction, load_run_view
    from paper_reader_batch.v2_local_prepare import local_prepare_attempt_has_execution_side_effects

    def parse_timestamp(value: str) -> datetime:
        try:
            return datetime.fromisoformat(value[:-1] + "+00:00")
        except (ValueError, IndexError) as exc:
            raise BatchRuntimeError("invalid_timestamp", f"invalid recovery timestamp: {value}") from exc

    preflight = load_run_view(run_dir)
    fingerprint = canonical_sha256(
        {
            "command": "run.recover",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "now_override": now,
        }
    )

    def recovered(lease, item_id: str) -> RecoveredLease:
        return RecoveredLease(
            item_id=item_id,
            lane=lease.lane,
            actor_id=lease.actor_id,
            claim_id=lease.claim_id,
            attempt_id=lease.attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=lease.lease_token_sha256,
            expires_at=lease.expires_at,
        )

    def result_for(data: RunRecoveredData) -> dict[str, Any]:
        return {
            "run_dir": str(preflight.run_dir),
            "expired_worker_items": [item.item_id for item in data.expired_worker_leases],
            "expired_local_prepare_items": [item.item_id for item in data.expired_local_prepare_leases],
            "resumed_local_prepare_items": [item.item_id for item in data.resumed_local_prepare_leases],
            "snapshot_repaired": data.snapshot_repaired,
        }

    def propose(view, transaction_time: str) -> ProposedTransition:
        current = parse_timestamp(transaction_time)
        worker = [
            recovered(item.worker_lease, item.item_id)
            for item in view.state.items
            if item.worker_lease is not None and parse_timestamp(item.worker_lease.expires_at) <= current
        ]
        local: list[RecoveredLease] = []
        resumed_local: list[ResumedLocalPrepareLease] = []
        resumed_expires_at = (current + timedelta(seconds=_RECOVERED_LOCAL_PREPARE_LEASE_SECONDS)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        for item in view.state.items:
            lease = item.local_prepare_lease
            if lease is None or parse_timestamp(lease.expires_at) > current:
                continue
            if local_prepare_attempt_has_execution_side_effects(
                view,
                item_id=item.item_id,
                claim_id=lease.claim_id,
                attempt_id=lease.attempt_id,
            ):
                resumed_local.append(
                    ResumedLocalPrepareLease(
                        item_id=item.item_id,
                        actor_id=lease.actor_id,
                        claim_id=lease.claim_id,
                        attempt_id=lease.attempt_id,
                        attempt_number=lease.attempt_number,
                        lease_token_sha256=lease.lease_token_sha256,
                        previous_expires_at=lease.expires_at,
                        issued_at=transaction_time,
                        expires_at=resumed_expires_at,
                    )
                )
            else:
                local.append(recovered(lease, item.item_id))
        data = RunRecoveredData(
            expired_worker_leases=worker,
            expired_local_prepare_leases=local,
            resumed_local_prepare_leases=resumed_local,
            snapshot_repaired=view.snapshot_status != "current",
        )
        if not worker and not local and not resumed_local and not data.snapshot_repaired:
            raise BatchRuntimeError("nothing_to_recover", "run has no expired lease or stale snapshot")
        return ProposedTransition(data=data, result=result_for(data))

    def reconstruct(_view, event) -> dict[str, Any]:
        if not isinstance(event.data, RunRecoveredData):
            raise BatchRuntimeError("journal_corrupt", "run recover request points to another event type")
        return result_for(event.data)

    return append_transaction(
        run_dir,
        request_id=request_id,
        command="run.recover",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        fault=fault,
    )
