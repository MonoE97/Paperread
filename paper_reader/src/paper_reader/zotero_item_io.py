from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paper_reader.zotero_sqlite import DEFAULT_ZOTERO_SQLITE_PATH, lookup_extra_by_item_key


def normalize_item_details_payload(payload: Any) -> dict[str, Any]:
    """Return a Zotero item-details dict from raw MCP or plain JSON payload."""
    raw = payload
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("result"), dict)
        and isinstance(raw["result"].get("content"), list)
    ):
        raw = raw["result"]["content"]

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


def _paper_reader_meta(normalized: dict[str, Any]) -> dict[str, Any]:
    meta = normalized.get("_paper_reader")
    if not isinstance(meta, dict):
        meta = {}
        normalized["_paper_reader"] = meta
    meta.setdefault("warnings", [])
    meta.setdefault("enrichment", {})
    return meta


def enrich_missing_extra_from_sqlite(
    normalized: dict[str, Any],
    *,
    sqlite_path: Path = DEFAULT_ZOTERO_SQLITE_PATH,
    enabled: bool = True,
) -> str:
    """Fill missing Zotero Extra from a read-only SQLite fallback."""
    existing_extra = str(normalized.get("extra", "")).strip()
    if existing_extra:
        return "mcp_payload"
    if not enabled:
        return "not_requested"

    lookup = lookup_extra_by_item_key(str(normalized["key"]), sqlite_path=sqlite_path)
    meta = _paper_reader_meta(normalized)
    warnings = meta.setdefault("warnings", [])
    for warning in lookup.get("warnings", []):
        warning_text = str(warning).strip()
        if warning_text and warning_text not in warnings:
            warnings.append(warning_text)

    extra = str(lookup.get("extra", "")).strip()
    if not extra:
        return "missing"

    normalized["extra"] = extra
    provenance = dict(lookup.get("provenance", {}))
    meta.setdefault("enrichment", {})["extra"] = provenance
    return str(provenance.get("source", "zotero_sqlite"))


def write_item_details_files(
    payload: Any,
    *,
    normalized_path: Path,
    raw_path: Path | None = None,
    sqlite_path: Path = DEFAULT_ZOTERO_SQLITE_PATH,
    sqlite_extra_fallback: bool = True,
) -> dict[str, Any]:
    """Write raw and normalized Zotero item details for a run bundle."""
    normalized = normalize_item_details_payload(payload)
    extra_source = enrich_missing_extra_from_sqlite(
        normalized,
        sqlite_path=sqlite_path,
        enabled=sqlite_extra_fallback,
    )
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if raw_path is not None:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "item_key": normalized["key"],
        "title": normalized["title"],
        "extra_source": extra_source,
        "normalized_path": str(normalized_path),
        "raw_path": str(raw_path) if raw_path is not None else None,
    }
