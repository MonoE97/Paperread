from __future__ import annotations

from collections import Counter
from typing import Any


def _manifest_items_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["item_id"]: item for item in manifest["items"]}


def _input_label(manifest_item: dict[str, Any]) -> str:
    input_payload = manifest_item.get("input", {})
    if not isinstance(input_payload, dict):
        return ""
    if manifest_item.get("input_type") == "pdf_path":
        return str(input_payload.get("path", "")).strip()
    return str(input_payload.get("title", "")).strip() or str(input_payload.get("item_key", "")).strip()


def _write_status(item_state: dict[str, Any], *, write_policy: str) -> str:
    status = item_state.get("status")
    if status == "failed":
        return "failed"
    if status == "blocked":
        return "blocked"
    if item_state.get("expected_output") == "local_note":
        return "not_applicable"
    explicit_status = str(item_state.get("write_status", "")).strip()
    if explicit_status == "written" and str(item_state.get("zotero_note_key", "")).strip():
        return "written"
    if explicit_status in {"pending_prepare", "pending_write", "prepared_not_written", "failed", "blocked"}:
        return explicit_status
    if status == "succeeded" and str(item_state.get("write_payload", "")).strip():
        if write_policy == "zotero_write":
            return "pending_write"
        return "prepared_not_written"
    if status == "succeeded":
        return "blocked"
    return "blocked"


def _effective_write_policy(manifest: dict[str, Any]) -> str:
    items = manifest.get("items", [])
    if items and all(item.get("expected_output") == "local_note" for item in items):
        return "local_only"
    return str(manifest.get("write_policy", ""))


def _path_entries(item_state: dict[str, Any]) -> list[str]:
    keys = [
        "note_md",
        "note_html",
        "gate_report",
        "write_payload",
        "verify_report",
        "local_note_path",
        "local_gate_report",
        "takeaway_source_path",
    ]
    entries: list[str] = []
    for key in keys:
        value = str(item_state.get(key, "")).strip()
        if value:
            entries.append(f"{key}=local-only path: {value}")
    return entries


def build_report(manifest: dict[str, Any], state: dict[str, Any], *, reported_at: str) -> dict[str, Any]:
    manifest_by_id = _manifest_items_by_id(manifest)
    status_counts = Counter(str(item.get("status", "")) for item in state.get("items", []))
    expected_output_counts = Counter(str(item.get("expected_output", "")) for item in manifest.get("items", []))
    items: list[dict[str, Any]] = []
    for item_state in state.get("items", []):
        item_id = str(item_state.get("item_id", ""))
        manifest_item = manifest_by_id.get(item_id, {})
        items.append(
            {
                "item_id": item_id,
                "input_type": item_state.get("input_type", ""),
                "expected_output": item_state.get("expected_output", ""),
                "status": item_state.get("status", ""),
                "input_label": _input_label(manifest_item),
                "thirty_second_takeaway": item_state.get("thirty_second_takeaway", ""),
                "takeaway_source_type": item_state.get("takeaway_source_type", ""),
                "takeaway_source_path": item_state.get("takeaway_source_path", ""),
                "takeaway_source_sha256": item_state.get("takeaway_source_sha256", ""),
                "failure_reason": item_state.get("failure_reason", ""),
                "write_status": _write_status(item_state, write_policy=str(manifest.get("write_policy", ""))),
                "zotero_note_key": item_state.get("zotero_note_key", ""),
                "zotero_parent_key": item_state.get("zotero_parent_key", ""),
                "content_sha256": item_state.get("content_sha256", ""),
                "output_paths": _path_entries(item_state),
            }
        )
    return {
        "schema_version": "paper_reader_batch.report.v1",
        "batch_title": manifest["batch_title"],
        "created_at": manifest["created_at"],
        "reported_at": reported_at,
        "source_summary": manifest["source_summary"],
        "configured_concurrency": manifest["default_concurrency"],
        "write_policy": manifest["write_policy"],
        "effective_write_policy": _effective_write_policy(manifest),
        "total_items": len(manifest["items"]),
        "batch_status": state.get("batch_status", ""),
        "counts_by_status": dict(sorted(status_counts.items())),
        "counts_by_expected_output": dict(sorted(expected_output_counts.items())),
        "items": items,
    }


def _cell(value: Any) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text.replace("|", "\\|")


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# paper_reader_batch Report: {report['batch_title']}",
        "",
        f"- Created: {report['created_at']}",
        f"- Reported: {report['reported_at']}",
        f"- Source: {report['source_summary']['source_type']} - {report['source_summary']['description']}",
        f"- Configured concurrency: {report['configured_concurrency']}",
        f"- Write policy: {report['write_policy']}",
        f"- Effective write policy: {report['effective_write_policy']}",
        f"- Batch status: {report['batch_status']}",
        f"- Total items: {report['total_items']}",
        "",
        "## Counts",
        "",
        "| Metric | Count |",
        "| --- | --- |",
    ]
    for status, count in report["counts_by_status"].items():
        lines.append(f"| status:{_cell(status)} | {count} |")
    for output, count in report["counts_by_expected_output"].items():
        lines.append(f"| output:{_cell(output)} | {count} |")
    lines.extend(
        [
            "",
            "## Items",
            "",
            "| Item | Type | Status | Write | Zotero Note | 30 秒结论 | Failure | Paths |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in report["items"]:
        paths = "; ".join(item["output_paths"])
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(item["item_id"]),
                    _cell(item["input_type"]),
                    _cell(item["status"]),
                    _cell(item["write_status"]),
                    _cell(item["zotero_note_key"]),
                    _cell(item["thirty_second_takeaway"]),
                    _cell(item["failure_reason"]),
                    _cell(paths),
                ]
            )
            + " |"
        )
    lines.append("")
    if report.get("effective_write_policy") == "local_only":
        lines.append("Note: Local PDF inputs do not enter Zotero write-through; output paths are local-only path references from this machine.")
    else:
        lines.append("Note: output paths are local-only path references from this machine.")
    return "\n".join(lines) + "\n"
