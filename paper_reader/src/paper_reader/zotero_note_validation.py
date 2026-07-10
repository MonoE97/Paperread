from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from paper_reader.contracts import PaperReaderWriteAuthorization, VerificationCheck
from paper_reader.note_hash import canonicalize_note_html_for_hash, note_html_sha256
from paper_reader.zotero_live import _parse_headings


@dataclass(frozen=True, slots=True)
class NoteEvaluation:
    verified: bool
    content_sha256: str
    content_length: int
    checks: tuple[VerificationCheck, ...]


def _check(
    name: str,
    passed: bool,
    *,
    expected: Any,
    actual: Any,
    message: str,
) -> VerificationCheck:
    return VerificationCheck(
        name=name,
        passed=passed,
        expected=expected,
        actual=actual,
        message=None if passed else message,
    )


def evaluate_note_snapshot(
    snapshot: dict[str, Any],
    *,
    authorization: PaperReaderWriteAuthorization,
    note_key: str,
) -> NoteEvaluation:
    data = snapshot.get("data")
    if not isinstance(data, dict):
        data = {}
    note_html = str(data.get("note", ""))
    canonical_html = canonicalize_note_html_for_hash(note_html)
    content_sha256 = note_html_sha256(note_html)
    content_length = len(canonical_html)
    title, headings = _parse_headings(note_html)
    snapshot_key = str(snapshot.get("key", "")).strip()
    data_key = str(data.get("key", "")).strip()
    parent_key = str(data.get("parentItem", "")).strip()
    tags = {
        str(item.get("tag", "")).strip()
        for item in data.get("tags", [])
        if isinstance(item, dict) and str(item.get("tag", "")).strip()
    }
    expected_tags = set(authorization.tags)
    missing_headings = [item for item in authorization.required_headings if item not in headings]
    forbidden_headings = [item for item in authorization.forbidden_headings if item in headings]
    checks = (
        _check(
            "note_key",
            snapshot_key == note_key and data_key == note_key,
            expected=note_key,
            actual={"snapshot": snapshot_key, "data": data_key},
            message="readback note key does not match the requested key",
        ),
        _check(
            "item_type",
            data.get("itemType") == "note",
            expected="note",
            actual=str(data.get("itemType", "")),
            message="readback itemType is not note",
        ),
        _check(
            "parent_key",
            parent_key == authorization.target.parent_key,
            expected=authorization.target.parent_key,
            actual=parent_key,
            message="readback parent key does not match authorization",
        ),
        _check(
            "note_title",
            title == authorization.note_title,
            expected=authorization.note_title,
            actual=title,
            message="readback H1 title does not match authorization",
        ),
        _check(
            "tag_set",
            tags == expected_tags,
            expected=sorted(expected_tags),
            actual=sorted(tags),
            message="readback tags are not the complete authorized set",
        ),
        _check(
            "required_headings",
            not missing_headings,
            expected=list(authorization.required_headings),
            actual=headings,
            message=f"readback is missing required headings: {missing_headings}",
        ),
        _check(
            "forbidden_headings",
            not forbidden_headings,
            expected=[],
            actual=forbidden_headings,
            message=f"readback contains forbidden headings: {forbidden_headings}",
        ),
        _check(
            "minimum_content_length",
            content_length >= authorization.minimum_content_length,
            expected=authorization.minimum_content_length,
            actual=content_length,
            message="readback content is shorter than the authorized minimum",
        ),
        _check(
            "content_length",
            content_length == authorization.content_length,
            expected=authorization.content_length,
            actual=content_length,
            message="readback canonical content length changed",
        ),
        _check(
            "content_sha256",
            content_sha256 == authorization.content_sha256,
            expected=authorization.content_sha256,
            actual=content_sha256,
            message="readback canonical HTML hash changed",
        ),
    )
    return NoteEvaluation(
        verified=all(item.passed for item in checks),
        content_sha256=content_sha256,
        content_length=content_length,
        checks=checks,
    )


__all__ = ["NoteEvaluation", "evaluate_note_snapshot"]
