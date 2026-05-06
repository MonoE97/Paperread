from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Literal

from bs4 import BeautifulSoup

from zotero_paperread.note import render_note_html

NoteContentType = Literal[
    "plain_markdown",
    "html_with_markdown_tables",
    "already_html_table",
    "no_markdown_tables",
]
MigrationStatus = Literal["converted", "skipped", "blocked"]

TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
HTML_TAG_PATTERN = re.compile(r"<[A-Za-z][^>]*>")
SUPPORTED_TABLE_CONTAINERS = {"p", "div"}


@dataclass(frozen=True)
class NoteTableConversionResult:
    content: str
    content_type: NoteContentType
    status: MigrationStatus
    reason: str
    before_hash: str
    after_hash: str


def note_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def has_markdown_table_separator(content: str) -> bool:
    return any(TABLE_SEPARATOR_PATTERN.match(line) for line in content.splitlines())


def classify_note_content(content: str) -> NoteContentType:
    lowered = content.lower()
    if "<table" in lowered:
        return "already_html_table"
    has_markdown_table = has_markdown_table_separator(_html_breaks_to_newlines(content))
    if has_markdown_table and HTML_TAG_PATTERN.search(content):
        return "html_with_markdown_tables"
    if has_markdown_table:
        return "plain_markdown"
    return "no_markdown_tables"


def convert_note_tables_to_html(content: str) -> NoteTableConversionResult:
    content_type = classify_note_content(content)
    before_hash = note_content_hash(content)
    if content_type in {"already_html_table", "no_markdown_tables"}:
        return NoteTableConversionResult(
            content=content,
            content_type=content_type,
            status="skipped",
            reason=content_type,
            before_hash=before_hash,
            after_hash=before_hash,
        )

    if content_type == "plain_markdown":
        converted = render_note_html(content)
        return NoteTableConversionResult(
            content=converted,
            content_type=content_type,
            status="converted",
            reason="plain_markdown_rendered",
            before_hash=before_hash,
            after_hash=note_content_hash(converted),
        )

    converted_html, reason = _convert_html_markdown_table_blocks(content)
    status: MigrationStatus = "converted" if reason == "html_blocks_converted" else "blocked"
    final_content = converted_html if status == "converted" else content
    return NoteTableConversionResult(
        content=final_content,
        content_type=content_type,
        status=status,
        reason=reason,
        before_hash=before_hash,
        after_hash=note_content_hash(final_content),
    )


def _convert_html_markdown_table_blocks(content: str) -> tuple[str, str]:
    soup = BeautifulSoup(content, "html.parser")
    converted_count = 0
    for tag in list(soup.find_all(True)):
        text = _tag_text_with_newlines(tag)
        if not has_markdown_table_separator(text):
            continue
        if tag.name not in SUPPORTED_TABLE_CONTAINERS:
            return content, f"unsupported_table_container:{tag.name}"
        fragment_html = _convert_markdown_table_blocks_in_text(text)
        fragment = BeautifulSoup(fragment_html, "html.parser")
        tag.replace_with(fragment)
        converted_count += 1

    if converted_count == 0:
        return content, "no_supported_table_container"
    return str(soup), "html_blocks_converted"


def _tag_text_with_newlines(tag) -> str:
    clone = BeautifulSoup(str(tag), "html.parser")
    for br in clone.find_all("br"):
        br.replace_with("\n")
    return clone.get_text()


def _html_breaks_to_newlines(content: str) -> str:
    return re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)


def _convert_markdown_table_blocks_in_text(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        if _starts_table(lines, index):
            table_lines = [lines[index], lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index].strip():
                table_lines.append(lines[index])
                index += 1
            output.append(render_note_html("\n".join(table_lines)).strip())
            continue

        line = lines[index].strip()
        if line:
            output.append(f"<p>{escape(line)}</p>")
        index += 1
    return "\n".join(output)


def _starts_table(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    return "|" in lines[index] and bool(TABLE_SEPARATOR_PATTERN.match(lines[index + 1]))


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
