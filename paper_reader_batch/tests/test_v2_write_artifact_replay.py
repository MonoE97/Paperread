from __future__ import annotations

from pathlib import Path

import pytest

from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_report import run_report
from paper_reader_batch.v2_write import (
    begin_write,
    claim_write,
    commit_write,
    mark_write_uncertain,
    reconcile_write,
)
from test_v2_write_runtime import (
    REQUEST_WRITE_BEGIN,
    REQUEST_WRITE_CLAIM,
    REQUEST_WRITE_COMMIT,
    REQUEST_WRITE_RECONCILE,
    REQUEST_WRITE_UNCERTAIN,
    _make_authorization,
    _make_reconciliation_matches,
    _make_verification,
    _make_write_result,
    _ready_write_run,
)


def _report_bytes(run_dir: Path) -> tuple[bytes, bytes]:
    return (
        (run_dir / "batch-report.json").read_bytes(),
        (run_dir / "batch-report.md").read_bytes(),
    )


def test_committed_write_replay_rejects_later_verification_tamper_without_rewriting_report(
    tmp_path: Path,
) -> None:
    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, authorization = _make_authorization(ready, claimed)
    begin_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        authorization_path=authorization_path,
        request_id=REQUEST_WRITE_BEGIN,
        now="2026-07-10T00:00:06Z",
    )
    verification_path = _make_verification(ready, authorization_path, authorization)
    result_path = _make_write_result(
        ready,
        claimed,
        authorization_path,
        authorization,
        verification_path,
    )
    commit_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        result_path=result_path,
        request_id=REQUEST_WRITE_COMMIT,
        now="2026-07-10T00:00:08Z",
    )
    run_report(ready.run_dir, generated_at="2026-07-10T00:01:00Z")
    reports_before = _report_bytes(ready.run_dir)

    (verification_path.with_suffix("") / "note.json").write_bytes(b"{}")

    with pytest.raises(BatchRuntimeError) as load_error:
        load_run_view(ready.run_dir)
    assert load_error.value.code == "journal_corrupt"
    with pytest.raises(BatchRuntimeError) as report_error:
        run_report(ready.run_dir, generated_at="2026-07-10T00:02:00Z")
    assert report_error.value.code == "journal_corrupt"
    assert _report_bytes(ready.run_dir) == reports_before

def test_reconciled_write_replay_rejects_later_children_tamper_without_rewriting_report(
    tmp_path: Path,
) -> None:
    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, authorization = _make_authorization(ready, claimed)
    begin_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        authorization_path=authorization_path,
        request_id=REQUEST_WRITE_BEGIN,
        now="2026-07-10T00:00:06Z",
    )
    mark_write_uncertain(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        reason="lost response",
        request_id=REQUEST_WRITE_UNCERTAIN,
        now="2026-07-10T00:00:08Z",
    )
    reconciliation_path = _make_reconciliation_matches(
        ready,
        authorization_path,
        authorization,
        note_keys=("NOTE1",),
    )
    reconcile_write(
        ready.run_dir,
        "001",
        readback_path=reconciliation_path,
        request_id=REQUEST_WRITE_RECONCILE,
        now="2026-07-10T00:00:10Z",
    )
    run_report(ready.run_dir, generated_at="2026-07-10T00:01:00Z")
    reports_before = _report_bytes(ready.run_dir)

    (reconciliation_path.with_suffix("") / "children.json").write_bytes(b"[]")

    with pytest.raises(BatchRuntimeError) as load_error:
        load_run_view(ready.run_dir)
    assert load_error.value.code == "journal_corrupt"
    with pytest.raises(BatchRuntimeError) as report_error:
        run_report(ready.run_dir, generated_at="2026-07-10T00:02:00Z")
    assert report_error.value.code == "journal_corrupt"
    assert _report_bytes(ready.run_dir) == reports_before
