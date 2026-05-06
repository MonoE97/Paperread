from __future__ import annotations

from pathlib import Path

from zotero_paperread.write_payload import build_write_payload


def test_build_write_payload_includes_content_length_and_snippets(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "note.html").write_text("<h1>Title</h1><p>Body</p>", encoding="utf-8")
    gate_report = {
        "status": "write_ready",
        "parentKey": "ABC123",
        "note_html_path": str(run_dir / "note.html"),
        "tags": ["codex-summary", "paper-summary"],
        "note_title": "[Codex Summary] Example - 2026-05-06",
    }

    payload = build_write_payload(gate_report)

    assert payload["parentKey"] == "ABC123"
    assert payload["contentLength"] == len("<h1>Title</h1><p>Body</p>")
    assert payload["tags"] == ["codex-summary", "paper-summary"]
    assert payload["required_readback_checks"]["parentKey"] == "ABC123"
