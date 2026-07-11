import multiprocessing
from pathlib import Path

import pytest

from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_contracts import WorkerResult
from paper_reader_batch.v2_json import canonical_json_bytes, sha256_bytes
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_run import initialize_run
from paper_reader_batch.v2_worker import (
    claim_worker,
    finish_worker,
    release_worker,
    renew_worker,
    retry_worker,
    worker_prompt,
)


REQUEST_MANIFEST = "11111111-1111-4111-8111-111111111111"
REQUEST_INIT = "22222222-2222-4222-8222-222222222222"
REQUEST_CLAIM = "33333333-3333-4333-8333-333333333333"


def _run(tmp_path: Path, *, pdf_count: int = 1, default_concurrency: int = 3) -> Path:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    pdfs: list[Path] = []
    for index in range(pdf_count):
        pdf = tmp_path / f"paper-{index}.pdf"
        pdf.write_bytes(f"%PDF-1.7\npaper {index}\n".encode())
        pdfs.append(pdf)
    paths = tmp_path / "paths.txt"
    paths.write_text("\n".join(str(pdf) for pdf in pdfs), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="lease batch",
        output=manifest,
        request_id=REQUEST_MANIFEST,
        skill_root=skill_root,
        default_concurrency=default_concurrency,
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


def _claim_process(barrier, queue, run_dir: str) -> None:
    barrier.wait()
    try:
        outcome = claim_worker(
            Path(run_dir),
            worker_id="worker-一",
            request_id=REQUEST_CLAIM,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )
    except Exception as exc:
        queue.put(("error", getattr(exc, "code", type(exc).__name__), str(exc)))
    else:
        assignment = outcome.result["assignments"][0]
        queue.put(("ok", outcome.replayed, assignment["lease_token"], assignment["claim_id"]))


def _independent_claim_process(barrier, queue, run_dir: str, worker_id: str, request_id: str) -> None:
    barrier.wait()
    try:
        outcome = claim_worker(
            Path(run_dir),
            worker_id=worker_id,
            request_id=request_id,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )
    except Exception as exc:
        queue.put(("error", getattr(exc, "code", type(exc).__name__)))
    else:
        assignment = outcome.result["assignments"][0]
        queue.put(("ok", assignment["worker_id"], assignment["item_id"]))


def test_same_request_cross_process_claim_replays_exact_token_and_one_event(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [context.Process(target=_claim_process, args=(barrier, queue, str(run_dir))) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    outcomes = [queue.get(timeout=2) for _ in processes]
    assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
    assert sorted(outcome[1] for outcome in outcomes) == [False, True]
    assert len({outcome[2] for outcome in outcomes}) == 1
    assert len({outcome[3] for outcome in outcomes}) == 1
    assert len(outcomes[0][2]) >= 32
    assert sorted(path.name for path in (run_dir / "events").iterdir()) == [
        "00000000000000000001.json",
        "00000000000000000002.json",
    ]


def test_independent_cross_process_claims_cannot_exceed_manifest_capacity(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, pdf_count=2, default_concurrency=1)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    identities = [
        ("worker-a", "44444444-4444-4444-8444-444444444444"),
        ("worker-b", "55555555-5555-4555-8555-555555555555"),
    ]
    processes = [
        context.Process(
            target=_independent_claim_process,
            args=(barrier, queue, str(run_dir), worker_id, request_id),
        )
        for worker_id, request_id in identities
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    outcomes = [queue.get(timeout=2) for _ in processes]
    assert sum(outcome[0] == "ok" for outcome in outcomes) == 1, outcomes
    assert [outcome[1] for outcome in outcomes if outcome[0] == "error"] == ["no_available_work"]
    view = load_run_view(run_dir)
    assert sum(item.worker_status == "claimed" for item in view.state.items) == 1
    assert sorted(path.name for path in (run_dir / "events").iterdir()) == [
        "00000000000000000001.json",
        "00000000000000000002.json",
    ]


def test_claim_rejects_pdf_replacement_without_journal_or_snapshot_mutation(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    view = load_run_view(run_dir)
    pdf = Path(view.manifest.items[0].source.path)
    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(b"%PDF-1.7\nreplacement bytes\n")
    replacement.replace(pdf)
    events_before = sorted((run_dir / "events").iterdir())
    state_before = (run_dir / "state.json").read_bytes()

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker-1",
            request_id=REQUEST_CLAIM,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )
    assert exc_info.value.code == "source_drift"
    assert sorted((run_dir / "events").iterdir()) == events_before
    assert (run_dir / "state.json").read_bytes() == state_before


def test_prompt_and_renew_reject_pdf_drift_without_mutation(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    assignment = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    view = load_run_view(run_dir)
    pdf = Path(view.manifest.items[0].source.path)
    pdf.write_bytes(b"%PDF-1.7\nchanged after claim\n")
    events_before = sorted((run_dir / "events").iterdir())
    state_before = (run_dir / "state.json").read_bytes()
    identity = {
        "worker_id": assignment["worker_id"],
        "claim_id": assignment["claim_id"],
        "lease_token": assignment["lease_token"],
        "attempt_id": assignment["attempt_id"],
    }

    with pytest.raises(BatchRuntimeError) as exc_info:
        worker_prompt(run_dir, assignment["item_id"], now="2026-07-10T00:00:02Z", **identity)
    assert exc_info.value.code == "source_drift"
    with pytest.raises(BatchRuntimeError) as exc_info:
        renew_worker(
            run_dir,
            assignment["item_id"],
            request_id="66666666-6666-4666-8666-666666666666",
            now="2026-07-10T00:00:02Z",
            **identity,
        )
    assert exc_info.value.code == "source_drift"
    assert sorted((run_dir / "events").iterdir()) == events_before
    assert (run_dir / "state.json").read_bytes() == state_before


@pytest.mark.parametrize("tamper", ["changed", "missing"])
def test_missing_or_tampered_lease_secret_fails_before_journal_or_snapshot_mutation(
    tmp_path: Path,
    tamper: str,
) -> None:
    run_dir = _run(tmp_path)
    lock_path = run_dir / ".run.lock"
    if tamper == "changed":
        lock_path.write_bytes(b"x" * 32)
    else:
        lock_path.rename(run_dir / ".run.lock.missing")
    event_before = sorted((run_dir / "events").iterdir())
    state_before = (run_dir / "state.json").read_bytes()

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker-1",
            request_id=REQUEST_CLAIM,
            limit=1,
            now="2026-07-10T00:00:01Z",
        )
    assert exc_info.value.code in {"lease_secret_mismatch", "lease_secret_missing"}
    assert sorted((run_dir / "events").iterdir()) == event_before
    assert (run_dir / "state.json").read_bytes() == state_before
    if tamper == "missing":
        assert not lock_path.exists()


def test_worker_prompt_is_read_only_and_renew_release_require_exact_live_identity(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, pdf_count=2)
    claim = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=2,
        now="2026-07-10T00:00:01Z",
    )
    first, second = claim.result["assignments"]
    state_before_prompt = (run_dir / "state.json").read_bytes()
    events_before_prompt = sorted((run_dir / "events").iterdir())

    prompt = worker_prompt(
        run_dir,
        first["item_id"],
        worker_id=first["worker_id"],
        claim_id=first["claim_id"],
        lease_token=first["lease_token"],
        attempt_id=first["attempt_id"],
        now="2026-07-10T00:00:02Z",
    )
    assert "paper_reader_batch.worker-result.v2" in prompt["instruction"]
    assert "local-output only" in prompt["instruction"]
    assert (run_dir / "state.json").read_bytes() == state_before_prompt
    assert sorted((run_dir / "events").iterdir()) == events_before_prompt

    for wrong in [
        {"lease_token": "x" * 43},
        {"worker_id": "other-worker"},
        {"item_id": second["item_id"]},
        {"attempt_id": second["attempt_id"]},
    ]:
        arguments = {
            "item_id": first["item_id"],
            "worker_id": first["worker_id"],
            "claim_id": first["claim_id"],
            "lease_token": first["lease_token"],
            "attempt_id": first["attempt_id"],
            **wrong,
        }
        with pytest.raises(BatchRuntimeError):
            worker_prompt(run_dir, arguments.pop("item_id"), now="2026-07-10T00:00:02Z", **arguments)
    assert sorted((run_dir / "events").iterdir()) == events_before_prompt

    renewed = renew_worker(
        run_dir,
        first["item_id"],
        worker_id=first["worker_id"],
        claim_id=first["claim_id"],
        lease_token=first["lease_token"],
        attempt_id=first["attempt_id"],
        request_id="44444444-4444-4444-8444-444444444444",
        now="2026-07-10T00:00:11Z",
    )
    assert renewed.result["expires_at"] == "2026-07-10T00:15:11.000000Z"
    events_after_renew = sorted((run_dir / "events").iterdir())

    with pytest.raises(BatchRuntimeError) as exc_info:
        renew_worker(
            run_dir,
            first["item_id"],
            worker_id=first["worker_id"],
            claim_id=first["claim_id"],
            lease_token="x" * 43,
            attempt_id=first["attempt_id"],
            request_id="55555555-5555-4555-8555-555555555555",
            now="2026-07-10T00:00:12Z",
        )
    assert exc_info.value.code == "lease_identity_mismatch"
    assert sorted((run_dir / "events").iterdir()) == events_after_renew

    with pytest.raises(BatchRuntimeError) as exc_info:
        release_worker(
            run_dir,
            first["item_id"],
            worker_id=first["worker_id"],
            claim_id=first["claim_id"],
            lease_token=first["lease_token"],
            attempt_id=first["attempt_id"],
            acknowledge_no_side_effects=False,
            request_id="66666666-6666-4666-8666-666666666666",
            now="2026-07-10T00:00:12Z",
        )
    assert exc_info.value.code == "acknowledgement_required"
    assert sorted((run_dir / "events").iterdir()) == events_after_renew

    released = release_worker(
        run_dir,
        first["item_id"],
        worker_id=first["worker_id"],
        claim_id=first["claim_id"],
        lease_token=first["lease_token"],
        attempt_id=first["attempt_id"],
        acknowledge_no_side_effects=True,
        request_id="77777777-7777-4777-8777-777777777777",
        now="2026-07-10T00:00:12Z",
    )
    assert released.result["status"] == "queued"


def test_worker_renew_rejects_expired_lease_without_mutation(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    claim = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        lease_seconds=1,
        now="2026-07-10T00:00:01Z",
    )
    assignment = claim.result["assignments"][0]
    before = sorted((run_dir / "events").iterdir())
    with pytest.raises(BatchRuntimeError) as exc_info:
        renew_worker(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            request_id="88888888-8888-4888-8888-888888888888",
            now="2026-07-10T00:00:02Z",
        )
    assert exc_info.value.code == "lease_expired"
    assert sorted((run_dir / "events").iterdir()) == before


def test_failed_worker_finish_requires_explicit_retry_and_binds_next_attempt(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    claim = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    )
    assignment = claim.result["assignments"][0]
    view = load_run_view(run_dir)
    manifest_item = view.manifest.items[0]
    result = WorkerResult(
        schema_version="paper_reader_batch.worker-result.v2",
        manifest_sha256=view.manifest_sha256,
        item_id=assignment["item_id"],
        worker_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        attempt_id=assignment["attempt_id"],
        attempt_number=assignment["attempt_number"],
        lease_token_sha256=sha256_bytes(assignment["lease_token"].encode()),
        status="failed",
        source=manifest_item.source,
        error={"code": "reader_failed", "message": "deterministic failure"},
    )
    result_path = tmp_path / "worker-result.json"
    result_path.write_bytes(canonical_json_bytes(result))

    finished = finish_worker(
        run_dir,
        assignment["item_id"],
        worker_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        lease_token=assignment["lease_token"],
        attempt_id=assignment["attempt_id"],
        result_path=result_path,
        request_id="99999999-9999-4999-8999-999999999999",
        now="2026-07-10T00:00:02Z",
    )
    assert finished.result["status"] == "failed"
    failed_state = load_run_view(run_dir).state.items[0]
    assert failed_state.worker_failure_code == "reader_failed"
    assert failed_state.worker_status == "failed"

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker-2",
            request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            now="2026-07-10T00:00:03Z",
        )
    assert exc_info.value.code == "no_available_work"

    retried = retry_worker(
        run_dir,
        assignment["item_id"],
        request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        now="2026-07-10T00:00:04Z",
    )
    assert retried.result["previous_attempt_id"] == assignment["attempt_id"]
    next_attempt_id = retried.result["next_attempt_id"]
    claimed_again = claim_worker(
        run_dir,
        worker_id="worker-2",
        request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        now="2026-07-10T00:00:05Z",
    )
    assert claimed_again.result["assignments"][0]["attempt_id"] == next_attempt_id
    assert claimed_again.result["assignments"][0]["attempt_number"] == 2
