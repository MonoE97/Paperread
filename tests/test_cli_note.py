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
    note = output_path.read_text(encoding="utf-8")
    assert "## 0. 速读卡片" in note
    assert "## 10. 证据链附录" in note


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


def test_finalize_note_command_can_write_zotero_ready_html(tmp_path: Path) -> None:
    metadata_path = tmp_path / "metadata.json"
    summary_path = tmp_path / "summary.json"
    output_path = tmp_path / "note.md"
    html_output_path = tmp_path / "note.html"
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
            "research_object": "Battery | Interface",
        },
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "finalize-note",
            str(metadata_path),
            str(summary_path),
            "--output",
            str(output_path),
            "--html-output",
            str(html_output_path),
        ],
    )

    assert result.exit_code == 0
    assert html_output_path.exists()
    html = html_output_path.read_text(encoding="utf-8")
    assert "Wrote note HTML:" in result.stdout
    assert "<table>" in html
    assert "<td>Battery | Interface</td>" in html
    assert "| --- | --- |" not in html


def test_render_note_html_command_converts_existing_note(tmp_path: Path) -> None:
    note_path = tmp_path / "note.md"
    html_output_path = tmp_path / "note.html"
    note_path.write_text(
        "# Existing Note\n\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        "| target | rendered table |\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(app, ["render-note-html", str(note_path), "--output", str(html_output_path)])

    assert result.exit_code == 0
    assert "Wrote note HTML:" in result.stdout
    html = html_output_path.read_text(encoding="utf-8")
    assert "<h1>Existing Note</h1>" in html
    assert "<table>" in html
    assert "<td>rendered table</td>" in html


def test_classify_note_tables_command_reports_content_type(tmp_path: Path) -> None:
    note_path = tmp_path / "note.html"
    note_path.write_text("<p>| A | B |<br>| --- | --- |<br>| 1 | 2 |</p>", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["classify-note-tables", str(note_path)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "content_type": "html_with_markdown_tables",
        "has_markdown_table": True,
        "has_html_table": False,
    }


def test_classify_note_tables_command_detects_self_closing_html_breaks(tmp_path: Path) -> None:
    note_path = tmp_path / "note.html"
    note_path.write_text("<p>| A | B |<br/>| --- | --- |<br/>| 1 | 2 |</p>", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["classify-note-tables", str(note_path)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["content_type"] == "html_with_markdown_tables"
    assert payload["has_markdown_table"] is True


def test_convert_note_tables_command_writes_converted_html_and_report(tmp_path: Path) -> None:
    note_path = tmp_path / "note.html"
    output_path = tmp_path / "converted.html"
    report_path = tmp_path / "report.json"
    note_path.write_text("<p>| A | B |<br>| --- | --- |<br>| 1 | 2 |</p>", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "convert-note-tables",
            str(note_path),
            "--output",
            str(output_path),
            "--report",
            str(report_path),
        ],
    )

    assert result.exit_code == 0
    assert "<table>" in output_path.read_text(encoding="utf-8")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "converted"
    assert report["content_type"] == "html_with_markdown_tables"
    assert report["reason"] == "html_blocks_converted"


def test_finalize_note_command_applies_version_suffix(tmp_path: Path) -> None:
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

    result = runner.invoke(
        app,
        [
            "finalize-note",
            str(metadata_path),
            str(summary_path),
            "--output",
            str(output_path),
            "--generated-date",
            "2026-04-25",
            "--version-suffix",
            " (v2)",
        ],
    )

    assert result.exit_code == 0
    assert "# [Codex Summary] Paper - 2026-04-25 (v2)" in output_path.read_text(encoding="utf-8")


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
    assert "## 10. 自动抽取质量报告" not in note
    assert "## 10. 证据链附录" in note
    assert "## 11. 补充优化记录" in note
    assert "note_valid" in result.stdout


def test_note_tags_command_prints_fixed_and_inferred_labels(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_json(
        summary_path,
        {
            "note_labels": [
                "Deep Learning",
                "inverse-design",
                "materials discovery",
                "physics-informed ML",
                "deep_learning",
                "ignored extra label",
            ],
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["note-tags", str(summary_path)])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [
        "codex-summary",
        "paper-summary",
        "deep_learning",
        "inverse_design",
        "materials_discovery",
        "physics_informed_ml",
    ]


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


def test_validate_summary_json_command_reports_invalid_json(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text('{"one_sentence_summary": "bad"', encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["validate-summary-json", str(summary_path)])

    assert result.exit_code == 1
    assert "json_invalid:" in result.stdout
    assert "summary JSON" in result.stdout
    assert str(summary_path) in result.stdout
    assert "line 1" in result.stdout
    assert "column" in result.stdout


def test_validate_summary_json_command_reports_missing_path(tmp_path: Path) -> None:
    summary_path = tmp_path / "missing-summary.json"
    runner = CliRunner()

    result = runner.invoke(app, ["validate-summary-json", str(summary_path)])

    assert result.exit_code == 1
    assert f"json_missing: summary JSON {summary_path}" in result.stdout


def test_validate_summary_json_command_reports_directory_path(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary-dir"
    summary_path.mkdir()
    runner = CliRunner()

    result = runner.invoke(app, ["validate-summary-json", str(summary_path)])

    assert result.exit_code == 1
    assert f"json_unreadable: summary JSON {summary_path}" in result.stdout
    assert "is a directory" in result.stdout.lower()


def test_validate_summary_json_command_reports_non_utf8_content(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_bytes(b"\xff\xfe\x00\x00")
    runner = CliRunner()

    result = runner.invoke(app, ["validate-summary-json", str(summary_path)])

    assert result.exit_code == 1
    assert f"json_unreadable: summary JSON {summary_path}" in result.stdout
    assert "utf-8" in result.stdout.lower()


def test_validate_summary_json_command_reports_non_object_top_level(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text('["not", "an", "object"]', encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["validate-summary-json", str(summary_path)])

    assert result.exit_code == 1
    assert f"json_invalid: summary JSON {summary_path}: expected top-level JSON object" in result.stdout


def test_validate_summary_json_command_success_output_does_not_imply_full_schema_validation(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_json(summary_path, {"one_sentence_summary": "ok"})
    runner = CliRunner()

    result = runner.invoke(app, ["validate-summary-json", str(summary_path)])

    assert result.exit_code == 0
    assert "summary_json_readable_object" in result.stdout
    assert "valid" not in result.stdout.lower()


def test_lint_summary_command_reports_issues(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "workflow_steps": "1. First. 2. Second.",
                "evidence_summary": [],
                "key_figures": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(app, ["lint-summary", str(summary_path)])

    assert result.exit_code == 1
    assert "workflow_steps_single_line_numbered_list" in result.stdout


def test_gate_run_command_writes_blocked_report(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(json.dumps({"review_status": "not_reviewed"}), encoding="utf-8")
    report_path = tmp_path / "gate-report.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "gate-run",
            str(run_dir),
            "--paper-title",
            "Example Paper",
            "--generated-date",
            "2026-05-06",
            "--output",
            str(report_path),
        ],
    )

    assert result.exit_code == 1
    assert report_path.exists()
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "blocked"


def test_prepare_write_payload_command_writes_payload(tmp_path: Path) -> None:
    note_html = tmp_path / "note.html"
    note_html.write_text("<h1>Title</h1>", encoding="utf-8")
    gate_report = tmp_path / "gate-report.json"
    gate_report.write_text(
        json.dumps(
            {
                "status": "write_ready",
                "parentKey": "ABC123",
                "note_html_path": str(note_html),
                "tags": ["codex-summary"],
                "note_title": "[Codex Summary] Title - 2026-05-06",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "write-payload.json"
    runner = CliRunner()

    result = runner.invoke(app, ["prepare-write-payload", str(gate_report), "--output", str(output)])

    assert result.exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["parentKey"] == "ABC123"


def test_finalize_note_command_reports_invalid_summary_json(tmp_path: Path) -> None:
    metadata_path = tmp_path / "metadata.json"
    summary_path = tmp_path / "summary.json"
    output_path = tmp_path / "note.md"
    write_json(metadata_path, {"key": "ABC123", "title": "Paper", "creators": "A", "date": "2026"})
    summary_path.write_text('{"one_sentence_summary": "bad"', encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["finalize-note", str(metadata_path), str(summary_path), "--output", str(output_path)])

    assert result.exit_code == 1
    assert "json_invalid:" in result.stdout
    assert "summary JSON" in result.stdout
    assert str(summary_path) in result.stdout
    assert "line 1" in result.stdout
    assert "column" in result.stdout
    assert not output_path.exists()


def test_prepare_item_command_reports_missing_details_json_path(tmp_path: Path) -> None:
    details_path = tmp_path / "missing-details.json"
    workdir = tmp_path / "bundle"
    runner = CliRunner()

    result = runner.invoke(app, ["prepare-item", str(details_path), "--workdir", str(workdir)])

    assert result.exit_code == 1
    assert f"json_missing: details JSON {details_path}" in result.stdout


def test_prepare_item_command_reports_non_object_details_json(tmp_path: Path) -> None:
    details_path = tmp_path / "details.json"
    workdir = tmp_path / "bundle"
    details_path.write_text('["not", "an", "object"]', encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["prepare-item", str(details_path), "--workdir", str(workdir)])

    assert result.exit_code == 1
    assert f"json_invalid: details JSON {details_path}: expected top-level JSON object" in result.stdout


def test_save_item_details_command_writes_normalized_and_raw(tmp_path: Path) -> None:
    input_path = tmp_path / "mcp-response.json"
    output_path = tmp_path / "run" / "item-details.json"
    raw_output_path = tmp_path / "run" / "item-details.raw.json"
    input_path.write_text(
        json.dumps(
            [{"type": "text", "text": json.dumps({"key": "ABC123", "title": "Example Paper"})}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "save-item-details",
            str(input_path),
            "--output",
            str(output_path),
            "--raw-output",
            str(raw_output_path),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["key"] == "ABC123"
    assert raw_output_path.exists()


def test_save_item_details_command_can_disable_sqlite_extra_fallback(tmp_path: Path) -> None:
    input_path = tmp_path / "mcp-response.json"
    output_path = tmp_path / "run" / "item-details.json"
    input_path.write_text(
        json.dumps({"key": "ABC123", "title": "Example Paper"}, ensure_ascii=False),
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "save-item-details",
            str(input_path),
            "--output",
            str(output_path),
            "--no-sqlite-extra-fallback",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["extra_source"] == "not_requested"
    assert "extra" not in json.loads(output_path.read_text(encoding="utf-8"))


def test_next_version_suffix_command_reads_item_details(tmp_path: Path) -> None:
    details_path = tmp_path / "item-details.json"
    write_json(
        details_path,
        {
            "notes": [
                "<h1>[Codex Summary] Paper A - 2026-04-26</h1>",
                "<h1>[Codex Summary] Paper A - 2026-04-26 (v2)</h1>",
            ]
        },
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "next-version-suffix",
            str(details_path),
            "--paper-title",
            "Paper A",
            "--generated-date",
            "2026-04-26",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == " (v3)\n"


def test_validate_trusted_summary_fails_without_review_gate(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_json(
        summary_path,
        {
            "one_sentence_summary": "ok",
            "paper_type": "research_article",
            "trust_status": "usable_with_caveats",
            "review_status": "not_reviewed",
            "evidence_summary": [],
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["validate-trusted-summary", str(summary_path)])

    assert result.exit_code == 1
    assert "trusted_summary_invalid:" in result.stdout
    assert "review_status must be passed or passed_with_caveats" in result.stdout
    assert "evidence_summary must contain at least one claim" in result.stdout


def test_validate_trusted_summary_fails_empty_core_content(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_json(
        summary_path,
        {
            "one_sentence_summary": "",
            "abstract_translation": "",
            "key_points": [],
            "research_question": "",
            "method": "",
            "experiments": "",
            "contributions": [],
            "limitations": [],
            "ai4s_relevance": "",
            "follow_up_keywords": [],
            "paper_type": "method_paper",
            "trust_status": "usable_with_caveats",
            "trust_rationale": "Evidence was checked.",
            "review_status": "passed",
            "evidence_summary": [
                {
                    "claim": "The method is supported.",
                    "evidence": [{"type": "text", "locator": "context.md page 2", "summary": "method evidence"}],
                    "confidence": "high",
                }
            ],
            "improvement_status": "not_needed",
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["validate-trusted-summary", str(summary_path)])

    assert result.exit_code == 1
    assert "one_sentence_summary is required" in result.stdout
    assert "method is required" in result.stdout
    assert "key_points must contain at least one item" in result.stdout


def test_validate_trusted_summary_rejects_null_required_text_fields(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_json(
        summary_path,
        {
            "one_sentence_summary": None,
            "abstract_translation": "本文提出一个有限电场机器学习工作流。",
            "key_points": ["Field-aware forces"],
            "research_question": "How can finite-field interface simulations be accelerated?",
            "method": "The method combines force learning and charge-response learning.",
            "experiments": "The paper validates the workflow on Au/NaCl interfaces.",
            "contributions": ["ML finite-field dynamics"],
            "limitations": ["Single benchmark chemistry"],
            "ai4s_relevance": "The decomposition is useful for field-driven AI4S simulations.",
            "follow_up_keywords": ["finite-field MD"],
            "paper_type": "method_paper",
            "trust_status": "usable_with_caveats",
            "trust_rationale": None,
            "review_status": "passed",
            "evidence_summary": [
                {
                    "claim": "The method is supported.",
                    "evidence": [{"type": "text", "locator": "context.md page 2", "summary": "method evidence"}],
                    "confidence": "high",
                }
            ],
            "improvement_status": "not_needed",
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["validate-trusted-summary", str(summary_path)])

    assert result.exit_code == 1
    assert "trust_rationale is required" in result.stdout
    assert "one_sentence_summary is required" in result.stdout


def write_ready_trusted_summary_with_evidence(
    path: Path,
    evidence_summary: list[dict],
) -> None:
    write_json(
        path,
        {
            "one_sentence_summary": "This paper proposes a field-aware ML workflow.",
            "abstract_translation": "本文提出一个有限电场机器学习工作流。",
            "key_points": ["Field-aware forces", "Charge response model"],
            "research_question": "How can finite-field interface simulations be accelerated?",
            "method": "The method combines force learning and charge-response learning.",
            "experiments": "The paper validates the workflow on Au/NaCl interfaces.",
            "contributions": ["ML finite-field dynamics", "ML charge response"],
            "limitations": ["Single benchmark chemistry"],
            "ai4s_relevance": "The decomposition is useful for field-driven AI4S simulations.",
            "follow_up_keywords": ["finite-field MD"],
            "paper_type": "method_paper",
            "trust_status": "usable_with_caveats",
            "trust_rationale": "Text extraction is complete and figure evidence is caveated.",
            "review_status": "passed_with_caveats",
            "evidence_summary": evidence_summary,
            "review_issues": [],
            "improvement_status": "completed",
        },
    )


def test_validate_trusted_summary_rejects_null_evidence_claim(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_ready_trusted_summary_with_evidence(
        summary_path,
        [
            {
                "claim": None,
                "evidence": [{"type": "text", "locator": "context.md page 2", "summary": "method evidence"}],
                "confidence": "high",
            }
        ],
    )
    runner = CliRunner()

    result = runner.invoke(app, ["validate-trusted-summary", str(summary_path)])

    assert result.exit_code == 1
    assert "evidence_summary[1] claim is required" in result.stdout


def test_validate_trusted_summary_rejects_null_evidence_locator_and_summary(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_ready_trusted_summary_with_evidence(
        summary_path,
        [
            {
                "claim": "The method is supported.",
                "evidence": [{"type": "text", "locator": None, "summary": None}],
                "confidence": "high",
            }
        ],
    )
    runner = CliRunner()

    result = runner.invoke(app, ["validate-trusted-summary", str(summary_path)])

    assert result.exit_code == 1
    assert "evidence_summary[1] must include at least one evidence locator" in result.stdout


def test_validate_trusted_summary_rejects_summary_only_evidence_without_locator(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_ready_trusted_summary_with_evidence(
        summary_path,
        [
            {
                "claim": "The method is supported.",
                "evidence": [{"type": "text", "summary": "method evidence"}],
                "confidence": "high",
            }
        ],
    )
    runner = CliRunner()

    result = runner.invoke(app, ["validate-trusted-summary", str(summary_path)])

    assert result.exit_code == 1
    assert "evidence_summary[1] must include at least one evidence locator" in result.stdout


def test_validate_trusted_summary_rejects_na_evidence_locator(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_ready_trusted_summary_with_evidence(
        summary_path,
        [
            {
                "claim": "The method is supported.",
                "evidence": [{"type": "text", "locator": "N/A", "summary": "method evidence"}],
                "confidence": "high",
            }
        ],
    )
    runner = CliRunner()

    result = runner.invoke(app, ["validate-trusted-summary", str(summary_path)])

    assert result.exit_code == 1
    assert "evidence_summary[1] must include at least one evidence locator" in result.stdout


def test_validate_trusted_summary_passes_ready_summary(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_json(
        summary_path,
        {
            "one_sentence_summary": "This paper proposes a field-aware ML workflow.",
            "abstract_translation": "本文提出一个有限电场机器学习工作流。",
            "key_points": ["Field-aware forces", "Charge response model"],
            "research_question": "How can finite-field interface simulations be accelerated?",
            "method": "The method combines force learning and charge-response learning.",
            "experiments": "The paper validates the workflow on Au/NaCl interfaces.",
            "contributions": ["ML finite-field dynamics", "ML charge response"],
            "limitations": ["Single benchmark chemistry"],
            "ai4s_relevance": "The decomposition is useful for field-driven AI4S simulations.",
            "follow_up_keywords": ["finite-field MD"],
            "paper_type": "method_paper",
            "trust_status": "usable_with_caveats",
            "trust_rationale": "Text extraction is complete and figure evidence is caveated.",
            "review_status": "passed_with_caveats",
            "evidence_summary": [
                {
                    "claim": "The method is supported.",
                    "evidence": [{"type": "text", "locator": "context.md page 2", "summary": "method evidence"}],
                    "confidence": "high",
                }
            ],
            "review_issues": [],
            "improvement_status": "completed",
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["validate-trusted-summary", str(summary_path)])

    assert result.exit_code == 0
    assert "trusted_summary_valid" in result.stdout


def test_apply_review_command_updates_summary(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    review_path = tmp_path / "review.json"
    write_json(summary_path, {"one_sentence_summary": "ok", "review_status": "not_reviewed"})
    write_json(
        review_path,
        {
            "review_status": "passed",
            "review_issues": [],
            "trust_status_recommendation": "trusted",
            "needs_improvement": False,
            "improvement_requests": [],
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["apply-review", str(summary_path), str(review_path)])

    assert result.exit_code == 0
    updated = json.loads(summary_path.read_text(encoding="utf-8"))
    assert updated["review_status"] == "passed"
    assert updated["trust_status"] == "trusted"
    assert updated["improvement_status"] == "not_needed"
    assert updated["improvement_notes"] == []


def test_apply_review_command_rejects_invalid_review_without_modifying_summary(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    review_path = tmp_path / "review.json"
    original_summary = {
        "one_sentence_summary": "ok",
        "trust_status": "trusted",
        "review_status": "failed",
        "improvement_status": "needed",
        "improvement_notes": [{"issue": "Old issue", "action": "", "source": "previous review"}],
    }
    write_json(summary_path, original_summary)
    write_json(
        review_path,
        {
            "review_status": "passed",
            "review_issues": [],
            "trust_status_recommendation": "trusted",
            "improvement_requests": [],
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["apply-review", str(summary_path), str(review_path)])

    assert result.exit_code == 1
    assert "review_payload_invalid:" in result.stdout
    assert json.loads(summary_path.read_text(encoding="utf-8")) == original_summary


def test_apply_review_command_output_creates_parent_dirs_and_leaves_original_unchanged(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    review_path = tmp_path / "review.json"
    output_path = tmp_path / "nested" / "reviewed" / "summary.json"
    original_summary = {"one_sentence_summary": "ok", "review_status": "not_reviewed"}
    write_json(summary_path, original_summary)
    write_json(
        review_path,
        {
            "review_status": "passed",
            "review_issues": [],
            "trust_status_recommendation": "trusted",
            "needs_improvement": False,
            "improvement_requests": [],
        },
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["apply-review", str(summary_path), str(review_path), "--output", str(output_path)],
    )

    assert result.exit_code == 0
    assert json.loads(summary_path.read_text(encoding="utf-8")) == original_summary
    updated = json.loads(output_path.read_text(encoding="utf-8"))
    assert updated["review_status"] == "passed"
    assert updated["trust_status"] == "trusted"
    assert updated["improvement_status"] == "not_needed"
