import json
from pathlib import Path

from typer.testing import CliRunner

from zotero_paperread.cli import app


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_render_note_command_writes_markdown(tmp_path: Path) -> None:
    metadata_path = tmp_path / "metadata.json"
    summary_path = tmp_path / "summary.json"
    output_path = tmp_path / "note.md"
    write_json(metadata_path, {"key": "ABC123", "title": "Paper", "creators": "A", "date": "2026"})
    write_json(
        summary_path,
        {
            "one_sentence_summary": "一句话总结。",
            "abstract_translation": "摘要翻译。",
            "key_points": ["要点"],
            "research_question": "问题",
            "method": "方法",
            "experiments": "实验",
            "contributions": ["贡献"],
            "limitations": ["局限"],
            "ai4s_relevance": "启发",
            "follow_up_keywords": ["keyword"],
            "quality_score": "8/10",
            "extraction_warnings": [],
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["render-note", str(metadata_path), str(summary_path), "--output", str(output_path)])

    assert result.exit_code == 0
    assert output_path.exists()
    assert "## 核心结论" in output_path.read_text(encoding="utf-8")


def test_finalize_note_command_writes_and_validates_markdown(tmp_path: Path) -> None:
    metadata_path = tmp_path / "metadata.json"
    summary_path = tmp_path / "summary.json"
    output_path = tmp_path / "note.md"
    write_json(metadata_path, {"key": "ABC123", "title": "Paper", "creators": "A", "date": "2026"})
    write_json(
        summary_path,
        {
            "one_sentence_summary": "一句话总结。",
            "abstract_translation": "摘要翻译。",
            "key_points": ["要点"],
            "research_question": "问题",
            "method": "方法",
            "experiments": "实验",
            "contributions": ["贡献"],
            "limitations": ["局限"],
            "ai4s_relevance": "启发",
            "follow_up_keywords": ["keyword"],
            "quality_score": "8/10",
            "extraction_warnings": [],
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["finalize-note", str(metadata_path), str(summary_path), "--output", str(output_path)])

    assert result.exit_code == 0
    assert output_path.exists()
    assert "Wrote note Markdown:" in result.stdout
    assert "note_valid" in result.stdout


def test_finalize_note_command_accepts_trusted_note_fields(tmp_path: Path) -> None:
    metadata_path = tmp_path / "metadata.json"
    summary_path = tmp_path / "summary.json"
    output_path = tmp_path / "note.md"
    write_json(metadata_path, {"key": "ABC123", "title": "Paper", "creators": "A", "date": "2026"})
    write_json(
        summary_path,
        {
            "one_sentence_summary": "一句话总结。",
            "abstract_translation": "摘要翻译。",
            "key_points": ["要点"],
            "research_question": "问题",
            "method": "方法",
            "figure_overview": "关键图片概览。",
            "key_figures": [],
            "experiments": "实验",
            "contributions": ["贡献"],
            "limitations": ["局限"],
            "ai4s_relevance": "启发",
            "follow_up_keywords": ["keyword"],
            "note_labels": ["deep_learning"],
            "quality_score": "8/10",
            "extraction_warnings": [],
            "paper_type": "research_article",
            "trust_status": "trusted",
            "trust_rationale": "证据充分。",
            "review_status": "passed",
            "evidence_summary": [
                {
                    "claim": "The method is supported by the method section.",
                    "evidence": [{"type": "text", "locator": "page 3", "summary": "method evidence"}],
                    "confidence": "high",
                }
            ],
            "review_issues": [],
            "improvement_status": "not_needed",
            "improvement_notes": [],
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["finalize-note", str(metadata_path), str(summary_path), "--output", str(output_path)])

    assert result.exit_code == 0
    note = output_path.read_text(encoding="utf-8")
    assert "## 可信度与证据" in note
    assert "note_valid" in result.stdout


def test_validate_note_command_fails_for_incomplete_note(tmp_path: Path) -> None:
    note_path = tmp_path / "bad.md"
    note_path.write_text("# bad\n", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["validate-note", str(note_path)])

    assert result.exit_code == 1
    assert "missing_section" in result.stdout


def test_validate_note_command_reports_missing_note_file(tmp_path: Path) -> None:
    note_path = tmp_path / "missing.md"
    runner = CliRunner()

    result = runner.invoke(app, ["validate-note", str(note_path)])

    assert result.exit_code == 1
    assert "note_missing:" in result.stdout
