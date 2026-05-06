from __future__ import annotations

from pathlib import Path
from typing import Any


def build_write_payload(gate_report: dict[str, Any]) -> dict[str, Any]:
    """Prepare safe metadata for a Zotero write_note call without writing."""
    if gate_report.get("status") != "write_ready":
        raise ValueError("gate report is not write_ready")
    note_html_path = Path(str(gate_report.get("note_html_path", "")))
    content = note_html_path.read_text(encoding="utf-8")
    title_prefix = str(gate_report.get("note_title", ""))[:120]
    parent_key = str(gate_report.get("parentKey", ""))
    tags = [str(tag) for tag in gate_report.get("tags", []) if str(tag)]
    return {
        "action": "create",
        "parentKey": parent_key,
        "note_html_path": str(note_html_path),
        "contentLength": len(content),
        "titlePrefix": title_prefix,
        "contentPreview": content[:240],
        "tags": tags,
        "required_readback_checks": {
            "parentKey": parent_key,
            "tags": tags,
            "titlePrefix": title_prefix,
            "contentLengthAtLeast": max(len(content) - 20, 0),
        },
    }
