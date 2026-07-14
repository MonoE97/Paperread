from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import stat
from pathlib import Path

import pytest

import paper_reader_batch.v2_journal as journal_module
import paper_reader_batch.v2_worker as worker_module
from paper_reader_batch.v2_artifacts import paper_reader_root_identity
from paper_reader_batch.v2_contracts import (
    BatchEvent,
    EventCommandResultSnapshot,
    FinishedData,
    LocalPrepareResult,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_json import canonical_json_bytes, canonical_sha256, sha256_bytes
from paper_reader_batch.v2_local_prepare import (
    _ChildProtocolError,
    claim_local_prepare,
    finish_local_prepare,
    renew_local_prepare,
    run_local_prepare,
)
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_reducer import apply_event
from paper_reader_batch.v2_run import initialize_run, recover_run
from paper_reader_batch.v2_worker import claim_worker, release_worker, renew_worker
from paper_reader_batch.v2_write import claim_write, renew_write

from test_v2_local_prepare_coordinator import (
    PAPER_READER_ROOT,
    _batch_run as _coordinator_batch_run,
    _run_kwargs as _coordinator_run_kwargs,
)
from test_v2_local_prepare_leases import _fake_paper_reader_root
from test_v2_write_runtime import _ready_write_run


def _local_run(tmp_path: Path) -> Path:
    skill = tmp_path / "batch-skill"
    skill.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\npre-recovery identity\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="pre-recovery identity",
        output=manifest,
        request_id="11111111-1111-4111-8111-111111111111",
        skill_root=skill,
        created_at="2026-07-10T00:00:00Z",
    )
    run_dir = tmp_path / "run"
    initialize_run(
        manifest,
        request_id="22222222-2222-4222-8222-222222222222",
        skill_root=skill,
        output=run_dir,
        initialized_at="2026-07-10T00:00:00Z",
    )
    return run_dir


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, int, bytes | None]]:
    snapshot: dict[str, tuple[int, int, int, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        snapshot[path.relative_to(root).as_posix()] = (
            stat.S_IMODE(metadata.st_mode),
            metadata.st_size,
            metadata.st_mtime_ns,
            path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None,
        )
    return snapshot


def _stop_during_snapshot_transition():
    snapshot_started = False

    def fault(stage: str) -> None:
        nonlocal snapshot_started
        if stage == "before_snapshot":
            snapshot_started = True
        elif snapshot_started and stage == "after_pending_rename":
            raise RuntimeError("stop with a durable pending state transition")

    return fault


def _durably_uncertain_runner(_argv, _cwd, _timeout_seconds, invocation):
    invocation.mark_started()
    raise _ChildProtocolError(
        "coordination_uncertain",
        "the exact child attempt started but its outcome is unknown",
    )


def test_worker_stale_token_is_rejected_before_pending_state_recovery(
    tmp_path: Path,
) -> None:
    run_dir = _local_run(tmp_path)
    assignment = claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        limit=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    with pytest.raises(RuntimeError, match="pending state transition"):
        renew_worker(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            request_id="44444444-4444-4444-8444-444444444444",
            lease_seconds=1000,
            now="2026-07-10T00:00:02Z",
            fault=_stop_during_snapshot_transition(),
        )
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        renew_worker(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token="stale-token",
            attempt_id=assignment["attempt_id"],
            request_id="55555555-5555-4555-8555-555555555555",
            lease_seconds=2000,
            now="2026-07-10T00:00:03Z",
        )

    assert exc_info.value.code == "lease_identity_mismatch"
    assert _tree_snapshot(run_dir) == before


def test_pre_recovery_identity_validation_cannot_call_mutation_primitives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _local_run(tmp_path)
    assignment = claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        limit=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    with pytest.raises(RuntimeError, match="pending state transition"):
        renew_worker(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            request_id="44444444-4444-4444-8444-444444444444",
            lease_seconds=1000,
            now="2026-07-10T00:00:02Z",
            fault=_stop_during_snapshot_transition(),
        )

    def forbidden_mutation(*_args, **_kwargs):
        raise AssertionError("pre-recovery validation attempted a storage mutation")

    monkeypatch.setattr(journal_module, "replace_bytes_atomic", forbidden_mutation)
    monkeypatch.setattr(journal_module, "promote_bytes_no_replace", forbidden_mutation)
    monkeypatch.setattr(journal_module, "publish_bytes_no_replace", forbidden_mutation)

    with pytest.raises(BatchRuntimeError) as exc_info:
        renew_worker(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token="stale-token",
            attempt_id=assignment["attempt_id"],
            request_id="55555555-5555-4555-8555-555555555555",
            lease_seconds=2000,
            now="2026-07-10T00:00:03Z",
        )

    assert exc_info.value.code == "lease_identity_mismatch"


@pytest.mark.parametrize("lane", ["worker", "local_prepare"])
def test_commit_validation_rebinds_pdf_after_pre_recovery_proposal(
    tmp_path: Path,
    lane: str,
) -> None:
    run_dir = _local_run(tmp_path)
    pdf = Path(load_run_view(run_dir).manifest.items[0].source.path)
    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(b"%PDF-1.7\nreplaced between validation and commit\n")
    before = _tree_snapshot(run_dir)
    observed: list[str] = []

    def replace_after_proposal(stage: str) -> None:
        observed.append(stage)
        if stage == "after_pre_recovery_validation":
            replacement.replace(pdf)

    with pytest.raises(BatchRuntimeError) as exc_info:
        if lane == "worker":
            claim_worker(
                run_dir,
                worker_id="worker",
                request_id="33333333-3333-4333-8333-333333333333",
                limit=1,
                now="2026-07-10T00:00:01Z",
                fault=replace_after_proposal,
            )
        else:
            claim_local_prepare(
                run_dir,
                worker_id="preparer",
                request_id="33333333-3333-4333-8333-333333333333",
                limit=1,
                now="2026-07-10T00:00:01Z",
                fault=replace_after_proposal,
            )

    assert exc_info.value.code == "source_drift"
    assert observed == ["after_pre_recovery_validation"]
    assert _tree_snapshot(run_dir) == before


def test_live_source_rebind_precedes_pending_state_recovery_mutation(
    tmp_path: Path,
) -> None:
    run_dir = _local_run(tmp_path)
    assignment = claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        limit=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    with pytest.raises(RuntimeError, match="pending state transition"):
        renew_worker(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            request_id="44444444-4444-4444-8444-444444444444",
            lease_seconds=1000,
            now="2026-07-10T00:00:02Z",
            fault=_stop_during_snapshot_transition(),
        )
    before = _tree_snapshot(run_dir)
    pdf = Path(load_run_view(run_dir, allow_pending_state_swap=True).manifest.items[0].source.path)
    replacement = tmp_path / "replacement-during-recovery.pdf"
    replacement.write_bytes(b"%PDF-1.7\nreplaced before pending recovery\n")

    def replace_after_proposal(stage: str) -> None:
        if stage == "after_pre_recovery_validation":
            replacement.replace(pdf)

    with pytest.raises(BatchRuntimeError) as exc_info:
        renew_worker(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            request_id="55555555-5555-4555-8555-555555555555",
            lease_seconds=2000,
            now="2026-07-10T00:00:03Z",
            fault=replace_after_proposal,
        )

    assert exc_info.value.code == "source_drift"
    assert _tree_snapshot(run_dir) == before


def test_proposal_is_evaluated_once_across_post_event_fault_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _local_run(tmp_path)
    real_append = journal_module.append_transaction
    proposal_calls = 0
    event_publications = 0
    real_publish = journal_module.publish_bytes_no_replace

    def counted_append(*args, **kwargs):
        original = kwargs["propose"]

        def counted_proposal(view, transaction_time):
            nonlocal proposal_calls
            proposal_calls += 1
            return original(view, transaction_time)

        kwargs["propose"] = counted_proposal
        return real_append(*args, **kwargs)

    def counted_publish(path, *args, **kwargs):
        nonlocal event_publications
        if path.parent == run_dir / "events" and path.name.endswith(".json"):
            event_publications += 1
        return real_publish(path, *args, **kwargs)

    def stop_after_event(stage: str) -> None:
        if stage == "after_event":
            raise RuntimeError("stop after durable event")

    monkeypatch.setattr(worker_module, "append_transaction", counted_append)
    monkeypatch.setattr(journal_module, "publish_bytes_no_replace", counted_publish)

    with pytest.raises(RuntimeError, match="durable event"):
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id="33333333-3333-4333-8333-333333333333",
            limit=1,
            now="2026-07-10T00:00:01Z",
            fault=stop_after_event,
        )

    event = load_run_view(run_dir).events[-1]
    assignment = event.data.assignments[0]
    replay = claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        limit=1,
        now="2026-07-10T00:00:01Z",
    )

    assert proposal_calls == 1
    assert event_publications == 1
    assert replay.replayed is True
    assert replay.result["assignments"][0]["claim_id"] == assignment.claim_id
    assert replay.result["assignments"][0]["attempt_id"] == assignment.attempt_id
    assert len(load_run_view(run_dir).events) == 2


def test_state_drift_after_pre_recovery_proposal_fails_without_reproposal_or_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _local_run(tmp_path)
    proposal_calls = 0
    real_append = journal_module.append_transaction

    def counted_append(*args, **kwargs):
        original = kwargs["propose"]

        def counted_proposal(view, transaction_time):
            nonlocal proposal_calls
            proposal_calls += 1
            return original(view, transaction_time)

        kwargs["propose"] = counted_proposal
        return real_append(*args, **kwargs)

    @contextmanager
    def drifted_locked_run(_run_dir, **kwargs):
        view = journal_module.load_run_view_for_mutation(run_dir)
        validator = kwargs["pre_recovery_validate"]
        validator(view)
        kwargs["pre_mutation_validate"](view)
        drifted_state = view.state.model_copy(
            update={"next_sequence": view.state.next_sequence + 1}
        )
        yield replace(view, state=drifted_state, lock_descriptor=99)

    def forbidden_mutation(*_args, **_kwargs):
        raise AssertionError("state drift attempted a storage mutation")

    monkeypatch.setattr(worker_module, "append_transaction", counted_append)
    monkeypatch.setattr(journal_module, "locked_run", drifted_locked_run)
    monkeypatch.setattr(journal_module, "_persist_snapshot", forbidden_mutation)
    monkeypatch.setattr(journal_module, "publish_bytes_no_replace", forbidden_mutation)
    monkeypatch.setattr(journal_module, "promote_bytes_no_replace", forbidden_mutation)
    monkeypatch.setattr(journal_module, "replace_bytes_atomic", forbidden_mutation)

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id="33333333-3333-4333-8333-333333333333",
            limit=1,
            now="2026-07-10T00:00:01Z",
        )

    assert exc_info.value.code == "journal_corrupt"
    assert proposal_calls == 1


def test_exact_request_replay_may_recover_pending_state_transition(
    tmp_path: Path,
) -> None:
    run_dir = _local_run(tmp_path)
    assignment = claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        limit=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    request_id = "44444444-4444-4444-8444-444444444444"
    kwargs = {
        "worker_id": assignment["worker_id"],
        "claim_id": assignment["claim_id"],
        "lease_token": assignment["lease_token"],
        "attempt_id": assignment["attempt_id"],
        "request_id": request_id,
        "lease_seconds": 1000,
        "now": "2026-07-10T00:00:02Z",
    }
    with pytest.raises(RuntimeError, match="pending state transition"):
        renew_worker(
            run_dir,
            assignment["item_id"],
            **kwargs,
            fault=_stop_during_snapshot_transition(),
        )

    replay = renew_worker(run_dir, assignment["item_id"], **kwargs)

    assert replay.replayed is True
    expected_expiry = "2026-07-10T00:16:42.000000Z"
    assert replay.result["expires_at"] == expected_expiry
    view = load_run_view(run_dir)
    assert view.snapshot_status == "current"
    assert view.pending_event is None
    assert view.state.items[0].worker_lease is not None
    assert view.state.items[0].worker_lease.expires_at == expected_expiry


def test_bound_request_conflict_precedes_static_acknowledgement_validation(
    tmp_path: Path,
) -> None:
    run_dir = _local_run(tmp_path)
    assignment = claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        limit=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    request_id = "44444444-4444-4444-8444-444444444444"
    release_worker(
        run_dir,
        assignment["item_id"],
        worker_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        lease_token=assignment["lease_token"],
        attempt_id=assignment["attempt_id"],
        acknowledge_no_side_effects=True,
        request_id=request_id,
        now="2026-07-10T00:00:02Z",
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        release_worker(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            acknowledge_no_side_effects=False,
            request_id=request_id,
            now="2026-07-10T00:00:02Z",
        )

    assert exc_info.value.code == "idempotency_conflict"


@pytest.mark.parametrize("lane", ["worker", "local_prepare"])
def test_exact_claim_replay_rebinds_event_assigned_pdf_source(
    tmp_path: Path,
    lane: str,
) -> None:
    run_dir = _local_run(tmp_path)
    request_id = "33333333-3333-4333-8333-333333333333"
    def claim():
        if lane == "worker":
            return claim_worker(
                run_dir,
                worker_id="worker",
                request_id=request_id,
                limit=1,
                now="2026-07-10T00:00:01Z",
            )
        return claim_local_prepare(
            run_dir,
            worker_id="preparer",
            request_id=request_id,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )
    claim()
    view = load_run_view(run_dir)
    Path(view.manifest.items[0].source.path).write_bytes(
        b"%PDF-1.7\nchanged after exact claim\n"
    )
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim()

    assert exc_info.value.code == "source_drift"
    assert _tree_snapshot(run_dir) == before


def test_exact_worker_renew_replay_rebinds_pdf_source(
    tmp_path: Path,
) -> None:
    run_dir = _local_run(tmp_path)
    assignment = claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        limit=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    request_id = "44444444-4444-4444-8444-444444444444"
    kwargs = {
        "worker_id": assignment["worker_id"],
        "claim_id": assignment["claim_id"],
        "lease_token": assignment["lease_token"],
        "attempt_id": assignment["attempt_id"],
        "request_id": request_id,
        "lease_seconds": 1000,
        "now": "2026-07-10T00:00:02Z",
    }
    renew_worker(run_dir, assignment["item_id"], **kwargs)
    view = load_run_view(run_dir)
    Path(view.manifest.items[0].source.path).write_bytes(
        b"%PDF-1.7\nchanged after exact renew\n"
    )
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        renew_worker(run_dir, assignment["item_id"], **kwargs)

    assert exc_info.value.code == "source_drift"
    assert _tree_snapshot(run_dir) == before


def test_local_prepare_stale_token_is_rejected_before_pending_state_recovery(
    tmp_path: Path,
) -> None:
    run_dir = _local_run(tmp_path)
    assignment = claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    with pytest.raises(RuntimeError, match="pending state transition"):
        renew_local_prepare(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            request_id="44444444-4444-4444-8444-444444444444",
            lease_seconds=1000,
            now="2026-07-10T00:00:02Z",
            fault=_stop_during_snapshot_transition(),
        )
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        renew_local_prepare(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token="stale-token",
            attempt_id=assignment["attempt_id"],
            request_id="55555555-5555-4555-8555-555555555555",
            lease_seconds=2000,
            now="2026-07-10T00:00:03Z",
        )

    assert exc_info.value.code == "lease_identity_mismatch"
    assert _tree_snapshot(run_dir) == before


def test_write_stale_token_is_rejected_before_pending_state_recovery(
    tmp_path: Path,
) -> None:
    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer",
        request_id="44444444-4444-4444-8444-444444444444",
        now="2026-07-10T00:00:03Z",
    ).result
    with pytest.raises(RuntimeError, match="pending state transition"):
        renew_write(
            ready.run_dir,
            "001",
            writer_id="writer",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            request_id="55555555-5555-4555-8555-555555555555",
            now="2026-07-10T00:00:04Z",
            fault=_stop_during_snapshot_transition(),
        )
    before = _tree_snapshot(ready.run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        renew_write(
            ready.run_dir,
            "001",
            writer_id="writer",
            claim_id=claimed["claim_id"],
            lease_token="stale-token",
            write_attempt_id=claimed["write_attempt_id"],
            request_id="66666666-6666-4666-8666-666666666666",
            now="2026-07-10T00:00:05Z",
        )

    assert exc_info.value.code == "write_lease_identity_mismatch"
    assert _tree_snapshot(ready.run_dir) == before


def test_coordination_request_conflict_is_rejected_before_pending_state_recovery(
    tmp_path: Path,
) -> None:
    run_dir, _source, assignment = _coordinator_batch_run(tmp_path)
    coordinator_request_id = "77777777-7777-4777-8777-777777777777"

    def stop_after_reservation(stage: str) -> None:
        if stage == "after_init_invocation_reserved":
            raise RuntimeError("stop after exact coordination reservation")

    with pytest.raises(RuntimeError, match="exact coordination reservation"):
        run_local_prepare(
            **_coordinator_run_kwargs(run_dir, assignment, coordinator_request_id),
            fault=stop_after_reservation,
        )
    with pytest.raises(RuntimeError, match="pending state transition"):
        renew_local_prepare(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            request_id="88888888-8888-4888-8888-888888888888",
            lease_seconds=1000,
            now="2026-07-11T00:00:03Z",
            fault=_stop_during_snapshot_transition(),
        )
    before = _tree_snapshot(run_dir)
    conflicting = _coordinator_run_kwargs(run_dir, assignment, coordinator_request_id)
    conflicting.update(timeout_seconds=601, now="2026-07-11T00:00:04Z")

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_local_prepare(
            **conflicting,
            runner=lambda *_args: (_ for _ in ()).throw(
                AssertionError("conflicting coordination must not dispatch")
            ),
        )

    assert exc_info.value.code == "idempotency_conflict"
    assert _tree_snapshot(run_dir) == before

def test_completed_local_run_rejects_same_request_with_changed_timeout(
    tmp_path: Path,
) -> None:
    run_dir, source, assignment = _coordinator_batch_run(tmp_path)
    request_id = "77777777-7777-4777-8777-777777777777"
    kwargs = _coordinator_run_kwargs(run_dir, assignment, request_id)
    first = run_local_prepare(**kwargs, runner=_durably_uncertain_runner)
    assert first.result["status"] == "blocked"
    view = load_run_view(run_dir)
    state_item = view.state.items[0]
    coordination_fingerprint = state_item.local_prepare_coordination_fingerprint
    assert coordination_fingerprint is not None
    finish_event = next(event for event in view.events if event.request_id == request_id)
    expected_finish_fingerprint = canonical_sha256(
        {
            "command": "local-prepare.finish",
            "run_dir": str(view.run_dir),
            "manifest_sha256": view.manifest_sha256,
            "item_id": assignment["item_id"],
            "worker_id": assignment["worker_id"],
            "claim_id": assignment["claim_id"],
            "attempt_id": assignment["attempt_id"],
            "lease_token_sha256": sha256_bytes(assignment["lease_token"].encode()),
            "result_input_path": first.result["result_path"],
            "result_sha256": first.result["result_sha256"],
            "expected_root": str(Path(kwargs["paper_reader_root"]).resolve()),
            "coordination_request_fingerprint": coordination_fingerprint,
            "now_override": kwargs["now"],
        }
    )
    assert finish_event.request_fingerprint == expected_finish_fingerprint
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_local_prepare(
            **{**kwargs, "timeout_seconds": 601},
            runner=lambda *_args: (_ for _ in ()).throw(
                AssertionError("conflicting replay must not dispatch")
            ),
        )

    assert exc_info.value.code == "idempotency_conflict"
    assert _tree_snapshot(run_dir) == before

    source.write_bytes(b"%PDF-1.7\ndrifted after committed local run\n")
    before_drifted_replays = _tree_snapshot(run_dir)
    with pytest.raises(BatchRuntimeError) as changed_input:
        run_local_prepare(
            **{**kwargs, "timeout_seconds": 601},
            runner=lambda *_args: (_ for _ in ()).throw(
                AssertionError("conflicting drifted replay must not dispatch")
            ),
        )
    assert changed_input.value.code == "idempotency_conflict"
    with pytest.raises(BatchRuntimeError) as exact_drift:
        run_local_prepare(
            **kwargs,
            runner=lambda *_args: (_ for _ in ()).throw(
                AssertionError("source-drifted exact replay must not dispatch")
            ),
        )
    assert exact_drift.value.code == "source_drift"
    assert _tree_snapshot(run_dir) == before_drifted_replays


def test_completed_local_run_rejects_same_request_after_root_identity_drift(
    tmp_path: Path,
) -> None:
    run_dir = _local_run(tmp_path)
    root = _fake_paper_reader_root(tmp_path)
    assignment = claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    request_id = "44444444-4444-4444-8444-444444444444"
    kwargs = {
        "run_dir": run_dir,
        "item_id": assignment["item_id"],
        "worker_id": assignment["worker_id"],
        "claim_id": assignment["claim_id"],
        "lease_token": assignment["lease_token"],
        "attempt_id": assignment["attempt_id"],
        "paper_reader_root": root,
        "request_id": request_id,
        "now": "2026-07-10T00:00:02Z",
    }
    first = run_local_prepare(**kwargs, runner=_durably_uncertain_runner)
    assert first.result["status"] == "blocked"
    (root / "SKILL.md").write_text("# changed root identity\n", encoding="utf-8")
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_local_prepare(
            **kwargs,
            runner=lambda *_args: (_ for _ in ()).throw(
                AssertionError("drifted replay must not dispatch")
            ),
        )

    assert exc_info.value.code == "idempotency_conflict"
    assert _tree_snapshot(run_dir) == before


def test_recover_request_conflict_precedes_pending_snapshot_recovery(
    tmp_path: Path,
) -> None:
    run_dir = _local_run(tmp_path)
    claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        limit=1,
        lease_seconds=1,
        now="2026-07-10T00:00:01Z",
    )
    request_id = "44444444-4444-4444-8444-444444444444"
    with pytest.raises(RuntimeError, match="pending state transition"):
        recover_run(
            run_dir,
            request_id=request_id,
            reconciliation_timeout_seconds=20,
            now="2026-07-10T00:00:03Z",
            fault=_stop_during_snapshot_transition(),
        )
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        recover_run(
            run_dir,
            request_id=request_id,
            reconciliation_timeout_seconds=21,
            now="2026-07-10T00:00:03Z",
        )

    assert exc_info.value.code == "idempotency_conflict"
    assert _tree_snapshot(run_dir) == before


def test_run_init_receipt_conflict_precedes_live_pdf_source_drift(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "batch-skill"
    skill.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nrun init receipt\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="run init receipt",
        output=manifest,
        request_id="11111111-1111-4111-8111-111111111111",
        skill_root=skill,
        created_at="2026-07-10T00:00:00Z",
    )
    request_id = "22222222-2222-4222-8222-222222222222"
    initialize_run(
        manifest,
        request_id=request_id,
        skill_root=skill,
        output=tmp_path / "run-a",
        initialized_at="2026-07-10T00:00:00Z",
    )
    pdf.write_bytes(b"%PDF-1.7\ndrifted after committed init\n")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(BatchRuntimeError) as exc_info:
        initialize_run(
            manifest,
            request_id=request_id,
            skill_root=skill,
            output=tmp_path / "run-b",
            initialized_at="2026-07-10T00:00:00Z",
        )

    assert exc_info.value.code == "idempotency_conflict"
    assert _tree_snapshot(tmp_path) == before

    with pytest.raises(BatchRuntimeError) as exact_drift:
        initialize_run(
            manifest,
            request_id=request_id,
            skill_root=skill,
            output=tmp_path / "run-a",
            initialized_at="2026-07-10T00:00:00Z",
        )
    assert exact_drift.value.code == "source_drift"
    assert _tree_snapshot(tmp_path) == before


def test_synthetic_coordination_uncertain_finish_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    run_dir = _local_run(tmp_path)
    root = _fake_paper_reader_root(tmp_path)
    assignment = claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    view = load_run_view(run_dir)
    result = LocalPrepareResult(
        schema_version="paper_reader_batch.local-prepare-result.v2",
        manifest_sha256=view.manifest_sha256,
        item_id=assignment["item_id"],
        worker_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        attempt_id=assignment["attempt_id"],
        attempt_number=assignment["attempt_number"],
        lease_token_sha256=sha256_bytes(assignment["lease_token"].encode()),
        status="blocked",
        source=view.manifest.items[0].source,
        paper_reader_root=paper_reader_root_identity(root),
        paper_reader_run_directory=None,
        error={
            "code": "coordination_uncertain",
            "message": "synthetic uncertainty without a reserved coordinator",
        },
    )
    result_path = tmp_path / "synthetic-uncertain.json"
    result_path.write_bytes(canonical_json_bytes(result))
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        finish_local_prepare(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            result_path=result_path,
            expected_root=root,
            request_id="44444444-4444-4444-8444-444444444444",
            now="2026-07-10T00:00:02Z",
        )

    assert exc_info.value.code == "coordination_corrupt"
    assert _tree_snapshot(run_dir) == before


def test_reducer_rejects_coordination_uncertain_finish_without_reservation(
    tmp_path: Path,
) -> None:
    run_dir = _local_run(tmp_path)
    assignment = claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    view = load_run_view(run_dir)
    lease = view.state.items[0].local_prepare_lease
    assert lease is not None
    data = FinishedData(
        kind="local_prepare.finished",
        item_id=assignment["item_id"],
        actor_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        attempt_id=assignment["attempt_id"],
        attempt_number=assignment["attempt_number"],
        lease_token_sha256=lease.lease_token_sha256,
        status="blocked",
        result_sha256="1" * 64,
        failure_code="coordination_uncertain",
        failure_message="synthetic uncertainty",
    )
    event = BatchEvent(
        schema_version="paper_reader_batch.event.v2",
        sequence=view.state.next_sequence,
        event_id="44444444-4444-4444-8444-444444444444",
        occurred_at="2026-07-10T00:00:02Z",
        request_id="55555555-5555-4555-8555-555555555555",
        command="local-prepare.finish",
        request_fingerprint="2" * 64,
        manifest_sha256=view.manifest_sha256,
        previous_event_sha256=view.state.latest_event_sha256,
        data=data,
        command_result=EventCommandResultSnapshot(
            schema_version="paper_reader_batch.command-result.v2",
            command="local-prepare.finish",
            request_id="55555555-5555-4555-8555-555555555555",
            semantic_result_sha256="3" * 64,
        ),
        event_sha256="4" * 64,
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        apply_event(view.state, view.manifest, event)

    assert exc_info.value.code == "journal_corrupt"
