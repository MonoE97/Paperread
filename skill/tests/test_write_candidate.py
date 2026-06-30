from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperread.write_candidate import prepare_write_candidate


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


def prepare_run_dir(tmp_path: Path, *, title: str) -> Path:
    run_dir = tmp_path / title.lower().replace(" ", "-")
    write_json(run_dir / "item-details.json", {"key": "P1", "title": title, "notes": []})
    write_json(run_dir / "metadata.json", {"key": "P1", "title": title, "creators": [], "date": "2026"})
    write_json(run_dir / "summary.json", trusted_summary())
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    return run_dir


def test_prepare_write_candidate_refreshes_live_notes_and_writes_payload(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_json(run_dir / "item-details.json", {"key": "P1", "title": "Paper", "notes": []})
    write_json(run_dir / "metadata.json", {"key": "P1", "title": "Paper", "creators": [], "date": "2026"})
    write_json(run_dir / "summary.json", trusted_summary())
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})

    def fake_fetch_item_children_notes(item_key: str, *, base_url: str):
        assert item_key == "P1"
        assert base_url == "http://zotero.test"
        return [
            {
                "key": "N1",
                "parentItem": "P1",
                "title": "[Codex Summary] Paper - 2026-06-22",
                "note": "<h1>[Codex Summary] Paper - 2026-06-22</h1><p>old body</p>",
                "tags": ["codex-summary"],
            }
        ]

    result = prepare_write_candidate(
        run_dir,
        paper_title="Paper",
        generated_date="2026-06-22",
        base_url="http://zotero.test",
        fetch_live_notes=fake_fetch_item_children_notes,
        refreshed_at="2026-06-22T12:00:00Z",
    )

    assert result["status"] == "write_ready"
    assert result["version_suffix"] == " (v2)"
    assert result["write_payload_path"] == str(run_dir / "write-payload.json")
    assert (run_dir / "note.md").read_text(encoding="utf-8").startswith(
        "# [Codex Summary] Paper - 2026-06-22 (v2)"
    )
    payload = json.loads((run_dir / "write-payload.json").read_text(encoding="utf-8"))
    assert payload["action"] == "create"
    assert payload["noteTitle"] == "[Codex Summary] Paper - 2026-06-22 (v2)"
    refreshed = json.loads((run_dir / "item-details.json").read_text(encoding="utf-8"))
    assert refreshed["_paperread"]["enrichment"]["live_notes"]["titles"] == [
        "[Codex Summary] Paper - 2026-06-22"
    ]


def test_prepare_write_candidate_removes_stale_payload_when_blocked(tmp_path: Path) -> None:
    run_dir = prepare_run_dir(tmp_path, title="Paper")
    write_json(run_dir / "review.json", {"review_status": "failed", "needs_improvement": True})
    stale_payload_path = run_dir / "write-payload.json"
    write_json(stale_payload_path, {"action": "create", "parentKey": "stale"})

    def fake_fetch_item_children_notes(item_key: str, *, base_url: str):
        return []

    result = prepare_write_candidate(
        run_dir,
        paper_title="Paper",
        generated_date="2026-06-22",
        base_url="http://zotero.test",
        fetch_live_notes=fake_fetch_item_children_notes,
        refreshed_at="2026-06-22T12:00:00Z",
    )

    assert result["status"] == "blocked"
    assert "review.json needs_improvement is not false" in result["blockers"]
    assert not stale_payload_path.exists()


@pytest.mark.parametrize(
    ("live_titles", "expected_suffix"),
    [
        ([], ""),
        (["[Codex Summary] Paper - 2026-06-22"], " (v2)"),
        (
            [
                "[Codex Summary] Paper - 2026-06-22",
                "[Codex Summary] Paper - 2026-06-22 (v2)",
            ],
            " (v3)",
        ),
        (["[Codex Summary] Paper - 2026-06-21"], ""),
    ],
)
def test_prepare_write_candidate_version_suffix_matrix(
    tmp_path: Path,
    live_titles: list[str],
    expected_suffix: str,
) -> None:
    run_dir = prepare_run_dir(tmp_path, title="Paper")

    def fake_fetch_item_children_notes(item_key: str, *, base_url: str):
        return [
            {
                "key": f"N{index}",
                "parentItem": item_key,
                "title": title,
                "note": f"<h1>{title}</h1>",
                "tags": ["codex-summary"],
            }
            for index, title in enumerate(live_titles, start=1)
        ]

    result = prepare_write_candidate(
        run_dir,
        paper_title="Paper",
        generated_date="2026-06-22",
        base_url="http://zotero.test",
        fetch_live_notes=fake_fetch_item_children_notes,
        refreshed_at="2026-06-22T12:00:00Z",
    )

    assert result["status"] == "write_ready"
    assert result["version_suffix"] == expected_suffix
    expected_title = f"[Codex Summary] Paper - 2026-06-22{expected_suffix}"
    payload = json.loads((run_dir / "write-payload.json").read_text(encoding="utf-8"))
    assert payload["noteTitle"] == expected_title
    assert (run_dir / "note.html").read_text(encoding="utf-8").startswith(f"<h1>{expected_title}</h1>")
