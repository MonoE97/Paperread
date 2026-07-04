from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from paperread_batch.io import JsonFileError, read_json
from paperread_batch.manifest import ZOTERO_WRITE_POLICY
from paperread_batch.takeaway import TakeawayError, extract_takeaway


STATE_SCHEMA_VERSION = "paperread-batch.state.v1"
ITEM_RESULT_SCHEMA_VERSION = "paperread-batch.item-result.v1"
WRITE_RESULT_SCHEMA_VERSION = "paperread-batch.write-result.v1"
LOCAL_PREPARE_RESULT_SCHEMA_VERSION = "paperread-batch.local-prepare-result.v1"

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"
BLOCKED = "blocked"
INTERRUPTED = "interrupted"

TERMINAL_STATUSES = {SUCCEEDED, FAILED, SKIPPED, BLOCKED}

PENDING_PREPARE = "pending_prepare"
PENDING_WRITE = "pending_write"
PREPARED_NOT_WRITTEN = "prepared_not_written"
WRITTEN = "written"
WRITE_NOT_APPLICABLE = "not_applicable"
WRITE_BLOCKED = "blocked"
WRITE_FAILED = "failed"


class StateError(ValueError):
    pass


def _empty_item_state(manifest_item: dict[str, Any]) -> dict[str, Any]:
    write_status = WRITE_NOT_APPLICABLE
    if manifest_item["expected_output"] == "zotero_note_candidate":
        write_status = PENDING_PREPARE
    local_prepare_status = "not_applicable"
    if manifest_item["expected_output"] == "local_note":
        local_prepare_status = "pending"
    return {
        "item_id": manifest_item["item_id"],
        "input_type": manifest_item["input_type"],
        "expected_output": manifest_item["expected_output"],
        "status": PENDING,
        "write_status": write_status,
        "local_prepare_status": local_prepare_status,
        "attempt_count": 0,
        "worker_id": "",
        "started_at": "",
        "completed_at": "",
        "write_completed_at": "",
        "paperread_run_dir": "",
        "summary_json": "",
        "note_md": "",
        "note_html": "",
        "gate_report": "",
        "write_payload": "",
        "verify_report": "",
        "local_note_path": "",
        "local_gate_report": "",
        "prepared_analysis_dir": "",
        "prepared_final_note_path": "",
        "prepared_manifest_path": "",
        "local_prepare_failure_reason": "",
        "zotero_note_key": "",
        "zotero_parent_key": "",
        "content_sha256": "",
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
    elif any(item.get("write_status") == PENDING_WRITE for item in state["items"]):
        state["batch_status"] = "completed_pending_writes"
    elif any(item.get("write_status") == WRITE_FAILED for item in state["items"]):
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


def _read_write_payload(path: str) -> dict[str, Any]:
    try:
        payload = read_json(Path(_require_readable_file(path, "write_payload")))
    except JsonFileError as exc:
        raise StateError(f"write_payload unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise StateError("write_payload must contain a JSON object")
    if str(payload.get("action", "")).strip() != "create":
        raise StateError("write_payload action must be create")
    _require_text(payload.get("parentKey"), "write_payload.parentKey")
    _require_text(payload.get("contentSha256"), "write_payload.contentSha256")
    note_html_path = str(payload.get("note_html_path", "")).strip()
    if note_html_path:
        _require_readable_file(note_html_path, "write_payload.note_html_path")
    return payload


def _write_status_after_success(
    manifest: dict[str, Any],
    manifest_item: dict[str, Any],
    state_item: dict[str, Any],
) -> str:
    if manifest_item["expected_output"] == "local_note":
        return WRITE_NOT_APPLICABLE
    if not str(state_item.get("write_payload", "")).strip():
        return WRITE_BLOCKED
    if manifest.get("write_policy") == ZOTERO_WRITE_POLICY:
        return PENDING_WRITE
    return PREPARED_NOT_WRITTEN


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
        state_item["write_status"] = _write_status_after_success(manifest, manifest_item, state_item)
        state_item["failure_reason"] = ""
    elif status == FAILED:
        state_item["failure_reason"] = _require_text(result.get("failure_reason"), "failure_reason")
        state_item["write_status"] = WRITE_FAILED
    else:
        state_item["failure_reason"] = str(result.get("failure_reason", "")).strip()
        if manifest_item["expected_output"] != "local_note":
            state_item["write_status"] = WRITE_BLOCKED

    updated["updated_at"] = now
    _refresh_batch_status(updated)
    return updated


def record_local_prepare_result(
    state: dict[str, Any],
    item_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    if result.get("schema_version") != LOCAL_PREPARE_RESULT_SCHEMA_VERSION:
        raise StateError(f"local prepare result schema_version must be {LOCAL_PREPARE_RESULT_SCHEMA_VERSION}")
    if result.get("item_id") != item_id:
        raise StateError(f"mismatched item_id: expected {item_id}, got {result.get('item_id')}")
    updated = copy.deepcopy(state)
    item = _find_item(updated["items"], item_id)
    if item.get("expected_output") != "local_note":
        raise StateError("local prepare is only valid for local_note items")
    status = _require_text(result.get("status"), "local_prepare.status")
    if status == "prepared":
        item["local_prepare_status"] = "prepared"
        item["prepared_analysis_dir"] = _require_directory(result.get("analysis_dir"), "local_prepare.analysis_dir")
        item["prepared_final_note_path"] = _require_text(result.get("final_note_path"), "local_prepare.final_note_path")
        item["prepared_manifest_path"] = _require_readable_file(result.get("manifest_path"), "local_prepare.manifest_path")
        item["local_prepare_failure_reason"] = ""
        return updated
    if status == "failed":
        item["local_prepare_status"] = "failed"
        item["local_prepare_failure_reason"] = _require_text(
            result.get("failure_reason"),
            "local_prepare.failure_reason",
        )
        return updated
    raise StateError(f"unsupported local prepare status: {status}")


def pending_write_items(
    manifest: dict[str, Any],
    state: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if limit < 1:
        raise StateError("limit must be positive")
    if manifest.get("write_policy") != ZOTERO_WRITE_POLICY:
        return []

    selected: list[dict[str, Any]] = []
    for state_item in state.get("items", []):
        if len(selected) >= limit:
            break
        if state_item.get("status") != SUCCEEDED:
            continue
        if state_item.get("expected_output") != "zotero_note_candidate":
            continue
        if state_item.get("write_status") != PENDING_WRITE:
            continue
        manifest_item = _find_manifest_item(manifest, str(state_item.get("item_id", "")))
        payload = _read_write_payload(str(state_item.get("write_payload", "")))
        selected.append(
            {
                "item_id": state_item["item_id"],
                "input_type": state_item["input_type"],
                "input": manifest_item["input"],
                "paperread_run_dir": state_item.get("paperread_run_dir", ""),
                "note_html": state_item.get("note_html", ""),
                "note_md": state_item.get("note_md", ""),
                "gate_report": state_item.get("gate_report", ""),
                "write_payload": state_item.get("write_payload", ""),
                "parentKey": payload["parentKey"],
                "contentSha256": payload["contentSha256"],
                "tags": payload.get("tags", []),
            }
        )
    return selected


def record_write_result(
    state: dict[str, Any],
    manifest: dict[str, Any],
    item_id: str,
    result: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    if result.get("schema_version") != WRITE_RESULT_SCHEMA_VERSION:
        raise StateError(f"write result schema_version must be {WRITE_RESULT_SCHEMA_VERSION}")
    if result.get("item_id") != item_id:
        raise StateError(f"mismatched item_id: expected {item_id}, got {result.get('item_id')}")
    if result.get("status") != WRITTEN:
        raise StateError(f"unsupported write result status: {result.get('status')}")
    if manifest.get("write_policy") != ZOTERO_WRITE_POLICY:
        raise StateError("manifest write_policy is not zotero_write")

    updated = copy.deepcopy(state)
    state_item = _find_item(updated["items"], item_id)
    manifest_item = _find_manifest_item(manifest, item_id)
    if manifest_item["expected_output"] != "zotero_note_candidate":
        raise StateError("item is not Zotero-backed")
    if state_item.get("status") != SUCCEEDED:
        raise StateError("item has not completed successfully")
    if state_item.get("write_status") == WRITTEN:
        raise StateError("item is already written")
    if state_item.get("write_status") != PENDING_WRITE:
        raise StateError(f"item is not pending write: {state_item.get('write_status')}")

    payload = _read_write_payload(str(state_item.get("write_payload", "")))
    verify_report_path = _require_readable_file(result.get("verify_report"), "verify_report")
    try:
        verify_report = read_json(Path(verify_report_path))
    except JsonFileError as exc:
        raise StateError(f"verify_report unreadable: {exc}") from exc
    if not isinstance(verify_report, dict):
        raise StateError("verify_report must contain a JSON object")
    if str(verify_report.get("status", "")).strip() != "passed":
        raise StateError("verify_report status must be passed")
    verify_note_key = _require_text(verify_report.get("noteKey"), "verify_report.noteKey")
    verify_parent_key = _require_text(verify_report.get("parentKey"), "verify_report.parentKey")
    verify_content_sha256 = _require_text(verify_report.get("contentSha256"), "verify_report.contentSha256")

    note_key = str(result.get("note_key") or verify_note_key).strip()
    parent_key = str(result.get("parent_key") or verify_parent_key).strip()
    content_sha256 = str(result.get("contentSha256") or verify_content_sha256).strip()
    _require_text(note_key, "note_key")
    _require_text(parent_key, "parent_key")
    _require_text(content_sha256, "contentSha256")

    if verify_note_key != note_key:
        raise StateError("verify_report noteKey does not match write result")
    if verify_parent_key != parent_key:
        raise StateError("verify_report parentKey does not match write result")
    if verify_content_sha256 != content_sha256:
        raise StateError("verify_report contentSha256 does not match write result")
    if str(payload.get("parentKey", "")).strip() != parent_key:
        raise StateError("write_payload parentKey does not match verify_report")
    if str(payload.get("contentSha256", "")).strip() != content_sha256:
        raise StateError("write_payload contentSha256 does not match verify_report")

    state_item["write_status"] = WRITTEN
    state_item["zotero_note_key"] = note_key
    state_item["zotero_parent_key"] = parent_key
    state_item["content_sha256"] = content_sha256
    state_item["verify_report"] = verify_report_path
    state_item["write_completed_at"] = now
    updated["updated_at"] = now
    _refresh_batch_status(updated)
    return updated


def mark_interrupted_running_items(state: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(state)
    for item in updated["items"]:
        if item["status"] == RUNNING:
            item["status"] = INTERRUPTED
            if not item.get("resume_decision"):
                item["resume_decision"] = "marked_interrupted_on_resume"
    _refresh_batch_status(updated)
    return updated


def set_resume_decision(state: dict[str, Any], item_id: str, decision: str) -> dict[str, Any]:
    updated = copy.deepcopy(state)
    item = _find_item(updated["items"], item_id)
    item["resume_decision"] = _require_text(decision, "resume_decision")
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
            if item.get("expected_output") == "zotero_note_candidate":
                item["write_status"] = PENDING_PREPARE
            item["resume_decision"] = "retry_requested"
    _refresh_batch_status(updated)
    return updated
