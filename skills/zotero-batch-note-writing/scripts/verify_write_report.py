#!/usr/bin/env python3
"""Verify Zotero batch write report readbacks."""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from typing import Any


DEFAULT_EXPECTED_TAGS = ["codex-summary", "paper-summary"]


def emit(ok: bool, verified: list[str], errors: list[str]) -> None:
    payload = {"ok": ok, "verified": verified, "errors": errors}
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def load_report(path: Path) -> tuple[Any | None, list[str]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle), []
    except OSError as exc:
        return None, [f"unable to read report: {exc}"]
    except json.JSONDecodeError as exc:
        return None, [
            f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ]


def normalized_title(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def non_empty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def expected_tags(write: dict[str, Any], prefix: str, errors: list[str]) -> set[str]:
    if "expected_note_tags" not in write or write["expected_note_tags"] is None:
        raw_tags = DEFAULT_EXPECTED_TAGS
    else:
        raw_tags = write["expected_note_tags"]

    if not isinstance(raw_tags, list):
        errors.append(f"{prefix}.expected_note_tags must be a list when present")
        return set()
    if not raw_tags:
        return set(DEFAULT_EXPECTED_TAGS)

    tags: set[str] = set()
    for index, tag in enumerate(raw_tags):
        if not isinstance(tag, str) or not tag.strip():
            errors.append(
                f"{prefix}.expected_note_tags[{index}] must be a non-empty string"
            )
            continue
        tags.add(tag.strip())
    return tags


def readback_tags(note: dict[str, Any], prefix: str, errors: list[str]) -> set[str]:
    raw_tags = note.get("tags", [])
    if not isinstance(raw_tags, list):
        errors.append(f"{prefix}.tags must be a list")
        return set()

    tags: set[str] = set()
    for index, tag in enumerate(raw_tags):
        if isinstance(tag, str):
            value = tag.strip()
        elif isinstance(tag, dict):
            raw_value = tag.get("tag", tag.get("name"))
            value = raw_value.strip() if isinstance(raw_value, str) else ""
        else:
            value = ""

        if not value:
            errors.append(f"{prefix}.tags[{index}] must be a non-empty tag")
            continue
        tags.add(value)
    return tags


def child_note_key(note: Any) -> str | None:
    if not isinstance(note, dict):
        return None
    return non_empty_string(note.get("key"))


def validate_readback_parent_identity(
    item_key: str | None,
    readback: dict[str, Any],
    prefix: str,
    errors: list[str],
) -> None:
    if not item_key:
        return

    for field in ("item_key", "parent_item_key"):
        if field not in readback:
            continue
        readback_item_key = non_empty_string(readback.get(field))
        if not readback_item_key:
            errors.append(
                f"{prefix}.readback.{field} must be a non-empty string when present"
            )
        elif readback_item_key != item_key:
            errors.append(
                f"{prefix}.readback.{field} parent item mismatch: "
                f"expected {item_key!r}, got {readback_item_key!r}"
            )


def verify_write(write: Any, index: int, errors: list[str]) -> str | None:
    prefix = f"writes[{index}]"
    before_error_count = len(errors)

    if not isinstance(write, dict):
        errors.append(f"{prefix} must be an object")
        return None

    item_key = non_empty_string(write.get("item_key"))
    if not item_key:
        errors.append(f"{prefix}.item_key must be non-empty")

    write_response = write.get("write_response")
    if not isinstance(write_response, dict):
        errors.append(f"{prefix}.write_response must be an object")
        write_response = {}

    readback = write.get("readback")
    if not isinstance(readback, dict):
        errors.append(f"{prefix}.readback must be an object")
        readback = {}

    validate_readback_parent_identity(item_key, readback, prefix, errors)

    note_key = non_empty_string(write_response.get("key"))
    if not note_key:
        errors.append(f"{prefix}.write_response.key must be non-empty")

    child_notes = readback.get("child_notes")
    if not isinstance(child_notes, list):
        errors.append(f"{prefix}.readback.child_notes must be a list")
        child_notes = []

    matched_note: dict[str, Any] | None = None
    if note_key:
        for child_index, child_note in enumerate(child_notes):
            if not isinstance(child_note, dict):
                errors.append(
                    f"{prefix}.readback.child_notes[{child_index}] must be an object"
                )
                continue
            if child_note_key(child_note) == note_key:
                matched_note = child_note
                break
        if matched_note is None:
            errors.append(f"{prefix}.readback.child_notes missing note key {note_key}")

    expected_title = normalized_title(write.get("expected_note_title"))
    if not expected_title:
        errors.append(f"{prefix}.expected_note_title must be a non-empty string")

    if matched_note is not None and expected_title:
        actual_title = normalized_title(matched_note.get("title"))
        if not actual_title:
            errors.append(
                f"{prefix}.readback.child_notes note {note_key} title must be non-empty"
            )
        elif actual_title != expected_title:
            errors.append(
                f"{prefix}.readback.child_notes note {note_key} title mismatch: "
                f"expected {expected_title!r}, got {actual_title!r}"
            )

    required_tags = expected_tags(write, prefix, errors)
    if matched_note is not None:
        actual_tags = readback_tags(
            matched_note,
            f"{prefix}.readback.child_notes note {note_key}",
            errors,
        )
        missing_tags = sorted(required_tags - actual_tags)
        if missing_tags:
            errors.append(
                f"{prefix}.readback.child_notes note {note_key} missing tags: "
                f"{', '.join(missing_tags)}"
            )

    if len(errors) == before_error_count and item_key:
        return item_key
    return None


def verify_report(report: Any) -> tuple[list[str], list[str]]:
    verified: list[str] = []
    errors: list[str] = []

    if not isinstance(report, dict):
        return verified, ["report root must be a JSON object"]

    writes = report.get("writes")
    if not isinstance(writes, list):
        return verified, ["writes must be a list"]

    for index, write in enumerate(writes):
        item_key = verify_write(write, index, errors)
        if item_key:
            verified.append(item_key)

    return verified, errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Zotero batch write report readbacks."
    )
    parser.add_argument("report", type=Path)
    args = parser.parse_args()

    report, load_errors = load_report(args.report)
    if load_errors:
        emit(False, [], load_errors)
        return 1

    verified, errors = verify_report(report)
    emit(not errors, verified, errors)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
