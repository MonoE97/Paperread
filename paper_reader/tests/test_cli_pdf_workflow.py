from __future__ import annotations

import json
from pathlib import Path

import fitz
from typer.testing import CliRunner

from paper_reader import workflow
from paper_reader.cli import app


def make_pdf(path: Path, pages: list[str] | None = None) -> None:
    doc = fitz.open()
    for text in pages or ["Abstract\nThis PDF studies solid-state electrolytes."]:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def trusted_summary() -> dict:
    return {
        "review_status": "passed_with_caveats",
        "improvement_status": "not_needed",
        "trust_status": "usable_with_caveats",
        "paper_type": "research_article",
        "one_sentence_summary": "这篇论文研究固态电解质。",
        "abstract_translation": "摘要说明了材料稳定性。",
        "research_question": "如何提升固态电解质稳定性？",
        "method": "方法结合合成和电化学测试。",
        "experiments": "实验包括阻抗和循环测试。",
        "ai4s_relevance": "可为材料筛选提供约束。",
        "key_points": ["稳定性", "界面兼容性"],
        "contributions": ["提出材料设计", "验证界面兼容性"],
        "limitations": ["仍需长期验证"],
        "follow_up_keywords": ["solid_state_electrolyte"],
        "trust_rationale": "证据来自 PDF 正文。",
        "evidence_summary": [
            {
                "claim": "材料稳定性较好。",
                "evidence": [{"locator": "context.md page 1", "summary": "摘要描述稳定性。"}],
            }
        ],
    }


def patch_figure_extraction(monkeypatch) -> None:
    def fake_extract_figures(
        requested_pdf_path: Path,
        output_dir: Path,
        top_k: int = 4,
        max_pages: int | None = None,
        *,
        arxiv_id: str | None = None,
        item_details: dict | None = None,
        enable_ocr_fallback: bool = False,
    ) -> dict:
        return {
            "arxiv_id": None,
            "pdf_path": str(requested_pdf_path),
            "candidate_count": 0,
            "selected_figures": [],
            "source_attempts": [{"stage": "direct_pdf", "status": "used"}],
            "warnings": [],
        }

    monkeypatch.setattr(workflow, "extract_figures", fake_extract_figures)


def test_prepare_pdf_command_creates_versioned_analysis_dir_and_manifest(monkeypatch, tmp_path: Path) -> None:
    patch_figure_extraction(monkeypatch)
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path)

    result = CliRunner().invoke(app, ["prepare-pdf", str(pdf_path), "--title", "Explicit PDF Title"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    analysis_dir = Path(payload["analysis_dir"])
    assert analysis_dir == tmp_path / "paper_analysis"
    assert Path(payload["final_note_path"]) == tmp_path / "paper_note.md"
    assert Path(payload["metadata_json"]).exists()
    assert Path(payload["context_md"]).exists()
    manifest = json.loads((analysis_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["source_type"] == "pdf_path"
    assert manifest["title"] == "Explicit PDF Title"
    assert manifest["final_note_path"] == str(tmp_path / "paper_note.md")


def test_prepare_pdf_command_writes_machine_json_to_file(monkeypatch, tmp_path: Path) -> None:
    patch_figure_extraction(monkeypatch)
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path)
    json_output = tmp_path / "prepare-result.json"

    result = CliRunner().invoke(
        app,
        [
            "prepare-pdf",
            str(pdf_path),
            "--title",
            "Explicit PDF Title",
            "--json-output",
            str(json_output),
        ],
    )

    assert result.exit_code == 0
    stdout_payload = json.loads(result.stdout)
    file_payload = json.loads(json_output.read_text(encoding="utf-8"))
    assert file_payload == stdout_payload
    assert Path(file_payload["analysis_dir"]) == tmp_path / "paper_analysis"
    assert Path(file_payload["manifest_path"]).exists()


def test_prepare_pdf_command_versions_when_outputs_already_exist(monkeypatch, tmp_path: Path) -> None:
    patch_figure_extraction(monkeypatch)
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path)
    (tmp_path / "paper_analysis").mkdir()
    (tmp_path / "paper_note.md").write_text("old", encoding="utf-8")

    result = CliRunner().invoke(app, ["prepare-pdf", str(pdf_path)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert Path(payload["analysis_dir"]) == tmp_path / "paper_analysis_v2"
    assert Path(payload["final_note_path"]) == tmp_path / "paper_note_v2.md"
    assert (tmp_path / "paper_note.md").read_text(encoding="utf-8") == "old"


def test_prepare_pdf_command_rejects_missing_pdf_without_creating_analysis_dir(tmp_path: Path) -> None:
    pdf_path = tmp_path / "missing.pdf"

    result = CliRunner().invoke(app, ["prepare-pdf", str(pdf_path)])

    assert result.exit_code == 1
    assert "PDF not found" in result.stdout
    assert not (tmp_path / "missing_analysis").exists()
    assert not (tmp_path / "missing_note.md").exists()


def test_prepare_pdf_command_rejects_invalid_pdf_without_creating_analysis_dir(tmp_path: Path) -> None:
    pdf_path = tmp_path / "bad.pdf"
    pdf_path.write_text("not a pdf", encoding="utf-8")

    result = CliRunner().invoke(app, ["prepare-pdf", str(pdf_path)])

    assert result.exit_code == 1
    assert "PDF unreadable" in result.stdout
    assert not (tmp_path / "bad_analysis").exists()
    assert not (tmp_path / "bad_note.md").exists()


def test_local_gate_run_command_prints_report(tmp_path: Path) -> None:
    analysis_dir = tmp_path / "paper_analysis"
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    write_json(
        analysis_dir / "metadata.json",
        {"title": "PDF Paper", "source_type": "pdf_path", "pdf_path": str(pdf_path)},
    )
    write_json(analysis_dir / "summary.json", trusted_summary())
    write_json(analysis_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    (analysis_dir / "note.md").write_text("# [Codex Summary] PDF Paper - 2026-06-29\n", encoding="utf-8")
    (analysis_dir / "note.html").write_text("<h1>[Codex Summary] PDF Paper - 2026-06-29</h1>", encoding="utf-8")
    write_json(analysis_dir / "run.json", {"final_note_path": str(tmp_path / "paper_note.md"), "version_suffix": ""})

    result = CliRunner().invoke(app, ["local-gate-run", str(analysis_dir), "--generated-date", "2026-06-29"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "local_ready"
    assert "parentKey" not in payload


def test_prepare_local_note_candidate_command_writes_final_note_without_payload(tmp_path: Path) -> None:
    analysis_dir = tmp_path / "paper_analysis"
    final_note_path = tmp_path / "paper_note.md"
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    write_json(
        analysis_dir / "metadata.json",
        {
            "title": "PDF Paper",
            "creators": "A. Author",
            "date": "2026",
            "source_type": "pdf_path",
            "pdf_path": str(pdf_path),
        },
    )
    write_json(analysis_dir / "summary.json", trusted_summary())
    write_json(analysis_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    write_json(analysis_dir / "run.json", {"final_note_path": str(final_note_path), "version_suffix": ""})

    result = CliRunner().invoke(
        app,
        ["prepare-local-note-candidate", str(analysis_dir), "--generated-date", "2026-06-29"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "local_ready"
    assert final_note_path.exists()
    assert not (analysis_dir / "write-payload.json").exists()
