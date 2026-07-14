import multiprocessing
import os
from pathlib import Path
import json
import hashlib

import pytest

import paper_reader_batch.v2_journal as journal_module
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_json import canonical_json_bytes, canonical_sha256
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_run import initialize_run
from paper_reader_batch.v2_worker import claim_worker, release_worker


REQUEST_MANIFEST = "11111111-1111-4111-8111-111111111111"
REQUEST_INIT = "22222222-2222-4222-8222-222222222222"
REQUEST_CLAIM = "33333333-3333-4333-8333-333333333333"


def _run(tmp_path: Path) -> Path:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nrecovery\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="journal recovery",
        output=manifest,
        request_id=REQUEST_MANIFEST,
        skill_root=skill_root,
    )
    run_dir = tmp_path / "run"
    initialize_run(
        manifest,
        request_id=REQUEST_INIT,
        skill_root=skill_root,
        output=run_dir,
        initialized_at="2026-07-10T00:00:00Z",
    )
    return run_dir


def _crash_claim(
    run_dir: str,
    stage: str,
    occurrence: int,
    request_id: str = REQUEST_CLAIM,
    worker_id: str = "worker-1",
    now: str = "2026-07-10T00:00:01Z",
) -> None:
    seen = 0

    def crash(current: str) -> None:
        nonlocal seen
        if current == stage:
            seen += 1
            if seen == occurrence:
                os._exit(77)

    claim_worker(
        Path(run_dir),
        worker_id=worker_id,
        request_id=request_id,
        limit=1,
        now=now,
        fault=crash,
    )


def _run_crashing_claim(
    run_dir: Path,
    *,
    stage: str,
    occurrence: int,
    request_id: str = REQUEST_CLAIM,
    worker_id: str = "worker-1",
    now: str = "2026-07-10T00:00:01Z",
) -> None:
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_claim,
        args=(str(run_dir), stage, occurrence, request_id, worker_id, now),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 77


def _assert_no_runtime_staging(run_dir: Path) -> None:
    assert not [path for path in run_dir.rglob("*") if path.name.endswith((".tmp", ".writing"))]


def _tree_snapshot(path: Path) -> dict[str, tuple[int, int, str]]:
    snapshot: dict[str, tuple[int, int, str]] = {}
    for candidate in sorted(path.rglob("*")):
        metadata = candidate.lstat()
        digest = ""
        if candidate.is_file():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        snapshot[str(candidate.relative_to(path))] = (
            metadata.st_mtime_ns,
            metadata.st_size,
            digest,
        )
    return snapshot


def test_event_directory_limit_is_enforced_before_replay_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    before = _tree_snapshot(run_dir)
    monkeypatch.setattr(journal_module, "_MAX_EVENT_DIRECTORY_ENTRIES", 0)

    with pytest.raises(BatchRuntimeError) as exc_info:
        load_run_view(run_dir)

    assert exc_info.value.code == "resource_limit"
    assert _tree_snapshot(run_dir) == before


def test_committed_event_aggregate_limit_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    )
    event_bytes = sum(
        path.stat().st_size
        for path in (run_dir / "events").iterdir()
        if path.name.endswith(".json") and not path.name.startswith(".")
    )
    before = _tree_snapshot(run_dir)
    monkeypatch.setattr(
        journal_module,
        "_MAX_COMMITTED_EVENT_BYTES",
        event_bytes - 1,
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        load_run_view(run_dir)

    assert exc_info.value.code == "resource_limit"
    assert _tree_snapshot(run_dir) == before


def test_append_rejects_event_that_would_exceed_journal_limit_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    initial_size = (run_dir / "events" / "00000000000000000001.json").stat().st_size
    monkeypatch.setattr(
        journal_module,
        "_MAX_COMMITTED_EVENT_BYTES",
        initial_size,
    )
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker-1",
            request_id=REQUEST_CLAIM,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )

    assert exc_info.value.code == "resource_limit"
    assert _tree_snapshot(run_dir) == before


def test_append_reserves_directory_headroom_for_proposal_and_abort_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    monkeypatch.setattr(journal_module, "_MAX_EVENT_DIRECTORY_ENTRIES", 1)
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker-1",
            request_id=REQUEST_CLAIM,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )

    assert exc_info.value.code == "resource_limit"
    assert _tree_snapshot(run_dir) == before


def test_pending_event_requires_abort_marker_directory_headroom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_writing_fsync", occurrence=1)
    before = _tree_snapshot(run_dir)
    monkeypatch.setattr(journal_module, "_MAX_EVENT_DIRECTORY_ENTRIES", 2)

    with pytest.raises(BatchRuntimeError) as exc_info:
        load_run_view(run_dir)

    assert exc_info.value.code == "resource_limit"
    assert _tree_snapshot(run_dir) == before


def test_pending_event_requires_committed_byte_headroom_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_writing_fsync", occurrence=1)
    view = load_run_view(run_dir)
    assert view.pending_event is not None
    pending_commit_bytes = max(
        len(view.pending_event.raw),
        len(
            canonical_json_bytes(
                journal_module._aborted_marker(view.pending_event.event)
            )
        ),
    )
    before = _tree_snapshot(run_dir)
    monkeypatch.setattr(
        journal_module,
        "_MAX_COMMITTED_EVENT_BYTES",
        view.committed_event_bytes + pending_commit_bytes - 1,
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        load_run_view(run_dir)

    assert exc_info.value.code == "resource_limit"
    assert _tree_snapshot(run_dir) == before


def test_partial_abort_marker_is_resumed_without_allocating_another_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_writing_fsync", occurrence=1)
    pending = load_run_view(run_dir).pending_event
    assert pending is not None
    marker_raw = canonical_json_bytes(
        journal_module._aborted_marker(pending.event)
    )
    partial_marker = (
        run_dir
        / "events"
        / ".00000000000000000002.json.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.writing"
    )
    partial_marker.write_bytes(marker_raw[: len(marker_raw) // 2])
    partial_marker.chmod(0o600)
    partial_inode = partial_marker.stat().st_ino
    entry_count = len(tuple((run_dir / "events").iterdir()))
    monkeypatch.setattr(
        journal_module,
        "_MAX_EVENT_DIRECTORY_ENTRIES",
        entry_count,
    )

    with pytest.raises(BatchRuntimeError) as replay_error:
        claim_worker(
            run_dir,
            worker_id="worker-1",
            request_id=REQUEST_CLAIM,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )

    assert replay_error.value.code == "request_aborted"
    marker_path = run_dir / "events" / "00000000000000000002.json"
    assert marker_path.stat().st_ino == partial_inode
    assert marker_path.read_bytes() == marker_raw
    assert not partial_marker.exists()
    proposal_residues = [
        path
        for path in (run_dir / "events").iterdir()
        if path.name.startswith(".00000000000000000002.json.")
    ]
    assert proposal_residues
    assert all(path.stat().st_size == 0 for path in proposal_residues)
    recovered = load_run_view(run_dir)
    assert len(recovered.events) == 2
    assert recovered.events[-1].data.kind == "request.aborted"


def test_event_pending_is_promoted_and_same_request_replays_without_residue(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_file_fsync", occurrence=1)
    before = load_run_view(run_dir)
    assert before.pending_event is not None
    assert len(before.events) == 1

    replayed = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    )
    assert replayed.replayed is True
    assert len(load_run_view(run_dir).events) == 2
    assert load_run_view(run_dir).snapshot_status == "current"
    _assert_no_runtime_staging(run_dir)


def test_state_pending_is_reused_by_replay_and_leaves_fixed_layout(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_file_fsync", occurrence=2)
    with pytest.raises(BatchRuntimeError) as exc_info:
        load_run_view(run_dir)
    assert exc_info.value.code == "storage_recovery_required"

    replayed = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    )
    assert replayed.replayed is True
    assert load_run_view(run_dir).snapshot_status == "current"
    _assert_no_runtime_staging(run_dir)


@pytest.mark.parametrize(
    ("stage", "requires_readonly_recovery"),
    [
        ("after_exchange", True),
        ("after_exchange_fsync", True),
        ("after_retired_leaf_unlink", True),
        ("after_active_owner_unlink", False),
    ],
)
def test_state_transition_hard_crash_boundaries_recover_without_second_transition(
    tmp_path: Path,
    stage: str,
    requires_readonly_recovery: bool,
) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage=stage, occurrence=1)

    if requires_readonly_recovery:
        with pytest.raises(BatchRuntimeError) as exc_info:
            load_run_view(run_dir)
        assert exc_info.value.code == "storage_recovery_required"
    else:
        assert load_run_view(run_dir).snapshot_status == "current"

    replayed = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    )
    assert replayed.replayed is True
    view = load_run_view(run_dir)
    assert len(view.events) == 2
    assert view.snapshot_status == "current"


@pytest.mark.parametrize("snapshot_kind", ["invalid", "noncanonical"])
@pytest.mark.parametrize("crash_stage", ["after_exchange", "after_retired_leaf_unlink"])
def test_snapshot_repair_exchange_crash_recovers_source_specific_transition(
    tmp_path: Path,
    snapshot_kind: str,
    crash_stage: str,
) -> None:
    run_dir = _run(tmp_path)
    claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    )
    state_path = run_dir / "state.json"
    if snapshot_kind == "invalid":
        state_path.write_bytes(b"{invalid snapshot")
    else:
        state_path.write_text(
            json.dumps(json.loads(state_path.read_bytes()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    crashed = False

    def crash_after_exchange(stage: str) -> None:
        nonlocal crashed
        if stage == crash_stage:
            crashed = True
            raise RuntimeError("injected snapshot repair crash")

    with pytest.raises(RuntimeError, match="injected snapshot repair crash"):
        claim_worker(
            run_dir,
            worker_id="worker-1",
            request_id=REQUEST_CLAIM,
            limit=1,
            now="2026-07-10T00:00:01Z",
            fault=crash_after_exchange,
        )
    assert crashed is True

    replayed = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    )

    assert replayed.replayed is True
    assert load_run_view(run_dir).snapshot_status == "current"


def test_unmarked_state_writing_is_cleaned_before_replay_snapshot_repair(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_writing_fsync", occurrence=2)
    with pytest.raises(BatchRuntimeError) as exc_info:
        load_run_view(run_dir)
    assert exc_info.value.code == "storage_recovery_required"

    replayed = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    )
    assert replayed.replayed is True
    assert load_run_view(run_dir).snapshot_status == "current"
    _assert_no_runtime_staging(run_dir)


def test_full_fsynced_writing_is_recovered_as_pending_and_replayed(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_writing_fsync", occurrence=1)
    diagnostic = load_run_view(run_dir)
    assert diagnostic.pending_event is not None
    assert diagnostic.incomplete_event_writes == ()
    assert len(diagnostic.events) == 1

    completed = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    )
    assert completed.replayed is True
    _assert_no_runtime_staging(run_dir)


def test_valid_unrelated_claim_retires_unparseable_next_event_write_before_commit(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_writing_fsync", occurrence=1)
    writing = next((run_dir / "events").glob("*.writing"))
    raw = writing.read_bytes()
    writing.write_bytes(raw[:200])
    writing_inode = writing.stat().st_ino

    claimed = claim_worker(
        run_dir,
        worker_id="worker-2",
        request_id="44444444-4444-4444-8444-444444444444",
        limit=1,
        now="2026-07-10T00:00:02Z",
    )

    assert claimed.replayed is False
    assert writing.stat().st_ino == writing_inode
    assert writing.read_bytes() == b""
    recovered = load_run_view(run_dir)
    assert recovered.snapshot_status == "current"
    assert len(recovered.events) == 2
    assert recovered.events[-1].request_id == "44444444-4444-4444-8444-444444444444"


def test_rejected_unrelated_claim_does_not_retire_unparseable_next_event_write(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_writing_fsync", occurrence=1)
    writing = next((run_dir / "events").glob("*.writing"))
    raw = writing.read_bytes()[:200]
    writing.write_bytes(raw)
    before = _tree_snapshot(run_dir / "events")
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.7\nsource drift after partial event write\n")

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker-2",
            request_id="44444444-4444-4444-8444-444444444444",
            limit=1,
            now="2026-07-10T00:00:02Z",
        )

    assert exc_info.value.code == "source_drift"
    assert _tree_snapshot(run_dir / "events") == before
    assert writing.read_bytes() == raw


def test_full_fsynced_writing_binds_request_fingerprint_before_cleanup(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_writing_fsync", occurrence=1)
    before = {path.name: path.read_bytes() for path in (run_dir / "events").iterdir()}

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="changed-worker",
            request_id=REQUEST_CLAIM,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )
    assert exc_info.value.code == "idempotency_conflict"
    assert {path.name: path.read_bytes() for path in (run_dir / "events").iterdir()} == before


def test_full_fsynced_writing_rejects_reused_claim_before_promote(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    first = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    release_worker(
        run_dir,
        first["item_id"],
        worker_id=first["worker_id"],
        claim_id=first["claim_id"],
        lease_token=first["lease_token"],
        attempt_id=first["attempt_id"],
        acknowledge_no_side_effects=True,
        request_id="44444444-4444-4444-8444-444444444444",
        now="2026-07-10T00:00:02Z",
    )
    _run_crashing_claim(
        run_dir,
        stage="after_writing_fsync",
        occurrence=1,
        request_id="55555555-5555-4555-8555-555555555555",
        worker_id="worker-2",
        now="2026-07-10T00:00:03Z",
    )
    writing = next((run_dir / "events").glob("*.writing"))
    payload = json.loads(writing.read_bytes())
    payload["data"]["assignments"][0]["claim_id"] = first["claim_id"]
    payload.pop("event_sha256")
    payload["event_sha256"] = canonical_sha256(payload)
    writing.write_bytes(canonical_json_bytes(payload))
    before = {path.name: path.read_bytes() for path in (run_dir / "events").iterdir()}

    with pytest.raises(BatchRuntimeError) as exc_info:
        load_run_view(run_dir)
    assert exc_info.value.code == "journal_corrupt"
    assert {path.name: path.read_bytes() for path in (run_dir / "events").iterdir()} == before
    assert not (run_dir / "events" / "00000000000000000004.json").exists()


def test_pending_same_request_conflict_is_zero_mutation(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_file_fsync", occurrence=1)
    events_before = {path.name: path.read_bytes() for path in (run_dir / "events").iterdir()}
    state_before = (run_dir / "state.json").read_bytes()

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="different-worker",
            request_id=REQUEST_CLAIM,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )
    assert exc_info.value.code == "idempotency_conflict"
    assert {path.name: path.read_bytes() for path in (run_dir / "events").iterdir()} == events_before
    assert (run_dir / "state.json").read_bytes() == state_before


def test_pending_next_event_plus_future_incomplete_writing_is_read_only_corruption(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_file_fsync", occurrence=1)
    future_writing = (
        run_dir
        / "events"
        / ".00000000000000000003.json.0123456789abcdef0123456789abcdef.writing"
    )
    future_writing.write_bytes(b'{"schema_version":')
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker-1",
            request_id=REQUEST_CLAIM,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )

    assert exc_info.value.code == "journal_corrupt"
    assert _tree_snapshot(run_dir) == before
    assert not (run_dir / "events" / "00000000000000000002.json").exists()


def test_unrelated_request_cannot_promote_pending_claim_without_its_source_guard(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_file_fsync", occurrence=1)
    pdf = run_dir.parent / "paper.pdf"
    original_pdf = pdf.read_bytes()
    pdf.write_bytes(b"%PDF-1.7\nsource drift after pending claim\n")
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker-2",
            request_id="44444444-4444-4444-8444-444444444444",
            limit=1,
            now="2026-07-10T00:00:02Z",
        )
    assert exc_info.value.code == "storage_recovery_required"
    assert _tree_snapshot(run_dir) == before
    recovered = load_run_view(run_dir)
    assert len(recovered.events) == 1
    assert recovered.pending_event is not None

    with pytest.raises(BatchRuntimeError) as origin_error:
        claim_worker(
            run_dir,
            worker_id="worker-1",
            request_id=REQUEST_CLAIM,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )
    assert origin_error.value.code == "source_drift"
    retired = load_run_view(run_dir)
    assert retired.pending_event is None
    assert len(retired.aborted_events) == 1
    assert retired.state.items[0].worker_status == "queued"

    pdf.write_bytes(original_pdf)
    replacement = claim_worker(
        run_dir,
        worker_id="worker-2",
        request_id="55555555-5555-4555-8555-555555555555",
        limit=1,
        now="2026-07-10T00:00:03Z",
    )
    assert replacement.result["assignments"][0]["item_id"] == "001"


def test_uncommitted_aborted_sidecar_is_never_request_identity(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_file_fsync", occurrence=1)
    pending = load_run_view(run_dir).pending_event
    assert pending is not None
    sidecar = pending.path.parent / (
        f".aborted.{pending.event.request_id}.{pending.event.event_sha256}.json"
    )
    pending.path.rename(sidecar)

    with pytest.raises(BatchRuntimeError) as exc_info:
        load_run_view(run_dir)
    assert exc_info.value.code == "journal_corrupt"
