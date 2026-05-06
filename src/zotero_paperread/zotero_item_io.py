from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def normalize_item_details_payload(payload: Any) -> dict[str, Any]:
    """Return a Zotero item-details dict from raw MCP or plain JSON payload."""
    raw = payload
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and raw[0].get("type") == "text":
        text = raw[0].get("text")
        if not isinstance(text, str):
            raise ValueError("mcp text payload is not a string")
        raw = json.loads(text)

    if not isinstance(raw, dict):
        raise ValueError("item details payload must be a JSON object")

    key = str(raw.get("key", "")).strip()
    if not key:
        raise ValueError("item details missing key")
    title = str(raw.get("title", "")).strip()
    if not title:
        raise ValueError("item details missing title")

    normalized = dict(raw)
    attachments = normalized.get("attachments", [])
    if not isinstance(attachments, list):
        normalized["attachments"] = []
    notes = normalized.get("notes", [])
    if not isinstance(notes, list):
        normalized["notes"] = []
    return normalized


def write_item_details_files(
    payload: Any,
    *,
    normalized_path: Path,
    raw_path: Path | None = None,
) -> dict[str, Any]:
    """Write raw and normalized Zotero item details for a run bundle."""
    normalized = normalize_item_details_payload(payload)
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if raw_path is not None:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "item_key": normalized["key"],
        "title": normalized["title"],
        "normalized_path": str(normalized_path),
        "raw_path": str(raw_path) if raw_path is not None else None,
    }
