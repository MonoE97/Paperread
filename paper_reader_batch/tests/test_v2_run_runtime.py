from pathlib import Path

import pytest

from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_local_prepare import claim_local_prepare
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_run import initialize_run, recover_run, run_status, validate_run
from paper_reader_batch.v2_worker import claim_worker, release_worker


def _run(tmp_path: Path) -> tuple[Path, Path]:
    skill = tmp_path / "skill"
    skill.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nrun runtime\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="run runtime",
        output=manifest,
        request_id="11111111-1111-4111-8111-111111111111",
        skill_root=skill,
    )
    run_dir = tmp_path / "run"
    initialize_run(
        manifest,
        request_id="22222222-2222-4222-8222-222222222222",
        skill_root=skill,
        output=run_dir,
        initialized_at="2026-07-10T00:00:00Z",
    )
    return run_dir, pdf


def test_run_status_replays_without_mutating_stale_snapshot_and_recover_repairs_it(tmp_path: Path) -> None:
    run_dir, _pdf = _run(tmp_path)
    initial_state = (run_dir / "state.json").read_bytes()
    claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-10T00:00:01Z",
    )
    (run_dir / "state.json").write_bytes(initial_state)
    stale_bytes = (run_dir / "state.json").read_bytes()
    status = run_status(run_dir)
    assert status["snapshot_status"] == "stale"
    assert status["state"]["items"][0]["worker_status"] == "claimed"
    assert (run_dir / "state.json").read_bytes() == stale_bytes

    recovered = recover_run(
        run_dir,
        request_id="44444444-4444-4444-8444-444444444444",
        now="2026-07-10T00:00:02Z",
    )
    assert recovered.result["snapshot_repaired"] is True
    assert recovered.result["expired_worker_items"] == []
    assert load_run_view(run_dir).snapshot_status == "current"


def test_run_recover_noop_is_structured_zero_mutation(tmp_path: Path) -> None:
    run_dir, _pdf = _run(tmp_path)
    events_before = sorted(path.name for path in (run_dir / "events").iterdir())
    state_before = (run_dir / "state.json").read_bytes()
    with pytest.raises(BatchRuntimeError) as exc_info:
        recover_run(
            run_dir,
            request_id="33333333-3333-4333-8333-333333333333",
            now="2026-07-10T00:00:01Z",
        )
    assert exc_info.value.code == "nothing_to_recover"
    assert sorted(path.name for path in (run_dir / "events").iterdir()) == events_before
    assert (run_dir / "state.json").read_bytes() == state_before


def test_recover_expired_worker_lease_uses_exact_journal_identity(tmp_path: Path) -> None:
    run_dir, _pdf = _run(tmp_path)
    claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        lease_seconds=1,
        now="2026-07-10T00:00:01Z",
    )
    recovered = recover_run(
        run_dir,
        request_id="44444444-4444-4444-8444-444444444444",
        now="2026-07-10T00:00:02Z",
    )
    assert recovered.result["expired_worker_items"] == ["001"]
    view = load_run_view(run_dir)
    assert view.state.items[0].worker_status == "queued"
    assert view.state.items[0].worker_lease is None


def test_recover_expired_local_lease_without_execution_requeues_exact_attempt(tmp_path: Path) -> None:
    run_dir, _pdf = _run(tmp_path)
    claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="33333333-3333-4333-8333-333333333333",
        lease_seconds=1,
        now="2026-07-10T00:00:01Z",
    )

    recovered = recover_run(
        run_dir,
        request_id="44444444-4444-4444-8444-444444444444",
        now="2026-07-10T00:00:02Z",
    )

    assert recovered.result["expired_local_prepare_items"] == ["001"]
    assert recovered.result["resumed_local_prepare_items"] == []
    item = load_run_view(run_dir).state.items[0]
    assert item.local_prepare_status == "queued"
    assert item.local_prepare_lease is None


def test_recover_expired_started_local_attempt_renews_same_identity_and_never_requeues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _pdf = _run(tmp_path)
    assignment = claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="33333333-3333-4333-8333-333333333333",
        lease_seconds=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    monkeypatch.setattr(
        "paper_reader_batch.v2_local_prepare.local_prepare_attempt_has_execution_side_effects",
        lambda view, *, item_id, claim_id, attempt_id: True,
    )

    recovered = recover_run(
        run_dir,
        request_id="44444444-4444-4444-8444-444444444444",
        now="2026-07-10T00:00:02Z",
    )

    assert recovered.result["expired_local_prepare_items"] == []
    assert recovered.result["resumed_local_prepare_items"] == ["001"]
    item = load_run_view(run_dir).state.items[0]
    assert item.local_prepare_status == "claimed"
    assert item.local_prepare_lease is not None
    assert item.local_prepare_lease.claim_id == assignment["claim_id"]
    assert item.local_prepare_lease.attempt_id == assignment["attempt_id"]
    assert item.local_prepare_lease.expires_at == "2026-07-10T00:15:02.000000Z"
    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_local_prepare(
            run_dir,
            worker_id="attempt-2",
            request_id="55555555-5555-4555-8555-555555555555",
            now="2026-07-10T00:00:03Z",
        )
    assert exc_info.value.code == "no_available_work"

    replayed = recover_run(
        run_dir,
        request_id="44444444-4444-4444-8444-444444444444",
        now="2026-07-10T00:00:02Z",
    )
    assert replayed.replayed is True
    assert replayed.result == recovered.result


def test_explicit_time_cannot_move_journal_backwards(tmp_path: Path) -> None:
    run_dir, _pdf = _run(tmp_path)
    assignment = claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-10T00:00:10Z",
    ).result["assignments"][0]
    before = sorted(path.name for path in (run_dir / "events").iterdir())
    with pytest.raises(BatchRuntimeError) as exc_info:
        release_worker(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            acknowledge_no_side_effects=True,
            request_id="44444444-4444-4444-8444-444444444444",
            now="2026-07-10T00:00:09Z",
        )
    assert exc_info.value.code == "nonmonotonic_time"
    assert sorted(path.name for path in (run_dir / "events").iterdir()) == before


def test_run_validate_checks_current_source_but_status_keeps_history_readable(tmp_path: Path) -> None:
    run_dir, pdf = _run(tmp_path)
    assert validate_run(run_dir)["valid"] is True
    pdf.write_bytes(b"%PDF-1.7\ndrifted\n")
    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_run(run_dir)
    assert exc_info.value.code == "source_drift"
    assert run_status(run_dir)["state"]["batch_status"] == "ready"
