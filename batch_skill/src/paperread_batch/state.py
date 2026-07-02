from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from paperread_batch.io import JsonFileError, read_json
from paperread_batch.takeaway import TakeawayError, extract_takeaway


STATE_SCHEMA_VERSION = "paperread-batch.state.v1"
ITEM_RESULT_SCHEMA_VERSION = "paperread-batch.item-result.v1"

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"
BLOCKED = "blocked"
INTERRUPTED = "interrupted"

TERMINAL_STATUSES = {SUCCEEDED, FAILED, SKIPPED, BLOCKED}


class StateError(ValueError):
    pass


def _empty_item_state(manifest_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": manifest_item["item_id"],
        "input_type": manifest_item["input_type"],
        "expected_output": manifest_item["expected_output"],
        "status": PENDING,
        "attempt_count": 0,
        "worker_id": "",
        "started_at": "",
        "completed_at": "",
        "paperread_run_dir": "",
        "summary_json": "",
        "note_md": "",
        "note_html": "",
        "gate_report": "",
        "write_payload": "",
        "local_note_path": "",
        "local_gate_report": "",
        "thirty_second_takeaway": "",
        "takeaway_source_type": "",
        "takeaway_source_path": "",
        "takeaway_source_sha256": "",
        "failure_reason": "",
        "resume_decision": "",
    }


def initial_state(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "manifest_schema_version": manifest["schema_version"],
        "batch_title": manifest["batch_title"],
        "batch_status": PENDING,
        "created_at": manifest["created_at"],
        "updated_at": "",
        "items": [_empty_item_state(item) for item in manifest["items"]],
    }


def _find_item(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    for item in items:
        if item.get("item_id") == item_id:
            return item
    raise StateError(f"unknown item_id: {item_id}")


def _find_manifest_item(manifest: dict[str, Any], item_id: str) -> dict[str, Any]:
    return _find_item(manifest["items"], item_id)


def _refresh_batch_status(state: dict[str, Any]) -> None:
    statuses = [item["status"] for item in state["items"]]
    if any(status in {PENDING, RUNNING, INTERRUPTED} for status in statuses):
        state["batch_status"] = RUNNING
    elif any(status in {FAILED, BLOCKED} for status in statuses):
        state["batch_status"] = "completed_with_failures"
    else:
        state["batch_status"] = "completed"


def allocate_next(
    state: dict[str, Any],
    *,
    limit: int,
    now: str,
    worker_prefix: str = "worker",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if limit < 1:
        raise StateError("limit must be positive")
    updated = copy.deepcopy(state)
    selected: list[dict[str, Any]] = []
    for item in updated["items"]:
        if len(selected) >= limit:
            break
        if item["status"] != PENDING:
            continue
        item["status"] = RUNNING
        item["attempt_count"] = int(item.get("attempt_count", 0)) + 1
        item["worker_id"] = f"{worker_prefix}-{item['item_id']}"
        item["started_at"] = now
        item["completed_at"] = ""
        selected.append(copy.deepcopy(item))
    if selected:
        updated["batch_status"] = RUNNING
        updated["updated_at"] = now
    return updated, selected


def _require_readable_file(path_value: Any, label: str) -> str:
    if not isinstance(path_value, str) or not path_value.strip():
        raise StateError(f"{label} is required")
    path = Path(path_value).expanduser()
    if not path.exists() or not path.is_file():
        raise StateError(f"{label} is not a readable file: {path}")
    try:
        path.read_bytes()
    except OSError as exc:
        raise StateError(f"{label} is not readable: {path}") from exc
    return str(path)


def _require_directory(path_value: Any, label: str) -> str:
    if not isinstance(path_value, str) or not path_value.strip():
        raise StateError(f"{label} is required")
    path = Path(path_value).expanduser()
    if not path.exists() or not path.is_dir():
        raise StateError(f"{label} is not a directory: {path}")
    return str(path)


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateError(f"{label} is required")
    return value.strip()


def _require_positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise StateError(f"{label} must be a positive integer")
    return value


def _gate_is_write_ready(path: str) -> bool:
    payload = read_json(Path(path))
    if not isinstance(payload, dict):
        raise StateError("gate_report must contain a JSON object")
    return str(payload.get("status", "")).strip() == "write_ready"


def _validate_success_result(result: dict[str, Any], manifest_item: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {
        "paperread_run_dir": _require_directory(result.get("paperread_run_dir"), "paperread_run_dir"),
        "summary_json": _require_readable_file(result.get("summary_json"), "summary_json"),
    }
    if manifest_item["expected_output"] == "zotero_note_candidate":
        normalized["note_md"] = _require_readable_file(result.get("note_md"), "note_md")
        normalized["note_html"] = _require_readable_file(result.get("note_html"), "note_html")
        normalized["gate_report"] = _require_readable_file(result.get("gate_report"), "gate_report")
        write_payload = str(result.get("write_payload", "")).strip()
        if _gate_is_write_ready(normalized["gate_report"]):
            normalized["write_payload"] = _require_readable_file(write_payload, "write_payload")
        else:
            normalized["write_payload"] = write_payload
        normalized["local_note_path"] = str(result.get("local_note_path", "")).strip()
        normalized["local_gate_report"] = str(result.get("local_gate_report", "")).strip()
    else:
        normalized["local_note_path"] = _require_readable_file(result.get("local_note_path"), "local_note_path")
        normalized["local_gate_report"] = _require_readable_file(result.get("local_gate_report"), "local_gate_report")
        normalized["note_md"] = str(result.get("note_md", "")).strip()
        normalized["note_html"] = str(result.get("note_html", "")).strip()
        normalized["gate_report"] = str(result.get("gate_report", "")).strip()
        normalized["write_payload"] = str(result.get("write_payload", "")).strip()
    try:
        note_source_path = normalized["note_md"] or normalized["local_note_path"]
        normalized.update(extract_takeaway(Path(note_source_path), Path(normalized["summary_json"])))
    except (JsonFileError, TakeawayError, OSError) as exc:
        raise StateError(f"takeaway_unavailable: {exc}") from exc
    return normalized


def _validate_result_assignment(state_item: dict[str, Any], result: dict[str, Any]) -> None:
    if state_item.get("status") not in {RUNNING, INTERRUPTED}:
        raise StateError(f"item is not currently assigned: {state_item.get('item_id')}")
    worker_id = _require_text(result.get("worker_id"), "worker_id")
    attempt_count = _require_positive_int(result.get("attempt_count"), "attempt_count")
    if worker_id != str(state_item.get("worker_id", "")):
        raise StateError("worker_id does not match current item assignment")
    if attempt_count != int(state_item.get("attempt_count", 0)):
        raise StateError("attempt_count does not match current item assignment")


def record_item_result(
    state: dict[str, Any],
    manifest: dict[str, Any],
    item_id: str,
    result: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    if result.get("schema_version") != ITEM_RESULT_SCHEMA_VERSION:
        raise StateError(f"result schema_version must be {ITEM_RESULT_SCHEMA_VERSION}")
    if result.get("item_id") != item_id:
        raise StateError(f"mismatched item_id: expected {item_id}, got {result.get('item_id')}")
    status = result.get("status")
    if status not in {SUCCEEDED, FAILED, SKIPPED, BLOCKED}:
        raise StateError(f"unsupported result status: {status}")

    updated = copy.deepcopy(state)
    state_item = _find_item(updated["items"], item_id)
    manifest_item = _find_manifest_item(manifest, item_id)
    _validate_result_assignment(state_item, result)
    state_item["status"] = status
    state_item["completed_at"] = now
    state_item["resume_decision"] = ""

    if status == SUCCEEDED:
        normalized = _validate_success_result(result, manifest_item)
        for key, value in normalized.items():
            state_item[key] = value
        state_item["failure_reason"] = ""
    elif status == FAILED:
        state_item["failure_reason"] = _require_text(result.get("failure_reason"), "failure_reason")
    else:
        state_item["failure_reason"] = str(result.get("failure_reason", "")).strip()

    updated["updated_at"] = now
    _refresh_batch_status(updated)
    return updated


def mark_interrupted_running_items(state: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(state)
    for item in updated["items"]:
        if item["status"] == RUNNING:
            item["status"] = INTERRUPTED
            item["resume_decision"] = "marked_interrupted_on_resume"
    _refresh_batch_status(updated)
    return updated


def retry_failed(state: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(state)
    for item in updated["items"]:
        if item["status"] in {FAILED, INTERRUPTED}:
            item["status"] = PENDING
            item["worker_id"] = ""
            item["started_at"] = ""
            item["completed_at"] = ""
            item["failure_reason"] = ""
            item["resume_decision"] = "retry_requested"
    _refresh_batch_status(updated)
    return updated
