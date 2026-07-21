from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError, model_validator

from paper_reader_batch.v2_artifacts import (
    validate_local_prepare_result_artifacts,
    validate_worker_result_artifacts,
    worker_result_artifact_commit_guard,
)
from paper_reader_batch.v2_contracts import (
    LOCAL_PREPARE_RESULT_SCHEMA_VERSION,
    WORKER_RESULT_SCHEMA_VERSION,
    ArtifactRef,
    ClaimedData,
    ClaimAssignment,
    FinishedData,
    ItemId,
    LeaseMutationData,
    LocalPrepareResult,
    ManifestItem,
    NonEmptyString,
    PdfManifestItem,
    PositiveInt,
    Rfc3339Utc,
    RetriedData,
    Sha256,
    SourceIdentity,
    StateItem,
    StrictModel,
    UuidString,
    WorkerResult,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_journal import (
    ProposedTransition,
    ResultPublication,
    RunView,
    append_transaction,
    load_request_preflight,
    load_run_view,
)
from paper_reader_batch.v2_json import (
    canonical_json_bytes,
    canonical_sha256,
    normalized_absolute_path,
    read_json_bytes,
    sha256_bytes,
    utc_now,
)
from paper_reader_batch.v2_manifest import validate_pdf_source
from paper_reader_batch.v2_receipts import FaultHook, RequestOutcome


DEFAULT_LEASE_SECONDS = 900
MAX_LEASE_SECONDS = 3600


def _raise_input_error(existing_event, code: str, message: str) -> None:
    if existing_event is not None:
        raise BatchRuntimeError(
            "idempotency_conflict",
            "request id is already bound to different command input",
        )
    raise BatchRuntimeError(code, message)


class _WorkerPromptContract(StrictModel):
    item_id: ItemId
    worker_id: NonEmptyString
    claim_id: UuidString
    attempt_id: UuidString
    attempt_number: PositiveInt
    expires_at: Rfc3339Utc
    source: SourceIdentity
    expected_output: Literal["local_note", "zotero_note_candidate"]
    instruction: NonEmptyString
    local_prepare_result_sha256: Sha256 | None = None
    paper_reader_run: ArtifactRef | None = None
    evidence: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_prepared_attempt_binding(self) -> "_WorkerPromptContract":
        prepared = (
            self.local_prepare_result_sha256,
            self.paper_reader_run,
            self.evidence,
        )
        if any(value is not None for value in prepared) != all(
            value is not None for value in prepared
        ):
            raise ValueError("prepared attempt prompt fields must be all present or all absent")
        return self


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except (ValueError, IndexError) as exc:
        raise BatchRuntimeError("invalid_timestamp", f"invalid RFC3339 UTC timestamp: {value}") from exc
    if not value.endswith("Z") or parsed.utcoffset() != timedelta(0):
        raise BatchRuntimeError("invalid_timestamp", f"timestamp must use UTC Z form: {value}")
    return parsed


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def derive_lease_token(secret: bytes, *, lane: str, claim_id: str, attempt_id: str) -> str:
    message = f"paper_reader_batch.v2\0{lane}\0{claim_id}\0{attempt_id}".encode()
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _active_claim_count(view: RunView) -> int:
    return sum(
        1
        for item in view.state.items
        if item.worker_status == "claimed" or item.local_prepare_status == "claimed"
    )


def _claimable_worker_items(view: RunView, *, max_items: int) -> list[StateItem]:
    if max_items <= 0:
        return []
    manifest_by_id = {item.item_id: item for item in view.manifest.items}
    selected: list[StateItem] = []
    selected_pdf = False
    for item in view.state.items:
        if item.worker_status != "queued" or item.local_prepare_status == "claimed":
            continue
        manifest_item = manifest_by_id[item.item_id]
        if isinstance(manifest_item, PdfManifestItem):
            if selected_pdf:
                continue
            selected_pdf = True
        selected.append(item)
        if len(selected) == max_items:
            break
    return selected


def _validate_worker_pdf_source(view: RunView, item_id: str) -> None:
    manifest_item = next(
        (item for item in view.manifest.items if item.item_id == item_id),
        None,
    )
    if manifest_item is None:
        raise BatchRuntimeError("journal_corrupt", "worker item is absent from manifest")
    if isinstance(manifest_item, PdfManifestItem):
        validate_pdf_source(manifest_item.source)


def _claim_result(view: RunView, data: ClaimedData) -> dict[str, Any]:
    assignments: list[dict[str, Any]] = []
    manifest_by_id = {item.item_id: item for item in view.manifest.items}
    for assignment in data.assignments:
        manifest_item = manifest_by_id[assignment.item_id]
        assignments.append(
            {
                "item_id": assignment.item_id,
                "input_type": manifest_item.input_type,
                "expected_output": manifest_item.expected_output,
                "worker_id": assignment.actor_id,
                "claim_id": assignment.claim_id,
                "attempt_id": assignment.attempt_id,
                "attempt_number": assignment.attempt_number,
                "lease_token": derive_lease_token(
                    view.lease_secret,
                    lane=assignment.lane,
                    claim_id=assignment.claim_id,
                    attempt_id=assignment.attempt_id,
                ),
                "issued_at": assignment.issued_at,
                "expires_at": assignment.expires_at,
                "source": manifest_item.source.model_dump(mode="json"),
            }
        )
    return {"assignments": assignments}


def claim_worker(
    run_dir: Path,
    *,
    worker_id: str,
    request_id: str,
    limit: int | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="worker.claim",
    )
    if not worker_id.strip():
        _raise_input_error(existing_event, "invalid_worker", "worker id must not be empty")
    requested_limit = preflight.manifest.default_concurrency if limit is None else limit
    if requested_limit < 1 or requested_limit > preflight.manifest.default_concurrency:
        _raise_input_error(
            existing_event,
            "invalid_limit",
            "worker claim limit must be positive and no greater than manifest default concurrency",
        )
    if lease_seconds < 1 or lease_seconds > MAX_LEASE_SECONDS:
        _raise_input_error(
            existing_event,
            "invalid_lease",
            f"lease seconds must be between 1 and {MAX_LEASE_SECONDS}",
        )
    fingerprint = canonical_sha256(
        {
            "command": "worker.claim",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "worker_id": worker_id,
            "limit": requested_limit,
            "lease_seconds": lease_seconds,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        expires_at = _format_utc(_parse_utc(transaction_time) + timedelta(seconds=lease_seconds))
        capacity = view.manifest.default_concurrency - _active_claim_count(view)
        count = min(requested_limit, capacity)
        eligible = _claimable_worker_items(view, max_items=count)
        if not eligible:
            raise BatchRuntimeError("no_available_work", "no worker item is currently claimable")
        manifest_by_id = {item.item_id: item for item in view.manifest.items}
        for item in eligible:
            manifest_item = manifest_by_id[item.item_id]
            if isinstance(manifest_item, PdfManifestItem):
                validate_pdf_source(manifest_item.source)
        assignments: list[ClaimAssignment] = []
        for item in eligible:
            claim_id = str(uuid4())
            attempt_id = item.worker_pending_attempt_id or str(uuid4())
            lease_token = derive_lease_token(
                view.lease_secret,
                lane="worker",
                claim_id=claim_id,
                attempt_id=attempt_id,
            )
            assignments.append(
                ClaimAssignment(
                    item_id=item.item_id,
                    lane="worker",
                    actor_id=worker_id,
                    claim_id=claim_id,
                    attempt_id=attempt_id,
                    attempt_number=item.worker_attempt_count + 1,
                    lease_token_sha256=sha256_bytes(lease_token.encode()),
                    issued_at=transaction_time,
                    expires_at=expires_at,
                    source=manifest_by_id[item.item_id].source,
                )
            )
        data = ClaimedData(kind="worker.claimed", assignments=assignments)
        return ProposedTransition(data=data, result=_claim_result(view, data))

    def reconstruct(view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, ClaimedData) or event.data.kind != "worker.claimed":
            raise BatchRuntimeError("journal_corrupt", "worker claim request points to another event type")
        return _claim_result(view, event.data)

    def replay_validate(view: RunView, event) -> None:
        if not isinstance(event.data, ClaimedData) or event.data.kind != "worker.claimed":
            raise BatchRuntimeError("journal_corrupt", "worker claim replay points to another event type")
        manifest_by_id = {item.item_id: item for item in view.manifest.items}
        for assignment in event.data.assignments:
            manifest_item = manifest_by_id.get(assignment.item_id)
            if manifest_item is None or manifest_item.source != assignment.source:
                raise BatchRuntimeError("journal_corrupt", "worker claim source differs from manifest")
            if isinstance(manifest_item, PdfManifestItem):
                validate_pdf_source(manifest_item.source)

    def commit_validate(view: RunView) -> None:
        capacity = view.manifest.default_concurrency - _active_claim_count(view)
        count = min(requested_limit, capacity)
        for item in _claimable_worker_items(view, max_items=count):
            _validate_worker_pdf_source(view, item.item_id)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="worker.claim",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        replay_validate=replay_validate,
        commit_validate=commit_validate,
        fault=fault,
    )


def _active_worker_lease(
    view: RunView,
    *,
    item_id: str,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    now: str,
) -> tuple[Any, Any]:
    item = next((candidate for candidate in view.state.items if candidate.item_id == item_id), None)
    if item is None:
        raise BatchRuntimeError("unknown_item", f"unknown item id: {item_id}")
    lease = item.worker_lease
    if item.worker_status != "claimed" or lease is None:
        raise BatchRuntimeError("lease_inactive", f"worker lease is not active: {item_id}")
    expected_token = derive_lease_token(
        view.lease_secret,
        lane="worker",
        claim_id=lease.claim_id,
        attempt_id=lease.attempt_id,
    )
    if (
        lease.actor_id != worker_id
        or lease.claim_id != claim_id
        or lease.attempt_id != attempt_id
        or not hmac.compare_digest(expected_token, lease_token)
        or lease.lease_token_sha256 != sha256_bytes(lease_token.encode())
    ):
        raise BatchRuntimeError("lease_identity_mismatch", "worker lease identity, actor, attempt, or token does not match")
    if _parse_utc(now) >= _parse_utc(lease.expires_at):
        raise BatchRuntimeError("lease_expired", f"worker lease has expired: {item_id}")
    return item, lease


def _load_prepared_local_result(
    view: RunView,
    *,
    item: StateItem,
    manifest_item: ManifestItem,
) -> LocalPrepareResult | None:
    if not isinstance(manifest_item, PdfManifestItem) or item.local_prepare_status != "prepared":
        return None
    digest = item.local_prepare_result_sha256
    if digest is None:
        raise BatchRuntimeError(
            "journal_corrupt",
            "prepared local state lacks its content-addressed result digest",
        )
    result_path = view.run_dir / "results" / "local-prepare" / f"{digest}.json"
    raw, payload = read_json_bytes(result_path, code="journal_corrupt")
    if not isinstance(payload, dict) or payload.get("schema_version") != LOCAL_PREPARE_RESULT_SCHEMA_VERSION:
        raise BatchRuntimeError(
            "unsupported_run_schema",
            f"prepared local result must use {LOCAL_PREPARE_RESULT_SCHEMA_VERSION}",
        )
    if sha256_bytes(raw) != digest:
        raise BatchRuntimeError(
            "journal_corrupt",
            "prepared local result bytes differ from the state-bound digest",
        )
    try:
        result = LocalPrepareResult.model_validate(payload)
    except ValidationError as exc:
        raise BatchRuntimeError(
            "journal_corrupt",
            "prepared local result fails strict validation",
        ) from exc
    if raw != canonical_json_bytes(result):
        raise BatchRuntimeError(
            "journal_corrupt",
            "prepared local result is not canonical JSON",
        )
    if (
        result.manifest_sha256 != view.manifest_sha256
        or result.item_id != item.item_id
        or result.worker_id != item.local_prepare_last_actor_id
        or result.claim_id != item.local_prepare_last_claim_id
        or result.attempt_id != item.local_prepare_last_attempt_id
        or result.attempt_number != item.local_prepare_attempt_count
        or result.lease_token_sha256 != item.local_prepare_last_lease_token_sha256
        or result.status != "prepared"
        or result.source != manifest_item.source
    ):
        raise BatchRuntimeError(
            "journal_corrupt",
            "prepared local result identity differs from journal-reduced state",
        )
    return result


def worker_prompt(
    run_dir: Path,
    item_id: str,
    *,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    view = load_run_view(run_dir)
    current_time = now or utc_now()
    item, lease = _active_worker_lease(
        view,
        item_id=item_id,
        worker_id=worker_id,
        claim_id=claim_id,
        lease_token=lease_token,
        attempt_id=attempt_id,
        now=current_time,
    )
    manifest_item = next(entry for entry in view.manifest.items if entry.item_id == item_id)
    if isinstance(manifest_item, PdfManifestItem):
        validate_pdf_source(manifest_item.source)
    prepared = _load_prepared_local_result(
        view,
        item=item,
        manifest_item=manifest_item,
    )
    if prepared is not None:
        validate_local_prepare_result_artifacts(
            view.manifest,
            prepared,
            expected_root=Path(prepared.paper_reader_root.path),
        )
    instruction = (
        f"Use $paper_reader for batch item {item_id}. "
        f"Bind worker_id={worker_id}, claim_id={claim_id}, attempt_id={attempt_id}, "
        f"manifest_sha256={view.manifest_sha256}. Produce one strict "
        "paper_reader_batch.worker-result.v2; do not call an LLM or Zotero from paper_reader_batch."
    )
    if manifest_item.input_type == "pdf_path":
        instruction += " This PDF is local-output only; never search or write Zotero."
    else:
        instruction += (
            " For this Zotero-backed item, have $paper_reader inspect eligible public HTTP(S) "
            "links from Zotero Extra, perform only plan-bound read-only captures, ingest them "
            "with paper_reader run prepare --secondary-capture-dir, and assess every eligible "
            "source in secondary_cross_checks before review. Unavailable pages are non-blocking; "
            "secondary material must never be cited in evidence_summary."
        )
    if prepared is not None:
        instruction += (
            " Continue the exact prepared paper_reader run and evidence returned by this prompt; "
            "do not initialize another run or substitute evidence."
        )
    prompt = _WorkerPromptContract(
        item_id=item_id,
        worker_id=worker_id,
        claim_id=claim_id,
        attempt_id=attempt_id,
        attempt_number=lease.attempt_number,
        expires_at=lease.expires_at,
        source=manifest_item.source,
        expected_output=item.expected_output,
        instruction=instruction,
        local_prepare_result_sha256=(
            item.local_prepare_result_sha256 if prepared is not None else None
        ),
        paper_reader_run=prepared.paper_reader_run if prepared is not None else None,
        evidence=prepared.evidence if prepared is not None else None,
    )
    if isinstance(manifest_item, PdfManifestItem):
        validate_pdf_source(manifest_item.source)
    return prompt.model_dump(mode="json", exclude_none=True)


def renew_worker(
    run_dir: Path,
    item_id: str,
    *,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    request_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="worker.renew",
    )
    if lease_seconds < 1 or lease_seconds > MAX_LEASE_SECONDS:
        _raise_input_error(
            existing_event,
            "invalid_lease",
            f"lease seconds must be between 1 and {MAX_LEASE_SECONDS}",
        )
    token_hash = sha256_bytes(lease_token.encode())
    fingerprint = canonical_sha256(
        {
            "command": "worker.renew",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "worker_id": worker_id,
            "claim_id": claim_id,
            "attempt_id": attempt_id,
            "lease_token_sha256": token_hash,
            "lease_seconds": lease_seconds,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        expires_at = _format_utc(_parse_utc(transaction_time) + timedelta(seconds=lease_seconds))
        _item, lease = _active_worker_lease(
            view,
            item_id=item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            now=transaction_time,
        )
        manifest_item = next(entry for entry in view.manifest.items if entry.item_id == item_id)
        if isinstance(manifest_item, PdfManifestItem):
            validate_pdf_source(manifest_item.source)
        if _parse_utc(expires_at) <= _parse_utc(lease.expires_at):
            raise BatchRuntimeError("lease_not_extended", "renewal must extend the existing lease expiry")
        data = LeaseMutationData(
            kind="worker.renewed",
            item_id=item_id,
            actor_id=worker_id,
            claim_id=claim_id,
            attempt_id=attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=token_hash,
            issued_at=transaction_time,
            expires_at=expires_at,
        )
        return ProposedTransition(data=data, result=_lease_mutation_result(data))

    def reconstruct(_view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, LeaseMutationData) or event.data.kind != "worker.renewed":
            raise BatchRuntimeError("journal_corrupt", "worker renew request points to another event type")
        return _lease_mutation_result(event.data)

    def replay_validate(view: RunView, event) -> None:
        if not isinstance(event.data, LeaseMutationData) or event.data.kind != "worker.renewed":
            raise BatchRuntimeError("journal_corrupt", "worker renew replay points to another event type")
        manifest_item = next(
            (item for item in view.manifest.items if item.item_id == event.data.item_id),
            None,
        )
        if manifest_item is None:
            raise BatchRuntimeError("journal_corrupt", "worker renew replay item is absent from manifest")
        if isinstance(manifest_item, PdfManifestItem):
            validate_pdf_source(manifest_item.source)

    def commit_validate(view: RunView) -> None:
        _validate_worker_pdf_source(view, item_id)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="worker.renew",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        replay_validate=replay_validate,
        commit_validate=commit_validate,
        fault=fault,
    )


def _lease_mutation_result(data: LeaseMutationData) -> dict[str, Any]:
    return {
        "item_id": data.item_id,
        "worker_id": data.actor_id,
        "claim_id": data.claim_id,
        "attempt_id": data.attempt_id,
        "attempt_number": data.attempt_number,
        "issued_at": data.issued_at,
        "expires_at": data.expires_at,
        "status": "claimed" if data.kind.endswith("renewed") else "queued",
    }


def release_worker(
    run_dir: Path,
    item_id: str,
    *,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    acknowledge_no_side_effects: bool,
    request_id: str,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="worker.release",
    )
    if not acknowledge_no_side_effects:
        _raise_input_error(
            existing_event,
            "acknowledgement_required",
            "worker release requires --acknowledge-no-side-effects",
        )
    token_hash = sha256_bytes(lease_token.encode())
    fingerprint = canonical_sha256(
        {
            "command": "worker.release",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "worker_id": worker_id,
            "claim_id": claim_id,
            "attempt_id": attempt_id,
            "lease_token_sha256": token_hash,
            "acknowledge_no_side_effects": True,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        _item, lease = _active_worker_lease(
            view,
            item_id=item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            now=transaction_time,
        )
        data = LeaseMutationData(
            kind="worker.released",
            item_id=item_id,
            actor_id=worker_id,
            claim_id=claim_id,
            attempt_id=attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=token_hash,
            issued_at=None,
            expires_at=None,
        )
        return ProposedTransition(data=data, result=_lease_mutation_result(data))

    def reconstruct(_view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, LeaseMutationData) or event.data.kind != "worker.released":
            raise BatchRuntimeError("journal_corrupt", "worker release request points to another event type")
        return _lease_mutation_result(event.data)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="worker.release",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        fault=fault,
    )


def _load_worker_result(path: Path) -> tuple[Path, bytes, WorkerResult, str]:
    result_path = normalized_absolute_path(path)
    raw, payload = read_json_bytes(result_path, code="result_unreadable")
    if not isinstance(payload, dict) or payload.get("schema_version") != WORKER_RESULT_SCHEMA_VERSION:
        raise BatchRuntimeError(
            "unsupported_run_schema",
            f"worker result schema must be exactly {WORKER_RESULT_SCHEMA_VERSION}",
        )
    try:
        result = WorkerResult.model_validate(payload)
    except ValidationError as exc:
        raise BatchRuntimeError("invalid_result", "worker result failed strict validation") from exc
    if raw != canonical_json_bytes(result):
        raise BatchRuntimeError("invalid_result", "worker result must use canonical JSON")
    return result_path, raw, result, sha256_bytes(raw)


def _finish_result(view: RunView, data: FinishedData) -> dict[str, Any]:
    return {
        "run_dir": str(view.run_dir),
        "item_id": data.item_id,
        "status": data.status,
        "result_path": str(view.run_dir / "results" / "worker" / f"{data.result_sha256}.json"),
        "result_sha256": data.result_sha256,
        "resolved_zotero_item_key": data.resolved_zotero_item_key,
        "candidate_sha256": data.candidate_sha256,
    }


def finish_worker(
    run_dir: Path,
    item_id: str,
    *,
    worker_id: str,
    claim_id: str,
    lease_token: str,
    attempt_id: str,
    result_path: Path,
    request_id: str,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, _existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="worker.finish",
    )
    input_path, raw, result, result_sha256 = _load_worker_result(result_path)
    token_hash = sha256_bytes(lease_token.encode())
    fingerprint = canonical_sha256(
        {
            "command": "worker.finish",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "worker_id": worker_id,
            "claim_id": claim_id,
            "attempt_id": attempt_id,
            "lease_token_sha256": token_hash,
            "result_input_path": str(input_path),
            "result_sha256": result_sha256,
            "now_override": now,
        }
    )

    def propose(view: RunView, transaction_time: str) -> ProposedTransition:
        item, lease = _active_worker_lease(
            view,
            item_id=item_id,
            worker_id=worker_id,
            claim_id=claim_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            now=transaction_time,
        )
        if (
            result.manifest_sha256 != view.manifest_sha256
            or result.item_id != item_id
            or result.worker_id != worker_id
            or result.claim_id != claim_id
            or result.attempt_id != attempt_id
            or result.attempt_number != lease.attempt_number
            or result.lease_token_sha256 != token_hash
        ):
            raise BatchRuntimeError("result_identity_mismatch", "worker result does not bind the active lease")
        prepared = _load_prepared_local_result(
            view,
            item=item,
            manifest_item=next(
                entry for entry in view.manifest.items if entry.item_id == item_id
            ),
        )
        resolved_key = validate_worker_result_artifacts(
            view.manifest,
            result,
            prepared_local_result=prepared,
        )
        if resolved_key is not None:
            for other_manifest in view.manifest.items:
                if other_manifest.item_id == item_id:
                    continue
                other_source = other_manifest.source
                manifest_key = getattr(other_source, "item_key", None) or getattr(other_source, "resolved_item_key", None)
                if manifest_key == resolved_key:
                    raise BatchRuntimeError("duplicate_source", f"resolved Zotero key duplicates manifest item: {resolved_key}")
            for other_state in view.state.items:
                if other_state.item_id != item_id and other_state.resolved_zotero_item_key == resolved_key:
                    raise BatchRuntimeError("duplicate_source", f"resolved Zotero key duplicates finished item: {resolved_key}")
        candidate_sha = result.candidate.sha256 if result.candidate is not None else None
        error_code = result.error.code if result.error is not None else None
        error_message = result.error.message if result.error is not None else None
        data = FinishedData(
            kind="worker.finished",
            item_id=item_id,
            actor_id=worker_id,
            claim_id=claim_id,
            attempt_id=attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=token_hash,
            status=result.status,
            result_sha256=result_sha256,
            resolved_zotero_item_key=resolved_key,
            candidate_sha256=candidate_sha,
            failure_code=error_code,
            failure_message=error_message,
        )
        publication = ResultPublication(
            path=view.run_dir / "results" / "worker" / f"{result_sha256}.json",
            content=raw,
        )
        return ProposedTransition(data=data, result=_finish_result(view, data), publication=publication)

    def reconstruct(view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, FinishedData) or event.data.kind != "worker.finished":
            raise BatchRuntimeError("journal_corrupt", "worker finish request points to another event type")
        return _finish_result(view, event.data)

    def replay_validate(view: RunView, event) -> None:
        if not isinstance(event.data, FinishedData) or event.data.kind != "worker.finished":
            raise BatchRuntimeError("journal_corrupt", "worker finish replay points to another event type")
        manifest_item = next(
            (item for item in view.manifest.items if item.item_id == event.data.item_id),
            None,
        )
        if manifest_item is None:
            raise BatchRuntimeError("journal_corrupt", "worker finish replay item is absent from manifest")
        if isinstance(manifest_item, PdfManifestItem):
            validate_pdf_source(manifest_item.source)

    def commit_validate(view: RunView) -> None:
        _validate_worker_pdf_source(view, item_id)

    def artifact_commit_guard(view: RunView, _event):
        state_item = next(
            entry for entry in view.state.items if entry.item_id == item_id
        )
        manifest_item = next(
            entry for entry in view.manifest.items if entry.item_id == item_id
        )
        prepared = _load_prepared_local_result(
            view,
            item=state_item,
            manifest_item=manifest_item,
        )
        return worker_result_artifact_commit_guard(
            view.manifest,
            result,
            prepared_local_result=prepared,
        )

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="worker.finish",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        replay_validate=replay_validate,
        commit_validate=commit_validate,
        commit_guard=artifact_commit_guard,
        fault=fault,
    )


def retry_worker(
    run_dir: Path,
    item_id: str,
    *,
    request_id: str,
    now: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    preflight, canonical_request_id, _existing_event = load_request_preflight(
        run_dir,
        request_id=request_id,
        command="worker.retry",
    )
    fingerprint = canonical_sha256(
        {
            "command": "worker.retry",
            "run_dir": str(preflight.run_dir),
            "manifest_sha256": preflight.manifest_sha256,
            "item_id": item_id,
            "now_override": now,
        }
    )

    def result_for(view: RunView, data: RetriedData) -> dict[str, Any]:
        return {
            "run_dir": str(view.run_dir),
            "item_id": data.item_id,
            "status": "queued",
            "previous_attempt_id": data.previous_attempt_id,
            "next_attempt_id": data.next_attempt_id,
            "next_attempt_number": data.next_attempt_number,
        }

    def propose(view: RunView, _transaction_time: str) -> ProposedTransition:
        item = next((entry for entry in view.state.items if entry.item_id == item_id), None)
        if item is None:
            raise BatchRuntimeError("unknown_item", f"unknown item id: {item_id}")
        if item.worker_status not in {"failed", "blocked"} or item.worker_lease is not None:
            raise BatchRuntimeError("retry_not_allowed", "worker retry requires a failed/blocked inactive attempt")
        if not all(
            [
                item.worker_last_actor_id,
                item.worker_last_claim_id,
                item.worker_last_attempt_id,
                item.worker_last_lease_token_sha256,
            ]
        ):
            raise BatchRuntimeError("journal_corrupt", "failed worker state is missing last attempt identity")
        data = RetriedData(
            item_id=item_id,
            previous_actor_id=item.worker_last_actor_id,
            previous_claim_id=item.worker_last_claim_id,
            previous_attempt_id=item.worker_last_attempt_id,
            previous_attempt_number=item.worker_attempt_count,
            previous_lease_token_sha256=item.worker_last_lease_token_sha256,
            next_attempt_id=str(uuid4()),
            next_attempt_number=item.worker_attempt_count + 1,
        )
        return ProposedTransition(data=data, result=result_for(view, data))

    def reconstruct(view: RunView, event) -> dict[str, Any]:
        if not isinstance(event.data, RetriedData):
            raise BatchRuntimeError("journal_corrupt", "worker retry request points to another event type")
        return result_for(view, event.data)

    return append_transaction(
        run_dir,
        expected_manifest_sha256=preflight.manifest_sha256,
        expected_run_dir_identity=preflight.run_dir_identity,
        request_id=canonical_request_id,
        command="worker.retry",
        request_fingerprint=fingerprint,
        occurred_at=now,
        propose=propose,
        reconstruct=reconstruct,
        fault=fault,
    )
