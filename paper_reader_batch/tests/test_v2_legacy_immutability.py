import hashlib
import json
from pathlib import Path

import pytest

from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_run import initialize_run, run_status
from paper_reader_batch.v2_worker import claim_worker, finish_worker


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    snapshot: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        snapshot[path.relative_to(root).as_posix()] = (
            metadata.st_mtime_ns,
            metadata.st_size,
            digest,
        )
    return snapshot


def _run(tmp_path: Path) -> Path:
    skill = tmp_path / "skill"
    skill.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nlegacy rejection\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    manifest = tmp_path / "manifest-source.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="legacy rejection",
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


@pytest.mark.parametrize("schema", ["paper_reader_batch.manifest.v1", None, "paper_reader_batch.manifest.v9"])
def test_legacy_manifest_is_read_only_rejected_before_runtime_mutation(
    tmp_path: Path,
    schema: str | None,
) -> None:
    run_dir = _run(tmp_path)
    manifest_path = run_dir / "manifest.json"
    payload = json.loads(manifest_path.read_bytes())
    if schema is None:
        payload.pop("schema_version")
    else:
        payload["schema_version"] = schema
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_status(run_dir)
    assert exc_info.value.code == "unsupported_run_schema"
    assert _tree_snapshot(run_dir) == before


@pytest.mark.parametrize("schema", ["paper_reader_batch.event.v1", None, "paper_reader_batch.event.v9"])
def test_legacy_event_is_read_only_rejected_before_lock_or_snapshot_mutation(
    tmp_path: Path,
    schema: str | None,
) -> None:
    run_dir = _run(tmp_path)
    event_path = run_dir / "events" / "00000000000000000001.json"
    payload = json.loads(event_path.read_bytes())
    if schema is None:
        payload.pop("schema_version")
    else:
        payload["schema_version"] = schema
    event_path.write_text(json.dumps(payload), encoding="utf-8")
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_status(run_dir)
    assert exc_info.value.code == "unsupported_run_schema"
    assert _tree_snapshot(run_dir) == before


@pytest.mark.parametrize("schema", ["paper_reader_batch.state.v1", None, "paper_reader_batch.state.v9"])
def test_legacy_state_is_read_only_rejected_without_snapshot_repair(
    tmp_path: Path,
    schema: str | None,
) -> None:
    run_dir = _run(tmp_path)
    state_path = run_dir / "state.json"
    payload = json.loads(state_path.read_bytes())
    if schema is None:
        payload.pop("schema_version")
    else:
        payload["schema_version"] = schema
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    before = _tree_snapshot(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_status(run_dir)
    assert exc_info.value.code == "unsupported_run_schema"
    assert _tree_snapshot(run_dir) == before


@pytest.mark.parametrize(
    "schema",
    ["paper_reader_batch.worker-result.v1", None, "paper_reader_batch.worker-result.v9"],
)
def test_legacy_worker_result_is_rejected_before_result_or_event_publication(
    tmp_path: Path,
    schema: str | None,
) -> None:
    run_dir = _run(tmp_path)
    assignment = claim_worker(
        run_dir,
        worker_id="worker",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    result_path = tmp_path / "historical-result.json"
    payload = {"status": "failed"}
    if schema is not None:
        payload["schema_version"] = schema
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    run_before = _tree_snapshot(run_dir)
    result_before = _tree_snapshot(tmp_path)

    with pytest.raises(BatchRuntimeError) as exc_info:
        finish_worker(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            result_path=result_path,
            request_id="44444444-4444-4444-8444-444444444444",
            now="2026-07-10T00:00:02Z",
        )
    assert exc_info.value.code == "unsupported_run_schema"
    assert _tree_snapshot(run_dir) == run_before
    assert _tree_snapshot(tmp_path) == result_before
