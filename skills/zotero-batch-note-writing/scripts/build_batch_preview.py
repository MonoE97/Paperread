#!/usr/bin/env python3
"""Build a compact Zotero batch write preview from a manifest."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any


MAX_TABLE_CELL_LENGTH = 160
SUSPICIOUS_TEXT_LENGTH = 80

SUSPICIOUS_TEXT_MARKERS = [
    "traceback (most recent call last)",
    "stack trace",
    'file "',
    "error_detail",
    "assistant:",
    "user:",
    "system:",
    "<html",
    "<body",
    "<h1",
    "```",
    "| --- |",
    "evidence_summary",
    "tags:",
]

COUNT_KEYS = [
    "discovered",
    "skipped_existing_summary",
    "skipped_invalid_item",
    "blocked_duplicate_normalized_title",
    "queued",
    "prepared",
    "summarized",
    "reviewed",
    "gated",
    "previewed",
    "write_ready",
    "written",
    "verified",
    "blocked",
    "failed",
]

COMPLETED_WRITE_STATUSES = {"write_ready", "written", "verified"}


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError("manifest root must be a JSON object")
    return manifest


def validated_items(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if "items" not in manifest:
        raise ValueError("manifest.items is required")
    items = manifest["items"]
    if not isinstance(items, list):
        raise ValueError("manifest.items must be a list")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"manifest.items[{index}] must be an object")
    return items


def validated_counts(manifest: dict[str, Any]) -> dict[str, int | float]:
    if "counts" not in manifest:
        return {}
    counts = manifest["counts"]
    if not isinstance(counts, dict):
        raise ValueError("manifest.counts must be an object")

    safe_counts: dict[str, int | float] = {}
    for key in COUNT_KEYS:
        if key not in counts:
            continue
        value = counts[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"manifest.counts.{key} must be numeric")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"manifest.counts.{key} must be finite")
        safe_counts[key] = value
    return safe_counts


def looks_like_long_diagnostic(text: str) -> bool:
    lowered = text.lower()
    if "traceback (most recent call last)" in lowered:
        return True
    if len(text) < SUSPICIOUS_TEXT_LENGTH and text.count("\n") < 3:
        return False
    return any(marker in lowered for marker in SUSPICIOUS_TEXT_MARKERS)


def compact_preview_text(value: Any, max_length: int = MAX_TABLE_CELL_LENGTH) -> str:
    text = str(value)
    if looks_like_long_diagnostic(text):
        return "[redacted diagnostic text]"

    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3].rstrip()}..."


def format_table_cell(value: Any) -> str:
    return compact_preview_text(value).replace("|", "\\|")


def format_inline_value(value: Any) -> str:
    return compact_preview_text(value).replace("`", "'")


def format_note_tags(value: Any) -> str:
    if not isinstance(value, list):
        return format_table_cell(value)

    tags: list[str] = []
    for tag in value:
        if isinstance(tag, str):
            text = tag.strip()
        elif isinstance(tag, dict):
            raw_text = tag.get("tag", tag.get("name"))
            text = raw_text.strip() if isinstance(raw_text, str) else ""
        else:
            text = ""
        if text:
            tags.append(text)
    return format_table_cell(", ".join(tags))


def captured_secondary_count(item: dict[str, Any]) -> int:
    sources = item.get("secondary_sources", [])
    if not isinstance(sources, list):
        return 0
    return sum(
        1
        for source in sources
        if isinstance(source, dict) and source.get("status") == "captured"
    )


def render_counts(counts: dict[str, Any]) -> list[str]:
    return [f"- `{key}`: {counts[key]}" for key in COUNT_KEYS if key in counts]


def render_preview(manifest: dict[str, Any]) -> str:
    items = validated_items(manifest)
    counts = validated_counts(manifest)

    lines = [
        "# Zotero Batch Write Preview",
        "",
        f"- Batch: `{format_inline_value(manifest.get('batch_id', ''))}`",
        f"- Target collection: `{format_inline_value(manifest.get('target_collection_path', ''))}`",
        f"- Generated date: `{format_inline_value(manifest.get('generated_date', ''))}`",
        f"- State: `{format_inline_value(manifest.get('state', ''))}`",
        "",
        "## Counts",
        "",
    ]
    lines.extend(render_counts(counts))

    lines.extend(
        [
            "",
            "## Write-Ready Items",
            "",
            "| Item Key | Title | Review Status | Trust Status | Secondary Captured | Note Title | Note Tags |",
            "| --- | --- | --- | --- | ---: | --- | --- |",
        ]
    )

    for item in items:
        if item.get("status") != "write_ready":
            continue
        lines.append(
            "| {item_key} | {title} | {review_status} | {trust_status} | {secondary_count} | {note_title} | {note_tags} |".format(
                item_key=format_table_cell(item.get("item_key", "")),
                title=format_table_cell(item.get("title", "")),
                review_status=format_table_cell(item.get("review_status", "")),
                trust_status=format_table_cell(item.get("trust_status", "")),
                secondary_count=captured_secondary_count(item),
                note_title=format_table_cell(item.get("note_title", "")),
                note_tags=format_note_tags(item.get("note_tags", [])),
            )
        )

    lines.extend(
        [
            "",
            "## Blocked Or Skipped Items",
            "",
            "| Item Key | Status | Blocked Reason | Title |",
            "| --- | --- | --- | --- |",
        ]
    )

    for item in items:
        status = str(item.get("status", ""))
        if status in COMPLETED_WRITE_STATUSES:
            continue
        lines.append(
            "| {item_key} | {status} | {blocked_reason} | {title} |".format(
                item_key=format_table_cell(item.get("item_key", "")),
                status=format_table_cell(status),
                blocked_reason=format_table_cell(item.get("blocked_reason", "")),
                title=format_table_cell(item.get("title", "")),
            )
        )

    lines.extend(
        [
            "",
            "## Write Boundary",
            "",
            "No Zotero write has been performed by this preview. Continuation requires explicit user confirmation.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a compact Zotero batch write preview."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        manifest = load_manifest(args.manifest)
        args.output.write_text(render_preview(manifest), encoding="utf-8")
    except json.JSONDecodeError as exc:
        print(
            f"error: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
