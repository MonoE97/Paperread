import json
from pathlib import Path

import fitz
from typer.testing import CliRunner

from zotero_paperread.cli import app
from zotero_paperread.pdf_extract import extract_pdf


def make_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Abstract\nWe propose a physics-informed model for solid-state battery materials.\n"
        "Methods\nThe method combines graph learning and physical constraints.\n"
        "Results\nThe model improves prediction accuracy on held-out compositions.",
    )
    doc.save(path)
    doc.close()


def test_pdf_to_note_dry_run(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path)
    extraction = extract_pdf(pdf_path)
    assert "physics-informed" in extraction["text"]

    metadata_path = tmp_path / "metadata.json"
    summary_path = tmp_path / "summary.json"
    note_path = tmp_path / "note.md"

    metadata_path.write_text(
        json.dumps(
            {
                "key": "DRYRUN1",
                "title": "Physics-Informed Materials Prediction",
                "creators": "Mono Researcher",
                "date": "2026",
                "DOI": "10.1000/dryrun",
                "url": "https://example.org/dryrun",
                "zoteroUrl": "zotero://select/library/items/DRYRUN1",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps(
            {
                "one_sentence_summary": "这篇论文用物理约束增强材料性质预测。",
                "abstract_translation": "作者提出一种面向固态电池材料的物理约束模型。",
                "key_points": ["面向固态电池材料", "结合图学习和物理约束"],
                "research_question": "如何在材料性质预测中融合物理先验？",
                "method": "方法结合 graph learning 和 physical constraints。",
                "experiments": "实验在 held-out compositions 上验证预测精度。",
                "contributions": ["提出物理约束预测框架", "验证泛化性能"],
                "limitations": ["测试 PDF 是最小夹具，不代表真实论文复杂度"],
                "ai4s_relevance": "该路线适合 AI+材料中的小数据泛化问题。",
                "follow_up_keywords": ["physics-informed ML", "solid-state battery"],
                "quality_score": "8/10",
                "extraction_warnings": extraction["warnings"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    render_result = runner.invoke(app, ["render-note", str(metadata_path), str(summary_path), "--output", str(note_path)])
    assert render_result.exit_code == 0

    validate_result = runner.invoke(app, ["validate-note", str(note_path)])
    assert validate_result.exit_code == 0
    assert "note_valid" in validate_result.stdout
