import hashlib
import json
from pathlib import Path
import shutil

import pytest

from paper_reader_batch import v2_journal, v2_worker
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import canonical_json_bytes, canonical_sha256
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_run import initialize_run, run_status
from paper_reader_batch.v2_worker import claim_worker


def _run(tmp_path: Path, *, claimed: bool = True) -> Path:
    skill = tmp_path / "skill"
    skill.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\njournal integrity\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    manifest = tmp_path / "manifest-source.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="journal integrity",
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
    if claimed:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id="33333333-3333-4333-8333-333333333333",
            now="2026-07-10T00:00:01Z",
        )
    return run_dir


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        result[path.relative_to(root).as_posix()] = (metadata.st_mtime_ns, metadata.st_size, digest)
    return result


def _rewrite_event(path: Path, mutate) -> None:
    payload = json.loads(path.read_bytes())
    mutate(payload)
    payload.pop("event_sha256", None)
    payload["event_sha256"] = canonical_sha256(payload)
    path.write_bytes(canonical_json_bytes(payload))


def _assert_read_only_corrupt(run_dir: Path, expected_code: str = "journal_corrupt") -> None:
    before = _snapshot(run_dir)
    with pytest.raises(BatchRuntimeError) as exc_info:
        run_status(run_dir)
    assert exc_info.value.code == expected_code
    assert _snapshot(run_dir) == before


def test_manifest_byte_drift_is_rejected_before_snapshot_or_lock_mutation(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    manifest_path = run_dir / "manifest.json"
    payload = json.loads(manifest_path.read_bytes())
    payload["batch_title"] = "drifted manifest"
    manifest_path.write_bytes(canonical_json_bytes(payload))
    _assert_read_only_corrupt(run_dir, "manifest_drift")


def test_manifest_whitespace_drift_is_reported_as_drift_not_reparsed(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    manifest_path = run_dir / "manifest.json"
    payload = json.loads(manifest_path.read_bytes())
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _assert_read_only_corrupt(run_dir, "manifest_drift")


def test_journal_gap_is_immediate_read_only_corruption(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    event = run_dir / "events" / "00000000000000000002.json"
    event.rename(run_dir / "events" / "00000000000000000003.json")
    _assert_read_only_corrupt(run_dir)


def test_event_hash_mismatch_is_read_only_corruption(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    event = run_dir / "events" / "00000000000000000002.json"
    payload = json.loads(event.read_bytes())
    payload["event_sha256"] = "0" * 64
    event.write_bytes(canonical_json_bytes(payload))
    _assert_read_only_corrupt(run_dir)


def test_previous_hash_mismatch_is_read_only_corruption(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    event = run_dir / "events" / "00000000000000000002.json"
    _rewrite_event(event, lambda payload: payload.__setitem__("previous_event_sha256", "0" * 64))
    _assert_read_only_corrupt(run_dir)


def test_impossible_reducer_transition_is_read_only_corruption(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    event = run_dir / "events" / "00000000000000000002.json"

    def mutate(payload: dict) -> None:
        payload["data"]["assignments"][0]["attempt_number"] = 2

    _rewrite_event(event, mutate)
    _assert_read_only_corrupt(run_dir)


def test_missing_snapshot_is_diagnostic_and_read_only(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    (run_dir / "state.json").unlink()
    before = _snapshot(run_dir)
    status = run_status(run_dir)
    assert status["snapshot_status"] == "missing"
    assert status["state"]["items"][0]["worker_status"] == "claimed"
    assert _snapshot(run_dir) == before


def test_orphan_content_addressed_result_is_ignored_by_replay(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    orphan = run_dir / "results" / "worker" / f"{'a' * 64}.json"
    orphan.write_bytes(b"{}")
    before = _snapshot(run_dir)
    status = run_status(run_dir)
    assert status["state"]["items"][0]["worker_status"] == "claimed"
    assert _snapshot(run_dir) == before


@pytest.mark.parametrize(
    ("same_manifest", "expected_code"),
    [
        (True, "run_identity_drift"),
        (False, "manifest_drift"),
    ],
)
def test_transaction_rejects_run_directory_swap_after_caller_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_manifest: bool,
    expected_code: str,
) -> None:
    run_dir = _run(tmp_path, claimed=False)
    if same_manifest:
        replacement = tmp_path / "replacement-run"
        shutil.copytree(run_dir, replacement)
    else:
        replacement_root = tmp_path / "replacement-source"
        replacement_root.mkdir()
        replacement = _run(replacement_root, claimed=False)
    moved_original = tmp_path / "original-run-moved"
    original_before = _snapshot(run_dir)
    replacement_before = _snapshot(replacement)

    def swap_before_lock(*args, **kwargs):
        run_dir.rename(moved_original)
        replacement.rename(run_dir)
        return v2_journal.append_transaction(*args, **kwargs)

    monkeypatch.setattr(v2_worker, "append_transaction", swap_before_lock)

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_worker(
            run_dir,
            worker_id="worker",
            request_id="44444444-4444-4444-8444-444444444444",
            now="2026-07-10T00:00:01Z",
        )

    assert exc_info.value.code == expected_code
    assert _snapshot(moved_original) == original_before
    assert _snapshot(run_dir) == replacement_before
