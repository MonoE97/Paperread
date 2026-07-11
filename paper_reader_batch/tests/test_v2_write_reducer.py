from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

from paper_reader_batch.v2_contracts import (
    BatchEvent,
    BatchManifest,
    BatchState,
    EventCommandResultSnapshot,
    StateItem,
    WriteClaimedData,
    WriteLeaseMutationData,
    WriteReconciledData,
    WriteRetriedData,
    WriteStartedData,
    WriteUncertainData,
    WriteWrittenData,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_reducer import apply_event


MANIFEST_SHA = "1" * 64
LEASE_SECRET_SHA = "2" * 64
CANDIDATE_SHA = "3" * 64
RESULT_SHA = "4" * 64
AUTHORIZATION_SHA = "5" * 64
NONCE_SHA = "6" * 64
RECONCILIATION_SHA = "7" * 64
LEASE_TOKEN_SHA = "8" * 64
CLAIM_ID = "11111111-1111-4111-8111-111111111111"
WRITE_ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"
NEXT_CLAIM_ID = "33333333-3333-4333-8333-333333333333"
NEXT_WRITE_ATTEMPT_ID = "44444444-4444-4444-8444-444444444444"


def _at(seconds: int) -> str:
    value = datetime(2026, 7, 11, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat().replace("+00:00", "Z")


def _manifest(*, count: int = 1, write_policy: str = "zotero_write") -> BatchManifest:
    return BatchManifest(
        schema_version="paper_reader_batch.manifest.v2",
        manifest_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        created_at=_at(0),
        batch_title="write reducer",
        default_concurrency=3,
        write_policy=write_policy,
        source_summary={"source_type": "zotero_collection", "description": "test"},
        items=[
            {
                "item_id": f"item-{index + 1}",
                "input_type": "zotero_item",
                "source": {
                    "source_type": "zotero_item",
                    "item_key": f"PARENT{index + 1}",
                    "title": f"Paper {index + 1}",
                    "inventory_sha256": format(index + 9, "x") * 64,
                },
                "expected_output": "zotero_note_candidate",
            }
            for index in range(count)
        ],
    )


def _state(manifest: BatchManifest) -> BatchState:
    return BatchState(
        schema_version="paper_reader_batch.state.v2",
        manifest_id=manifest.manifest_id,
        manifest_sha256=MANIFEST_SHA,
        lease_secret_sha256=LEASE_SECRET_SHA,
        initialized_at=_at(0),
        updated_at=_at(0),
        next_sequence=2,
        latest_event_sha256="a" * 64,
        batch_status="awaiting_write",
        items=[
            StateItem(
                item_id=item.item_id,
                input_type=item.input_type,
                expected_output=item.expected_output,
                worker_status="succeeded",
                worker_attempt_count=1,
                worker_result_sha256=format(index + 11, "x") * 64,
                candidate_sha256=CANDIDATE_SHA if index == 0 else format(index + 12, "x") * 64,
                resolved_zotero_item_key=item.source.item_key,
                local_prepare_status="not_applicable",
                write_status="queued",
            )
            for index, item in enumerate(manifest.items)
        ],
    )


def _event(state: BatchState, manifest: BatchManifest, data, *, second: int) -> BatchEvent:
    sequence = state.next_sequence
    event_id = str(uuid5(NAMESPACE_URL, f"event-{sequence}-{data.kind}"))
    request_id = str(uuid5(NAMESPACE_URL, f"request-{sequence}-{data.kind}"))
    event_sha = format((sequence + 7) % 16, "x") * 64
    command = data.kind.replace(".", "-")
    return BatchEvent(
        schema_version="paper_reader_batch.event.v2",
        sequence=sequence,
        event_id=event_id,
        occurred_at=_at(second),
        request_id=request_id,
        command=command,
        request_fingerprint=format((sequence + 8) % 16, "x") * 64,
        manifest_sha256=MANIFEST_SHA,
        previous_event_sha256=state.latest_event_sha256,
        data=data,
        command_result=EventCommandResultSnapshot(
            schema_version="paper_reader_batch.command-result.v2",
            command=command,
            request_id=request_id,
            semantic_result_sha256=format((sequence + 9) % 16, "x") * 64,
        ),
        event_sha256=event_sha,
    )


def _claim(
    *,
    item_id: str = "item-1",
    second: int = 1,
    duration: int = 120,
    candidate_sha256: str = CANDIDATE_SHA,
    claim_id: str = CLAIM_ID,
    write_attempt_id: str = WRITE_ATTEMPT_ID,
    attempt_number: int = 1,
    lease_token_sha256: str = LEASE_TOKEN_SHA,
) -> WriteClaimedData:
    return WriteClaimedData(
        item_id=item_id,
        writer_id="writer-1",
        claim_id=claim_id,
        write_attempt_id=write_attempt_id,
        attempt_number=attempt_number,
        lease_token_sha256=lease_token_sha256,
        issued_at=_at(second),
        expires_at=_at(second + duration),
        candidate_sha256=candidate_sha256,
    )


def _start(*, second: int = 10, authorization_sha256: str = AUTHORIZATION_SHA) -> WriteStartedData:
    return WriteStartedData(
        item_id="item-1",
        writer_id="writer-1",
        claim_id=CLAIM_ID,
        write_attempt_id=WRITE_ATTEMPT_ID,
        attempt_number=1,
        lease_token_sha256=LEASE_TOKEN_SHA,
        candidate_sha256=CANDIDATE_SHA,
        authorization_sha256=authorization_sha256,
        authorization_nonce_sha256=NONCE_SHA,
        external_claim_id=CLAIM_ID,
        started_at=_at(second),
    )


def _lease_mutation(
    kind: str,
    *,
    second: int | None = None,
    expires_second: int | None = None,
    candidate_sha256: str = CANDIDATE_SHA,
) -> WriteLeaseMutationData:
    return WriteLeaseMutationData(
        kind=kind,
        item_id="item-1",
        writer_id="writer-1",
        claim_id=CLAIM_ID,
        write_attempt_id=WRITE_ATTEMPT_ID,
        attempt_number=1,
        lease_token_sha256=LEASE_TOKEN_SHA,
        candidate_sha256=candidate_sha256,
        issued_at=_at(second) if second is not None else None,
        expires_at=_at(expires_second) if expires_second is not None else None,
    )


def _written(*, second: int = 20) -> WriteWrittenData:
    return WriteWrittenData(
        item_id="item-1",
        writer_id="writer-1",
        claim_id=CLAIM_ID,
        write_attempt_id=WRITE_ATTEMPT_ID,
        attempt_number=1,
        lease_token_sha256=LEASE_TOKEN_SHA,
        candidate_sha256=CANDIDATE_SHA,
        authorization_sha256=AUTHORIZATION_SHA,
        result_sha256=RESULT_SHA,
        note_key="NOTE1",
        parent_key="PARENT1",
        canonical_html_sha256="9" * 64,
    )


def _uncertain(kind: str, *, reason: str = "MCP outcome is unknown") -> WriteUncertainData:
    return WriteUncertainData(
        kind=kind,
        item_id="item-1",
        writer_id="writer-1",
        claim_id=CLAIM_ID,
        write_attempt_id=WRITE_ATTEMPT_ID,
        attempt_number=1,
        lease_token_sha256=LEASE_TOKEN_SHA,
        candidate_sha256=CANDIDATE_SHA,
        authorization_sha256=AUTHORIZATION_SHA,
        reason=reason,
    )


def _reconciled(outcome: str) -> WriteReconciledData:
    return WriteReconciledData(
        item_id="item-1",
        writer_id="writer-1",
        claim_id=CLAIM_ID,
        write_attempt_id=WRITE_ATTEMPT_ID,
        attempt_number=1,
        lease_token_sha256=LEASE_TOKEN_SHA,
        candidate_sha256=CANDIDATE_SHA,
        authorization_sha256=AUTHORIZATION_SHA,
        reconciliation_sha256=RECONCILIATION_SHA,
        outcome=outcome,
    )


def _retry(*, acknowledged: bool = True) -> WriteRetriedData:
    return WriteRetriedData(
        item_id="item-1",
        previous_writer_id="writer-1",
        previous_claim_id=CLAIM_ID,
        previous_write_attempt_id=WRITE_ATTEMPT_ID,
        previous_attempt_number=1,
        previous_lease_token_sha256=LEASE_TOKEN_SHA,
        candidate_sha256=CANDIDATE_SHA,
        authorization_sha256=AUTHORIZATION_SHA,
        previous_authorization_nonce_sha256=NONCE_SHA,
        previous_external_claim_id=CLAIM_ID,
        reconciliation_sha256=RECONCILIATION_SHA,
        acknowledged_no_match=acknowledged,
        next_write_attempt_id=NEXT_WRITE_ATTEMPT_ID,
        next_attempt_number=2,
    )


def _apply(state: BatchState, manifest: BatchManifest, data, *, second: int) -> BatchState:
    return apply_event(state, manifest, _event(state, manifest, data, second=second))


def test_write_happy_path_is_queued_claimed_started_written() -> None:
    manifest = _manifest()
    state = _state(manifest)

    state = _apply(state, manifest, _claim(), second=1)
    assert state.items[0].write_status == "claimed"
    assert state.items[0].write_attempt_count == 1
    assert state.items[0].write_lease is not None
    assert state.batch_status == "running"

    start_event = _event(state, manifest, _start(), second=10)
    state = apply_event(state, manifest, start_event)
    assert state.items[0].write_status == "started"
    assert state.items[0].write_started_event_sha256 == start_event.event_sha256
    assert state.items[0].authorization_sha256 == AUTHORIZATION_SHA
    assert state.items[0].authorization_nonce_sha256 == NONCE_SHA
    assert state.items[0].external_claim_id == CLAIM_ID
    assert state.items[0].write_lease is not None

    state = _apply(state, manifest, _written(), second=20)
    assert state.items[0].write_status == "written"
    assert state.items[0].write_result_sha256 == RESULT_SHA
    assert state.items[0].write_lease is None
    assert state.batch_status == "succeeded"


@pytest.mark.parametrize(
    ("claim", "event_second"),
    [
        (_claim(candidate_sha256="f" * 64), 1),
        (_claim(duration=301), 1),
        (_claim(second=1), 2),
        (_claim(attempt_number=2), 1),
    ],
)
def test_write_claim_rejects_candidate_time_and_attempt_mismatch(
    claim: WriteClaimedData,
    event_second: int,
) -> None:
    manifest = _manifest()
    state = _state(manifest)

    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(state, manifest, claim, second=event_second)
    assert exc_info.value.code == "journal_corrupt"


def test_write_claim_rejects_prepare_only_and_parallel_write() -> None:
    prepare_only = _manifest(write_policy="prepare_only")
    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(_state(prepare_only), prepare_only, _claim(), second=1)
    assert exc_info.value.code == "journal_corrupt"

    manifest = _manifest(count=2)
    state = _apply(_state(manifest), manifest, _claim(), second=1)
    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(
            state,
            manifest,
            _claim(
                item_id="item-2",
                second=2,
                candidate_sha256=state.items[1].candidate_sha256,
                claim_id=NEXT_CLAIM_ID,
                write_attempt_id=NEXT_WRITE_ATTEMPT_ID,
            ),
            second=2,
        )
    assert exc_info.value.code == "journal_corrupt"


@pytest.mark.parametrize(
    "item_update",
    [
        {"worker_status": "queued"},
        {"resolved_zotero_item_key": None},
        {"resolved_zotero_item_key": "OTHER"},
    ],
)
def test_write_claim_requires_a_completed_zotero_candidate(item_update: dict[str, object]) -> None:
    manifest = _manifest()
    state = _state(manifest)
    state = BatchState.model_validate(
        state.model_copy(
            update={"items": [state.items[0].model_copy(update=item_update)]}
        ).model_dump(mode="json")
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(state, manifest, _claim(), second=1)
    assert exc_info.value.code == "journal_corrupt"


def test_write_claim_allows_a_300_second_hard_maximum() -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(duration=300), second=1)

    assert state.items[0].write_lease is not None
    assert state.items[0].write_lease.expires_at == _at(301)


def test_claimed_write_can_renew_for_at_most_300_seconds() -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)

    state = _apply(
        state,
        manifest,
        _lease_mutation("write.renewed", second=20, expires_second=140),
        second=20,
    )
    assert state.items[0].write_status == "claimed"
    assert state.items[0].write_lease is not None
    assert state.items[0].write_lease.expires_at == _at(140)

    for mutation, event_second in [
        (_lease_mutation("write.renewed", second=30, expires_second=331), 30),
        (_lease_mutation("write.renewed", second=140, expires_second=150), 140),
        (_lease_mutation("write.renewed", second=30, expires_second=130), 30),
        (
            _lease_mutation(
                "write.renewed",
                second=30,
                expires_second=150,
                candidate_sha256="f" * 64,
            ),
            30,
        ),
    ]:
        with pytest.raises(BatchRuntimeError) as exc_info:
            _apply(state, manifest, mutation, second=event_second)
        assert exc_info.value.code == "journal_corrupt"


def test_started_write_can_renew_but_cannot_return_to_queue() -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)
    state = _apply(state, manifest, _start(), second=10)

    state = _apply(
        state,
        manifest,
        _lease_mutation("write.renewed", second=20, expires_second=140),
        second=20,
    )
    assert state.items[0].write_status == "started"
    assert state.items[0].write_lease is not None
    assert state.items[0].write_lease.expires_at == _at(140)
    assert state.items[0].authorization_sha256 == AUTHORIZATION_SHA

    for kind, second in [("write.released", 30), ("write.lease_expired", 140)]:
        with pytest.raises(BatchRuntimeError) as exc_info:
            _apply(state, manifest, _lease_mutation(kind), second=second)
        assert exc_info.value.code == "journal_corrupt"


@pytest.mark.parametrize(
    ("kind", "event_second"),
    [("write.released", 20), ("write.lease_expired", 121)],
)
def test_claimed_release_or_expiry_returns_write_to_queue(kind: str, event_second: int) -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)

    state = _apply(state, manifest, _lease_mutation(kind), second=event_second)

    assert state.items[0].write_status == "queued"
    assert state.items[0].write_lease is None
    assert state.items[0].write_attempt_count == 1
    assert state.batch_status == "awaiting_write"


@pytest.mark.parametrize(
    ("kind", "event_second"),
    [("write.released", 121), ("write.lease_expired", 120)],
)
def test_release_and_expiry_obey_the_authoritative_expiry(kind: str, event_second: int) -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)

    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(state, manifest, _lease_mutation(kind), second=event_second)
    assert exc_info.value.code == "journal_corrupt"


@pytest.mark.parametrize(
    "updates",
    [
        {"candidate_sha256": "f" * 64},
        {"lease_token_sha256": "e" * 64},
        {"external_claim_id": NEXT_CLAIM_ID},
        {"started_at": _at(11)},
    ],
)
def test_write_start_rejects_cross_identity_candidate_and_time(updates: dict[str, object]) -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)

    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(state, manifest, _start().model_copy(update=updates), second=10)
    assert exc_info.value.code == "journal_corrupt"


@pytest.mark.parametrize(
    ("kind", "event_second"),
    [("write.marked_uncertain", 20), ("write.lease_expired_uncertain", 121)],
)
def test_started_error_or_expiry_becomes_uncertain(kind: str, event_second: int) -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)
    state = _apply(state, manifest, _start(), second=10)

    state = _apply(state, manifest, _uncertain(kind), second=event_second)

    item = state.items[0]
    assert item.write_status == "uncertain"
    assert item.write_lease is None
    assert item.write_failure_code == "write_outcome_uncertain"
    assert item.write_failure_message == "MCP outcome is unknown"
    assert state.batch_status == "write_uncertain"


@pytest.mark.parametrize(
    ("kind", "event_second"),
    [("write.marked_uncertain", 121), ("write.lease_expired_uncertain", 120)],
)
def test_started_uncertain_events_obey_active_or_expired_lease(kind: str, event_second: int) -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)
    state = _apply(state, manifest, _start(), second=10)

    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(state, manifest, _uncertain(kind), second=event_second)
    assert exc_info.value.code == "journal_corrupt"


@pytest.mark.parametrize(
    ("updates", "event_second"),
    [
        ({"authorization_sha256": "f" * 64}, 20),
        ({"candidate_sha256": "e" * 64}, 20),
        ({"lease_token_sha256": "d" * 64}, 20),
        ({"parent_key": "OTHER"}, 20),
        ({}, 121),
    ],
)
def test_write_commit_requires_exact_identity_parent_authorization_and_live_lease(
    updates: dict[str, object],
    event_second: int,
) -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)
    state = _apply(state, manifest, _start(), second=10)

    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(state, manifest, _written().model_copy(update=updates), second=event_second)
    assert exc_info.value.code == "journal_corrupt"


@pytest.mark.parametrize(
    ("outcome", "write_status", "batch_status"),
    [
        ("verified", "written", "succeeded"),
        ("not_found", "retry_confirmation_required", "needs_attention"),
        ("ambiguous", "blocked", "needs_attention"),
        ("blocked", "blocked", "needs_attention"),
    ],
)
def test_reconciliation_outcome_drives_write_state(
    outcome: str,
    write_status: str,
    batch_status: str,
) -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)
    state = _apply(state, manifest, _start(), second=10)
    state = _apply(state, manifest, _uncertain("write.marked_uncertain"), second=20)

    state = _apply(state, manifest, _reconciled(outcome), second=30)

    assert state.items[0].write_status == write_status
    assert state.items[0].reconciliation_sha256 == RECONCILIATION_SHA
    assert state.batch_status == batch_status


def test_reconcile_rejects_identity_or_authorization_mismatch() -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)
    state = _apply(state, manifest, _start(), second=10)
    state = _apply(state, manifest, _uncertain("write.marked_uncertain"), second=20)

    for updates in [
        {"claim_id": NEXT_CLAIM_ID},
        {"write_attempt_id": NEXT_WRITE_ATTEMPT_ID},
        {"authorization_sha256": "f" * 64},
        {"candidate_sha256": "e" * 64},
    ]:
        with pytest.raises(BatchRuntimeError) as exc_info:
            _apply(state, manifest, _reconciled("verified").model_copy(update=updates), second=30)
        assert exc_info.value.code == "journal_corrupt"


def test_retry_requires_acknowledged_not_found_and_reserves_new_attempt() -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)
    state = _apply(state, manifest, _start(), second=10)
    state = _apply(state, manifest, _uncertain("write.marked_uncertain"), second=20)
    state = _apply(state, manifest, _reconciled("not_found"), second=30)

    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(state, manifest, _retry(acknowledged=False), second=40)
    assert exc_info.value.code == "journal_corrupt"

    state = _apply(state, manifest, _retry(), second=40)
    item = state.items[0]
    assert item.write_status == "queued"
    assert item.write_pending_attempt_id == NEXT_WRITE_ATTEMPT_ID
    assert item.write_lease is None
    assert item.write_started_event_sha256 is None
    assert item.authorization_sha256 is None
    assert item.authorization_nonce_sha256 is None
    assert item.external_claim_id is None
    assert item.reconciliation_sha256 is None
    assert state.batch_status == "awaiting_write"

    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(
            state,
            manifest,
            _claim(
                second=41,
                claim_id=NEXT_CLAIM_ID,
                write_attempt_id="55555555-5555-4555-8555-555555555555",
                attempt_number=2,
                lease_token_sha256="d" * 64,
            ),
            second=41,
        )
    assert exc_info.value.code == "journal_corrupt"

    state = _apply(
        state,
        manifest,
        _claim(
            second=41,
            claim_id=NEXT_CLAIM_ID,
            write_attempt_id=NEXT_WRITE_ATTEMPT_ID,
            attempt_number=2,
            lease_token_sha256="d" * 64,
        ),
        second=41,
    )
    assert state.items[0].write_status == "claimed"
    assert state.items[0].write_pending_attempt_id is None
    assert state.items[0].write_attempt_count == 2


def test_retry_rejects_blocked_or_mismatched_previous_attempt() -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)
    state = _apply(state, manifest, _start(), second=10)
    state = _apply(state, manifest, _uncertain("write.marked_uncertain"), second=20)

    blocked = _apply(state, manifest, _reconciled("ambiguous"), second=30)
    with pytest.raises(BatchRuntimeError):
        _apply(blocked, manifest, _retry(), second=40)

    not_found = _apply(state, manifest, _reconciled("not_found"), second=30)
    with pytest.raises(BatchRuntimeError):
        _apply(
            not_found,
            manifest,
            _retry().model_copy(update={"previous_authorization_nonce_sha256": "f" * 64}),
            second=40,
        )


def test_retry_requires_a_new_authorization_for_the_reserved_attempt() -> None:
    manifest = _manifest()
    state = _apply(_state(manifest), manifest, _claim(), second=1)
    state = _apply(state, manifest, _start(), second=10)
    state = _apply(state, manifest, _uncertain("write.marked_uncertain"), second=20)
    state = _apply(state, manifest, _reconciled("not_found"), second=30)
    state = _apply(state, manifest, _retry(), second=40)
    state = _apply(
        state,
        manifest,
        _claim(
            second=41,
            claim_id=NEXT_CLAIM_ID,
            write_attempt_id=NEXT_WRITE_ATTEMPT_ID,
            attempt_number=2,
            lease_token_sha256="d" * 64,
        ),
        second=41,
    )
    new_start = WriteStartedData(
        item_id="item-1",
        writer_id="writer-1",
        claim_id=NEXT_CLAIM_ID,
        write_attempt_id=NEXT_WRITE_ATTEMPT_ID,
        attempt_number=2,
        lease_token_sha256="d" * 64,
        candidate_sha256=CANDIDATE_SHA,
        authorization_sha256=AUTHORIZATION_SHA,
        authorization_nonce_sha256="c" * 64,
        external_claim_id=NEXT_CLAIM_ID,
        started_at=_at(42),
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(state, manifest, new_start, second=42)
    assert exc_info.value.code == "journal_corrupt"

    state = _apply(
        state,
        manifest,
        new_start.model_copy(update={"authorization_sha256": "b" * 64}),
        second=42,
    )
    assert state.items[0].write_status == "started"
    assert state.items[0].authorization_sha256 == "b" * 64


def test_state_contract_rejects_claimed_write_without_exact_lease_binding() -> None:
    manifest = _manifest()
    item = _state(manifest).items[0]

    with pytest.raises(ValidationError, match="write"):
        StateItem.model_validate(item.model_copy(update={"write_status": "claimed"}).model_dump(mode="json"))


@pytest.mark.parametrize(
    ("status", "updates"),
    [
        ("written", {}),
        ("uncertain", {}),
        ("retry_confirmation_required", {}),
        ("blocked", {}),
        ("queued", {"write_result_sha256": RESULT_SHA}),
    ],
)
def test_state_contract_rejects_write_status_without_required_terminal_closure(
    status: str,
    updates: dict[str, object],
) -> None:
    item = _state(_manifest()).items[0]

    with pytest.raises(ValidationError, match="write"):
        StateItem.model_validate(
            item.model_copy(update={"write_status": status, **updates}).model_dump(mode="json")
        )


def test_write_retry_rejects_attempt_reserved_by_another_item() -> None:
    manifest = _manifest(count=2)
    state = _apply(_state(manifest), manifest, _claim(), second=1)
    state = _apply(state, manifest, _start(), second=10)
    state = _apply(state, manifest, _uncertain("write.marked_uncertain"), second=20)
    state = _apply(state, manifest, _reconciled("not_found"), second=30)
    other = state.items[1].model_copy(
        update={"write_pending_attempt_id": NEXT_WRITE_ATTEMPT_ID}
    )
    state = BatchState.model_validate(
        state.model_copy(update={"items": [state.items[0], other]}).model_dump(mode="json")
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        _apply(state, manifest, _retry(), second=40)

    assert exc_info.value.code == "journal_corrupt"
