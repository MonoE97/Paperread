from __future__ import annotations

import json
from pathlib import Path

from zotero_paperread.local_candidate import prepare_local_note_candidate
from zotero_paperread.local_gate import build_local_gate_report


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def trusted_summary() -> dict:
    return {
        "review_status": "passed_with_caveats",
        "improvement_status": "not_needed",
        "trust_status": "usable_with_caveats",
        "paper_type": "research_article",
        "one_sentence_summary": "这篇论文提出一种低成本硫银锗矿电解质。",
        "abstract_translation": "作者研究了空气稳定性和界面兼容性。",
        "research_question": "如何在低成本条件下提高硫银锗矿电解质的空气稳定性？",
        "method": "方法结合材料合成、电化学阻抗和界面表征。",
        "experiments": "实验比较了空气暴露后的阻抗和电池循环表现。",
        "ai4s_relevance": "该工作可为固态电解质筛选提供实验约束。",
        "key_points": ["低成本路线", "空气稳定性", "界面兼容性"],
        "contributions": ["提出稳定电解质设计", "验证锂金属界面兼容性"],
        "limitations": ["长期空气暴露窗口仍需进一步验证"],
        "follow_up_keywords": ["argyrodite", "solid_state_electrolyte"],
        "trust_rationale": "证据来自 PDF 正文和图表候选，仍需注意抽取质量。",
        "evidence_summary": [
            {
                "claim": "材料具有较好的空气稳定性。",
                "evidence": [{"locator": "context.md page 1", "summary": "摘要描述了空气稳定性。"}],
            }
        ],
    }


def prepare_ready_analysis_dir(tmp_path: Path) -> Path:
    analysis_dir = tmp_path / "paper_analysis"
    write_json(
        analysis_dir / "metadata.json",
        {
            "title": "Example PDF Paper",
            "creators": "A. Researcher",
            "date": "2026",
            "source_type": "pdf_path",
        },
    )
    write_json(analysis_dir / "summary.json", trusted_summary())
    write_json(analysis_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    (analysis_dir / "note.md").write_text(
        "# [Codex Summary] Example PDF Paper - 2026-06-29\n",
        encoding="utf-8",
    )
    (analysis_dir / "note.html").write_text(
        "<h1>[Codex Summary] Example PDF Paper - 2026-06-29</h1>",
        encoding="utf-8",
    )
    write_json(
        analysis_dir / "run.json",
        {
            "title": "Example PDF Paper",
            "source_type": "pdf_path",
            "final_note_path": str(tmp_path / "paper_note.md"),
        },
    )
    return analysis_dir


def test_build_local_gate_report_blocks_missing_required_files(tmp_path: Path) -> None:
    analysis_dir = tmp_path / "paper_analysis"
    analysis_dir.mkdir()

    report = build_local_gate_report(analysis_dir, generated_date="2026-06-29")

    assert report["status"] == "blocked"
    assert "missing summary.json" in report["blockers"]
    assert "missing review.json" in report["blockers"]
    assert "missing run.json" in report["blockers"]
    assert "missing note.md" in report["blockers"]
    assert "missing note.html" in report["blockers"]


def test_build_local_gate_report_ready_has_no_zotero_write_fields(tmp_path: Path) -> None:
    analysis_dir = prepare_ready_analysis_dir(tmp_path)

    report = build_local_gate_report(analysis_dir, generated_date="2026-06-29")

    assert report["status"] == "local_ready"
    assert report["analysis_dir"] == str(analysis_dir)
    assert report["final_note_path"] == str(tmp_path / "paper_note.md")
    assert report["note_md_path"] == str(analysis_dir / "note.md")
    assert report["note_html_path"] == str(analysis_dir / "note.html")
    assert report["tags"][:2] == ["codex-summary", "paper-summary"]
    assert "parentKey" not in report
    assert "write_payload_path" not in report


def test_build_local_gate_report_blocks_review_needing_improvement(tmp_path: Path) -> None:
    analysis_dir = prepare_ready_analysis_dir(tmp_path)
    write_json(analysis_dir / "review.json", {"review_status": "failed", "needs_improvement": True})

    report = build_local_gate_report(analysis_dir, generated_date="2026-06-29")

    assert report["status"] == "blocked"
    assert "review.json needs_improvement is not false" in report["blockers"]


def test_build_local_gate_report_blocks_invalid_json_without_traceback(tmp_path: Path) -> None:
    analysis_dir = prepare_ready_analysis_dir(tmp_path)
    (analysis_dir / "summary.json").write_text("{not-json", encoding="utf-8")

    report = build_local_gate_report(analysis_dir, generated_date="2026-06-29")

    assert report["status"] == "blocked"
    assert any(blocker.startswith("invalid summary.json:") for blocker in report["blockers"])
    assert "trusted summary: summary.json unavailable" in report["blockers"]


def test_build_local_gate_report_requires_pdf_source_and_final_note_path(tmp_path: Path) -> None:
    analysis_dir = prepare_ready_analysis_dir(tmp_path)
    metadata = json.loads((analysis_dir / "metadata.json").read_text(encoding="utf-8"))
    metadata["source_type"] = "zotero"
    write_json(analysis_dir / "metadata.json", metadata)
    write_json(analysis_dir / "run.json", {"title": "Example PDF Paper", "source_type": "pdf_path"})

    report = build_local_gate_report(analysis_dir, generated_date="2026-06-29")

    assert report["status"] == "blocked"
    assert "metadata.json source_type must be pdf_path" in report["blockers"]
    assert "run.json final_note_path is required" in report["blockers"]


def test_build_local_gate_report_blocks_empty_run_manifest(tmp_path: Path) -> None:
    analysis_dir = prepare_ready_analysis_dir(tmp_path)
    write_json(analysis_dir / "run.json", {})

    report = build_local_gate_report(analysis_dir, generated_date="2026-06-29")

    assert report["status"] == "blocked"
    assert "run.json final_note_path is required" in report["blockers"]


def test_prepare_local_note_candidate_writes_previews_tags_and_final_markdown(tmp_path: Path) -> None:
    analysis_dir = prepare_ready_analysis_dir(tmp_path)
    final_note_path = tmp_path / "paper_note.md"
    for path in [
        analysis_dir / "note.md",
        analysis_dir / "note.html",
        analysis_dir / "preview-note-md.txt",
        analysis_dir / "preview-note-html.txt",
        analysis_dir / "note-tags.json",
        final_note_path,
    ]:
        path.unlink(missing_ok=True)

    result = prepare_local_note_candidate(analysis_dir, generated_date="2026-06-29")

    assert result["status"] == "local_ready"
    assert Path(result["note_md_path"]).exists()
    assert Path(result["note_html_path"]).exists()
    assert (analysis_dir / "preview-note-md.txt").exists()
    assert (analysis_dir / "preview-note-html.txt").exists()
    assert json.loads((analysis_dir / "note-tags.json").read_text(encoding="utf-8"))[:2] == [
        "codex-summary",
        "paper-summary",
    ]
    assert final_note_path.read_text(encoding="utf-8") == (analysis_dir / "note.md").read_text(encoding="utf-8")
    assert not (analysis_dir / "write-payload.json").exists()
