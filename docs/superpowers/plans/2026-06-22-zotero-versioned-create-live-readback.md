# Zotero Smooth Versioned Write Candidate Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Implementation Status:** Completed and merged to `main` on 2026-06-23. The durable workflow is now documented in `README.md`, `AGENTS.md`, `skills/zotero-paper-summary/SKILL.md`, and `docs/references/zotero-batch-write-runbook.md`. Current code includes `prepare-write-candidate`, read-only `refresh-live-notes`, `verify-zotero-note`, canonical note HTML hashing, stale payload removal, and strict `write-payload.json` output path checks.

**Goal:** Make future Zotero paper-summary writes smooth and repeatable by generating a fully checked versioned write candidate from any run directory before the agent calls Zotero MCP.

**Architecture:** Keep all persistent Zotero writes behind `zotero-mcp write_note`; add a project-local read-only Zotero local API client only for live child-note discovery and note verification. Add a one-command `prepare-write-candidate` workflow that runs live refresh, suffix calculation, note regeneration, preview, gate, and payload generation in a fixed order, so daily use does not depend on hand-running a fragile command chain. The write gate must require live note refresh provenance and must verify the generated `note.md`/`note.html` titles match the computed `note_title` before reporting `write_ready`. Historical note migration keeps `write_note(update)` as an explicit migration-only path, but update timeout must stop instead of silently switching write channels.

**Tech Stack:** Python stdlib `urllib.request`, `html.parser`, `hashlib`, Typer CLI, pytest, Jinja2 note rendering, Zotero local API read-only GET requests, Zotero MCP for writes.

---

## File Structure

- Modify `AGENTS.md`
  - Record the new project rule: single-paper summary writes always create a new versioned note; no update of existing summary notes.
  - Record that Zotero local API is allowed only for read-only live note discovery and readback verification.
- Modify `README.md`
  - Update Trusted Notes write-through instructions and migration timeout behavior.
  - Document `prepare-write-candidate` as the recommended daily entry point and keep the lower-level gate order for debugging.
- Modify `skills/zotero-paper-summary/SKILL.md`
  - Update the single-paper workflow to run `prepare-write-candidate` before calling `write_note(action="create")`.
  - State that `write_note(action="update")` is not used for normal paper summaries.
- Modify `skills/zotero-batch-note-writing/SKILL.md`
  - Update the per-item gate chain so batch work also refreshes live child-note titles before suffix calculation.
- Create `src/zotero_paperread/zotero_live.py`
  - Provide paginated read-only Zotero local API helpers for item children and note verification.
  - Store structured live note summaries for version calculation; do not copy full note bodies into `item-details.json`.
- Create `src/zotero_paperread/write_candidate.py`
  - Orchestrate the safe daily workflow from a run directory: live refresh, suffix, note render, previews, gate, and write payload.
- Modify `src/zotero_paperread/cli.py`
  - Add `refresh-live-notes`.
  - Add `verify-zotero-note`.
  - Add `prepare-write-candidate`.
  - Extend `preview-note` with `--output`.
- Modify `src/zotero_paperread/gate.py`
  - Require live note refresh provenance in `item-details.json` before `write_ready`.
  - Require `note.md` and `note.html` titles to match computed `note_title`.
- Modify `src/zotero_paperread/write_payload.py`
  - Keep action as `create`.
  - Include computed `note_title`, `version_suffix`, and `contentSha256` in payload readback checks.
- Modify `src/zotero_paperread/zotero_details.py`
  - Reuse existing title extraction against structured live note title snippets.
- Create `tests/test_zotero_live.py`
  - Unit-test read-only paginated URL construction, child-note extraction, minimal enrichment provenance, and note verification.
- Create `tests/test_write_candidate.py`
  - Unit-test the one-command workflow without touching Zotero.
- Modify `tests/test_cli_note.py`
  - Cover new CLI commands and `preview-note --output`.
- Modify `tests/test_gate.py`
  - Cover blocking behavior when live note refresh provenance is absent.
- Modify `tests/test_write_payload.py`
  - Cover `note_title`, `version_suffix`, `contentSha256`, and `action=create`.
- Modify `tests/test_default_workflow_docs.py`
  - Keep docs/skill text aligned with the new write contract.

## Task 1: Update Rules Before Implementation

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `skills/zotero-paper-summary/SKILL.md`
- Modify: `skills/zotero-batch-note-writing/SKILL.md`
- Modify: `tests/test_default_workflow_docs.py`

- [ ] **Step 1: Add failing docs-contract tests**

Add this test to `tests/test_default_workflow_docs.py`:

```python
def test_single_paper_write_contract_uses_versioned_create_only() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    skill = Path("skills/zotero-paper-summary/SKILL.md").read_text(encoding="utf-8")
    batch_skill = Path("skills/zotero-batch-note-writing/SKILL.md").read_text(encoding="utf-8")
    agents = Path("AGENTS.md").read_text(encoding="utf-8")

    required = [
        "single-paper summary writes always create a new versioned Zotero child note",
        "Zotero local API is read-only in this project",
        "prepare-write-candidate",
        "refresh-live-notes",
        "write_note(action=\"create\"",
    ]
    for text in (readme, skill, agents):
        for phrase in required:
            assert phrase in text

    assert "write_note(action=\"update\"" in readme
    assert "historical note migration" in readme
    assert "stop and report the failed update readback" in readme
    assert "refresh-live-notes" in batch_skill
    assert "prepare-write-payload" in batch_skill
```

- [ ] **Step 2: Run the docs-contract test and verify RED**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py::test_single_paper_write_contract_uses_versioned_create_only -q
```

Expected: FAIL because the exact new phrases are not present yet.

- [ ] **Step 3: Update `AGENTS.md` Zotero write rules**

In `AGENTS.md`, under `## 写入规则`, replace the current duplicate-run sentence:

```markdown
- 重复运行不覆盖旧 note；同日重复创建时使用 `[Codex Summary] <paper title> - YYYY-MM-DD (v2)`、`(v3)` 等标题后缀创建新版本。
```

with:

```markdown
- single-paper summary writes always create a new versioned Zotero child note；不 update 既有 `[Codex Summary]` 总结 note。真实写入前必须运行 `prepare-write-candidate` 或等价底层链路，用只读 live note refresh 计算同日后缀；同日重复创建时使用 `[Codex Summary] <paper title> - YYYY-MM-DD (v2)`、`(v3)` 等标题后缀创建新版本。
- Zotero local API is read-only in this project；只允许用于 live 子笔记标题/正文读取和写后验证，禁止通过 Zotero local API、SQLite 或其他非 MCP 路径写入 Zotero。
```

- [ ] **Step 4: Update `README.md` Trusted Notes write-through text**

In `README.md`, in the Trusted Notes write-through section after `prepare-write-payload does not write to Zotero`, add:

```markdown
For single-paper summaries, single-paper summary writes always create a new versioned Zotero child note. Do not update an existing `[Codex Summary]` note for normal paper summaries. The recommended daily command is `prepare-write-candidate <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD`; it runs read-only `refresh-live-notes`, computes the next suffix, regenerates `note.md` and `note.html`, writes previews, runs `gate-run`, and writes `write-payload.json`. The lower-level debug chain is `refresh-live-notes -> next-version-suffix -> finalize-note --html-output -> note-tags -> preview-note note.md/note.html -> gate-run -> prepare-write-payload`. The actual persistent write remains `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`.

Zotero local API is read-only in this project. It may be used by `refresh-live-notes` and `verify-zotero-note`, but it must not be used for PUT, PATCH, POST, DELETE, SQLite mutation, or any persistent write.

For historical note migration, `write_note(action="update", ...)` is still allowed after explicit confirmation because the task is a content-format migration, not a new paper summary. If a migration update times out and readback still shows old content, stop and report the failed update readback; do not create a duplicate migration note unless the user explicitly asks for that separate recovery action.
```

- [ ] **Step 5: Update `skills/zotero-paper-summary/SKILL.md` write step**

In `skills/zotero-paper-summary/SKILL.md`, replace the normal write bullet that says to call `write_note(action="create", ...)` with this stricter sequence:

```markdown
   - single-paper summary writes always create a new versioned Zotero child note；不要 update 既有 `[Codex Summary]` 总结 note。
   - 日常写入前先运行 `prepare-write-candidate <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD`，它会用只读 Zotero local API 刷新 live 子笔记标题、计算后缀、重新生成 `note.md`/`note.html`、预览、跑 gate 并生成 `write-payload.json`。
   - 只有调试底层链路时才手动运行 `refresh-live-notes -> next-version-suffix -> finalize-note --html-output -> note-tags -> preview-note -> gate-run -> prepare-write-payload`。
   - 真实写入仍必须来自用户明确写入意图，且只能调用 `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`。
   - 写完用 `get_item_details` 回读 key/tags，再用 `verify-zotero-note` 只读验证 parent、标题、tags、正文长度和章节结构。
```

- [ ] **Step 6: Update batch skill gate chain**

In `skills/zotero-batch-note-writing/SKILL.md`, replace the central per-item gate chain with:

```markdown
8. Run central per-item gate chain: `create-run -> prepare-item -> validate-summary-json -> apply-review -> lint-summary -> validate-trusted-summary -> refresh-live-notes -> next-version-suffix -> finalize-note --html-output -> note-tags -> preview-note -> gate-run -> prepare-write-payload`. For single-item interactive work, prefer the wrapper `prepare-write-candidate`.
```

- [ ] **Step 7: Update historical migration timeout rule**

In `README.md` and `skills/zotero-paper-summary/SKILL.md`, in historical note table migration sections, add:

```markdown
If `write_note(action="update", ...)` times out during historical note migration, run readback. If readback still shows old content, stop and report the failed update readback. Do not switch to Zotero local API writes, SQLite writes, or automatic duplicate-note creation.
```

- [ ] **Step 8: Run docs tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit docs contract**

Run:

```bash
git add AGENTS.md README.md skills/zotero-paper-summary/SKILL.md skills/zotero-batch-note-writing/SKILL.md tests/test_default_workflow_docs.py
git commit -m "docs: require versioned zotero summary writes"
```

Expected: commit succeeds.

## Task 2: Add Read-Only Zotero Live API Helpers

**Files:**
- Create: `src/zotero_paperread/zotero_live.py`
- Create: `tests/test_zotero_live.py`

- [ ] **Step 1: Write failing live helper tests**

Create `tests/test_zotero_live.py` with:

```python
from __future__ import annotations

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
```

- [ ] **Step 2: Run live helper tests and verify RED**

Run:

```bash
uv run pytest tests/test_zotero_live.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'zotero_paperread.zotero_live'`.

- [ ] **Step 3: Implement `zotero_live.py`**

Create `src/zotero_paperread/zotero_live.py`:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import urlopen


FetchJson = Callable[[str], object]


class LiveNoteVerificationError(ValueError):
    def __init__(self, errors: list[str], report: dict[str, Any]):
        super().__init__("; ".join(errors))
        self.errors = errors
        self.report = report


class _HeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._current_tag = ""
        self._parts: list[str] = []
        self.h1 = ""
        self.headings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"h1", "h2"}:
            self._current_tag = lowered
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._current_tag:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == self._current_tag:
            heading = " ".join("".join(self._parts).split())
            if lowered == "h1" and not self.h1:
                self.h1 = heading
            elif lowered == "h2" and heading:
                self.headings.append(heading)
            self._current_tag = ""
            self._parts = []


def fetch_json_url(url: str) -> object:
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def fetch_item_children_notes(
    item_key: str,
    *,
    base_url: str = "http://127.0.0.1:23119",
    fetch_json: FetchJson = fetch_json_url,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    start = 0
    while True:
        url = _api_url(
            base_url,
            f"/api/users/0/items/{quote(item_key)}/children?format=json&limit={page_size}&start={start}",
        )
        payload = fetch_json(url)
        if not isinstance(payload, list):
            raise ValueError("zotero children response is not a list")

        for item in payload:
            if not isinstance(item, dict):
                continue
            data = item.get("data", {})
            if not isinstance(data, dict) or data.get("itemType") != "note":
                continue
            note_html = str(data.get("note", ""))
            tags = [
                str(tag.get("tag"))
                for tag in data.get("tags", [])
                if isinstance(tag, dict) and str(tag.get("tag", "")).strip()
            ]
            notes.append(
                {
                    "key": str(item.get("key", "")),
                    "parentItem": str(data.get("parentItem", "")),
                    "title": _h1_title(note_html),
                    "note": note_html,
                    "tags": tags,
                }
            )

        if len(payload) < page_size:
            break
        start += page_size
    return notes


def fetch_note_snapshot(
    note_key: str,
    *,
    base_url: str = "http://127.0.0.1:23119",
    fetch_json: FetchJson = fetch_json_url,
) -> dict[str, Any]:
    url = _api_url(base_url, f"/api/users/0/items/{quote(note_key)}?format=json")
    payload = fetch_json(url)
    if not isinstance(payload, dict):
        raise ValueError("zotero note response is not an object")
    return payload


def refresh_details_with_live_notes(
    details: dict[str, Any],
    *,
    live_notes: list[dict[str, Any]],
    base_url: str = "http://127.0.0.1:23119",
    refreshed_at: str | None = None,
) -> dict[str, Any]:
    refreshed = dict(details)
    titles = [str(note.get("title", "")).strip() for note in live_notes if str(note.get("title", "")).strip()]
    refreshed["notes"] = [f"<h1>{title}</h1>" for title in titles]
    paperread = dict(refreshed.get("_paperread", {})) if isinstance(refreshed.get("_paperread"), dict) else {}
    enrichment = dict(paperread.get("enrichment", {})) if isinstance(paperread.get("enrichment"), dict) else {}
    enrichment["live_notes"] = {
        "status": "refreshed",
        "source": "zotero_local_api_readonly",
        "item_key": str(details.get("key", "")),
        "base_url": base_url.rstrip("/"),
        "refreshed_at": refreshed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "note_count": len(live_notes),
        "note_keys": [str(note.get("key", "")) for note in live_notes if str(note.get("key", ""))],
        "titles": titles,
    }
    paperread["enrichment"] = enrichment
    refreshed["_paperread"] = paperread
    return refreshed


def _parse_headings(note_html: str) -> tuple[str, list[str]]:
    parser = _HeadingParser()
    parser.feed(note_html)
    parser.close()
    return parser.h1, parser.headings


def _h1_title(note_html: str) -> str:
    title, _headings = _parse_headings(note_html)
    return title


def verify_note_snapshot(
    snapshot: dict[str, Any],
    *,
    expected_parent: str,
    expected_title: str,
    required_headings: list[str],
    forbidden_headings: list[str],
    expected_tags: list[str],
    min_content_length: int,
) -> dict[str, Any]:
    data = snapshot.get("data", {})
    if not isinstance(data, dict):
        data = {}
    note = str(data.get("note", ""))
    tags = [
        str(tag.get("tag"))
        for tag in data.get("tags", [])
        if isinstance(tag, dict) and str(tag.get("tag", "")).strip()
    ]
    title, headings = _parse_headings(note)
    errors: list[str] = []

    if data.get("itemType") != "note":
        errors.append(f"itemType mismatch: expected note, got {data.get('itemType')}")
    parent = str(data.get("parentItem", ""))
    if parent != expected_parent:
        errors.append(f"parent mismatch: expected {expected_parent}, got {parent}")
    if expected_title and title != expected_title:
        errors.append(f"title mismatch: expected {expected_title}, got {title}")
    if len(note) < min_content_length:
        errors.append(f"content too short: expected at least {min_content_length}, got {len(note)}")
    for heading in required_headings:
        if heading not in headings:
            errors.append(f"missing required heading: {heading}")
    for heading in forbidden_headings:
        if heading in headings:
            errors.append(f"forbidden heading present: {heading}")
    for tag in expected_tags:
        if tag not in tags:
            errors.append(f"missing tag: {tag}")

    report = {
        "status": "failed" if errors else "passed",
        "errors": errors,
        "noteKey": str(snapshot.get("key", "")),
        "parentKey": parent,
        "title": title,
        "contentLength": len(note),
        "headings": headings,
        "tags": tags,
    }
    if errors:
        raise LiveNoteVerificationError(errors, report)
    return report
```

- [ ] **Step 4: Run live helper tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_zotero_live.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit live helper**

Run:

```bash
git add src/zotero_paperread/zotero_live.py tests/test_zotero_live.py
git commit -m "feat: add read-only zotero live note helpers"
```

Expected: commit succeeds.

## Task 3: Add CLI Commands for Live Refresh, Verification, and Preview Output

**Files:**
- Modify: `src/zotero_paperread/cli.py`
- Modify: `tests/test_cli_note.py`

- [ ] **Step 1: Add failing CLI tests**

Append these tests to `tests/test_cli_note.py`:

```python
def test_preview_note_command_can_write_output_file(tmp_path: Path) -> None:
    note_path = tmp_path / "note.md"
    output_path = tmp_path / "preview.txt"
    note_path.write_text("# Title\n\nBody\n", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["preview-note", str(note_path), "--output", str(output_path)])

    assert result.exit_code == 0
    assert "# Title" in result.stdout
    assert output_path.read_text(encoding="utf-8") == "# Title\n\nBody\n"


def test_refresh_live_notes_command_updates_details(monkeypatch, tmp_path: Path) -> None:
    details_path = tmp_path / "item-details.json"
    details_path.write_text(json.dumps({"key": "P1", "title": "Paper", "notes": []}), encoding="utf-8")

    def fake_fetch_item_children_notes(item_key: str, *, base_url: str):
        assert item_key == "P1"
        assert base_url == "http://zotero.test"
        return [
            {
                "key": "N1",
                "parentItem": "P1",
                "title": "[Codex Summary] Paper - 2026-06-22",
                "note": "<h1>[Codex Summary] Paper - 2026-06-22</h1><p>large body omitted</p>",
                "tags": ["codex-summary"],
            }
        ]

    monkeypatch.setattr("zotero_paperread.cli.fetch_item_children_notes", fake_fetch_item_children_notes)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "refresh-live-notes",
            str(details_path),
            "--output",
            str(details_path),
            "--base-url",
            "http://zotero.test",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(details_path.read_text(encoding="utf-8"))
    assert payload["notes"] == ["<h1>[Codex Summary] Paper - 2026-06-22</h1>"]
    assert "large body omitted" not in json.dumps(payload, ensure_ascii=False)
    assert payload["_paperread"]["enrichment"]["live_notes"]["status"] == "refreshed"


def test_verify_zotero_note_command_reports_pass(monkeypatch) -> None:
    def fake_fetch_note_snapshot(note_key: str, *, base_url: str):
        assert note_key == "N1"
        assert base_url == "http://zotero.test"
        return {
            "key": "N1",
            "data": {
                "itemType": "note",
                "parentItem": "P1",
                "note": "<h1>[Codex Summary] Paper - 2026-06-22 (v2)</h1><h2>0. 阅读结论</h2>",
                "tags": [{"tag": "codex-summary"}, {"tag": "paper-summary"}],
            },
        }

    monkeypatch.setattr("zotero_paperread.cli.fetch_note_snapshot", fake_fetch_note_snapshot)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "verify-zotero-note",
            "N1",
            "--expected-parent",
            "P1",
            "--expected-title",
            "[Codex Summary] Paper - 2026-06-22 (v2)",
            "--required-heading",
            "0. 阅读结论",
            "--forbidden-heading",
            "9. 元数据",
            "--expected-tag",
            "codex-summary",
            "--expected-tag",
            "paper-summary",
            "--min-content-length",
            "20",
            "--base-url",
            "http://zotero.test",
        ],
    )

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["status"] == "passed"
    assert report["noteKey"] == "N1"
```

- [ ] **Step 2: Run CLI tests and verify RED**

Run:

```bash
uv run pytest tests/test_cli_note.py::test_preview_note_command_can_write_output_file tests/test_cli_note.py::test_refresh_live_notes_command_updates_details tests/test_cli_note.py::test_verify_zotero_note_command_reports_pass -q
```

Expected: FAIL because the new CLI options and commands do not exist.

- [ ] **Step 3: Import live helpers in `cli.py`**

Add this import block near existing imports:

```python
from zotero_paperread.zotero_live import (
    LiveNoteVerificationError,
    fetch_item_children_notes,
    fetch_note_snapshot,
    refresh_details_with_live_notes,
    verify_note_snapshot,
)
```

- [ ] **Step 4: Extend `preview-note`**

Replace the existing `preview_note_command` with:

```python
@app.command("preview-note")
def preview_note_command(
    note_path: Path,
    output: Path | None = typer.Option(None, "--output", "-o", help="Also write preview text to this file."),
) -> None:
    """Print a rendered note without writing to Zotero."""
    content = note_path.read_text(encoding="utf-8")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
    console.print(content)
```

- [ ] **Step 5: Add `refresh-live-notes` command**

Add this command after `save-item-details`:

```python
@app.command("refresh-live-notes")
def refresh_live_notes_command(
    details_json: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write refreshed item details JSON."),
    base_url: str = typer.Option("http://127.0.0.1:23119", "--base-url", help="Zotero local API base URL."),
) -> None:
    """Refresh item-details notes from Zotero local API using read-only GET requests."""
    details = read_json_or_exit(details_json, label="details JSON")
    item_key = str(details.get("key", "")).strip()
    if not item_key:
        console.print("details JSON missing key")
        raise typer.Exit(1)
    try:
        live_notes = fetch_item_children_notes(item_key, base_url=base_url)
        refreshed = refresh_details_with_live_notes(details, live_notes=live_notes, base_url=base_url)
    except Exception as exc:
        console.print(f"live_notes_refresh_failed: {exc}", soft_wrap=True)
        raise typer.Exit(1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    typer.echo(json.dumps(refreshed["_paperread"]["enrichment"]["live_notes"], ensure_ascii=False))
```

- [ ] **Step 6: Add `verify-zotero-note` command**

Add this command after `refresh-live-notes`:

```python
@app.command("verify-zotero-note")
def verify_zotero_note_command(
    note_key: str,
    expected_parent: str = typer.Option(..., "--expected-parent", help="Expected parent Zotero item key."),
    expected_title: str = typer.Option("", "--expected-title", help="Expected exact h1 note title."),
    required_heading: list[str] = typer.Option([], "--required-heading", help="Required h2 heading text."),
    forbidden_heading: list[str] = typer.Option([], "--forbidden-heading", help="Forbidden h2 heading text."),
    expected_tag: list[str] = typer.Option([], "--expected-tag", help="Tag that must be present on the note."),
    min_content_length: int = typer.Option(0, "--min-content-length", min=0, help="Minimum note HTML length."),
    base_url: str = typer.Option("http://127.0.0.1:23119", "--base-url", help="Zotero local API base URL."),
) -> None:
    """Verify a Zotero note through read-only Zotero local API."""
    try:
        snapshot = fetch_note_snapshot(note_key, base_url=base_url)
        report = verify_note_snapshot(
            snapshot,
            expected_parent=expected_parent,
            expected_title=expected_title,
            required_headings=required_heading,
            forbidden_headings=forbidden_heading,
            expected_tags=expected_tag,
            min_content_length=min_content_length,
        )
    except LiveNoteVerificationError as exc:
        typer.echo(json.dumps(exc.report, ensure_ascii=False, indent=2))
        raise typer.Exit(1)
    except Exception as exc:
        console.print(f"zotero_note_verify_failed: {exc}", soft_wrap=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
```

- [ ] **Step 7: Run targeted CLI tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_cli_note.py::test_preview_note_command_can_write_output_file tests/test_cli_note.py::test_refresh_live_notes_command_updates_details tests/test_cli_note.py::test_verify_zotero_note_command_reports_pass -q
```

Expected: PASS.

- [ ] **Step 8: Commit CLI commands**

Run:

```bash
git add src/zotero_paperread/cli.py tests/test_cli_note.py
git commit -m "feat: add zotero live readback cli"
```

Expected: commit succeeds.

## Task 4: Require Live Notes Refresh and Title Consistency in the Write Gate

**Files:**
- Modify: `src/zotero_paperread/gate.py`
- Modify: `tests/test_gate.py`

- [ ] **Step 1: Add failing gate tests**

Add these tests to `tests/test_gate.py`:

```python
def test_build_gate_report_blocks_without_live_note_refresh(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_json(
        run_dir / "summary.json",
        {
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
            "evidence_summary": [
                {"claim": "claim", "evidence": [{"locator": "context.md page 1", "summary": "evidence"}]}
            ],
        },
    )
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    write_json(run_dir / "item-details.json", {"key": "ABC123", "title": "Example Paper", "notes": []})
    (run_dir / "note.md").write_text("# note", encoding="utf-8")
    (run_dir / "note.html").write_text("<h1>note</h1>", encoding="utf-8")

    report = build_gate_report(run_dir, paper_title="Example Paper", generated_date="2026-05-06")

    assert report["status"] == "blocked"
    assert "item-details.json live_notes refresh missing or stale" in report["blockers"]


def test_build_gate_report_uses_live_notes_for_version_suffix(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_json(
        run_dir / "summary.json",
        {
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
            "evidence_summary": [
                {"claim": "claim", "evidence": [{"locator": "context.md page 1", "summary": "evidence"}]}
            ],
        },
    )
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    write_json(
        run_dir / "item-details.json",
        {
            "key": "ABC123",
            "title": "Example Paper",
            "notes": ["<h1>[Codex Summary] Example Paper - 2026-05-06</h1>"],
            "_paperread": {
                "enrichment": {
                    "live_notes": {
                        "status": "refreshed",
                        "source": "zotero_local_api_readonly",
                        "item_key": "ABC123",
                        "refreshed_at": "2026-06-22T12:00:00Z",
                        "note_count": 1,
                        "note_keys": ["N1"],
                        "titles": ["[Codex Summary] Example Paper - 2026-05-06"],
                    }
                }
            },
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
    write_json(
        run_dir / "summary.json",
        {
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
            "evidence_summary": [
                {"claim": "claim", "evidence": [{"locator": "context.md page 1", "summary": "evidence"}]}
            ],
        },
    )
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    write_json(
        run_dir / "item-details.json",
        {
            "key": "ABC123",
            "title": "Example Paper",
            "notes": ["<h1>[Codex Summary] Example Paper - 2026-05-06</h1>"],
            "_paperread": {
                "enrichment": {
                    "live_notes": {
                        "status": "refreshed",
                        "source": "zotero_local_api_readonly",
                        "item_key": "ABC123",
                        "refreshed_at": "2026-06-22T12:00:00Z",
                        "note_count": 1,
                    }
                }
            },
        },
    )
    (run_dir / "note.md").write_text("# [Codex Summary] Example Paper - 2026-05-06\n", encoding="utf-8")
    (run_dir / "note.html").write_text("<h1>[Codex Summary] Example Paper - 2026-05-06</h1>", encoding="utf-8")

    report = build_gate_report(run_dir, paper_title="Example Paper", generated_date="2026-05-06")

    assert report["status"] == "blocked"
    assert "note.md title mismatch: expected [Codex Summary] Example Paper - 2026-05-06 (v2), got [Codex Summary] Example Paper - 2026-05-06" in report["blockers"]
    assert "note.html h1 mismatch: expected [Codex Summary] Example Paper - 2026-05-06 (v2), got [Codex Summary] Example Paper - 2026-05-06" in report["blockers"]
```

- [ ] **Step 2: Run gate tests and verify RED**

Run:

```bash
uv run pytest tests/test_gate.py::test_build_gate_report_blocks_without_live_note_refresh tests/test_gate.py::test_build_gate_report_uses_live_notes_for_version_suffix tests/test_gate.py::test_build_gate_report_blocks_note_file_title_mismatch -q
```

Expected: first test FAIL because `build_gate_report` currently allows missing live refresh.

- [ ] **Step 3: Add live refresh validation to `gate.py`**

Add this import near existing imports:

```python
from html.parser import HTMLParser
```

Add this helper above `build_gate_report`:

```python
def _has_live_notes_refresh(item_details: dict[str, Any], *, parent_key: str) -> bool:
    paperread = item_details.get("_paperread", {})
    if not isinstance(paperread, dict):
        return False
    enrichment = paperread.get("enrichment", {})
    if not isinstance(enrichment, dict):
        return False
    live_notes = enrichment.get("live_notes", {})
    if not isinstance(live_notes, dict):
        return False
    return (
        live_notes.get("status") == "refreshed"
        and live_notes.get("source") == "zotero_local_api_readonly"
        and live_notes.get("item_key") == parent_key
        and bool(str(live_notes.get("refreshed_at", "")).strip())
    )
```

Then add these helpers above `build_gate_report`:

```python
class _H1Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside_h1 = False
        self._parts: list[str] = []
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "h1":
            self._inside_h1 = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._inside_h1:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "h1" and self._inside_h1:
            self.title = " ".join("".join(self._parts).split())
            self._inside_h1 = False
            self._parts = []


def _markdown_h1_title(path: Path) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _html_h1_title(path: Path) -> str:
    if not path.exists():
        return ""
    parser = _H1Parser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser.title
```

After loading `item_details`, before suffix calculation, add:

```python
    parent_key = str(item_details.get("key", ""))
    if item_details and not _has_live_notes_refresh(item_details, parent_key=parent_key):
        blockers.append("item-details.json live_notes refresh missing or stale")
```

After `version_suffix` and `note_title` are calculated, add:

```python
    note_title = f"[Codex Summary] {paper_title} - {generated_date}{version_suffix}"
    if note_md_path.exists():
        md_title = _markdown_h1_title(note_md_path)
        if md_title != note_title:
            blockers.append(f"note.md title mismatch: expected {note_title}, got {md_title}")
    if note_html_path.exists():
        html_title = _html_h1_title(note_html_path)
        if html_title != note_title:
            blockers.append(f"note.html h1 mismatch: expected {note_title}, got {html_title}")
```

In the returned report, use the local `note_title` variable:

```python
        "note_title": note_title,
```

- [ ] **Step 4: Update existing ready gate test fixture**

In `test_build_gate_report_passes_ready_run`, change the `item-details.json` payload to include live refresh:

```python
write_json(
    run_dir / "item-details.json",
    {
        "key": "ABC123",
        "title": "Example Paper",
        "notes": [],
        "_paperread": {
            "enrichment": {
                "live_notes": {
                        "status": "refreshed",
                        "source": "zotero_local_api_readonly",
                        "item_key": "ABC123",
                        "refreshed_at": "2026-06-22T12:00:00Z",
                        "note_count": 0,
                    }
            }
        },
    },
)
```

Also change that fixture's note files to match the computed first-version title:

```python
(run_dir / "note.md").write_text("# [Codex Summary] Example Paper - 2026-05-06\n", encoding="utf-8")
(run_dir / "note.html").write_text("<h1>[Codex Summary] Example Paper - 2026-05-06</h1>", encoding="utf-8")
```

- [ ] **Step 5: Run gate tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_gate.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit gate requirement**

Run:

```bash
git add src/zotero_paperread/gate.py tests/test_gate.py
git commit -m "feat: require live note refresh before write gate"
```

Expected: commit succeeds.

## Task 5: Strengthen Write Payload Readback Checks

**Files:**
- Modify: `src/zotero_paperread/write_payload.py`
- Modify: `tests/test_write_payload.py`

- [ ] **Step 1: Add failing write-payload test**

Add this test to `tests/test_write_payload.py`:

```python
def test_build_write_payload_includes_title_and_version_readback(tmp_path: Path) -> None:
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
```

- [ ] **Step 2: Run write-payload test and verify RED**

Run:

```bash
uv run pytest tests/test_write_payload.py::test_build_write_payload_includes_title_and_version_readback -q
```

Expected: FAIL because `noteTitle` and `versionSuffix` are not emitted yet.

- [ ] **Step 3: Update `build_write_payload`**

Add this import near existing imports in `src/zotero_paperread/write_payload.py`:

```python
import hashlib
```

Then change `build_write_payload`:

```python
    note_title = str(gate_report.get("note_title", ""))
    version_suffix = str(gate_report.get("version_suffix", ""))
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
```

and include these keys in the returned dict:

```python
        "noteTitle": note_title,
        "versionSuffix": version_suffix,
        "contentSha256": content_sha256,
```

and in `required_readback_checks`:

```python
            "expectedTitle": note_title,
            "versionSuffix": version_suffix,
            "contentSha256": content_sha256,
```

- [ ] **Step 4: Run write-payload tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_write_payload.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit payload checks**

Run:

```bash
git add src/zotero_paperread/write_payload.py tests/test_write_payload.py
git commit -m "feat: include note title in write payload checks"
```

Expected: commit succeeds.

## Task 6: Add One-Command Write Candidate Preparation

**Files:**
- Create: `src/zotero_paperread/write_candidate.py`
- Modify: `src/zotero_paperread/cli.py`
- Create: `tests/test_write_candidate.py`
- Modify: `tests/test_cli_note.py`

- [ ] **Step 1: Add failing workflow test**

Create `tests/test_write_candidate.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from zotero_paperread.write_candidate import prepare_write_candidate


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
```

- [ ] **Step 2: Run workflow test and verify RED**

Run:

```bash
uv run pytest tests/test_write_candidate.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'zotero_paperread.write_candidate'`.

- [ ] **Step 3: Implement `write_candidate.py`**

Create `src/zotero_paperread/write_candidate.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from zotero_paperread.gate import build_gate_report
from zotero_paperread.note import build_note_labels, render_note, render_note_html, validate_note
from zotero_paperread.write_payload import build_write_payload
from zotero_paperread.zotero_details import next_version_suffix_from_details
from zotero_paperread.zotero_live import fetch_item_children_notes, refresh_details_with_live_notes


FetchLiveNotes = Callable[..., list[dict[str, Any]]]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _metadata_path(run_dir: Path) -> Path:
    metadata_path = run_dir / "metadata.json"
    return metadata_path if metadata_path.exists() else run_dir / "item-details.json"


def prepare_write_candidate(
    run_dir: Path,
    *,
    paper_title: str,
    generated_date: str,
    base_url: str = "http://127.0.0.1:23119",
    fetch_live_notes: FetchLiveNotes = fetch_item_children_notes,
    refreshed_at: str | None = None,
) -> dict[str, Any]:
    """Prepare local files for a Zotero MCP create write without writing to Zotero."""
    run_dir = Path(run_dir)
    item_details_path = run_dir / "item-details.json"
    summary_path = run_dir / "summary.json"
    review_path = run_dir / "review.json"
    note_md_path = run_dir / "note.md"
    note_html_path = run_dir / "note.html"
    gate_report_path = run_dir / "gate-report.json"
    write_payload_path = run_dir / "write-payload.json"

    details = _read_json(item_details_path)
    item_key = str(details.get("key", "")).strip()
    if not item_key:
        raise ValueError("item-details.json missing key")

    live_notes = fetch_live_notes(item_key, base_url=base_url)
    refreshed_details = refresh_details_with_live_notes(
        details,
        live_notes=live_notes,
        base_url=base_url,
        refreshed_at=refreshed_at,
    )
    _write_json(item_details_path, refreshed_details)

    version_suffix = next_version_suffix_from_details(
        refreshed_details,
        paper_title=paper_title,
        generated_date=generated_date,
    )

    summary = _read_json(summary_path)
    note_md = render_note(
        _read_json(_metadata_path(run_dir)),
        summary,
        generated_date=generated_date,
        version_suffix=version_suffix,
    )
    note_errors = validate_note(note_md)
    if note_errors:
        raise ValueError("; ".join(note_errors))
    note_md_path.write_text(note_md, encoding="utf-8")
    note_html_path.write_text(render_note_html(note_md), encoding="utf-8")
    (run_dir / "preview-note-md.txt").write_text(note_md, encoding="utf-8")
    (run_dir / "preview-note-html.txt").write_text(note_html_path.read_text(encoding="utf-8"), encoding="utf-8")
    _write_json(run_dir / "note-tags.json", build_note_labels(summary))

    if not review_path.exists():
        raise ValueError(f"missing review.json: {review_path}")
    gate_report = build_gate_report(run_dir, paper_title=paper_title, generated_date=generated_date)
    _write_json(gate_report_path, gate_report)
    if gate_report["status"] != "write_ready":
        return {
            "status": "blocked",
            "version_suffix": version_suffix,
            "gate_report_path": str(gate_report_path),
            "blockers": gate_report["blockers"],
        }

    write_payload = build_write_payload(gate_report)
    _write_json(write_payload_path, write_payload)
    return {
        "status": "write_ready",
        "version_suffix": version_suffix,
        "note_md_path": str(note_md_path),
        "note_html_path": str(note_html_path),
        "gate_report_path": str(gate_report_path),
        "write_payload_path": str(write_payload_path),
    }
```

- [ ] **Step 4: Add CLI wrapper test**

Append this test to `tests/test_cli_note.py`:

```python
def test_prepare_write_candidate_command_writes_payload(monkeypatch, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def fake_prepare_write_candidate(run_dir_arg: Path, **kwargs):
        assert run_dir_arg == run_dir
        assert kwargs["paper_title"] == "Paper"
        assert kwargs["generated_date"] == "2026-06-22"
        return {"status": "write_ready", "write_payload_path": str(run_dir / "write-payload.json")}

    monkeypatch.setattr("zotero_paperread.cli.prepare_write_candidate", fake_prepare_write_candidate)
    result = CliRunner().invoke(
        app,
        [
            "prepare-write-candidate",
            str(run_dir),
            "--paper-title",
            "Paper",
            "--generated-date",
            "2026-06-22",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "write_ready"
```

- [ ] **Step 5: Add CLI wrapper implementation**

Add this import near existing imports in `src/zotero_paperread/cli.py`:

```python
from zotero_paperread.write_candidate import prepare_write_candidate
```

Add this command after `prepare-write-payload`:

```python
@app.command("prepare-write-candidate")
def prepare_write_candidate_command(
    run_dir: Path,
    paper_title: str = typer.Option(..., "--paper-title", help="Paper title used in generated note titles."),
    generated_date: str = typer.Option(..., "--generated-date", help="Generated note date in YYYY-MM-DD form."),
    base_url: str = typer.Option("http://127.0.0.1:23119", "--base-url", help="Zotero local API base URL."),
) -> None:
    """Prepare a fully gated create-note payload without writing to Zotero."""
    try:
        result = prepare_write_candidate(
            run_dir,
            paper_title=paper_title,
            generated_date=generated_date,
            base_url=base_url,
        )
    except Exception as exc:
        console.print(f"prepare_write_candidate_failed: {exc}", soft_wrap=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") != "write_ready":
        raise typer.Exit(1)
```

- [ ] **Step 6: Run workflow and CLI tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_write_candidate.py tests/test_cli_note.py::test_prepare_write_candidate_command_writes_payload -q
```

Expected: PASS.

- [ ] **Step 7: Commit write candidate workflow**

Run:

```bash
git add src/zotero_paperread/write_candidate.py src/zotero_paperread/cli.py tests/test_write_candidate.py tests/test_cli_note.py
git commit -m "feat: add write candidate preparation workflow"
```

Expected: commit succeeds.

## Task 7: Update Workflow Documentation and Skill Examples

**Files:**
- Modify: `README.md`
- Modify: `skills/zotero-paper-summary/SKILL.md`
- Modify: `skills/zotero-batch-note-writing/SKILL.md`
- Modify: `tests/test_default_workflow_docs.py`

- [ ] **Step 1: Add failing command-order docs test**

Add this to `tests/test_default_workflow_docs.py`:

```python
def test_write_gate_documents_live_refresh_order() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    skill = Path("skills/zotero-paper-summary/SKILL.md").read_text(encoding="utf-8")
    batch_skill = Path("skills/zotero-batch-note-writing/SKILL.md").read_text(encoding="utf-8")
    expected_order = [
        "prepare-write-candidate",
        "refresh-live-notes",
        "next-version-suffix",
        "finalize-note",
        "gate-run",
        "prepare-write-payload",
        "write_note(action=\"create\"",
        "verify-zotero-note",
    ]
    for text in (readme, skill):
        positions = [text.index(item) for item in expected_order]
        assert positions == sorted(positions)
    assert "refresh-live-notes" in batch_skill
    assert "prepare-write-payload" in batch_skill
```

- [ ] **Step 2: Run command-order docs test and verify RED**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py::test_write_gate_documents_live_refresh_order -q
```

Expected: FAIL until README and skill command order are updated.

- [ ] **Step 3: Update README command sequence**

In README Trusted Notes section, replace the final write-through gate order with:

````markdown
Recommended final write-through preparation for a single-paper summary:

```bash
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread lint-summary <run_dir>/summary.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
uv run zotero-paperread prepare-write-candidate <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD
```

The lower-level debug chain inside `prepare-write-candidate` is:

```bash
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread lint-summary <run_dir>/summary.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
uv run zotero-paperread refresh-live-notes <run_dir>/item-details.json --output <run_dir>/item-details.json
SUFFIX=$(uv run zotero-paperread next-version-suffix <run_dir>/item-details.json --paper-title "<paper title>" --generated-date YYYY-MM-DD)
uv run zotero-paperread finalize-note <run_dir>/item-details.json <run_dir>/summary.json --version-suffix "$SUFFIX" --output <run_dir>/note.md --html-output <run_dir>/note.html
uv run zotero-paperread note-tags <run_dir>/summary.json
uv run zotero-paperread preview-note <run_dir>/note.md --output <run_dir>/preview-note-md.txt
uv run zotero-paperread preview-note <run_dir>/note.html --output <run_dir>/preview-note-html.txt
uv run zotero-paperread gate-run <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD --output <run_dir>/gate-report.json
uv run zotero-paperread prepare-write-payload <run_dir>/gate-report.json --output <run_dir>/write-payload.json
```

Then call `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`.

After the MCP write returns a note key, verify readback:

```bash
uv run zotero-paperread verify-zotero-note <note_key> \
  --expected-parent <parent_key> \
  --expected-title "<payload noteTitle>" \
  --required-heading "0. 阅读结论" \
  --required-heading "1. 论文主张" \
  --required-heading "2. 方法与设计" \
  --required-heading "3. 结果可信度" \
  --required-heading "4. 图表导读" \
  --required-heading "5. 边界与机会" \
  --required-heading "6. 我能怎么用" \
  --required-heading "7. 术语与检索" \
  --forbidden-heading "9. 元数据" \
  --forbidden-heading "10. 证据链附录" \
  --forbidden-heading "11. 补充优化记录" \
  --expected-tag codex-summary \
  --expected-tag paper-summary \
  --min-content-length <payload required_readback_checks.contentLengthAtLeast>
```
````

- [ ] **Step 4: Update skill command sequence**

In `skills/zotero-paper-summary/SKILL.md`, mirror the same recommended command and debug-chain order, and explicitly say:

```markdown
Do not call `write_note(action="update", ...)` for normal single-paper summaries.
```

In `skills/zotero-batch-note-writing/SKILL.md`, update the central per-item gate chain to include `refresh-live-notes` before `next-version-suffix`.

- [ ] **Step 5: Run docs tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit docs workflow**

Run:

```bash
git add README.md skills/zotero-paper-summary/SKILL.md skills/zotero-batch-note-writing/SKILL.md tests/test_default_workflow_docs.py
git commit -m "docs: document live refresh write gate"
```

Expected: commit succeeds.

## Task 8: Generic Versioning Regression Matrix

**Files:**
- Modify: `tests/test_write_candidate.py`

- [ ] **Step 1: Add generic future-paper regression tests**

Append this matrix test to `tests/test_write_candidate.py`:

```python
import pytest


def prepare_run_dir(tmp_path: Path, *, title: str) -> Path:
    run_dir = tmp_path / title.lower().replace(" ", "-")
    write_json(run_dir / "item-details.json", {"key": "P1", "title": title, "notes": []})
    write_json(run_dir / "metadata.json", {"key": "P1", "title": title, "creators": [], "date": "2026"})
    write_json(run_dir / "summary.json", trusted_summary())
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    return run_dir


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
```

- [ ] **Step 2: Run matrix tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_write_candidate.py -q
```

Expected: PASS.

- [ ] **Step 3: Commit generic regression matrix**

Run:

```bash
git add tests/test_write_candidate.py
git commit -m "test: cover generic write candidate versioning"
```

Expected: commit succeeds.

## Task 9: Polyanion Live Smoke Only

**Files/artifacts:**
- Use existing run directory:
  - `runs/2026-06-18/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/`
- Do not write a new Zotero note unless the user explicitly asks after this implementation.

- [ ] **Step 1: Run one-command candidate preparation for the Polyanion run**

Run:

```bash
RUN_DIR="runs/2026-06-18/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries"
uv run zotero-paperread prepare-write-candidate "$RUN_DIR" \
  --paper-title "Polyanion-stabilized amorphous halide electrolytes with low lithium content for all-solid-state lithium batteries" \
  --generated-date "2026-06-18"
```

Expected:

```json
{
  "status": "write_ready",
  "version_suffix": " (v3)"
}
```

This is a live smoke only. The generic fixture matrix above is the durable regression suite. Do not call `zotero-mcp write_note` in this task.

- [ ] **Step 2: Verify the existing v2 Zotero note**

Run:

```bash
uv run zotero-paperread verify-zotero-note GVZQD7HJ \
  --expected-parent CABS9KGA \
  --expected-title "[Codex Summary] Polyanion-stabilized amorphous halide electrolytes with low lithium content for all-solid-state lithium batteries - 2026-06-18 (v2)" \
  --required-heading "0. 阅读结论" \
  --required-heading "1. 论文主张" \
  --required-heading "2. 方法与设计" \
  --required-heading "3. 结果可信度" \
  --required-heading "4. 图表导读" \
  --required-heading "5. 边界与机会" \
  --required-heading "6. 我能怎么用" \
  --required-heading "7. 术语与检索" \
  --forbidden-heading "9. 元数据" \
  --forbidden-heading "10. 证据链附录" \
  --forbidden-heading "11. 补充优化记录" \
  --expected-tag codex-summary \
  --expected-tag paper-summary \
  --expected-tag amorphous_halide_sse \
  --expected-tag polyanion_cluster \
  --expected-tag low_lithium_content \
  --expected-tag solid_state_battery \
  --min-content-length 16000
```

Expected: JSON with `"status": "passed"`.

## Task 10: Full Verification and Final Review

**Files:** no intended edits unless verification reveals failures.

- [ ] **Step 1: Run full tests**

Run:

```bash
uv run pytest
```

Expected: all tests pass.

- [ ] **Step 2: Run CLI smoke**

Run:

```bash
uv run zotero-paperread --help
```

Expected: exits 0 and lists `refresh-live-notes`, `verify-zotero-note`, and `prepare-write-candidate`.

- [ ] **Step 3: Run PDF extraction smoke**

Run:

```bash
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

Expected: exits 0 and writes `/tmp/zotero-paperread-extract.json`.

- [ ] **Step 4: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 5: Review code for write-boundary violations**

Run:

```bash
rg -n "urlopen|Request\\(|method=|PUT|PATCH|POST|DELETE|sqlite|write_note\\(action=\\\"update\\\"" src tests README.md AGENTS.md skills/
```

Expected:

- `src/zotero_paperread/zotero_live.py` may contain `urlopen` read-only GET helpers only.
- `zotero_sqlite.py` and existing SQLite read-only fallback references may remain.
- `write_note(action="update"` may appear only in historical migration documentation/tests.
- No project code performs Zotero HTTP PUT/PATCH/POST/DELETE.

- [ ] **Step 6: Commit final fixes if any**

If Step 5 reveals issues, fix them and run the relevant targeted tests again. Then run:

```bash
git status --short --branch --untracked-files=all
```

Expected tracked work is either clean or only the intentionally fixed files remain before a final commit.

## Self-Review

- Spec coverage: The plan implements all three confirmed decisions plus the approved smoothing change: single-paper writes always create versioned notes, Zotero local API is read-only and accepted for live readback, historical migration update timeouts stop and report, and daily use gets `prepare-write-candidate`.
- No placeholders: Every task names exact files, commands, and expected outcomes.
- Type consistency: `refresh_details_with_live_notes`, `fetch_item_children_notes`, `fetch_note_snapshot`, `verify_note_snapshot`, and `prepare_write_candidate` are defined before CLI/docs tasks use them.
- Safety: No task adds a Zotero local API write path. Persistent Zotero writes remain agent-side `zotero-mcp write_note`.
- Verification: Unit tests cover helpers, CLI, gate behavior, payload checks, docs contract, generic versioning matrix, and live Polyanion smoke verification.
