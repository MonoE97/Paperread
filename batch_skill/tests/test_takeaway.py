import json
from pathlib import Path

import pytest

from paperread_batch.io import file_sha256
from paperread_batch.takeaway import TakeawayError, extract_takeaway


def test_extracts_30_second_row_from_rendered_note(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        """# [Codex Summary] Paper

## 0. 阅读结论

| 项目 | 内容 |
| --- | --- |
| 30 秒结论 | 这篇论文给出一个可复用的材料筛选流程。 |
| 主要风险 | 数据集较小。 |
""",
        encoding="utf-8",
    )
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"tldr": "不应使用 fallback"}), encoding="utf-8")

    result = extract_takeaway(note_md_path=note, summary_json_path=summary)

    assert result == {
        "thirty_second_takeaway": "这篇论文给出一个可复用的材料筛选流程。",
        "takeaway_source_type": "rendered_note_30_second_row",
        "takeaway_source_path": str(note),
        "takeaway_source_sha256": file_sha256(note),
    }


def test_falls_back_to_tldr_when_note_row_missing(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text("# Note without table\n", encoding="utf-8")
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"tldr": "结构化 tldr。", "one_sentence_summary": "更弱 fallback。"}), encoding="utf-8")

    result = extract_takeaway(note_md_path=note, summary_json_path=summary)

    assert result["thirty_second_takeaway"] == "结构化 tldr。"
    assert result["takeaway_source_type"] == "structured_tldr_fallback"
    assert result["takeaway_source_path"] == str(summary)
    assert result["takeaway_source_sha256"] == file_sha256(summary)


def test_falls_back_to_one_sentence_summary_when_tldr_missing(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"one_sentence_summary": "一句话总结。"}), encoding="utf-8")

    result = extract_takeaway(note_md_path=tmp_path / "missing-note.md", summary_json_path=summary)

    assert result["thirty_second_takeaway"] == "一句话总结。"
    assert result["takeaway_source_type"] == "structured_one_sentence_summary_fallback"


def test_rejects_missing_takeaway_sources(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"tldr": "", "one_sentence_summary": ""}), encoding="utf-8")

    with pytest.raises(TakeawayError, match="30-second takeaway"):
        extract_takeaway(note_md_path=tmp_path / "missing-note.md", summary_json_path=summary)
