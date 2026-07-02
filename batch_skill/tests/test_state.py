import json
from pathlib import Path

import pytest

from paperread_batch.io import file_sha256, write_json_atomic
from paperread_batch.manifest import build_manifest, validate_manifest
from paperread_batch.state import (
    STATE_SCHEMA_VERSION,
    StateError,
    allocate_next,
    initial_state,
    mark_interrupted_running_items,
    record_item_result,
    retry_failed,
)


def _zotero_manifest() -> dict:
    return validate_manifest(
        build_manifest(
            batch_title="state batch",
            source_summary={"source_type": "zotero_titles", "description": "test"},
            items=[
                {
                    "item_id": "001",
                    "input_type": "zotero_title",
                    "input": {"title": "First paper"},
                    "expected_output": "zotero_note_candidate",
                },
                {
                    "item_id": "002",
                    "input_type": "zotero_title",
                    "input": {"title": "Second paper"},
                    "expected_output": "zotero_note_candidate",
                },
            ],
            created_at="2026-07-02T10:00:00+08:00",
        )
    )


def _write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _write_text(path: Path, text: str = "ok") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_initial_state_tracks_manifest_items() -> None:
    state = initial_state(_zotero_manifest())

    assert state["schema_version"] == STATE_SCHEMA_VERSION
    assert state["batch_status"] == "pending"
    assert [item["status"] for item in state["items"]] == ["pending", "pending"]
    assert [item["attempt_count"] for item in state["items"]] == [0, 0]


def test_allocate_next_marks_items_running() -> None:
    state = initial_state(_zotero_manifest())

    updated, selected = allocate_next(state, limit=1, now="2026-07-02T10:01:00+08:00")

    assert [item["item_id"] for item in selected] == ["001"]
    assert updated["batch_status"] == "running"
    assert updated["items"][0]["status"] == "running"
    assert updated["items"][0]["attempt_count"] == 1
    assert updated["items"][0]["worker_id"] == "worker-001"
    assert updated["items"][1]["status"] == "pending"


def test_record_success_requires_zotero_evidence_and_updates_state(tmp_path: Path) -> None:
    manifest = _zotero_manifest()
    state, _selected = allocate_next(initial_state(manifest), limit=1, now="2026-07-02T10:01:00+08:00")
    run_dir = tmp_path / "paperread" / "runs" / "paper"
    result = {
        "schema_version": "paperread-batch.item-result.v1",
        "item_id": "001",
        "worker_id": "worker-001",
        "attempt_count": 1,
        "status": "succeeded",
        "paperread_run_dir": str(run_dir),
        "summary_json": _write_json(run_dir / "summary.json", {"tldr": "单篇结论"}),
        "note_md": _write_text(run_dir / "note.md", "| 30 秒结论 | 单篇结论 |"),
        "note_html": _write_text(run_dir / "note.html", "<h1>note</h1>"),
        "gate_report": _write_json(run_dir / "gate-report.json", {"status": "blocked"}),
        "write_payload": "",
        "local_note_path": "",
        "local_gate_report": "",
        "thirty_second_takeaway": "伪造结论",
        "takeaway_source_type": "fake",
        "takeaway_source_path": str(run_dir / "summary.json"),
        "takeaway_source_sha256": "fake",
        "failure_reason": "",
    }

    updated = record_item_result(state, manifest, "001", result, now="2026-07-02T10:02:00+08:00")

    item = updated["items"][0]
    assert item["status"] == "succeeded"
    assert item["completed_at"] == "2026-07-02T10:02:00+08:00"
    assert item["paperread_run_dir"] == str(run_dir)
    assert item["thirty_second_takeaway"] == "单篇结论"
    assert item["takeaway_source_type"] == "rendered_note_30_second_row"
    assert item["takeaway_source_path"] == str(run_dir / "note.md")
    assert item["takeaway_source_sha256"] == file_sha256(run_dir / "note.md")


def test_record_success_requires_write_payload_when_gate_is_write_ready(tmp_path: Path) -> None:
    manifest = _zotero_manifest()
    state, _selected = allocate_next(initial_state(manifest), limit=1, now="2026-07-02T10:01:00+08:00")
    run_dir = tmp_path / "paperread" / "runs" / "paper"
    result = {
        "schema_version": "paperread-batch.item-result.v1",
        "item_id": "001",
        "worker_id": "worker-001",
        "attempt_count": 1,
        "status": "succeeded",
        "paperread_run_dir": str(run_dir),
        "summary_json": _write_json(run_dir / "summary.json", {}),
        "note_md": _write_text(run_dir / "note.md"),
        "note_html": _write_text(run_dir / "note.html"),
        "gate_report": _write_json(run_dir / "gate-report.json", {"status": "write_ready"}),
        "write_payload": "",
        "local_note_path": "",
        "local_gate_report": "",
        "thirty_second_takeaway": "结论",
        "takeaway_source_type": "rendered_note_30_second_row",
        "takeaway_source_path": str(run_dir / "note.md"),
        "takeaway_source_sha256": "abc",
        "failure_reason": "",
    }

    with pytest.raises(StateError, match="write_payload"):
        record_item_result(state, manifest, "001", result, now="2026-07-02T10:02:00+08:00")


def test_record_failed_item_keeps_failure_reason() -> None:
    manifest = _zotero_manifest()
    state, _selected = allocate_next(initial_state(manifest), limit=1, now="2026-07-02T10:01:00+08:00")
    result = {
        "schema_version": "paperread-batch.item-result.v1",
        "item_id": "001",
        "worker_id": "worker-001",
        "attempt_count": 1,
        "status": "failed",
        "failure_reason": "duplicate Zotero title",
    }

    updated = record_item_result(state, manifest, "001", result, now="2026-07-02T10:02:00+08:00")

    assert updated["items"][0]["status"] == "failed"
    assert updated["items"][0]["failure_reason"] == "duplicate Zotero title"
    assert updated["batch_status"] == "running"


def test_record_result_rejects_mismatched_item_id() -> None:
    manifest = _zotero_manifest()
    state, _selected = allocate_next(initial_state(manifest), limit=1, now="2026-07-02T10:01:00+08:00")

    with pytest.raises(StateError, match="mismatched item_id"):
        record_item_result(
            state,
            manifest,
            "001",
            {"schema_version": "paperread-batch.item-result.v1", "item_id": "002", "status": "failed"},
            now="2026-07-02T10:02:00+08:00",
        )


def test_record_result_rejects_stale_attempt_result() -> None:
    manifest = _zotero_manifest()
    state, _selected = allocate_next(initial_state(manifest), limit=1, now="2026-07-02T10:01:00+08:00")
    state["items"][0]["status"] = "running"
    state["items"][0]["attempt_count"] = 2
    state["items"][0]["worker_id"] = "worker-001"

    with pytest.raises(StateError, match="attempt_count"):
        record_item_result(
            state,
            manifest,
            "001",
            {
                "schema_version": "paperread-batch.item-result.v1",
                "item_id": "001",
                "worker_id": "worker-001",
                "attempt_count": 1,
                "status": "failed",
                "failure_reason": "late attempt result",
            },
            now="2026-07-02T10:02:00+08:00",
        )


def test_running_items_become_interrupted_on_resume() -> None:
    state, _selected = allocate_next(initial_state(_zotero_manifest()), limit=2, now="2026-07-02T10:01:00+08:00")

    resumed = mark_interrupted_running_items(state)

    assert [item["status"] for item in resumed["items"]] == ["interrupted", "interrupted"]


def test_retry_failed_resets_failed_and_interrupted_items() -> None:
    manifest = _zotero_manifest()
    state, _selected = allocate_next(initial_state(manifest), limit=2, now="2026-07-02T10:01:00+08:00")
    state["items"][0]["status"] = "failed"
    state["items"][0]["failure_reason"] = "bad PDF"
    state["items"][1]["status"] = "interrupted"

    retried = retry_failed(state)

    assert [item["status"] for item in retried["items"]] == ["pending", "pending"]
    assert all(item["failure_reason"] == "" for item in retried["items"])


def test_atomic_json_write_replaces_file_without_temp_leftovers(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    write_json_atomic(path, {"status": "first"})
    write_json_atomic(path, {"status": "second"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "second"}
    assert list(tmp_path.glob("*.tmp")) == []
