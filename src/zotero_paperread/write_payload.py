from __future__ import annotations

from pathlib import Path
from typing import Any

from zotero_paperread.note_hash import canonicalize_note_html_for_hash, note_html_sha256


def build_write_payload(gate_report: dict[str, Any]) -> dict[str, Any]:
    """Prepare safe metadata for a Zotero write_note call without writing."""
    if gate_report.get("status") != "write_ready":
        raise ValueError("gate report is not write_ready")
    note_html_path = Path(str(gate_report.get("note_html_path", "")))
    content = note_html_path.read_text(encoding="utf-8")
    note_title = str(gate_report.get("note_title", ""))
    version_suffix = str(gate_report.get("version_suffix", ""))
    title_prefix = note_title[:120]
    canonical_content = canonicalize_note_html_for_hash(content)
    content_sha256 = note_html_sha256(content)
    parent_key = str(gate_report.get("parentKey", ""))
    tags = [str(tag) for tag in gate_report.get("tags", []) if str(tag)]
    return {
        "action": "create",
        "parentKey": parent_key,
        "note_html_path": str(note_html_path),
        "contentLength": len(canonical_content),
        "noteTitle": note_title,
        "versionSuffix": version_suffix,
        "contentSha256": content_sha256,
        "titlePrefix": title_prefix,
        "contentPreview": content[:240],
        "tags": tags,
        "required_readback_checks": {
            "parentKey": parent_key,
            "tags": tags,
            "expectedTitle": note_title,
            "versionSuffix": version_suffix,
            "contentSha256": content_sha256,
            "titlePrefix": title_prefix,
            "contentLengthAtLeast": max(len(canonical_content) - 20, 0),
        },
    }
