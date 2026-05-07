# Zotero Extra Secondary Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically discover HTTP links stored in Zotero item `Extra` / `其他`, preserve them as secondary-source run artifacts, and make the paper-summary workflow capture and use them for cross-checking without admitting them into the primary evidence chain.

**Architecture:** Keep `prepare-item` deterministic and local-only. `save-item-details` normalizes the MCP payload and, only when the MCP payload lacks `extra`, enriches the normalized item details from Zotero SQLite through a read-only fallback. `prepare-item` parses `extra` into `secondary_sources.json`; the agent workflow then uses the existing Chrome CDP capture script to create `secondary_contexts/*.md` files. `summary_lint` remains the enforcement point that prevents secondary material from being cited in `evidence_summary`.

**Tech Stack:** Python 3 via `uv`, Typer CLI, pytest, stdlib `sqlite3`, stdlib `re` / `urllib.parse`, existing Node.js CDP script `skills/zotero-paper-summary/scripts/capture-secondary-url.mjs`, Zotero MCP for normal item lookup.

---

## Implementation Reality Update

2026-05-07 hardening supersedes two diagnostic details in the original task
snippets below:

- successful immutable SQLite Extra fallback is now recorded under
  `_paperread.enrichment.extra.diagnostics`, not `_paperread.warnings`;
- CDP secondary capture should use `--request-retries 2 --request-retry-ms 500`
  for normal WeChat/secondary-source capture.

The architecture and cross-check-only boundary remain current. For exact retry
and warning policy, use
`docs/superpowers/plans/2026-05-07-extra-fallback-and-cdp-retry-hardening.md`
and the runbook in `docs/references/zotero-batch-write-runbook.md`.

---

## Review Decision

Scheme A is accepted with constraints. The core direction is correct: make Extra-link discovery a first-class run artifact instead of a manual SQLite inspection. The important correction is where boundaries sit:

1. `save-item-details` may do read-only Zotero SQLite enrichment because it already owns normalization of the item detail payload.
2. `prepare-item` must not query Zotero SQLite, open Chrome, or perform network access. It only reads normalized `item-details.json` and emits deterministic artifacts.
3. Secondary web content remains cross-check material. It can improve interpretation, fill background, and flag conflicts, but `evidence_summary` must cite only `context.md` and `figure_context.md`.
4. SQLite fallback must be soft-fail. If Zotero SQLite is locked, unavailable, unreadable, or missing the item, the paper PDF workflow continues and records warnings.
5. The fallback is strictly read-only. It must not modify Zotero SQLite, Zotero storage metadata, Better Notes, or Zotero item data.

Rejected alternatives:

- Do not make `prepare-item` directly fetch webpages. That would turn a deterministic bundle command into a network/browser command.
- Do not depend on direct SQLite as the primary path. Zotero MCP remains primary; SQLite is only a missing-field fallback for `extra`.
- Do not parse or capture non-web local paths. Only `http://` and `https://` URLs are secondary-source candidates.

## File Structure

- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/zotero_sqlite.py`
  - Owns read-only SQLite fallback for Zotero `extra`.
  - Tries `mode=ro` first, then `mode=ro&immutable=1` only for lock-related failures.
  - Returns a small dictionary with `extra`, `warnings`, and provenance.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/zotero_item_io.py`
  - Calls SQLite fallback only when normalized item details have no non-empty `extra`.
  - Preserves raw MCP response unchanged in `item-details.raw.json`.
  - Writes enrichment provenance into normalized `item-details.json`.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
  - Adds `--zotero-sqlite <path>` and `--no-sqlite-extra-fallback` options to `save-item-details`.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/secondary_sources.py`
  - Extracts and normalizes `http://` / `https://` URLs from Zotero `extra`.
  - Builds the stable `secondary_sources.json` payload.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/workflow.py`
  - Writes `<run_dir>/secondary_sources.json` during `prepare-item`.
  - Adds `secondary_sources_json` to the command result and to `run.json` when a manifest exists.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/summary_lint.py`
  - Expands secondary-evidence detection beyond `secondary_context.md`.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`
  - Documents automatic Extra-link discovery and capture.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`
  - Documents the user-facing workflow and evidence boundary.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_zotero_item_io.py`
  - Tests SQLite enrichment through `write_item_details_files`.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_zotero_sqlite.py`
  - Tests the read-only SQLite query behavior with a minimal Zotero-like schema.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_secondary_sources.py`
  - Tests URL extraction and `secondary_sources.json` payload shape.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_workflow.py`
  - Tests `prepare-item` writes and manifests secondary-source artifacts.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_summary_lint.py`
  - Tests expanded secondary-evidence lint coverage.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_default_workflow_docs.py`
  - Locks the README and skill workflow wording.

---

### Task 0: Baseline And Branch Safety

**Files:**
- No file changes.

- [ ] **Step 1: Inspect branch and dirty state**

Run:

```bash
git branch --show-current
git status --short
```

Expected: current branch prints. If there are dirty files unrelated to this plan, leave them untouched.

- [ ] **Step 2: Create an implementation branch if still on `main`**

Run only if Step 1 prints `main`:

```bash
git switch -c codex/zotero-extra-secondary-sources
```

Expected:

```text
Switched to a new branch 'codex/zotero-extra-secondary-sources'
```

- [ ] **Step 3: Run baseline verification**

Run:

```bash
uv run pytest
uv run zotero-paperread --help
```

Expected: pytest exits 0 and CLI help exits 0 before the behavior change begins.

---

### Task 1: Read Zotero Extra From SQLite As A Soft Fallback

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/zotero_sqlite.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_zotero_sqlite.py`

- [ ] **Step 1: Write failing tests for SQLite Extra lookup**

Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_zotero_sqlite.py`:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

from zotero_paperread.zotero_sqlite import lookup_extra_by_item_key


def make_zotero_db(path: Path, *, key: str = "ABC123", extra: str = "https://mp.weixin.qq.com/s/example") -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
        CREATE TABLE fieldsCombined (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        INSERT INTO items (itemID, key, itemTypeID) VALUES (10, :key, 22);
        INSERT INTO fieldsCombined (fieldID, fieldName) VALUES (1, 'extra');
        INSERT INTO itemDataValues (valueID, value) VALUES (1, :extra);
        INSERT INTO itemData (itemID, fieldID, valueID) VALUES (10, 1, 1);
        """,
        {"key": key, "extra": extra},
    )
    conn.commit()
    conn.close()


def test_lookup_extra_by_item_key_reads_extra_without_writing(tmp_path: Path) -> None:
    db_path = tmp_path / "zotero.sqlite"
    make_zotero_db(db_path)

    result = lookup_extra_by_item_key("ABC123", sqlite_path=db_path)

    assert result["extra"] == "https://mp.weixin.qq.com/s/example"
    assert result["provenance"]["source"] == "zotero_sqlite"
    assert result["provenance"]["sqlite_mode"] == "ro"
    assert result["warnings"] == []


def test_lookup_extra_by_item_key_soft_fails_when_db_missing(tmp_path: Path) -> None:
    result = lookup_extra_by_item_key("ABC123", sqlite_path=tmp_path / "missing.sqlite")

    assert result["extra"] == ""
    assert result["warnings"] == ["sqlite_extra_unavailable"]


def test_lookup_extra_by_item_key_soft_fails_when_item_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "zotero.sqlite"
    make_zotero_db(db_path, key="OTHER1")

    result = lookup_extra_by_item_key("ABC123", sqlite_path=db_path)

    assert result["extra"] == ""
    assert result["warnings"] == ["sqlite_extra_item_not_found"]
```

- [ ] **Step 2: Run tests and verify they fail because the module is missing**

Run:

```bash
uv run pytest tests/test_zotero_sqlite.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'zotero_paperread.zotero_sqlite'`.

- [ ] **Step 3: Implement read-only SQLite lookup**

Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/zotero_sqlite.py`:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote


DEFAULT_ZOTERO_SQLITE_PATH = Path.home() / "Zotero" / "zotero.sqlite"


def _sqlite_uri(path: Path, *, immutable: bool = False) -> str:
    suffix = "&immutable=1" if immutable else ""
    return f"file:{quote(str(path.expanduser().resolve()))}?mode=ro{suffix}"


def _query_extra(conn: sqlite3.Connection, item_key: str) -> str:
    row = conn.execute(
        """
        SELECT itemDataValues.value
        FROM items
        JOIN itemData ON itemData.itemID = items.itemID
        JOIN fieldsCombined ON fieldsCombined.fieldID = itemData.fieldID
        JOIN itemDataValues ON itemDataValues.valueID = itemData.valueID
        WHERE items.key = ? AND fieldsCombined.fieldName = 'extra'
        LIMIT 1
        """,
        (item_key,),
    ).fetchone()
    return str(row[0]).strip() if row and row[0] is not None else ""


def _item_exists(conn: sqlite3.Connection, item_key: str) -> bool:
    row = conn.execute("SELECT 1 FROM items WHERE key = ? LIMIT 1", (item_key,)).fetchone()
    return row is not None


def lookup_extra_by_item_key(
    item_key: str,
    *,
    sqlite_path: Path = DEFAULT_ZOTERO_SQLITE_PATH,
    allow_immutable: bool = True,
) -> dict[str, Any]:
    key = str(item_key).strip()
    db_path = Path(sqlite_path).expanduser()
    if not key or not db_path.exists():
        return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}

    warnings: list[str] = []
    mode = "ro"
    try:
        conn = sqlite3.connect(_sqlite_uri(db_path), uri=True)
    except sqlite3.OperationalError as error:
        if not allow_immutable or "locked" not in str(error).lower():
            return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}
        warnings.append("sqlite_immutable_snapshot_used")
        mode = "immutable"
        try:
            conn = sqlite3.connect(_sqlite_uri(db_path, immutable=True), uri=True)
        except sqlite3.Error:
            return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}

    try:
        if not _item_exists(conn, key):
            return {"extra": "", "warnings": warnings + ["sqlite_extra_item_not_found"], "provenance": {}}
        extra = _query_extra(conn, key)
    except sqlite3.Error:
        return {"extra": "", "warnings": warnings + ["sqlite_extra_unavailable"], "provenance": {}}
    finally:
        conn.close()

    return {
        "extra": extra,
        "warnings": warnings,
        "provenance": {
            "source": "zotero_sqlite",
            "item_key": key,
            "sqlite_path": str(db_path),
            "sqlite_mode": mode,
        },
    }
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
uv run pytest tests/test_zotero_sqlite.py -q
```

Expected: all tests in `tests/test_zotero_sqlite.py` pass.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add src/zotero_paperread/zotero_sqlite.py tests/test_zotero_sqlite.py
git commit -m "feat: add read-only Zotero extra lookup"
```

Expected: local commit succeeds. Do not push.

---

### Task 2: Enrich Normalized Item Details When MCP Omits Extra

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/zotero_item_io.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_zotero_item_io.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_note.py`

- [ ] **Step 1: Write failing unit test for missing-Extra enrichment**

Append to `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_zotero_item_io.py`:

```python
def test_write_item_details_files_enriches_missing_extra_from_sqlite(monkeypatch, tmp_path: Path) -> None:
    item = {"key": "ABC123", "title": "Example Paper", "attachments": [], "notes": []}
    normalized_path = tmp_path / "item-details.json"

    def fake_lookup(item_key: str, **kwargs):
        assert item_key == "ABC123"
        return {
            "extra": "https://mp.weixin.qq.com/s/example",
            "warnings": ["sqlite_immutable_snapshot_used"],
            "provenance": {"source": "zotero_sqlite", "item_key": "ABC123", "sqlite_mode": "immutable"},
        }

    monkeypatch.setattr("zotero_paperread.zotero_item_io.lookup_extra_by_item_key", fake_lookup)

    result = write_item_details_files(item, normalized_path=normalized_path)
    normalized = json.loads(normalized_path.read_text(encoding="utf-8"))

    assert result["extra_source"] == "zotero_sqlite"
    assert normalized["extra"] == "https://mp.weixin.qq.com/s/example"
    assert normalized["_paperread"]["warnings"] == ["sqlite_immutable_snapshot_used"]
    assert normalized["_paperread"]["enrichment"]["extra"]["source"] == "zotero_sqlite"
```

- [ ] **Step 2: Write failing unit test that preserves MCP Extra**

Append to `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_zotero_item_io.py`:

```python
def test_write_item_details_files_keeps_mcp_extra_without_sqlite_lookup(monkeypatch, tmp_path: Path) -> None:
    item = {
        "key": "ABC123",
        "title": "Example Paper",
        "extra": "https://example.org/from-mcp",
        "attachments": [],
        "notes": [],
    }
    normalized_path = tmp_path / "item-details.json"

    def fail_lookup(*args, **kwargs):
        raise AssertionError("sqlite fallback should not run when MCP extra exists")

    monkeypatch.setattr("zotero_paperread.zotero_item_io.lookup_extra_by_item_key", fail_lookup)

    result = write_item_details_files(item, normalized_path=normalized_path)
    normalized = json.loads(normalized_path.read_text(encoding="utf-8"))

    assert result["extra_source"] == "mcp_payload"
    assert normalized["extra"] == "https://example.org/from-mcp"
```

- [ ] **Step 3: Run tests and verify they fail because enrichment is absent**

Run:

```bash
uv run pytest tests/test_zotero_item_io.py -q
```

Expected: the new tests fail because `write_item_details_files()` has no `extra_source` result and does not call SQLite fallback.

- [ ] **Step 4: Implement enrichment in `zotero_item_io.py`**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/zotero_item_io.py`:

```python
from zotero_paperread.zotero_sqlite import DEFAULT_ZOTERO_SQLITE_PATH, lookup_extra_by_item_key
```

Add helper functions:

```python
def _paperread_meta(normalized: dict[str, Any]) -> dict[str, Any]:
    meta = normalized.get("_paperread")
    if not isinstance(meta, dict):
        meta = {}
        normalized["_paperread"] = meta
    meta.setdefault("warnings", [])
    meta.setdefault("enrichment", {})
    return meta


def enrich_missing_extra_from_sqlite(
    normalized: dict[str, Any],
    *,
    sqlite_path: Path = DEFAULT_ZOTERO_SQLITE_PATH,
    enabled: bool = True,
) -> str:
    existing_extra = str(normalized.get("extra", "")).strip()
    if existing_extra:
        return "mcp_payload"
    if not enabled:
        return "not_requested"

    lookup = lookup_extra_by_item_key(str(normalized["key"]), sqlite_path=sqlite_path)
    meta = _paperread_meta(normalized)
    warnings = meta.setdefault("warnings", [])
    for warning in lookup.get("warnings", []):
        if warning not in warnings:
            warnings.append(warning)
    extra = str(lookup.get("extra", "")).strip()
    if not extra:
        return "missing"

    normalized["extra"] = extra
    provenance = dict(lookup.get("provenance", {}))
    meta.setdefault("enrichment", {})["extra"] = provenance
    return str(provenance.get("source", "zotero_sqlite"))
```

Update `write_item_details_files()` signature and body:

```python
def write_item_details_files(
    payload: Any,
    *,
    normalized_path: Path,
    raw_path: Path | None = None,
    sqlite_path: Path = DEFAULT_ZOTERO_SQLITE_PATH,
    sqlite_extra_fallback: bool = True,
) -> dict[str, Any]:
    normalized = normalize_item_details_payload(payload)
    extra_source = enrich_missing_extra_from_sqlite(
        normalized,
        sqlite_path=sqlite_path,
        enabled=sqlite_extra_fallback,
    )
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if raw_path is not None:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "item_key": normalized["key"],
        "title": normalized["title"],
        "extra_source": extra_source,
        "normalized_path": str(normalized_path),
        "raw_path": str(raw_path) if raw_path is not None else None,
    }
```

- [ ] **Step 5: Add CLI options**

Modify `save_item_details_command()` in `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`:

```python
def save_item_details_command(
    input_json: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write normalized item details JSON."),
    raw_output: Path | None = typer.Option(None, "--raw-output", help="Optionally write raw MCP payload JSON."),
    zotero_sqlite: Path = typer.Option(DEFAULT_ZOTERO_SQLITE_PATH, "--zotero-sqlite", help="Read-only Zotero SQLite path for missing Extra fallback."),
    sqlite_extra_fallback: bool = typer.Option(True, "--sqlite-extra-fallback/--no-sqlite-extra-fallback", help="Use read-only SQLite to fill missing Extra."),
) -> None:
    """Save raw MCP item details as normalized run item-details.json."""
    payload = json.loads(input_json.read_text(encoding="utf-8"))
    result = write_item_details_files(
        payload,
        normalized_path=output,
        raw_path=raw_output,
        sqlite_path=zotero_sqlite,
        sqlite_extra_fallback=sqlite_extra_fallback,
    )
    typer.echo(json.dumps(result, ensure_ascii=False))
```

Also import `DEFAULT_ZOTERO_SQLITE_PATH` from `zotero_paperread.zotero_sqlite`.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run pytest tests/test_zotero_item_io.py tests/test_cli_note.py::test_save_item_details_command_writes_normalized_and_raw -q
```

Expected: focused tests pass.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git add src/zotero_paperread/zotero_item_io.py src/zotero_paperread/cli.py tests/test_zotero_item_io.py tests/test_cli_note.py
git commit -m "feat: enrich missing Zotero extra during item normalization"
```

Expected: local commit succeeds. Do not push.

---

### Task 3: Parse Extra Into Secondary Source Metadata

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/secondary_sources.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_secondary_sources.py`

- [ ] **Step 1: Write failing tests for URL extraction and payload shape**

Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_secondary_sources.py`:

```python
from __future__ import annotations

from zotero_paperread.secondary_sources import build_secondary_sources, extract_http_urls


def test_extract_http_urls_accepts_only_http_and_https_and_dedupes() -> None:
    text = """
    https://mp.weixin.qq.com/s/example?scene=334,
    http://example.org/a.
    zotero://select/library/items/ABC123
    file:///tmp/local.pdf
    https://mp.weixin.qq.com/s/example?scene=334
    """

    assert extract_http_urls(text) == [
        "https://mp.weixin.qq.com/s/example?scene=334",
        "http://example.org/a",
    ]


def test_build_secondary_sources_records_cross_check_boundary() -> None:
    details = {
        "key": "ABC123",
        "title": "Example Paper",
        "extra": "Related: https://mp.weixin.qq.com/s/example?scene=334",
        "_paperread": {
            "enrichment": {
                "extra": {"source": "zotero_sqlite", "sqlite_mode": "immutable"}
            },
            "warnings": ["sqlite_immutable_snapshot_used"],
        },
    }

    payload = build_secondary_sources(details)

    assert payload["item_key"] == "ABC123"
    assert payload["usage_boundary"] == "cross-check only; must not be cited in evidence_summary"
    assert payload["warnings"] == ["sqlite_immutable_snapshot_used"]
    assert payload["sources"] == [
        {
            "source_id": "secondary-001",
            "url": "https://mp.weixin.qq.com/s/example?scene=334",
            "source_field": "extra",
            "source_provenance": "zotero_sqlite",
            "capture_status": "pending_capture",
        }
    ]


def test_build_secondary_sources_soft_handles_missing_extra() -> None:
    payload = build_secondary_sources({"key": "ABC123", "title": "No Extra"})

    assert payload["sources"] == []
    assert payload["warnings"] == ["missing_extra_field"]
```

- [ ] **Step 2: Run tests and verify they fail because the module is missing**

Run:

```bash
uv run pytest tests/test_secondary_sources.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'zotero_paperread.secondary_sources'`.

- [ ] **Step 3: Implement `secondary_sources.py`**

Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/secondary_sources.py`:

```python
from __future__ import annotations

import re
from typing import Any


HTTP_URL_RE = re.compile(r"https?://[^\s<>()\"']+")
TRAILING_URL_PUNCTUATION = ".,;:!?)，。；：！？）】》"
USAGE_BOUNDARY = "cross-check only; must not be cited in evidence_summary"


def _clean_url(url: str) -> str:
    return url.rstrip(TRAILING_URL_PUNCTUATION)


def extract_http_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in HTTP_URL_RE.finditer(str(text or "")):
        url = _clean_url(match.group(0))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _extra_provenance(details: dict[str, Any]) -> str:
    paperread = details.get("_paperread")
    if not isinstance(paperread, dict):
        return "mcp_payload"
    enrichment = paperread.get("enrichment")
    if not isinstance(enrichment, dict):
        return "mcp_payload"
    extra = enrichment.get("extra")
    if not isinstance(extra, dict):
        return "mcp_payload"
    return str(extra.get("source", "mcp_payload"))


def _paperread_warnings(details: dict[str, Any]) -> list[str]:
    paperread = details.get("_paperread")
    if not isinstance(paperread, dict):
        return []
    warnings = paperread.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [str(item) for item in warnings if str(item).strip()]


def build_secondary_sources(details: dict[str, Any]) -> dict[str, Any]:
    extra = str(details.get("extra", "")).strip()
    warnings = _paperread_warnings(details)
    if not extra:
        warnings = warnings + ["missing_extra_field"]
    urls = extract_http_urls(extra)
    if extra and not urls:
        warnings = warnings + ["extra_contains_no_http_url"]

    sources = [
        {
            "source_id": f"secondary-{index:03d}",
            "url": url,
            "source_field": "extra",
            "source_provenance": _extra_provenance(details),
            "capture_status": "pending_capture",
        }
        for index, url in enumerate(urls, start=1)
    ]
    return {
        "item_key": str(details.get("key", "")),
        "title": str(details.get("title", "")),
        "usage_boundary": USAGE_BOUNDARY,
        "sources": sources,
        "warnings": warnings,
    }
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
uv run pytest tests/test_secondary_sources.py -q
```

Expected: all tests in `tests/test_secondary_sources.py` pass.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add src/zotero_paperread/secondary_sources.py tests/test_secondary_sources.py
git commit -m "feat: parse Zotero extra secondary sources"
```

Expected: local commit succeeds. Do not push.

---

### Task 4: Emit `secondary_sources.json` From `prepare-item`

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/workflow.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_workflow.py`

- [ ] **Step 1: Add failing workflow test for secondary source artifact**

Append to `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_workflow.py`:

```python
def test_prepare_item_bundle_writes_secondary_sources_json(tmp_path: Path) -> None:
    details = {
        "key": "WEB123",
        "title": "Paper With Secondary Web Source",
        "creators": [],
        "date": "2026",
        "DOI": "",
        "url": "https://example.org/paper",
        "zoteroUrl": "zotero://select/library/items/WEB123",
        "abstractNote": "",
        "extra": "https://mp.weixin.qq.com/s/example?scene=334",
        "attachments": [],
    }

    result = prepare_item_bundle(details, tmp_path / "bundle")

    secondary_path = Path(result["secondary_sources_json"])
    secondary = json.loads(secondary_path.read_text(encoding="utf-8"))
    assert secondary_path.name == "secondary_sources.json"
    assert secondary["sources"][0]["url"] == "https://mp.weixin.qq.com/s/example?scene=334"
    assert secondary["sources"][0]["capture_status"] == "pending_capture"
```

- [ ] **Step 2: Add failing manifest update test**

Extend `test_prepare_item_bundle_updates_existing_run_manifest()` in `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_workflow.py` by adding `extra` to `details`:

```python
"extra": "https://mp.weixin.qq.com/s/manifest",
```

Then add these assertions after existing manifest assertions:

```python
assert manifest["secondary_sources_json"] == result["secondary_sources_json"]
secondary = json.loads(Path(result["secondary_sources_json"]).read_text(encoding="utf-8"))
assert secondary["sources"][0]["url"] == "https://mp.weixin.qq.com/s/manifest"
```

- [ ] **Step 3: Run tests and verify they fail because workflow does not emit secondary sources**

Run:

```bash
uv run pytest tests/test_workflow.py::test_prepare_item_bundle_writes_secondary_sources_json tests/test_workflow.py::test_prepare_item_bundle_updates_existing_run_manifest -q
```

Expected: FAIL because `secondary_sources_json` is missing from the result and manifest.

- [ ] **Step 4: Implement workflow artifact emission**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/workflow.py`:

```python
from zotero_paperread.secondary_sources import build_secondary_sources
```

Inside `prepare_item_bundle()`, after `context_path` is defined, add:

```python
secondary_sources_path = bundle_dir / "secondary_sources.json"
secondary_sources = build_secondary_sources(details)
```

After writing `context.md`, add:

```python
secondary_sources_path.write_text(
    json.dumps(secondary_sources, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
```

In `result`, add:

```python
"secondary_sources_json": str(secondary_sources_path),
```

In the `manifest.update()` payload, add:

```python
"secondary_sources_json": result["secondary_sources_json"],
```

- [ ] **Step 5: Run focused workflow tests**

Run:

```bash
uv run pytest tests/test_workflow.py::test_prepare_item_bundle_writes_secondary_sources_json tests/test_workflow.py::test_prepare_item_bundle_updates_existing_run_manifest -q
```

Expected: both focused tests pass.

- [ ] **Step 6: Commit Task 4**

Run:

```bash
git add src/zotero_paperread/workflow.py tests/test_workflow.py
git commit -m "feat: emit secondary sources in prepare item bundle"
```

Expected: local commit succeeds. Do not push.

---

### Task 5: Strengthen Summary Lint Against Secondary Evidence

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/summary_lint.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_summary_lint.py`

- [ ] **Step 1: Write failing lint tests for new secondary artifact paths**

Append to `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_summary_lint.py`:

```python
def test_lint_summary_flags_secondary_contexts_directory_locator() -> None:
    summary = {
        "workflow_steps": "1. First.\n2. Second.",
        "evidence_summary": [
            {
                "claim": "Claim",
                "evidence": [
                    {"type": "text", "locator": "secondary_contexts/001.md", "summary": "Not allowed"}
                ],
            }
        ],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "secondary_context_used_as_evidence" for issue in issues)


def test_lint_summary_flags_secondary_sources_json_locator() -> None:
    summary = {
        "workflow_steps": "1. First.\n2. Second.",
        "evidence_summary": [
            {
                "claim": "Claim",
                "evidence": [
                    {"type": "text", "locator": "secondary_sources.json", "summary": "Not allowed"}
                ],
            }
        ],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "secondary_context_used_as_evidence" for issue in issues)
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/test_summary_lint.py -q
```

Expected: the new secondary locator tests fail before the lint rule is expanded.

- [ ] **Step 3: Expand secondary locator detection**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/summary_lint.py`:

```python
SECONDARY_EVIDENCE_PREFIXES = (
    "secondary_context",
    "secondary_context.md",
    "secondary_contexts/",
    "secondary_sources.json",
    "wechat-context",
)
```

Replace the existing `locator.startswith(("secondary_context", "wechat-context"))` condition with:

```python
if locator.startswith(SECONDARY_EVIDENCE_PREFIXES):
```

- [ ] **Step 4: Run lint tests**

Run:

```bash
uv run pytest tests/test_summary_lint.py -q
```

Expected: all summary lint tests pass.

- [ ] **Step 5: Commit Task 5**

Run:

```bash
git add src/zotero_paperread/summary_lint.py tests/test_summary_lint.py
git commit -m "test: reject secondary sources as evidence"
```

Expected: local commit succeeds. Do not push.

---

### Task 6: Document Automatic Extra-Link Capture Workflow

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_default_workflow_docs.py`

- [ ] **Step 1: Add failing docs test for Extra-link discovery**

Append to `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_default_workflow_docs.py`:

```python
def test_docs_explain_zotero_extra_secondary_sources() -> None:
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for text in (skill, readme):
        assert "secondary_sources.json" in text
        assert "Extra" in text or "其他" in text
        assert "secondary_contexts" in text
        assert "cross-check only" in text
        assert "must not be cited in evidence_summary" in text
        assert "--no-sqlite-extra-fallback" in text
```

- [ ] **Step 2: Run docs test and verify it fails before docs update**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py::test_docs_explain_zotero_extra_secondary_sources -q
```

Expected: FAIL because current docs do not mention `secondary_sources.json` or the SQLite fallback options.

- [ ] **Step 3: Update skill workflow**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md` in the item-details and secondary-context sections with this wording:

````markdown
- `save-item-details` 默认在 MCP 响应缺少 `extra` 字段时，用只读 Zotero SQLite fallback 补齐 Zotero `Extra` / `其他` 字段。
- 该 fallback 只读 `~/Zotero/zotero.sqlite`，不会写 Zotero；如果 SQLite 不可读、被锁、缺少 item 或没有 extra，保留 warning 并继续主 PDF workflow。
- 如需禁用 fallback，运行：

```bash
uv run zotero-paperread save-item-details <run_dir>/mcp-response.json --output <run_dir>/item-details.json --raw-output <run_dir>/item-details.raw.json --no-sqlite-extra-fallback
```

- `prepare-item` 会从规范化后的 `item-details.json` 解析 `Extra` / `其他` 中的 `http://` / `https://` 链接，并写入 `<run_dir>/secondary_sources.json`。
- 如果 `secondary_sources.json` 中存在 `sources`，逐条使用现有 CDP capture 脚本写入 `<run_dir>/secondary_contexts/<source_id>.md`。
- 二级材料是 `cross-check only; must not be cited in evidence_summary`。可信证据仍只来自 `context.md` 和 `figure_context.md`。
````

- [ ] **Step 4: Update README**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md` in the Secondary Context section with this wording:

````markdown
`save-item-details` also smooths Zotero `Extra` / `其他` handling. If the MCP `get_item_details` response omits `extra`, the command uses a read-only SQLite fallback against `~/Zotero/zotero.sqlite`. Successful immutable SQLite Extra reads are recorded as provenance diagnostics under `_paperread.enrichment.extra.diagnostics`, not normal workflow warnings. Actual missing or unreadable Extra fallback remains a warning. Disable this fallback with `--no-sqlite-extra-fallback`.

`prepare-item` reads normalized `item-details.json`, extracts `http://` / `https://` URLs from `extra`, and writes `<run_dir>/secondary_sources.json`. When `sources` is non-empty, capture each URL with:

```bash
mkdir -p <run_dir>/secondary_contexts
node skills/zotero-paper-summary/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

Captured secondary contexts are cross-check only and must not be cited in `evidence_summary`. Trusted evidence remains limited to `context.md` and `figure_context.md`.
````

- [ ] **Step 5: Run docs tests**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: docs tests pass.

- [ ] **Step 6: Commit Task 6**

Run:

```bash
git add README.md skills/zotero-paper-summary/SKILL.md tests/test_default_workflow_docs.py
git commit -m "docs: document Zotero extra secondary sources"
```

Expected: local commit succeeds. Do not push.

---

### Task 7: End-To-End Verification

**Files:**
- No additional planned file changes.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/test_zotero_sqlite.py tests/test_zotero_item_io.py tests/test_secondary_sources.py tests/test_workflow.py tests/test_summary_lint.py tests/test_default_workflow_docs.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run project verification required by AGENTS.md**

Run:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

Expected: all commands exit 0. `extract-pdf` writes `/tmp/zotero-paperread-extract.json`.

- [ ] **Step 3: Run a read-only real-item smoke test if Zotero SQLite is available**

Use an existing MCP raw response file for a real item whose MCP payload omits `extra`, or create a minimal local input:

```bash
RUN_DIR="$(mktemp -d)"
printf '{"key":"9B7RVPKL","title":"Adaptive interphase enabled pressure-free all-solid-state lithium metal batteries","attachments":[],"notes":[]}\n' > "$RUN_DIR/mcp-response.json"
uv run zotero-paperread save-item-details "$RUN_DIR/mcp-response.json" --output "$RUN_DIR/item-details.json" --raw-output "$RUN_DIR/item-details.raw.json" --zotero-sqlite /Users/jwxi/Zotero/zotero.sqlite
uv run zotero-paperread prepare-item "$RUN_DIR/item-details.json" --workdir "$RUN_DIR"
python - <<'PY' "$RUN_DIR/secondary_sources.json"
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(json.dumps(payload, ensure_ascii=False, indent=2))
assert any("mp.weixin.qq.com" in source["url"] for source in payload["sources"])
PY
```

Expected: `secondary_sources.json` contains the WeChat URL from Zotero `Extra`. This smoke test is read-only and does not write Zotero.

- [ ] **Step 4: Inspect diff**

Run:

```bash
git diff --stat
git diff -- src/zotero_paperread tests README.md skills/zotero-paper-summary/SKILL.md
```

Expected: diff contains only files listed in this plan, with no Zotero write path, no direct SQLite mutation, and no browser/network call inside `prepare-item`.

- [ ] **Step 5: Final local commit if any verification-only doc adjustment was needed**

Run only if Step 4 shows uncommitted implementation changes:

```bash
git add src/zotero_paperread tests README.md skills/zotero-paper-summary/SKILL.md
git commit -m "feat: surface Zotero extra secondary sources"
```

Expected: local commit succeeds. Do not push.

---

## Completion Criteria

- `save-item-details` preserves raw MCP output and enriches normalized `item-details.json` from read-only SQLite only when `extra` is missing.
- `item-details.json` records `_paperread.warnings` and `_paperread.enrichment.extra` provenance when SQLite fallback is used.
- `prepare-item` always writes `secondary_sources.json` and records `secondary_sources_json` in both CLI output and `run.json`.
- `secondary_sources.json` contains only `http://` / `https://` URLs from `extra`, with stable IDs and `pending_capture` status.
- Existing `capture-secondary-url.mjs` remains the only browser capture path.
- `summary_lint` rejects `secondary_context.md`, `secondary_contexts/*`, `secondary_sources.json`, and `wechat-context*` locators in `evidence_summary`.
- README and skill docs explain the Extra fallback, capture workflow, and cross-check-only boundary.
- Required verification commands pass:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-07-zotero-extra-secondary-sources.md`. Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh worker per task, review between tasks, faster iteration.
2. **Inline Execution** - execute tasks in this session using `superpowers:executing-plans`, with checkpoints after each task.

Choose one execution mode before implementation starts.
