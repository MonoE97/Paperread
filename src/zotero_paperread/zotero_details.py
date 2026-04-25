from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from zotero_paperread.note import next_same_day_version_suffix
from zotero_paperread.workflow import select_pdf_attachment

CODEX_SUMMARY_PREFIX = "[Codex Summary]"
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")


class _HeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._active_heading: str | None = None
        self._parts: list[str] = []
        self.titles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"h1", "h2"}:
            self._active_heading = tag.lower()
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._active_heading is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._active_heading == tag.lower():
            title = _normalize_heading_text("".join(self._parts))
            if title:
                self.titles.append(title)
            self._active_heading = None
            self._parts = []


def _normalize_heading_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _html_heading_titles(note: str) -> list[str]:
    parser = _HeadingParser()
    parser.feed(note)
    parser.close()
    return parser.titles


def _markdown_heading_titles(note: str) -> list[str]:
    titles: list[str] = []
    for line in note.splitlines():
        match = MARKDOWN_HEADING_RE.match(line)
        if not match:
            continue
        title = _normalize_heading_text(match.group("title"))
        if title:
            titles.append(title)
    return titles


def codex_summary_titles_from_details(details: dict[str, Any]) -> list[str]:
    """Extract generated Codex note titles from Zotero item details."""
    notes = details.get("notes", [])
    if not isinstance(notes, list):
        return []

    titles: list[str] = []
    for note in notes:
        if not isinstance(note, str):
            continue
        for title in [*_html_heading_titles(note), *_markdown_heading_titles(note)]:
            if title.startswith(CODEX_SUMMARY_PREFIX):
                titles.append(title)
    return titles


def next_version_suffix_from_details(
    details: dict[str, Any],
    *,
    paper_title: str,
    generated_date: str,
) -> str:
    """Return the next same-day generated-note suffix for a Zotero item."""
    return next_same_day_version_suffix(
        codex_summary_titles_from_details(details),
        paper_title=paper_title,
        generated_date=generated_date,
    )


def primary_pdf_path_from_details(details: dict[str, Any]) -> str:
    """Return the selected local main-PDF path from Zotero item details."""
    attachments = details.get("attachments", [])
    if not isinstance(attachments, list):
        return ""
    pdf_attachment = select_pdf_attachment([item for item in attachments if isinstance(item, dict)])
    if pdf_attachment is None:
        return ""
    path = pdf_attachment.get("path", "")
    return str(path) if path else ""
