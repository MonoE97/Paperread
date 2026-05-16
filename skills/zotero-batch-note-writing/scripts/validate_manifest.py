#!/usr/bin/env python3
"""Validate a zotero-batch-note-writing manifest."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REQUIRED_TOP_LEVEL_FIELDS = {
    "batch_id",
    "target_collection_path",
    "generated_date",
    "state",
    "items",
    "counts",
}

REQUIRED_TOP_LEVEL_STRING_FIELDS = {
    "batch_id",
    "target_collection_path",
    "generated_date",
    "state",
}

REQUIRED_ITEM_FIELDS = {
    "item_key",
    "title",
    "normalized_title",
    "source_collection_path",
    "status",
    "run_dir",
    "existing_summary_notes",
    "secondary_sources",
    "primary_pdf_status",
    "trust_status",
    "review_status",
    "note_title",
    "note_tags",
    "write_payload",
    "written_note_key",
    "blocked_reason",
    "error_detail",
}

STRING_ITEM_FIELDS = {
    "item_key",
    "title",
    "normalized_title",
    "source_collection_path",
    "status",
    "run_dir",
    "primary_pdf_status",
    "trust_status",
    "review_status",
    "note_title",
    "write_payload",
    "written_note_key",
    "blocked_reason",
    "error_detail",
}

LIST_ITEM_FIELDS = {
    "existing_summary_notes",
    "secondary_sources",
    "note_tags",
}

VALID_STATUSES = {
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
}

RUN_DIR_REQUIRED_STATUSES = {
    "queued",
    "prepared",
    "summarized",
    "reviewed",
    "gated",
    "previewed",
    "write_ready",
    "written",
    "verified",
}


def trimmed_string(item: dict[str, Any], field: str) -> str | None:
    value = item.get(field)
    if not isinstance(value, str):
        return None
    return value.strip()


def emit(ok: bool, errors: list[str]) -> None:
    payload = {"ok": ok, "errors": errors}
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def load_manifest(path: Path) -> tuple[Any | None, list[str]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle), []
    except OSError as exc:
        return None, [f"unable to read manifest: {exc}"]
    except json.JSONDecodeError as exc:
        return None, [f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"]


def validate_item(
    item: Any,
    index: int,
    errors: list[str],
    seen_item_keys: set[str],
    normalized_title_groups: dict[str, list[tuple[int, str]]],
    status_counts: Counter[str],
) -> None:
    prefix = f"items[{index}]"
    if not isinstance(item, dict):
        errors.append(f"{prefix} must be an object")
        return

    missing = sorted(REQUIRED_ITEM_FIELDS - set(item))
    if missing:
        errors.append(f"{prefix} missing required fields: {', '.join(missing)}")

    for field in sorted(STRING_ITEM_FIELDS & set(item)):
        if not isinstance(item[field], str):
            errors.append(f"{prefix}.{field} must be a string")

    for field in sorted(LIST_ITEM_FIELDS & set(item)):
        if not isinstance(item[field], list):
            errors.append(f"{prefix}.{field} must be a list")

    item_key = trimmed_string(item, "item_key")
    if not item_key:
        errors.append(f"{prefix}.item_key must be non-empty")
    else:
        if item_key in seen_item_keys:
            errors.append(f"{prefix}.item_key must be unique: {item_key}")
        seen_item_keys.add(item_key)

    status = trimmed_string(item, "status")
    if not status:
        errors.append(f"{prefix}.status must be non-empty")
    else:
        status_counts[status] += 1
        if status not in VALID_STATUSES:
            errors.append(f"{prefix}.status is invalid: {status}")

    if status in RUN_DIR_REQUIRED_STATUSES and not trimmed_string(item, "run_dir"):
        errors.append(f"{prefix}.run_dir is required for status {status}")

    if status == "write_ready" and not trimmed_string(item, "write_payload"):
        errors.append(f"{prefix}.write_payload is required for status write_ready")

    if status == "write_ready" and not trimmed_string(item, "note_title"):
        errors.append(f"{prefix}.note_title is required for status write_ready")

    if status == "write_ready":
        note_tags = item.get("note_tags")
        if not isinstance(note_tags, list) or not note_tags:
            errors.append(
                f"{prefix}.note_tags must contain at least one tag for status write_ready"
            )
        elif any(not isinstance(tag, str) or not tag.strip() for tag in note_tags):
            errors.append(
                f"{prefix}.note_tags must contain only non-empty string tags for status write_ready"
            )

    if status == "verified" and not trimmed_string(item, "written_note_key"):
        errors.append(f"{prefix}.written_note_key is required for status verified")

    normalized_title = trimmed_string(item, "normalized_title")
    if not normalized_title:
        errors.append(f"{prefix}.normalized_title must be a non-empty string")
    else:
        normalized_title_groups[normalized_title].append((index, status or ""))


def validate_counts(
    counts: Any,
    items: list[Any],
    status_counts: Counter[str],
    errors: list[str],
) -> None:
    if not isinstance(counts, dict):
        errors.append("counts must be an object")
        return

    if "discovered" in counts and counts["discovered"] != len(items):
        errors.append(
            f"counts.discovered mismatch: expected {len(items)}, got {counts['discovered']}"
        )

    for status in sorted(VALID_STATUSES - {"discovered"}):
        if status in counts and counts[status] != status_counts.get(status, 0):
            errors.append(
                f"counts.{status} mismatch: expected {status_counts.get(status, 0)}, "
                f"got {counts[status]}"
            )


def validate_manifest(manifest: Any) -> list[str]:
    errors: list[str] = []

    if not isinstance(manifest, dict):
        return ["manifest root must be a JSON object"]

    missing = sorted(REQUIRED_TOP_LEVEL_FIELDS - set(manifest))
    if missing:
        errors.append(f"manifest missing required fields: {', '.join(missing)}")

    for field in sorted(REQUIRED_TOP_LEVEL_STRING_FIELDS & set(manifest)):
        if not isinstance(manifest[field], str) or not manifest[field].strip():
            errors.append(f"{field} must be a non-empty string")

    items = manifest.get("items")
    if not isinstance(items, list):
        errors.append("items must be a list")
        items = []

    seen_item_keys: set[str] = set()
    normalized_title_groups: dict[str, list[tuple[int, str]]] = defaultdict(list)
    status_counts: Counter[str] = Counter()

    for index, item in enumerate(items):
        validate_item(
            item=item,
            index=index,
            errors=errors,
            seen_item_keys=seen_item_keys,
            normalized_title_groups=normalized_title_groups,
            status_counts=status_counts,
        )

    for normalized_title, group in sorted(normalized_title_groups.items()):
        if len(group) <= 1:
            continue
        if any(status != "blocked_duplicate_normalized_title" for _, status in group):
            locations = ", ".join(f"items[{index}]" for index, _ in group)
            errors.append(
                "duplicate normalized_title group must be blocked_duplicate_normalized_title: "
                f"{normalized_title!r} at {locations}"
            )

    validate_counts(manifest.get("counts"), items, status_counts, errors)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)

    manifest, load_errors = load_manifest(args.manifest)
    if load_errors:
        emit(False, load_errors)
        return 1

    errors = validate_manifest(manifest)
    emit(not errors, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
