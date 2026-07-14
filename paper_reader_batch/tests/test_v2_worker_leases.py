import multiprocessing
from contextlib import contextmanager
import json
from pathlib import Path

import pytest

import paper_reader_batch.v2_journal as journal_module
import paper_reader_batch.v2_worker as worker_module
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_contracts import (
    BatchEvent,
    BatchManifest,
    WorkerResult,
    ZoteroTitleManifestItem,
    ZoteroTitleSource,
)
from paper_reader_batch.v2_json import canonical_json_bytes, canonical_sha256, sha256_bytes
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_run import initialize_run, recover_run
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


def _mixed_run(tmp_path: Path) -> Path:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    pdfs: list[Path] = []
    for index in range(2):
        pdf = tmp_path / f"paper-{index}.pdf"
        pdf.write_bytes(f"%PDF-1.7\npaper {index}\n".encode())
        pdfs.append(pdf)
    paths = tmp_path / "paths.txt"
    paths.write_text("\n".join(str(pdf) for pdf in pdfs), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="mixed claim batch",
        output=manifest_path,
        request_id=REQUEST_MANIFEST,
        skill_root=skill_root,
        default_concurrency=4,
    )
    base = BatchManifest.model_validate_json(manifest_path.read_bytes())
    mixed = BatchManifest.model_validate(
        {
            **base.model_dump(mode="json"),
            "items": [
                *base.items,
                ZoteroTitleManifestItem(
                    item_id="003",
                    source=ZoteroTitleSource(title="First Zotero Paper"),
                ),
                ZoteroTitleManifestItem(
                    item_id="004",
                    source=ZoteroTitleSource(title="Second Zotero Paper"),
                ),
            ],
        }
    )
    manifest_path.write_bytes(canonical_json_bytes(mixed))
    run_dir = tmp_path / "run"
    initialize_run(
        manifest_path,
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


def test_implicit_transaction_clock_is_sampled_only_after_run_lock_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    real_locked_file = journal_module.locked_file
    lock_held = False

    @contextmanager
    def recording_locked_file(*args, **kwargs):
        nonlocal lock_held
        with real_locked_file(*args, **kwargs) as descriptor:
            lock_held = True
            try:
                yield descriptor
            finally:
                lock_held = False

    def locked_utc_now() -> str:
        assert lock_held, "implicit transaction time was sampled before the run lock"
        return "2026-07-10T00:00:01.000000Z"

    monkeypatch.setattr(journal_module, "locked_file", recording_locked_file)
    monkeypatch.setattr(journal_module, "utc_now", locked_utc_now)

    outcome = claim_worker(
        run_dir,
        worker_id="worker",
        request_id=REQUEST_CLAIM,
        lease_seconds=1,
    )

    assignment = outcome.result["assignments"][0]
    assert assignment["issued_at"] == "2026-07-10T00:00:01.000000Z"
    assert assignment["expires_at"] == "2026-07-10T00:00:02.000000Z"


def test_expired_fsynced_claim_is_aborted_and_does_not_block_new_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    advanced = False

    def current_time() -> str:
        return (
            "2026-07-10T00:00:02.000000Z"
            if advanced
            else "2026-07-10T00:00:01.000000Z"
        )

    def advance_after_event_fsync(stage: str) -> None:
        nonlocal advanced
        if stage == "after_writing_fsync":
            advanced = True

    monkeypatch.setattr(journal_module, "utc_now", current_time)

    with pytest.raises(BatchRuntimeError) as first_error:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
            fault=advance_after_event_fsync,
        )
    assert first_error.value.code == "lease_expired"
    view = load_run_view(run_dir)
    assert view.pending_event is None
    assert len(view.aborted_events) == 1
    assert view.state.items[0].worker_status == "queued"

    with pytest.raises(BatchRuntimeError) as replay_error:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
        )

    assert replay_error.value.code == "request_aborted"

    replacement = claim_worker(
        run_dir,
        worker_id="worker",
        request_id="44444444-4444-4444-8444-444444444444",
        lease_seconds=1,
    )
    assert replacement.result["assignments"][0]["item_id"] == "001"


def test_aborted_request_identity_survives_auxiliary_receipt_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    advanced = False

    def current_time() -> str:
        return (
            "2026-07-10T00:00:02.000000Z"
            if advanced
            else "2026-07-10T00:00:01.000000Z"
        )

    def advance_after_event_fsync(stage: str) -> None:
        nonlocal advanced
        if stage == "after_writing_fsync":
            advanced = True

    monkeypatch.setattr(journal_module, "utc_now", current_time)
    with pytest.raises(BatchRuntimeError) as first_error:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
            fault=advance_after_event_fsync,
        )
    assert first_error.value.code == "lease_expired"

    aborted = load_run_view(run_dir)
    assert any(event.data.kind == "request.aborted" for event in aborted.events)
    auxiliary_receipts = tuple((run_dir / "events").glob(".aborted.*.json"))
    for index, receipt in enumerate(auxiliary_receipts):
        receipt.rename(tmp_path / f"detached-aborted-receipt-{index}.json")

    with pytest.raises(BatchRuntimeError) as replay_error:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
        )
    assert replay_error.value.code == "request_aborted"

    with pytest.raises(BatchRuntimeError) as conflict_error:
        claim_worker(
            run_dir,
            worker_id="different-worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
        )
    assert conflict_error.value.code == "idempotency_conflict"


@pytest.mark.parametrize(
    "abort_fault_stage",
    ["after_writing_fsync", "after_pending_rename", "after_rename", "before_parent_fsync"],
)
def test_abort_marker_recovers_before_original_proposal_can_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    abort_fault_stage: str,
) -> None:
    run_dir = _run(tmp_path)
    advanced = False
    real_publish = journal_module.publish_bytes_no_replace

    def current_time() -> str:
        return (
            "2026-07-10T00:00:02.000000Z"
            if advanced
            else "2026-07-10T00:00:01.000000Z"
        )

    def advance_original_event(stage: str) -> None:
        nonlocal advanced
        if stage == "after_writing_fsync":
            advanced = True

    def publish_with_abort_crash(path: Path, data: bytes, **kwargs) -> None:
        payload = json.loads(data)
        if payload.get("data", {}).get("kind") != "request.aborted":
            real_publish(path, data, **kwargs)
            return

        def crash_abort(stage: str) -> None:
            if stage == abort_fault_stage:
                raise RuntimeError(f"abort marker crash at {stage}")

        kwargs["fault"] = crash_abort
        real_publish(path, data, **kwargs)

    monkeypatch.setattr(journal_module, "utc_now", current_time)
    monkeypatch.setattr(
        journal_module,
        "publish_bytes_no_replace",
        publish_with_abort_crash,
    )
    with pytest.raises(RuntimeError, match="abort marker crash"):
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
            fault=advance_original_event,
        )

    monkeypatch.setattr(journal_module, "publish_bytes_no_replace", real_publish)
    with pytest.raises(BatchRuntimeError) as replay_error:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
        )
    assert replay_error.value.code == "request_aborted"

    committed = load_run_view(run_dir)
    abort_markers = [
        event for event in committed.events if event.data.kind == "request.aborted"
    ]
    assert len(abort_markers) == 1
    assert not any(event.data.kind == "worker.claimed" for event in committed.events)
    assert committed.state.items[0].worker_status == "queued"

    residues = [
        residue
        for residue in sorted((run_dir / "events").iterdir())
        if (
            residue.name.startswith(".00000000000000000002.json.")
            and residue.name.endswith((".tmp", ".writing"))
        )
    ]
    assert residues
    assert all(residue.stat().st_size == 0 for residue in residues)
    for index, residue in enumerate(residues):
        residue.rename(tmp_path / f"detached-abort-residue-{abort_fault_stage}-{index}")

    with pytest.raises(BatchRuntimeError) as residue_free_replay:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
        )
    assert residue_free_replay.value.code == "request_aborted"

    replacement = claim_worker(
        run_dir,
        worker_id="worker",
        request_id="44444444-4444-4444-8444-444444444444",
        lease_seconds=1,
    )
    assert replacement.result["assignments"][0]["item_id"] == "001"
    final = load_run_view(run_dir)
    assert final.events[-1].previous_event_sha256 == abort_markers[0].event_sha256


def test_unrelated_source_drift_does_not_clear_committed_abort_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    advanced = False
    real_publish = journal_module.publish_bytes_no_replace

    def current_time() -> str:
        return (
            "2026-07-10T00:00:02.000000Z"
            if advanced
            else "2026-07-10T00:00:01.000000Z"
        )

    def advance_original_event(stage: str) -> None:
        nonlocal advanced
        if stage == "after_writing_fsync":
            advanced = True

    def publish_with_abort_crash(path: Path, data: bytes, **kwargs) -> None:
        payload = json.loads(data)
        if payload.get("data", {}).get("kind") != "request.aborted":
            real_publish(path, data, **kwargs)
            return

        def crash_after_marker_rename(stage: str) -> None:
            if stage == "after_rename":
                raise RuntimeError("abort marker crash after rename")

        kwargs["fault"] = crash_after_marker_rename
        real_publish(path, data, **kwargs)

    monkeypatch.setattr(journal_module, "utc_now", current_time)
    monkeypatch.setattr(
        journal_module,
        "publish_bytes_no_replace",
        publish_with_abort_crash,
    )
    with pytest.raises(RuntimeError, match="abort marker crash"):
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
            fault=advance_original_event,
        )

    monkeypatch.setattr(journal_module, "publish_bytes_no_replace", real_publish)
    residue = next(
        path
        for path in sorted((run_dir / "events").iterdir())
        if (
            path.name.startswith(".00000000000000000002.json.")
            and path.name.endswith((".tmp", ".writing"))
            and path.stat().st_size > 0
        )
    )
    residue_before = (residue.read_bytes(), residue.stat().st_mtime_ns)
    (tmp_path / "paper-0.pdf").write_bytes(b"%PDF-1.7\ndrifted source\n")

    with pytest.raises(BatchRuntimeError) as drift_error:
        claim_worker(
            run_dir,
            worker_id="different-worker",
            request_id="44444444-4444-4444-8444-444444444444",
            lease_seconds=1,
        )
    assert drift_error.value.code == "source_drift"
    assert residue.exists()
    assert (residue.read_bytes(), residue.stat().st_mtime_ns) == residue_before

    with pytest.raises(BatchRuntimeError) as direct_error:
        with journal_module.locked_run(run_dir):
            raise BatchRuntimeError("lease_expired", "direct caller rejected")
    assert direct_error.value.code == "lease_expired"
    assert (residue.read_bytes(), residue.stat().st_mtime_ns) == residue_before

    with pytest.raises(ValueError, match="requires both"):
        with journal_module.locked_run(
            run_dir,
            pre_recovery_validate=lambda _view: None,
            allow_unrelated_residue_cleanup=True,
        ):
            pass
    assert (residue.read_bytes(), residue.stat().st_mtime_ns) == residue_before

    with pytest.raises(BatchRuntimeError) as replay_error:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
        )
    assert replay_error.value.code == "request_aborted"
    assert residue.stat().st_size == 0


def test_exact_aborted_replay_retires_only_its_bound_residues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    advanced = False
    real_publish = journal_module.publish_bytes_no_replace

    def current_time() -> str:
        return (
            "2026-07-10T00:00:02.000000Z"
            if advanced
            else "2026-07-10T00:00:01.000000Z"
        )

    def advance_original_event(stage: str) -> None:
        nonlocal advanced
        if stage == "after_writing_fsync":
            advanced = True

    def publish_with_abort_crash(path: Path, data: bytes, **kwargs) -> None:
        payload = json.loads(data)
        if payload.get("data", {}).get("kind") != "request.aborted":
            real_publish(path, data, **kwargs)
            return

        def crash_after_marker_rename(stage: str) -> None:
            if stage == "after_rename":
                raise RuntimeError("abort marker crash after rename")

        kwargs["fault"] = crash_after_marker_rename
        real_publish(path, data, **kwargs)

    monkeypatch.setattr(journal_module, "utc_now", current_time)
    monkeypatch.setattr(
        journal_module,
        "publish_bytes_no_replace",
        publish_with_abort_crash,
    )
    with pytest.raises(RuntimeError, match="abort marker crash"):
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
            fault=advance_original_event,
        )
    monkeypatch.setattr(journal_module, "publish_bytes_no_replace", real_publish)

    first_residues = tuple(
        path
        for path in sorted((run_dir / "events").iterdir())
        if path.name.startswith(".00000000000000000002.json.")
        and path.name.endswith((".tmp", ".writing"))
        and path.stat().st_size > 0
    )
    assert first_residues
    first_proposal = BatchEvent.model_validate_json(first_residues[0].read_bytes())
    first_marker = load_run_view(run_dir).events[-1]

    second_request_id = "44444444-4444-4444-8444-444444444444"
    second_payload = first_proposal.model_dump(mode="json")
    second_payload.update(
        {
            "sequence": 3,
            "event_id": "55555555-5555-4555-8555-555555555555",
            "request_id": second_request_id,
            "previous_event_sha256": first_marker.event_sha256,
        }
    )
    second_payload["command_result"]["request_id"] = second_request_id
    second_payload.pop("event_sha256")
    second_payload["event_sha256"] = canonical_sha256(second_payload)
    second_proposal = BatchEvent.model_validate(second_payload)
    second_marker = journal_module._aborted_marker(second_proposal)
    second_marker_path = run_dir / "events" / "00000000000000000003.json"
    second_marker_path.write_bytes(canonical_json_bytes(second_marker))
    second_marker_path.chmod(0o600)
    second_raw = canonical_json_bytes(second_proposal)
    second_residue = (
        run_dir
        / "events"
        / f".00000000000000000003.json.{sha256_bytes(second_raw)}.tmp"
    )
    second_residue.write_bytes(second_raw)
    second_residue.chmod(0o600)

    bound = load_run_view(run_dir).aborted_residues
    assert {residue.proposed_event_sha256 for residue in bound} == {
        first_proposal.event_sha256,
        second_proposal.event_sha256,
    }
    second_before = (second_residue.read_bytes(), second_residue.stat().st_mtime_ns)

    with pytest.raises(BatchRuntimeError) as first_replay:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
        )
    assert first_replay.value.code == "request_aborted"
    assert all(path.stat().st_size == 0 for path in first_residues)
    assert (second_residue.read_bytes(), second_residue.stat().st_mtime_ns) == second_before

    with pytest.raises(BatchRuntimeError) as second_replay:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=second_request_id,
            lease_seconds=1,
        )
    assert second_replay.value.code == "request_aborted"
    assert second_residue.stat().st_size == 0


def test_claim_rejects_oversized_derived_abort_marker_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    real_aborted_marker = journal_module._aborted_marker
    limit = 4096

    def oversized_aborted_marker(event):
        marker = real_aborted_marker(event)
        assert marker.data.kind == "request.aborted"
        oversized_data = marker.data.model_copy(
            update={
                "proposed_event_canonical_json": (
                    marker.data.proposed_event_canonical_json + ("x" * limit)
                )
            }
        )
        return marker.model_copy(update={"data": oversized_data})

    monkeypatch.setattr(journal_module, "MAX_JSON_ARTIFACT_BYTES", limit)
    monkeypatch.setattr(journal_module, "_aborted_marker", oversized_aborted_marker)

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            now="2026-07-10T00:00:01Z",
        )
    assert exc_info.value.code == "resource_limit"

    view = load_run_view(run_dir)
    assert len(view.events) == 1
    assert view.pending_event is None
    assert view.state.items[0].worker_status == "queued"
    assert not any(
        name.startswith(".00000000000000000002.json.")
        for name in (path.name for path in (run_dir / "events").iterdir())
    )


@pytest.mark.parametrize("tamper_kind", ["command_mismatch", "illegal_prefix"])
def test_committed_abort_marker_rejects_invalid_embedded_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper_kind: str,
) -> None:
    run_dir = _run(tmp_path)
    advanced = False

    def current_time() -> str:
        return (
            "2026-07-10T00:00:02.000000Z"
            if advanced
            else "2026-07-10T00:00:01.000000Z"
        )

    def advance_after_event_fsync(stage: str) -> None:
        nonlocal advanced
        if stage == "after_writing_fsync":
            advanced = True

    monkeypatch.setattr(journal_module, "utc_now", current_time)
    with pytest.raises(BatchRuntimeError) as first_error:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            lease_seconds=1,
            fault=advance_after_event_fsync,
        )
    assert first_error.value.code == "lease_expired"

    view = load_run_view(run_dir)
    proposed = journal_module._proposal_from_aborted_marker(view.events[-1])
    payload = proposed.model_dump(mode="json")
    payload.pop("event_sha256")
    if tamper_kind == "command_mismatch":
        payload["command"] = "worker.finish"
        payload["command_result"]["command"] = "worker.finish"
    else:
        payload["data"] = view.events[0].data.model_dump(mode="json")
        payload["command"] = "run.init"
        payload["command_result"]["command"] = "run.init"
    tampered = type(proposed)(
        **payload,
        event_sha256=canonical_sha256(payload),
    )
    (run_dir / "events" / "00000000000000000002.json").write_bytes(
        canonical_json_bytes(journal_module._aborted_marker(tampered))
    )

    with pytest.raises(BatchRuntimeError) as corrupt_error:
        load_run_view(run_dir)
    assert corrupt_error.value.code == "journal_corrupt"


def test_pending_abort_marker_rejects_embedded_committed_request_identity(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)

    def crash_after_proposal_fsync(stage: str) -> None:
        if stage == "after_writing_fsync":
            raise RuntimeError("proposal staged")

    with pytest.raises(RuntimeError, match="proposal staged"):
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            now="2026-07-10T00:00:01Z",
            fault=crash_after_proposal_fsync,
        )
    pending = load_run_view(run_dir).pending_event
    assert pending is not None
    pending.path.rename(tmp_path / "detached-proposal.json")

    payload = pending.event.model_dump(mode="json")
    payload.pop("event_sha256")
    payload["request_id"] = REQUEST_INIT
    payload["command_result"]["request_id"] = REQUEST_INIT
    colliding = type(pending.event)(
        **payload,
        event_sha256=canonical_sha256(payload),
    )
    marker = journal_module._aborted_marker(colliding)

    def crash_after_marker_fsync(stage: str) -> None:
        if stage == "after_writing_fsync":
            raise RuntimeError("marker staged")

    with pytest.raises(RuntimeError, match="marker staged"):
        journal_module.publish_bytes_no_replace(
            run_dir / "events" / "00000000000000000002.json",
            canonical_json_bytes(marker),
            fault=crash_after_marker_fsync,
        )

    with pytest.raises(BatchRuntimeError) as corrupt_error:
        load_run_view(run_dir)
    assert corrupt_error.value.code == "journal_corrupt"


def test_postrename_clock_advance_cannot_reverse_committed_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    advanced = False

    def current_time() -> str:
        return (
            "2026-07-10T00:00:02.000000Z"
            if advanced
            else "2026-07-10T00:00:01.000000Z"
        )

    def advance_after_commit(stage: str) -> None:
        nonlocal advanced
        if stage == "after_rename":
            advanced = True

    monkeypatch.setattr(journal_module, "utc_now", current_time)

    outcome = claim_worker(
        run_dir,
        worker_id="worker",
        request_id=REQUEST_CLAIM,
        lease_seconds=1,
        fault=advance_after_commit,
    )
    assert outcome.replayed is False
    committed = load_run_view(run_dir)
    assert committed.pending_event is None
    assert len(committed.events) == 2
    assert committed.state.items[0].worker_status == "claimed"

    assert outcome.result["assignments"][0]["expires_at"] == "2026-07-10T00:00:02.000000Z"

    recover_run(
        run_dir,
        request_id="44444444-4444-4444-8444-444444444444",
        now="2026-07-10T00:00:02Z",
    )
    assert load_run_view(run_dir).state.items[0].worker_status == "queued"


@pytest.mark.parametrize(
    ("fault_stage", "expected_aborted"),
    [("after_commit_validation", 1), ("after_writing_fsync", 1)],
)
def test_worker_claim_rebinds_pdf_at_final_event_precommit(
    tmp_path: Path,
    fault_stage: str,
    expected_aborted: int,
) -> None:
    run_dir = _run(tmp_path)
    pdf = run_dir.parent / "paper-0.pdf"
    mutated = False

    def drift_source(stage: str) -> None:
        nonlocal mutated
        if stage == fault_stage and not mutated:
            mutated = True
            pdf.write_bytes(b"%PDF-1.7\nchanged during claim commit\n")

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id=REQUEST_CLAIM,
            fault=drift_source,
            now="2026-07-10T00:00:01Z",
        )

    assert exc_info.value.code == "source_drift"
    assert mutated is True
    view = load_run_view(run_dir)
    assert len(view.events) == 2
    assert view.events[-1].data.kind == "request.aborted"
    assert view.pending_event is None
    assert len(view.aborted_events) == expected_aborted
    assert view.state.items[0].worker_status == "queued"


def test_worker_claim_event_limits_pdf_assignments_but_fills_with_non_pdf_items(
    tmp_path: Path,
) -> None:
    run_dir = _mixed_run(tmp_path)

    outcome = claim_worker(
        run_dir,
        worker_id="worker",
        request_id=REQUEST_CLAIM,
        limit=3,
        now="2026-07-10T00:00:01Z",
    )

    assignments = outcome.result["assignments"]
    assert [assignment["item_id"] for assignment in assignments] == ["001", "003", "004"]
    assert sum(assignment["source"]["source_type"] == "pdf_path" for assignment in assignments) == 1


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


def test_prompt_rebinds_pdf_after_prepared_artifact_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    assignment = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    pdf = Path(load_run_view(run_dir).manifest.items[0].source.path)
    original_loader = worker_module._load_prepared_local_result
    mutated = False

    def drift_while_materializing(*args, **kwargs):
        nonlocal mutated
        result = original_loader(*args, **kwargs)
        if not mutated:
            mutated = True
            pdf.write_bytes(b"%PDF-1.7\ndrift during prompt materialization\n")
        return result

    monkeypatch.setattr(
        worker_module,
        "_load_prepared_local_result",
        drift_while_materializing,
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        worker_prompt(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            now="2026-07-10T00:00:02Z",
        )

    assert exc_info.value.code == "source_drift"
    assert mutated is True


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
    first = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_CLAIM,
        limit=2,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    second = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id="38383838-3838-4838-8838-383838383838",
        limit=2,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
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
    assert {
        "local_prepare_result_sha256",
        "paper_reader_run",
        "evidence",
    }.isdisjoint(prompt)
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


def test_failed_worker_finish_requires_explicit_retry_and_binds_next_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    advanced = False

    def current_time() -> str:
        return (
            "2026-07-10T00:00:06.000000Z"
            if advanced
            else "2026-07-10T00:00:05.000000Z"
        )

    def expire_after_event_fsync(stage: str) -> None:
        nonlocal advanced
        if stage == "after_writing_fsync":
            advanced = True

    monkeypatch.setattr(journal_module, "utc_now", current_time)
    with pytest.raises(BatchRuntimeError) as aborted_error:
        claim_worker(
            run_dir,
            worker_id="worker-2",
            request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            lease_seconds=1,
            fault=expire_after_event_fsync,
        )
    assert aborted_error.value.code == "lease_expired"
    assert load_run_view(run_dir).state.items[0].worker_status == "queued"

    claimed_again = claim_worker(
        run_dir,
        worker_id="worker-2",
        request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    )
    assert claimed_again.result["assignments"][0]["attempt_id"] == next_attempt_id
    assert claimed_again.result["assignments"][0]["attempt_number"] == 2
