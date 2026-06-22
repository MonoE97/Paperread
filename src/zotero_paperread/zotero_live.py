from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import urlopen


FetchJson = Callable[[str], object]


class LiveNoteVerificationError(ValueError):
    def __init__(self, errors: list[str], report: dict[str, Any]):
        super().__init__("; ".join(errors))
        self.errors = errors
        self.report = report


class _HeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._current_tag = ""
        self._parts: list[str] = []
        self.h1 = ""
        self.headings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"h1", "h2"}:
            self._current_tag = lowered
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._current_tag:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == self._current_tag:
            heading = " ".join("".join(self._parts).split())
            if lowered == "h1" and not self.h1:
                self.h1 = heading
            elif lowered == "h2" and heading:
                self.headings.append(heading)
            self._current_tag = ""
            self._parts = []


def fetch_json_url(url: str) -> object:
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def _parse_headings(note_html: str) -> tuple[str, list[str]]:
    parser = _HeadingParser()
    parser.feed(note_html)
    parser.close()
    return parser.h1, parser.headings


def _h1_title(note_html: str) -> str:
    title, _headings = _parse_headings(note_html)
    return title


def fetch_item_children_notes(
    item_key: str,
    *,
    base_url: str = "http://127.0.0.1:23119",
    fetch_json: FetchJson = fetch_json_url,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    start = 0
    while True:
        url = _api_url(
            base_url,
            f"/api/users/0/items/{quote(item_key)}/children?format=json&limit={page_size}&start={start}",
        )
        payload = fetch_json(url)
        if not isinstance(payload, list):
            raise ValueError("zotero children response is not a list")

        for item in payload:
            if not isinstance(item, dict):
                continue
            data = item.get("data", {})
            if not isinstance(data, dict) or data.get("itemType") != "note":
                continue
            note_html = str(data.get("note", ""))
            tags = [
                str(tag.get("tag"))
                for tag in data.get("tags", [])
                if isinstance(tag, dict) and str(tag.get("tag", "")).strip()
            ]
            notes.append(
                {
                    "key": str(item.get("key", "")),
                    "parentItem": str(data.get("parentItem", "")),
                    "title": _h1_title(note_html),
                    "note": note_html,
                    "tags": tags,
                }
            )

        if len(payload) < page_size:
            break
        start += page_size
    return notes


def fetch_note_snapshot(
    note_key: str,
    *,
    base_url: str = "http://127.0.0.1:23119",
    fetch_json: FetchJson = fetch_json_url,
) -> dict[str, Any]:
    url = _api_url(base_url, f"/api/users/0/items/{quote(note_key)}?format=json")
    payload = fetch_json(url)
    if not isinstance(payload, dict):
        raise ValueError("zotero note response is not an object")
    return payload


def refresh_details_with_live_notes(
    details: dict[str, Any],
    *,
    live_notes: list[dict[str, Any]],
    base_url: str = "http://127.0.0.1:23119",
    refreshed_at: str | None = None,
) -> dict[str, Any]:
    refreshed = dict(details)
    titles = [str(note.get("title", "")).strip() for note in live_notes if str(note.get("title", "")).strip()]
    refreshed["notes"] = [f"<h1>{escape(title)}</h1>" for title in titles]
    paperread = dict(refreshed.get("_paperread", {})) if isinstance(refreshed.get("_paperread"), dict) else {}
    enrichment = dict(paperread.get("enrichment", {})) if isinstance(paperread.get("enrichment"), dict) else {}
    enrichment["live_notes"] = {
        "status": "refreshed",
        "source": "zotero_local_api_readonly",
        "item_key": str(details.get("key", "")),
        "base_url": base_url.rstrip("/"),
        "refreshed_at": refreshed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "note_count": len(live_notes),
        "note_keys": [str(note.get("key", "")) for note in live_notes if str(note.get("key", ""))],
        "titles": titles,
    }
    paperread["enrichment"] = enrichment
    refreshed["_paperread"] = paperread
    return refreshed


def verify_note_snapshot(
    snapshot: dict[str, Any],
    *,
    expected_parent: str,
    expected_title: str,
    required_headings: list[str],
    forbidden_headings: list[str],
    expected_tags: list[str],
    min_content_length: int,
    expected_content_sha256: str = "",
) -> dict[str, Any]:
    data = snapshot.get("data", {})
    if not isinstance(data, dict):
        data = {}
    note = str(data.get("note", ""))
    tags = [
        str(tag.get("tag"))
        for tag in data.get("tags", [])
        if isinstance(tag, dict) and str(tag.get("tag", "")).strip()
    ]
    title, headings = _parse_headings(note)
    content_sha256 = hashlib.sha256(note.encode("utf-8")).hexdigest()
    errors: list[str] = []

    if data.get("itemType") != "note":
        errors.append(f"itemType mismatch: expected note, got {data.get('itemType')}")
    parent = str(data.get("parentItem", ""))
    if parent != expected_parent:
        errors.append(f"parent mismatch: expected {expected_parent}, got {parent}")
    if expected_title and title != expected_title:
        errors.append(f"title mismatch: expected {expected_title}, got {title}")
    if len(note) < min_content_length:
        errors.append(f"content too short: expected at least {min_content_length}, got {len(note)}")
    expected_hash = expected_content_sha256.strip()
    if expected_hash and content_sha256 != expected_hash:
        errors.append(f"content hash mismatch: expected {expected_hash}, got {content_sha256}")
    for heading in required_headings:
        if heading not in headings:
            errors.append(f"missing required heading: {heading}")
    for heading in forbidden_headings:
        if heading in headings:
            errors.append(f"forbidden heading present: {heading}")
    for tag in expected_tags:
        if tag not in tags:
            errors.append(f"missing tag: {tag}")

    report = {
        "status": "failed" if errors else "passed",
        "errors": errors,
        "noteKey": str(snapshot.get("key", "")),
        "parentKey": parent,
        "title": title,
        "contentLength": len(note),
        "contentSha256": content_sha256,
        "headings": headings,
        "tags": tags,
    }
    if errors:
        raise LiveNoteVerificationError(errors, report)
    return report
