from __future__ import annotations

from pathlib import Path
from typing import Any

from paperread_batch.io import file_sha256, read_json


class TakeawayError(ValueError):
    pass


def _split_markdown_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def extract_30_second_row(note_md_path: Path) -> str:
    path = Path(note_md_path)
    if not path.exists() or not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = _split_markdown_table_row(line)
        if len(cells) >= 2 and cells[0] == "30 秒结论":
            return cells[1].strip()
    return ""


def _summary_field(summary: Any, field_name: str) -> str:
    if not isinstance(summary, dict):
        return ""
    value = summary.get(field_name)
    return value.strip() if isinstance(value, str) else ""


def _source_result(takeaway: str, *, source_type: str, source_path: Path) -> dict[str, str]:
    return {
        "thirty_second_takeaway": takeaway,
        "takeaway_source_type": source_type,
        "takeaway_source_path": str(source_path),
        "takeaway_source_sha256": file_sha256(source_path),
    }


def extract_takeaway(note_md_path: Path, summary_json_path: Path) -> dict[str, str]:
    note_path = Path(note_md_path)
    if note_path.exists() and note_path.is_file():
        note_takeaway = extract_30_second_row(note_path)
        if note_takeaway:
            return _source_result(
                note_takeaway,
                source_type="rendered_note_30_second_row",
                source_path=note_path,
            )

    summary_path = Path(summary_json_path)
    if summary_path.exists() and summary_path.is_file():
        summary = read_json(summary_path)
        tldr = _summary_field(summary, "tldr")
        if tldr:
            return _source_result(
                tldr,
                source_type="structured_tldr_fallback",
                source_path=summary_path,
            )
        one_sentence = _summary_field(summary, "one_sentence_summary")
        if one_sentence:
            return _source_result(
                one_sentence,
                source_type="structured_one_sentence_summary_fallback",
                source_path=summary_path,
            )

    raise TakeawayError("30-second takeaway is unavailable from note.md or summary.json")
