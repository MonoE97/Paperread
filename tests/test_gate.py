from __future__ import annotations

import json
from pathlib import Path

from paperread.gate import build_gate_report


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def trusted_summary() -> dict:
    return {
        "review_status": "passed_with_caveats",
        "improvement_status": "not_needed",
        "trust_status": "usable_with_caveats",
        "paper_type": "research_article",
        "one_sentence_summary": "ok",
        "abstract_translation": "摘要",
        "research_question": "问题",
        "method": "方法",
        "experiments": "实验",
        "ai4s_relevance": "相关",
        "key_points": ["point"],
        "contributions": ["contribution"],
        "limitations": ["limitation"],
        "follow_up_keywords": ["keyword"],
        "trust_rationale": "complete text",
        "evidence_summary": [{"claim": "claim", "evidence": [{"locator": "context.md page 1", "summary": "evidence"}]}],
    }


def live_notes_enrichment(*, item_key: str = "ABC123", note_count: int = 0) -> dict:
    return {
        "_paperread": {
            "enrichment": {
                "live_notes": {
                    "status": "refreshed",
                    "source": "zotero_local_api_readonly",
                    "item_key": item_key,
                    "refreshed_at": "2026-06-22T12:00:00Z",
                    "note_count": note_count,
                }
            }
        }
    }


def test_build_gate_report_passes_ready_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_json(run_dir / "summary.json", trusted_summary())
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    write_json(
        run_dir / "item-details.json",
        {
            "key": "ABC123",
            "title": "Example Paper",
            "notes": [],
            **live_notes_enrichment(note_count=0),
        },
    )
    (run_dir / "note.md").write_text("# [Codex Summary] Example Paper - 2026-05-06\n", encoding="utf-8")
    (run_dir / "note.html").write_text("<h1>[Codex Summary] Example Paper - 2026-05-06</h1>", encoding="utf-8")

    report = build_gate_report(run_dir, paper_title="Example Paper", generated_date="2026-05-06")

    assert report["status"] == "write_ready"
    assert report["parentKey"] == "ABC123"
    assert report["note_html_path"].endswith("note.html")


def test_build_gate_report_blocks_missing_note_html(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_json(run_dir / "summary.json", {"review_status": "not_reviewed"})
    write_json(run_dir / "review.json", {"needs_improvement": True})
    write_json(run_dir / "item-details.json", {"key": "ABC123", "title": "Example Paper"})

    report = build_gate_report(run_dir, paper_title="Example Paper", generated_date="2026-05-06")

    assert report["status"] == "blocked"
    assert "missing note.html" in report["blockers"]


def test_build_gate_report_blocks_without_live_note_refresh(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_json(run_dir / "summary.json", trusted_summary())
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    write_json(run_dir / "item-details.json", {"key": "ABC123", "title": "Example Paper", "notes": []})
    (run_dir / "note.md").write_text("# [Codex Summary] Example Paper - 2026-05-06\n", encoding="utf-8")
    (run_dir / "note.html").write_text("<h1>[Codex Summary] Example Paper - 2026-05-06</h1>", encoding="utf-8")

    report = build_gate_report(run_dir, paper_title="Example Paper", generated_date="2026-05-06")

    assert report["status"] == "blocked"
    assert "item-details.json live_notes refresh missing or stale" in report["blockers"]


def test_build_gate_report_blocks_missing_parent_key(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_json(run_dir / "summary.json", trusted_summary())
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    write_json(
        run_dir / "item-details.json",
        {
            "key": "",
            "title": "Example Paper",
            "notes": [],
            **live_notes_enrichment(item_key="", note_count=0),
        },
    )
    (run_dir / "note.md").write_text("# [Codex Summary] Example Paper - 2026-05-06\n", encoding="utf-8")
    (run_dir / "note.html").write_text("<h1>[Codex Summary] Example Paper - 2026-05-06</h1>", encoding="utf-8")

    report = build_gate_report(run_dir, paper_title="Example Paper", generated_date="2026-05-06")

    assert report["status"] == "blocked"
    assert "item-details.json key is required" in report["blockers"]
    assert "item-details.json live_notes refresh missing or stale" in report["blockers"]


def test_build_gate_report_uses_live_notes_for_version_suffix(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_json(run_dir / "summary.json", trusted_summary())
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    write_json(
        run_dir / "item-details.json",
        {
            "key": "ABC123",
            "title": "Example Paper",
            "notes": ["<h1>[Codex Summary] Example Paper - 2026-05-06</h1>"],
            **live_notes_enrichment(note_count=1),
        },
    )
    (run_dir / "note.md").write_text("# [Codex Summary] Example Paper - 2026-05-06 (v2)\n", encoding="utf-8")
    (run_dir / "note.html").write_text("<h1>[Codex Summary] Example Paper - 2026-05-06 (v2)</h1>", encoding="utf-8")

    report = build_gate_report(run_dir, paper_title="Example Paper", generated_date="2026-05-06")

    assert report["status"] == "write_ready"
    assert report["version_suffix"] == " (v2)"
    assert report["note_title"] == "[Codex Summary] Example Paper - 2026-05-06 (v2)"


def test_build_gate_report_blocks_note_file_title_mismatch(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_json(run_dir / "summary.json", trusted_summary())
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    write_json(
        run_dir / "item-details.json",
        {
            "key": "ABC123",
            "title": "Example Paper",
            "notes": ["<h1>[Codex Summary] Example Paper - 2026-05-06</h1>"],
            **live_notes_enrichment(note_count=1),
        },
    )
    (run_dir / "note.md").write_text("# [Codex Summary] Example Paper - 2026-05-06\n", encoding="utf-8")
    (run_dir / "note.html").write_text("<h1>[Codex Summary] Example Paper - 2026-05-06</h1>", encoding="utf-8")

    report = build_gate_report(run_dir, paper_title="Example Paper", generated_date="2026-05-06")

    assert report["status"] == "blocked"
    assert (
        "note.md title mismatch: expected [Codex Summary] Example Paper - 2026-05-06 (v2), "
        "got [Codex Summary] Example Paper - 2026-05-06"
    ) in report["blockers"]
    assert (
        "note.html h1 mismatch: expected [Codex Summary] Example Paper - 2026-05-06 (v2), "
        "got [Codex Summary] Example Paper - 2026-05-06"
    ) in report["blockers"]
