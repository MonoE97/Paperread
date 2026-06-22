from __future__ import annotations

import hashlib
import json

import pytest

from zotero_paperread.zotero_live import (
    LiveNoteVerificationError,
    fetch_item_children_notes,
    refresh_details_with_live_notes,
    verify_note_snapshot,
)


def test_fetch_item_children_notes_uses_read_only_get_urls() -> None:
    calls: list[str] = []

    def fake_fetch_json(url: str) -> object:
        calls.append(url)
        return [
            {
                "key": "N1",
                "data": {
                    "itemType": "note",
                    "parentItem": "P1",
                    "note": "<h1>[Codex Summary] Paper - 2026-06-22</h1>",
                    "tags": [{"tag": "codex-summary"}],
                },
            },
            {"key": "A1", "data": {"itemType": "attachment", "title": "PDF"}},
        ]

    notes = fetch_item_children_notes("P1", base_url="http://127.0.0.1:23119", fetch_json=fake_fetch_json)

    assert calls == [
        "http://127.0.0.1:23119/api/users/0/items/P1/children?format=json&limit=100&start=0",
    ]
    assert notes == [
        {
            "key": "N1",
            "parentItem": "P1",
            "title": "[Codex Summary] Paper - 2026-06-22",
            "note": "<h1>[Codex Summary] Paper - 2026-06-22</h1>",
            "tags": ["codex-summary"],
        }
    ]


def test_fetch_item_children_notes_paginates_until_short_page() -> None:
    calls: list[str] = []

    def fake_fetch_json(url: str) -> object:
        calls.append(url)
        if "start=0" in url:
            return [
                {
                    "key": f"N{i}",
                    "data": {
                        "itemType": "note",
                        "parentItem": "P1",
                        "note": f"<h1>[Codex Summary] Paper - 2026-06-22 (v{i})</h1>",
                        "tags": [],
                    },
                }
                for i in range(100)
            ]
        if "start=100" in url:
            return [
                {
                    "key": "N100",
                    "data": {
                        "itemType": "note",
                        "parentItem": "P1",
                        "note": "<h1>[Codex Summary] Paper - 2026-06-22 (v100)</h1>",
                        "tags": [],
                    },
                }
            ]
        raise AssertionError(url)

    notes = fetch_item_children_notes("P1", base_url="http://127.0.0.1:23119", fetch_json=fake_fetch_json)

    assert len(notes) == 101
    assert calls[-1].endswith("start=100")
    assert notes[-1]["title"] == "[Codex Summary] Paper - 2026-06-22 (v100)"


def test_refresh_details_with_live_notes_records_provenance() -> None:
    details = {"key": "P1", "title": "Paper", "notes": ["old"]}
    live_notes = [
        {
            "key": "N1",
            "parentItem": "P1",
            "title": "[Codex Summary] Paper - 2026-06-22",
            "note": "<h1>[Codex Summary] Paper - 2026-06-22</h1><p>large body omitted</p>",
            "tags": [],
        }
    ]

    refreshed = refresh_details_with_live_notes(
        details,
        live_notes=live_notes,
        base_url="http://127.0.0.1:23119",
        refreshed_at="2026-06-22T12:00:00Z",
    )

    assert refreshed["notes"] == ["<h1>[Codex Summary] Paper - 2026-06-22</h1>"]
    live = refreshed["_paperread"]["enrichment"]["live_notes"]
    assert live["status"] == "refreshed"
    assert live["source"] == "zotero_local_api_readonly"
    assert live["item_key"] == "P1"
    assert live["base_url"] == "http://127.0.0.1:23119"
    assert live["refreshed_at"] == "2026-06-22T12:00:00Z"
    assert live["note_count"] == 1
    assert live["note_keys"] == ["N1"]
    assert live["titles"] == ["[Codex Summary] Paper - 2026-06-22"]
    assert "large body omitted" not in json.dumps(refreshed, ensure_ascii=False)


def test_verify_note_snapshot_accepts_expected_note() -> None:
    snapshot = {
        "key": "N1",
        "data": {
            "itemType": "note",
            "parentItem": "P1",
            "note": "<h1>[Codex Summary] Paper - 2026-06-22 (v2)</h1><h2>0. 阅读结论</h2><p>body</p>",
            "tags": [{"tag": "codex-summary"}, {"tag": "paper-summary"}],
        },
    }

    report = verify_note_snapshot(
        snapshot,
        expected_parent="P1",
        expected_title="[Codex Summary] Paper - 2026-06-22 (v2)",
        required_headings=["0. 阅读结论"],
        forbidden_headings=["9. 元数据"],
        expected_tags=["codex-summary", "paper-summary"],
        min_content_length=20,
    )

    assert report["status"] == "passed"
    assert report["noteKey"] == "N1"
    assert report["parentKey"] == "P1"
    assert report["contentLength"] >= 20


def test_verify_note_snapshot_reports_old_layout() -> None:
    snapshot = {
        "key": "N1",
        "data": {
            "itemType": "note",
            "parentItem": "P1",
            "note": "<h1>[Codex Summary] Paper - 2026-06-22</h1><h2>9. 元数据</h2>",
            "tags": [{"tag": "codex-summary"}],
        },
    }

    with pytest.raises(LiveNoteVerificationError) as exc:
        verify_note_snapshot(
            snapshot,
            expected_parent="P1",
            expected_title="[Codex Summary] Paper - 2026-06-22",
            required_headings=["0. 阅读结论"],
            forbidden_headings=["9. 元数据"],
            expected_tags=["codex-summary", "paper-summary"],
            min_content_length=20,
        )

    errors = exc.value.errors
    assert "missing required heading: 0. 阅读结论" in errors
    assert "forbidden heading present: 9. 元数据" in errors
    assert "missing tag: paper-summary" in errors


def test_verify_note_snapshot_requires_h1_title_match() -> None:
    snapshot = {
        "key": "N1",
        "data": {
            "itemType": "note",
            "parentItem": "P1",
            "note": "<h1>Wrong Title</h1><p>[Codex Summary] Paper - 2026-06-22 (v2)</p><h2>0. 阅读结论</h2>",
            "tags": [{"tag": "codex-summary"}, {"tag": "paper-summary"}],
        },
    }

    with pytest.raises(LiveNoteVerificationError) as exc:
        verify_note_snapshot(
            snapshot,
            expected_parent="P1",
            expected_title="[Codex Summary] Paper - 2026-06-22 (v2)",
            required_headings=["0. 阅读结论"],
            forbidden_headings=[],
            expected_tags=["codex-summary", "paper-summary"],
            min_content_length=20,
        )

    assert "title mismatch: expected [Codex Summary] Paper - 2026-06-22 (v2), got Wrong Title" in exc.value.errors


def test_verify_note_snapshot_checks_expected_content_hash() -> None:
    note = "<h1>[Codex Summary] Paper - 2026-06-22 (v2)</h1><h2>0. 阅读结论</h2><p>body</p>"
    snapshot = {
        "key": "N1",
        "data": {
            "itemType": "note",
            "parentItem": "P1",
            "note": note,
            "tags": [{"tag": "codex-summary"}, {"tag": "paper-summary"}],
        },
    }
    content_hash = hashlib.sha256(note.encode("utf-8")).hexdigest()

    report = verify_note_snapshot(
        snapshot,
        expected_parent="P1",
        expected_title="[Codex Summary] Paper - 2026-06-22 (v2)",
        required_headings=["0. 阅读结论"],
        forbidden_headings=[],
        expected_tags=["codex-summary", "paper-summary"],
        min_content_length=20,
        expected_content_sha256=content_hash,
    )

    assert report["status"] == "passed"
    assert report["contentSha256"] == content_hash

    with pytest.raises(LiveNoteVerificationError) as exc:
        verify_note_snapshot(
            snapshot,
            expected_parent="P1",
            expected_title="[Codex Summary] Paper - 2026-06-22 (v2)",
            required_headings=["0. 阅读结论"],
            forbidden_headings=[],
            expected_tags=["codex-summary", "paper-summary"],
            min_content_length=20,
            expected_content_sha256="0" * 64,
        )

    assert f"content hash mismatch: expected {'0' * 64}, got {content_hash}" in exc.value.errors
