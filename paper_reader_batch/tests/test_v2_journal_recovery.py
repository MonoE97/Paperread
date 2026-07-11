import multiprocessing
import os
from pathlib import Path
import json
import hashlib

import pytest

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
    before = load_run_view(run_dir)
    assert len(before.events) == 2
    assert before.state_pending_write is not None
    assert before.snapshot_status == "stale"

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


def test_unmarked_state_writing_is_cleaned_before_replay_snapshot_repair(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_writing_fsync", occurrence=2)
    before = load_run_view(run_dir)
    assert len(before.events) == 2
    assert len(before.incomplete_state_writes) == 1
    assert before.snapshot_status == "stale"

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


def test_different_request_promotes_prior_event_even_when_new_transition_has_no_work(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _run_crashing_claim(run_dir, stage="after_file_fsync", occurrence=1)

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker-2",
            request_id="44444444-4444-4444-8444-444444444444",
            limit=1,
            now="2026-07-10T00:00:02Z",
        )
    assert exc_info.value.code == "no_available_work"
    recovered = load_run_view(run_dir)
    assert len(recovered.events) == 2
    assert recovered.state.items[0].worker_status == "claimed"
    assert recovered.snapshot_status == "current"
    _assert_no_runtime_staging(run_dir)
