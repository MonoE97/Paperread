from pathlib import Path

import pytest

from paper_reader_batch.v2_artifacts import paper_reader_root_identity
from paper_reader_batch.v2_contracts import LocalPrepareResult
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_json import canonical_json_bytes, sha256_bytes
from paper_reader_batch.v2_local_prepare import (
    claim_local_prepare,
    finish_local_prepare,
    release_local_prepare,
    renew_local_prepare,
)
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_run import initialize_run
from paper_reader_batch.v2_worker import claim_worker, release_worker


def _run(tmp_path: Path) -> Path:
    skill = tmp_path / "batch-skill"
    skill.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nlocal prepare\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
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
