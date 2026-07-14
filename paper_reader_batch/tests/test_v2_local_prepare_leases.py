from pathlib import Path

import pytest

import paper_reader_batch.v2_local_prepare as local_prepare_module
from paper_reader_batch.v2_artifacts import paper_reader_root_identity
from paper_reader_batch.v2_contracts import LocalPrepareResult
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_json import canonical_json_bytes, sha256_bytes
from paper_reader_batch.v2_local_prepare import (
    _ChildProtocolError,
    claim_local_prepare,
    finish_local_prepare,
    release_local_prepare,
    renew_local_prepare,
    run_local_prepare,
)
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_run import initialize_run, recover_run
from paper_reader_batch.v2_worker import claim_worker, release_worker


def _run(tmp_path: Path, *, pdf_count: int = 1) -> Path:
    skill = tmp_path / "batch-skill"
    skill.mkdir()
    pdfs: list[Path] = []
    for index in range(pdf_count):
        pdf = tmp_path / f"paper-{index}.pdf"
        pdf.write_bytes(f"%PDF-1.7\nlocal prepare {index}\n".encode())
        pdfs.append(pdf)
    paths = tmp_path / "paths.txt"
    paths.write_text("\n".join(str(pdf) for pdf in pdfs), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="local prepare",
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
    return run_dir


def _fake_paper_reader_root(tmp_path: Path) -> Path:
    root = tmp_path / "paper-reader"
    (root / "src" / "paper_reader").mkdir(parents=True)
    (root / "references" / "schemas").mkdir(parents=True)
    (root / "SKILL.md").write_text("# paper_reader V2\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname="paper_reader"\nversion="2.0.0"\n[project.scripts]\n'
        'paper_reader="paper_reader.public_cli:app"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "src" / "paper_reader" / "public_cli.py").write_text("app = object()\n", encoding="utf-8")
    for name in [
        "paper_reader.run.v2.schema.json",
        "paper_reader.command-result.v2.schema.json",
        "paper_reader.review-package.v2.schema.json",
        "paper_reader.candidate.v2.schema.json",
    ]:
        (root / "references" / "schemas" / name).write_text("{}\n", encoding="utf-8")
    return root


def _durably_uncertain_runner(_argv, _cwd, _timeout_seconds, invocation):
    invocation.mark_started()
    raise _ChildProtocolError(
        "coordination_uncertain",
        "the exact child attempt started but its outcome is unknown",
    )


def test_worker_and_local_prepare_claims_are_mutually_exclusive(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    worker = claim_worker(
        run_dir,
        worker_id="reader",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_local_prepare(
            run_dir,
            worker_id="preparer",
            request_id="44444444-4444-4444-8444-444444444444",
            now="2026-07-10T00:00:02Z",
        )
    assert exc_info.value.code == "no_available_work"

    release_worker(
        run_dir,
        worker["item_id"],
        worker_id=worker["worker_id"],
        claim_id=worker["claim_id"],
        lease_token=worker["lease_token"],
        attempt_id=worker["attempt_id"],
        acknowledge_no_side_effects=True,
        request_id="55555555-5555-4555-8555-555555555555",
        now="2026-07-10T00:00:03Z",
    )
    local = claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="66666666-6666-4666-8666-666666666666",
        now="2026-07-10T00:00:04Z",
    ).result["assignments"][0]
    assert local["attempt_number"] == 1


def test_local_renew_release_require_exact_identity_and_acknowledgement(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    assignment = claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    with pytest.raises(BatchRuntimeError) as exc_info:
        renew_local_prepare(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token="x" * 43,
            attempt_id=assignment["attempt_id"],
            request_id="44444444-4444-4444-8444-444444444444",
            now="2026-07-10T00:00:02Z",
        )
    assert exc_info.value.code == "lease_identity_mismatch"
    renewed = renew_local_prepare(
        run_dir,
        assignment["item_id"],
        worker_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        lease_token=assignment["lease_token"],
        attempt_id=assignment["attempt_id"],
        request_id="55555555-5555-4555-8555-555555555555",
        now="2026-07-10T00:00:11Z",
    )
    assert renewed.result["status"] == "claimed"
    with pytest.raises(BatchRuntimeError) as exc_info:
        release_local_prepare(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            acknowledge_no_side_effects=False,
            request_id="66666666-6666-4666-8666-666666666666",
            now="2026-07-10T00:00:12Z",
        )
    assert exc_info.value.code == "acknowledgement_required"
    released = release_local_prepare(
        run_dir,
        assignment["item_id"],
        worker_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        lease_token=assignment["lease_token"],
        attempt_id=assignment["attempt_id"],
        acknowledge_no_side_effects=True,
        request_id="77777777-7777-4777-8777-777777777777",
        now="2026-07-10T00:00:12Z",
    )
    assert released.result["status"] == "queued"


def test_failed_local_prepare_is_directly_reclaimable_with_new_attempt(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
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
        status="failed",
        source=view.manifest.items[0].source,
        paper_reader_root=paper_reader_root_identity(root),
        paper_reader_run_directory=None,
        error={"code": "prepare_failed", "message": "deterministic failure"},
    )
    result_path = tmp_path / "local-result.json"
    result_path.write_bytes(canonical_json_bytes(result))
    finished = finish_local_prepare(
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
    assert finished.result["status"] == "failed"
    assert load_run_view(run_dir).state.items[0].local_prepare_failure_code == "prepare_failed"

    reclaimed = claim_local_prepare(
        run_dir,
        worker_id="preparer-2",
        request_id="55555555-5555-4555-8555-555555555555",
        now="2026-07-10T00:00:03Z",
    ).result["assignments"][0]
    assert reclaimed["attempt_id"] != assignment["attempt_id"]
    assert reclaimed["attempt_number"] == 2


def test_finish_rebinds_pdf_after_result_and_coordination_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _run(tmp_path)
    root = _fake_paper_reader_root(tmp_path)
    assignment = claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="30303030-3030-4030-8030-303030303030",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    view = load_run_view(run_dir)
    pdf = Path(view.manifest.items[0].source.path)
    result = LocalPrepareResult(
        schema_version="paper_reader_batch.local-prepare-result.v2",
        manifest_sha256=view.manifest_sha256,
        item_id=assignment["item_id"],
        worker_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        attempt_id=assignment["attempt_id"],
        attempt_number=assignment["attempt_number"],
        lease_token_sha256=sha256_bytes(assignment["lease_token"].encode()),
        status="failed",
        source=view.manifest.items[0].source,
        paper_reader_root=paper_reader_root_identity(root),
        paper_reader_run_directory=None,
        error={"code": "prepare_failed", "message": "deterministic failure"},
    )
    result_path = tmp_path / "finish-race-result.json"
    result_path.write_bytes(canonical_json_bytes(result))
    original_validator = local_prepare_module.validate_local_prepare_result_artifacts
    armed = False
    mutated = False

    def arm_after_event_fsync(stage: str) -> None:
        nonlocal armed
        if stage == "after_writing_fsync":
            armed = True

    def drift_after_artifact_validation(*args, **kwargs):
        nonlocal mutated
        validated = original_validator(*args, **kwargs)
        if armed and not mutated:
            mutated = True
            pdf.write_bytes(b"%PDF-1.7\ndrift during finish closure\n")
        return validated

    monkeypatch.setattr(
        local_prepare_module,
        "validate_local_prepare_result_artifacts",
        drift_after_artifact_validation,
    )

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
            request_id="31313131-3131-4131-8131-313131313131",
            now="2026-07-10T00:00:02Z",
            fault=arm_after_event_fsync,
        )

    assert exc_info.value.code == "source_drift"
    assert mutated is True
    after = load_run_view(run_dir)
    assert len(after.events) == 3
    assert after.events[-1].data.kind == "request.aborted"
    assert after.pending_event is None
    assert len(after.aborted_events) == 1
    assert after.state.items[0].local_prepare_status == "claimed"


def test_local_prepare_claim_rebinds_pdf_at_final_event_precommit(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    pdf = run_dir.parent / "paper-0.pdf"
    mutated = False

    def drift_source(stage: str) -> None:
        nonlocal mutated
        if stage == "after_writing_fsync" and not mutated:
            mutated = True
            pdf.write_bytes(b"%PDF-1.7\nchanged during local claim commit\n")

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_local_prepare(
            run_dir,
            worker_id="preparer",
            request_id="34343434-3434-4434-8434-343434343434",
            now="2026-07-10T00:00:01Z",
            fault=drift_source,
        )

    assert exc_info.value.code == "source_drift"
    assert mutated is True
    view = load_run_view(run_dir)
    assert len(view.events) == 2
    assert view.events[-1].data.kind == "request.aborted"
    assert view.pending_event is None
    assert len(view.aborted_events) == 1
    assert view.state.items[0].local_prepare_status == "queued"


def test_local_prepare_claim_event_assigns_only_one_pdf(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, pdf_count=2)

    first = claim_local_prepare(
        run_dir,
        worker_id="preparer-1",
        request_id="35353535-3535-4535-8535-353535353535",
        limit=2,
        now="2026-07-10T00:00:01Z",
    )
    second = claim_local_prepare(
        run_dir,
        worker_id="preparer-2",
        request_id="36363636-3636-4636-8636-363636363636",
        limit=2,
        now="2026-07-10T00:00:02Z",
    )

    assert [assignment["item_id"] for assignment in first.result["assignments"]] == ["001"]
    assert [assignment["item_id"] for assignment in second.result["assignments"]] == ["002"]


def test_coordination_uncertain_local_prepare_cannot_be_reclaimed_as_attempt_two(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    root = _fake_paper_reader_root(tmp_path)
    assignment = claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    uncertain = run_local_prepare(
        run_dir,
        assignment["item_id"],
        worker_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        lease_token=assignment["lease_token"],
        attempt_id=assignment["attempt_id"],
        paper_reader_root=root,
        request_id="44444444-4444-4444-8444-444444444444",
        now="2026-07-10T00:00:02Z",
        runner=_durably_uncertain_runner,
    )
    assert uncertain.result["status"] == "blocked"
    before = (run_dir / "state.json").read_bytes()

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_local_prepare(
            run_dir,
            worker_id="attempt-2",
            request_id="55555555-5555-4555-8555-555555555555",
            now="2026-07-10T00:00:03Z",
        )

    assert exc_info.value.code == "no_available_work"
    assert (run_dir / "state.json").read_bytes() == before
    item = load_run_view(run_dir).state.items[0]
    assert item.local_prepare_last_attempt_id == assignment["attempt_id"]
    assert item.local_prepare_attempt_count == 1


def test_run_recover_resumes_same_coordination_uncertain_attempt_after_last_expiry(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    root = _fake_paper_reader_root(tmp_path)
    assignment = claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
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
    )
    uncertain = run_local_prepare(
        run_dir,
        assignment["item_id"],
        worker_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        lease_token=assignment["lease_token"],
        attempt_id=assignment["attempt_id"],
        paper_reader_root=root,
        request_id="55555555-5555-4555-8555-555555555555",
        now="2026-07-10T00:00:03Z",
        runner=_durably_uncertain_runner,
    )
    result_path = Path(uncertain.result["result_path"])
    assert uncertain.result["status"] == "blocked"

    recovered = recover_run(
        run_dir,
        request_id="66666666-6666-4666-8666-666666666666",
        now="2026-07-10T00:16:42Z",
    )

    assert recovered.result["expired_local_prepare_items"] == []
    assert recovered.result["resumed_local_prepare_items"] == [assignment["item_id"]]
    view = load_run_view(run_dir)
    item = view.state.items[0]
    lease = item.local_prepare_lease
    assert item.local_prepare_status == "claimed"
    assert item.local_prepare_attempt_count == 1
    assert item.local_prepare_result_sha256 is None
    assert item.local_prepare_failure_code is None
    assert item.local_prepare_failure_message is None
    assert lease is not None
    assert lease.actor_id == assignment["worker_id"]
    assert lease.claim_id == assignment["claim_id"]
    assert lease.attempt_id == assignment["attempt_id"]
    assert lease.attempt_number == assignment["attempt_number"]
    assert lease.lease_token_sha256 == sha256_bytes(assignment["lease_token"].encode())
    assert lease.issued_at == "2026-07-10T00:16:42Z"
    assert lease.expires_at == "2026-07-10T00:31:42.000000Z"
    resumed = view.events[-1].data.resumed_local_prepare_leases
    assert len(resumed) == 1
    assert resumed[0].previous_expires_at == "2026-07-10T00:16:42.000000Z"

    replayed = recover_run(
        run_dir,
        request_id="66666666-6666-4666-8666-666666666666",
        now="2026-07-10T00:16:42Z",
    )
    assert replayed.replayed is True
    assert replayed.result == recovered.result

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_local_prepare(
            run_dir,
            worker_id="attempt-2",
            request_id="77777777-7777-4777-8777-777777777777",
            now="2026-07-10T00:16:43Z",
        )
    assert exc_info.value.code == "no_available_work"

    finish_local_prepare(
        run_dir,
        assignment["item_id"],
        worker_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        lease_token=assignment["lease_token"],
        attempt_id=assignment["attempt_id"],
        result_path=result_path,
        expected_root=root,
        request_id="88888888-8888-4888-8888-888888888888",
        now="2026-07-10T00:16:43Z",
    )
    events_before_early_recovery = tuple(
        path.read_bytes() for path in sorted((run_dir / "events").glob("*.json"))
    )
    state_before_early_recovery = (run_dir / "state.json").read_bytes()

    with pytest.raises(BatchRuntimeError) as exc_info:
        recover_run(
            run_dir,
            request_id="99999999-9999-4999-8999-999999999999",
            now="2026-07-10T00:16:44Z",
        )
    assert exc_info.value.code == "nothing_to_recover"
    assert tuple(
        path.read_bytes() for path in sorted((run_dir / "events").glob("*.json"))
    ) == events_before_early_recovery
    assert (run_dir / "state.json").read_bytes() == state_before_early_recovery

    recovered_again = recover_run(
        run_dir,
        request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        now="2026-07-10T00:31:42Z",
    )
    assert recovered_again.result["expired_local_prepare_items"] == []
    assert recovered_again.result["resumed_local_prepare_items"] == [assignment["item_id"]]
    item = load_run_view(run_dir).state.items[0]
    assert item.local_prepare_status == "claimed"
    assert item.local_prepare_attempt_count == 1
    assert item.local_prepare_lease is not None
    assert item.local_prepare_lease.claim_id == assignment["claim_id"]
    assert item.local_prepare_lease.attempt_id == assignment["attempt_id"]
    resumed_again = load_run_view(run_dir).events[-1].data.resumed_local_prepare_leases
    assert len(resumed_again) == 1
    assert resumed_again[0].previous_expires_at == "2026-07-10T00:31:42.000000Z"
