from __future__ import annotations

import hashlib
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


def test_build_write_payload_includes_title_version_and_content_hash(tmp_path: Path) -> None:
    note_path = tmp_path / "note.html"
    note_path.write_text("<h1>[Codex Summary] Paper - 2026-06-22 (v2)</h1>", encoding="utf-8")

    payload = build_write_payload(
        {
            "status": "write_ready",
            "parentKey": "P1",
            "note_html_path": str(note_path),
            "note_title": "[Codex Summary] Paper - 2026-06-22 (v2)",
            "version_suffix": " (v2)",
            "tags": ["codex-summary", "paper-summary"],
        }
    )

    assert payload["action"] == "create"
    assert payload["noteTitle"] == "[Codex Summary] Paper - 2026-06-22 (v2)"
    assert payload["versionSuffix"] == " (v2)"
    assert payload["contentSha256"] == "57acd0190ab524cb2a04bc2b8b40bcaa5c5c588a8814d37a1eaa250567483d47"
    assert payload["required_readback_checks"]["expectedTitle"] == "[Codex Summary] Paper - 2026-06-22 (v2)"
    assert payload["required_readback_checks"]["versionSuffix"] == " (v2)"
    assert payload["required_readback_checks"]["contentSha256"] == payload["contentSha256"]


def test_build_write_payload_content_hash_ignores_trailing_newline(tmp_path: Path) -> None:
    html = "<h1>[Codex Summary] Paper - 2026-06-22 (v2)</h1>"
    note_path = tmp_path / "note.html"
    note_path.write_text(html + "\n", encoding="utf-8")

    payload = build_write_payload(
        {
            "status": "write_ready",
            "parentKey": "P1",
            "note_html_path": str(note_path),
            "note_title": "[Codex Summary] Paper - 2026-06-22 (v2)",
            "version_suffix": " (v2)",
            "tags": ["codex-summary", "paper-summary"],
        }
    )

    assert payload["contentSha256"] == hashlib.sha256(html.encode("utf-8")).hexdigest()
    assert payload["contentLength"] == len(html)
    assert payload["required_readback_checks"]["contentLengthAtLeast"] == max(len(html) - 20, 0)
    assert payload["required_readback_checks"]["contentSha256"] == payload["contentSha256"]
