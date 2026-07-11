import pytest

from paper_reader_batch.v2_contracts import (
    BatchEvent,
    ClaimAssignment,
    ClaimedData,
    EventCommandResultSnapshot,
    LeaseMutationData,
    StateItem,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_local_prepare import claim_local_prepare
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_reducer import _status, apply_event
from paper_reader_batch.v2_run import initialize_run
from paper_reader_batch.v2_worker import claim_worker


def _item(item_id: str, *, worker: str = "succeeded", write: str = "not_applicable") -> StateItem:
    zotero = write != "not_applicable"
    values = {
        "item_id": item_id,
        "input_type": "zotero_item" if zotero else "pdf_path",
        "expected_output": "zotero_note_candidate" if zotero else "local_note",
        "worker_status": worker,
        "local_prepare_status": "not_applicable" if zotero else "prepared",
        "local_prepare_result_sha256": None if zotero else "1" * 64,
        "write_status": write,
    }
    if worker == "succeeded":
        values.update(worker_result_sha256="2" * 64, candidate_sha256="3" * 64)
        if zotero:
            values.update(resolved_zotero_item_key="PARENT1")
    elif worker == "claimed":
        values.update(
            worker_attempt_count=1,
            worker_last_actor_id="worker",
            worker_last_claim_id="11111111-1111-4111-8111-111111111111",
            worker_last_attempt_id="22222222-2222-4222-8222-222222222222",
            worker_last_lease_token_sha256="4" * 64,
            worker_lease={
                "lane": "worker",
                "actor_id": "worker",
                "claim_id": "11111111-1111-4111-8111-111111111111",
                "attempt_id": "22222222-2222-4222-8222-222222222222",
                "attempt_number": 1,
                "lease_token_sha256": "4" * 64,
                "issued_at": "2026-07-10T00:00:00Z",
                "expires_at": "2026-07-10T00:15:00Z",
            },
        )
    if write in {"uncertain", "blocked"}:
        values.update(
            write_attempt_count=1,
            write_last_writer_id="writer",
            write_last_claim_id="33333333-3333-4333-8333-333333333333",
            write_last_attempt_id="44444444-4444-4444-8444-444444444444",
            write_last_lease_token_sha256="5" * 64,
            write_started_event_sha256="6" * 64,
            authorization_sha256="7" * 64,
            authorization_nonce_sha256="8" * 64,
            external_claim_id="33333333-3333-4333-8333-333333333333",
            write_last_authorization_sha256="7" * 64,
            write_last_authorization_nonce_sha256="8" * 64,
            write_last_external_claim_id="33333333-3333-4333-8333-333333333333",
            write_failure_code="write_outcome_uncertain" if write == "uncertain" else "write_verification_blocked",
            write_failure_message="attention required",
        )
    if write == "blocked":
        values.update(reconciliation_sha256="9" * 64)
    return StateItem(**values)


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([_item("001", write="uncertain"), _item("002", worker="claimed")], "write_uncertain"),
        ([_item("001", write="blocked"), _item("002", worker="queued")], "needs_attention"),
        ([_item("001", write="queued"), _item("002", worker="queued")], "awaiting_write"),
    ],
)
def test_batch_status_uses_final_cross_item_priority(items: list[StateItem], expected: str) -> None:
    assert _status(items) == expected


def _run(tmp_path, *, concurrency: int = 1, pdf_count: int = 2):
    skill = tmp_path / "skill"
    skill.mkdir()
    pdfs = []
    for index in range(pdf_count):
        pdf = tmp_path / f"paper-{index}.pdf"
        pdf.write_bytes(f"%PDF-1.7\n{index}\n".encode())
        pdfs.append(pdf)
    paths = tmp_path / "paths.txt"
    paths.write_text("\n".join(str(path) for path in pdfs), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="reducer invariants",
        output=manifest_path,
        request_id="11111111-1111-4111-8111-111111111111",
        skill_root=skill,
        default_concurrency=concurrency,
        created_at="2026-07-10T00:00:00Z",
    )
    run_dir = tmp_path / "run"
    initialize_run(
        manifest_path,
        request_id="22222222-2222-4222-8222-222222222222",
        skill_root=skill,
        output=run_dir,
        initialized_at="2026-07-10T00:00:00Z",
    )
    return run_dir


def _claim_event(view, assignment: ClaimAssignment, *, occurred_at: str) -> BatchEvent:
    return BatchEvent(
        schema_version="paper_reader_batch.event.v2",
        sequence=view.state.next_sequence,
        event_id="33333333-3333-4333-8333-333333333333",
        occurred_at=occurred_at,
        request_id="44444444-4444-4444-8444-444444444444",
        command="worker.claim",
        request_fingerprint="5" * 64,
        manifest_sha256=view.manifest_sha256,
        previous_event_sha256=view.state.latest_event_sha256,
        data=ClaimedData(kind="worker.claimed", assignments=[assignment]),
        command_result=EventCommandResultSnapshot(
            schema_version="paper_reader_batch.command-result.v2",
            command="worker.claim",
            request_id="44444444-4444-4444-8444-444444444444",
            semantic_result_sha256="6" * 64,
        ),
        event_sha256="7" * 64,
    )


def _assignment(view, item_index: int, *, issued_at: str, expires_at: str) -> ClaimAssignment:
    item = view.manifest.items[item_index]
    return ClaimAssignment(
        item_id=item.item_id,
        lane="worker",
        actor_id="worker",
        claim_id="88888888-8888-4888-8888-888888888888",
        attempt_id="99999999-9999-4999-8999-999999999999",
        attempt_number=1,
        lease_token_sha256="a" * 64,
        issued_at=issued_at,
        expires_at=expires_at,
        source=item.source,
    )


def test_reducer_rejects_claim_that_exceeds_manifest_capacity(tmp_path) -> None:
    run_dir = _run(tmp_path, concurrency=1)
    claim_worker(
        run_dir,
        worker_id="first",
        request_id="33333333-3333-4333-8333-333333333333",
        limit=1,
        now="2026-07-10T00:00:01Z",
    )
    view = load_run_view(run_dir)
    event = _claim_event(
        view,
        _assignment(
            view,
            1,
            issued_at="2026-07-10T00:00:02Z",
            expires_at="2026-07-10T00:15:02Z",
        ),
        occurred_at="2026-07-10T00:00:02Z",
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        apply_event(view.state, view.manifest, event)
    assert exc_info.value.code == "journal_corrupt"


@pytest.mark.parametrize(
    ("occurred_at", "issued_at", "expires_at"),
    [
        ("2026-07-10T00:00:01Z", "2026-07-10T00:00:00Z", "2026-07-10T00:15:00Z"),
        ("2026-07-10T00:00:01Z", "2026-07-10T00:00:01Z", "2026-07-10T00:00:01Z"),
        ("2026-07-10T00:00:01Z", "2026-07-10T00:00:01Z", "2026-07-10T01:00:02Z"),
    ],
)
def test_reducer_rejects_non_authoritative_initial_lease_times(
    tmp_path,
    occurred_at: str,
    issued_at: str,
    expires_at: str,
) -> None:
    run_dir = _run(tmp_path, concurrency=1, pdf_count=1)
    view = load_run_view(run_dir)
    event = _claim_event(
        view,
        _assignment(view, 0, issued_at=issued_at, expires_at=expires_at),
        occurred_at=occurred_at,
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        apply_event(view.state, view.manifest, event)
    assert exc_info.value.code == "journal_corrupt"


@pytest.mark.parametrize("lane", ["worker", "local_prepare"])
def test_reducer_rejects_renewal_beyond_maximum_duration(tmp_path, lane: str) -> None:
    run_dir = _run(tmp_path, concurrency=1, pdf_count=1)
    if lane == "worker":
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id="33333333-3333-4333-8333-333333333333",
            now="2026-07-10T00:00:01Z",
        )
    else:
        claim_local_prepare(
            run_dir,
            worker_id="preparer",
            request_id="33333333-3333-4333-8333-333333333333",
            now="2026-07-10T00:00:01Z",
        )
    view = load_run_view(run_dir)
    item = view.state.items[0]
    lease = item.worker_lease if lane == "worker" else item.local_prepare_lease
    assert lease is not None
    command = "worker.renew" if lane == "worker" else "local-prepare.renew"
    event = BatchEvent(
        schema_version="paper_reader_batch.event.v2",
        sequence=view.state.next_sequence,
        event_id="44444444-4444-4444-8444-444444444444",
        occurred_at="2026-07-10T00:00:02Z",
        request_id="55555555-5555-4555-8555-555555555555",
        command=command,
        request_fingerprint="6" * 64,
        manifest_sha256=view.manifest_sha256,
        previous_event_sha256=view.state.latest_event_sha256,
        data=LeaseMutationData(
            kind=f"{lane}.renewed",
            item_id=item.item_id,
            actor_id=lease.actor_id,
            claim_id=lease.claim_id,
            attempt_id=lease.attempt_id,
            attempt_number=lease.attempt_number,
            lease_token_sha256=lease.lease_token_sha256,
            issued_at="2026-07-10T00:00:02Z",
            expires_at="2026-07-10T01:00:03Z",
        ),
        command_result=EventCommandResultSnapshot(
            schema_version="paper_reader_batch.command-result.v2",
            command=command,
            request_id="55555555-5555-4555-8555-555555555555",
            semantic_result_sha256="7" * 64,
        ),
        event_sha256="8" * 64,
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        apply_event(view.state, view.manifest, event)
    assert exc_info.value.code == "journal_corrupt"
