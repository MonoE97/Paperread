# Zotero Batch Note Writing Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a repo-local `zotero-batch-note-writing` skill that guides batch Zotero paper summarization, preview-gated note creation, and readback verification without letting worker agents mutate Zotero.

**Architecture:** Keep orchestration rules in `SKILL.md`, move durable schemas and failure guidance into one-level `references/`, and script only the fragile repeatable checks: manifest validation, compact preview generation, and write report verification. Reuse the existing single-paper `zotero-paperread` CLI for extraction, summarization gates, note rendering, and write payload preparation; this plan does not add a second summarization engine.

**Tech Stack:** Codex skills, Markdown, Python 3 via `uv`, pytest, existing `zotero-paperread` CLI, Zotero MCP read/write boundaries, local batch artifacts under `runs/`.

---

## Review And Optimization Decisions

1. This plan creates a new skill; it does not run a Zotero batch and does not write Zotero notes.
2. V1 should not build a full batch runner. The strongest immediate value is a strict orchestration skill plus small deterministic scripts that catch mistakes before Zotero writes.
3. The repo currently has `skills/zotero-paper-summary/SKILL.md` for single-paper work and no `skills/zotero-batch-note-writing/` directory. The implementation must create the batch skill from scratch.
4. The batch skill must preserve the proven workflow: freeze candidates, analyze in bounded parallel workers, gate centrally, preview compactly, require explicit confirmation, serialize `write_note`, then verify with `get_item_details`.
5. Worker agents may create only local run artifacts. The coordinator is the only actor allowed to call Zotero MCP write tools.
6. WeChat, news, and blog links found in Zotero `Extra` / `其他` are secondary cross-check material only. They must not become locators in `evidence_summary`.
7. Batch reports must stay compact. `manifest.json`, `write-preview.md`, and `write-report.md` should use concise reasons and avoid raw note bodies, raw tracebacks, or chat transcript fragments.
8. Live Zotero hierarchy and tool exposure can drift. The skill must instruct agents to re-check tools and collection state before using historical keys or commands.

## File Structure

- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/SKILL.md`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/references/manifest-schema.md`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/references/worker-contract.md`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/references/failure-modes.md`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/scripts/validate_manifest.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/scripts/build_batch_preview.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/scripts/verify_write_report.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/fixtures/batch_manifest_valid.json`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/fixtures/batch_manifest_invalid_duplicate.json`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/fixtures/batch_manifest_for_preview.json`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/fixtures/batch_write_report_escaped_title.json`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_zotero_batch_skill_scripts.py`

---

### Task 1: Add Script Tests And Fixtures

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/fixtures/batch_manifest_valid.json`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/fixtures/batch_manifest_invalid_duplicate.json`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/fixtures/batch_manifest_for_preview.json`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/fixtures/batch_write_report_escaped_title.json`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_zotero_batch_skill_scripts.py`

- [ ] **Step 1: Write the valid manifest fixture**

Create `tests/fixtures/batch_manifest_valid.json` with this content:

```json
{
  "batch_id": "fixture-batch-001",
  "target_collection_path": "CATL/固态电池",
  "generated_date": "2026-05-11",
  "state": "frozen",
  "items": [
    {
      "item_key": "ABC123",
      "title": "Stable sulfide electrolyte",
      "normalized_title": "stable sulfide electrolyte",
      "source_collection_path": "CATL/固态电池",
      "status": "write_ready",
      "run_dir": "runs/2026-05-11/stable-sulfide-electrolyte-abc123",
      "existing_summary_notes": [],
      "secondary_sources": [
        {
          "kind": "wechat",
          "url": "https://mp.weixin.qq.com/s/example",
          "status": "captured"
        }
      ],
      "primary_pdf_status": "available",
      "trust_status": "trusted",
      "review_status": "passed",
      "note_title": "[Codex Summary] Stable sulfide electrolyte - 2026-05-11",
      "note_tags": ["codex-summary", "paper-summary"],
      "write_payload": "runs/2026-05-11/stable-sulfide-electrolyte-abc123/write-payload.json",
      "written_note_key": "",
      "blocked_reason": "",
      "error_detail": ""
    },
    {
      "item_key": "DEF456",
      "title": "Already summarized paper",
      "normalized_title": "already summarized paper",
      "source_collection_path": "CATL/固态电池",
      "status": "skipped_existing_summary",
      "run_dir": "",
      "existing_summary_notes": [
        {
          "key": "NOTE1",
          "title": "[Codex Summary] Already summarized paper - 2026-05-10"
        }
      ],
      "secondary_sources": [],
      "primary_pdf_status": "not_checked",
      "trust_status": "",
      "review_status": "",
      "note_title": "",
      "note_tags": [],
      "write_payload": "",
      "written_note_key": "",
      "blocked_reason": "existing_codex_summary",
      "error_detail": ""
    }
  ],
  "counts": {
    "discovered": 2,
    "queued": 0,
    "skipped_existing_summary": 1,
    "skipped_invalid_item": 0,
    "blocked_duplicate_normalized_title": 0,
    "blocked": 0,
    "write_ready": 1,
    "written": 0,
    "verified": 0,
    "failed": 0
  }
}
```

- [ ] **Step 2: Write the invalid duplicate fixture**

Create `tests/fixtures/batch_manifest_invalid_duplicate.json` with this content:

```json
{
  "batch_id": "fixture-batch-duplicate",
  "target_collection_path": "CATL/固态电池",
  "generated_date": "2026-05-11",
  "state": "frozen",
  "items": [
    {
      "item_key": "DUP111",
      "title": "Duplicate title",
      "normalized_title": "duplicate title",
      "source_collection_path": "CATL/固态电池",
      "status": "queued",
      "run_dir": "runs/2026-05-11/duplicate-title-dup111",
      "existing_summary_notes": [],
      "secondary_sources": [],
      "primary_pdf_status": "available",
      "trust_status": "",
      "review_status": "",
      "note_title": "",
      "note_tags": [],
      "write_payload": "",
      "written_note_key": "",
      "blocked_reason": "",
      "error_detail": ""
    },
    {
      "item_key": "DUP222",
      "title": "Duplicate title",
      "normalized_title": "duplicate title",
      "source_collection_path": "CATL/固态电池",
      "status": "queued",
      "run_dir": "runs/2026-05-11/duplicate-title-dup222",
      "existing_summary_notes": [],
      "secondary_sources": [],
      "primary_pdf_status": "available",
      "trust_status": "",
      "review_status": "",
      "note_title": "",
      "note_tags": [],
      "write_payload": "",
      "written_note_key": "",
      "blocked_reason": "",
      "error_detail": ""
    }
  ],
  "counts": {
    "discovered": 2,
    "queued": 2,
    "skipped_existing_summary": 0,
    "skipped_invalid_item": 0,
    "blocked_duplicate_normalized_title": 0,
    "blocked": 0,
    "write_ready": 0,
    "written": 0,
    "verified": 0,
    "failed": 0
  }
}
```

- [ ] **Step 3: Write the preview manifest fixture**

Create `tests/fixtures/batch_manifest_for_preview.json` with this content:

```json
{
  "batch_id": "fixture-preview-001",
  "target_collection_path": "CATL/固态电池",
  "generated_date": "2026-05-11",
  "state": "preview_ready",
  "items": [
    {
      "item_key": "READY1",
      "title": "Ready paper",
      "normalized_title": "ready paper",
      "source_collection_path": "CATL/固态电池",
      "status": "write_ready",
      "run_dir": "runs/2026-05-11/ready-paper-ready1",
      "existing_summary_notes": [],
      "secondary_sources": [{"kind": "wechat", "url": "https://mp.weixin.qq.com/s/example", "status": "captured"}],
      "primary_pdf_status": "available",
      "trust_status": "usable_with_caveats",
      "review_status": "passed_with_caveats",
      "note_title": "[Codex Summary] Ready paper - 2026-05-11",
      "note_tags": ["codex-summary", "paper-summary"],
      "write_payload": "runs/2026-05-11/ready-paper-ready1/write-payload.json",
      "written_note_key": "",
      "blocked_reason": "",
      "error_detail": "long traceback must not be printed"
    },
    {
      "item_key": "BLOCK1",
      "title": "Blocked paper",
      "normalized_title": "blocked paper",
      "source_collection_path": "CATL/固态电池",
      "status": "blocked",
      "run_dir": "",
      "existing_summary_notes": [],
      "secondary_sources": [],
      "primary_pdf_status": "missing",
      "trust_status": "",
      "review_status": "",
      "note_title": "",
      "note_tags": [],
      "write_payload": "",
      "written_note_key": "",
      "blocked_reason": "primary_pdf_missing",
      "error_detail": "full diagnostic kept out of preview"
    }
  ],
  "counts": {
    "discovered": 2,
    "queued": 0,
    "skipped_existing_summary": 0,
    "skipped_invalid_item": 0,
    "blocked_duplicate_normalized_title": 0,
    "blocked": 1,
    "write_ready": 1,
    "written": 0,
    "verified": 0,
    "failed": 0
  }
}
```

- [ ] **Step 4: Write the escaped-title write report fixture**

Create `tests/fixtures/batch_write_report_escaped_title.json` with this content:

```json
{
  "batch_id": "fixture-preview-001",
  "writes": [
    {
      "item_key": "READY1",
      "expected_note_title": "[Codex Summary] Li<sub>6</sub>PS<sub>5</sub>Cl interface paper - 2026-05-11",
      "expected_note_tags": ["codex-summary", "paper-summary"],
      "write_response": {
        "key": "NOTE123",
        "title": "[Codex Summary] Li&lt;sub&gt;6&lt;/sub&gt;PS&lt;sub&gt;5&lt;/sub&gt;Cl interface paper - 2026-05-11"
      },
      "readback": {
        "child_notes": [
          {
            "key": "NOTE123",
            "title": "[Codex Summary] Li&lt;sub&gt;6&lt;/sub&gt;PS&lt;sub&gt;5&lt;/sub&gt;Cl interface paper - 2026-05-11",
            "tags": ["codex-summary", "paper-summary"]
          }
        ]
      }
    }
  ]
}
```

- [ ] **Step 5: Write failing tests for the three scripts**

Create `tests/test_zotero_batch_skill_scripts.py` with this content:

```python
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "zotero-batch-note-writing"
FIXTURES = ROOT / "tests" / "fixtures"


def run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    script = SKILL / "scripts" / name
    return subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def parse_json_stdout(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def test_validate_manifest_accepts_valid_manifest():
    result = run_script(
        "validate_manifest.py",
        FIXTURES / "batch_manifest_valid.json",
    )
    assert result.returncode == 0, result.stderr
    payload = parse_json_stdout(result)
    assert payload["ok"] is True
    assert payload["errors"] == []


def test_validate_manifest_rejects_unblocked_duplicate_titles():
    result = run_script(
        "validate_manifest.py",
        FIXTURES / "batch_manifest_invalid_duplicate.json",
    )
    assert result.returncode == 1
    payload = parse_json_stdout(result)
    assert payload["ok"] is False
    assert any("duplicate normalized_title" in error for error in payload["errors"])


def test_build_batch_preview_is_compact_and_hides_error_detail(tmp_path):
    output = tmp_path / "write-preview.md"
    result = run_script(
        "build_batch_preview.py",
        FIXTURES / "batch_manifest_for_preview.json",
        "--output",
        output,
    )
    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    assert "# Zotero Batch Write Preview" in text
    assert "READY1" in text
    assert "primary_pdf_missing" in text
    assert "long traceback" not in text
    assert "full diagnostic" not in text


def test_verify_write_report_accepts_html_escaped_note_title():
    result = run_script(
        "verify_write_report.py",
        FIXTURES / "batch_write_report_escaped_title.json",
    )
    assert result.returncode == 0, result.stderr
    payload = parse_json_stdout(result)
    assert payload["ok"] is True
    assert payload["verified"] == ["READY1"]
```

- [ ] **Step 6: Run tests to verify they fail before scripts exist**

Run:

```bash
uv run pytest tests/test_zotero_batch_skill_scripts.py -q
```

Expected: tests fail because the skill scripts do not exist yet.

---

### Task 2: Create Manifest Validation Script

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/scripts/validate_manifest.py`

- [ ] **Step 1: Create the scripts directory**

Run:

```bash
mkdir -p skills/zotero-batch-note-writing/scripts
```

Expected: `skills/zotero-batch-note-writing/scripts/` exists.

- [ ] **Step 2: Write the complete validator**

Create `skills/zotero-batch-note-writing/scripts/validate_manifest.py` with this content:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


VALID_STATUSES = {
    "discovered",
    "skipped_existing_summary",
    "skipped_invalid_item",
    "blocked_duplicate_normalized_title",
    "queued",
    "prepared",
    "summarized",
    "reviewed",
    "gated",
    "previewed",
    "write_ready",
    "written",
    "verified",
    "blocked",
    "failed",
}

RUN_DIR_REQUIRED_STATUSES = {
    "queued",
    "prepared",
    "summarized",
    "reviewed",
    "gated",
    "previewed",
    "write_ready",
    "written",
    "verified",
}

REQUIRED_TOP_LEVEL = {"batch_id", "target_collection_path", "generated_date", "state", "items", "counts"}
REQUIRED_ITEM_FIELDS = {
    "item_key",
    "title",
    "normalized_title",
    "source_collection_path",
    "status",
    "run_dir",
    "existing_summary_notes",
    "secondary_sources",
    "primary_pdf_status",
    "trust_status",
    "review_status",
    "note_title",
    "note_tags",
    "write_payload",
    "written_note_key",
    "blocked_reason",
    "error_detail",
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be a JSON object")
    return data


def validate_manifest(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing_top = sorted(REQUIRED_TOP_LEVEL - data.keys())
    if missing_top:
        errors.append(f"missing top-level fields: {', '.join(missing_top)}")

    items = data.get("items")
    if not isinstance(items, list):
        errors.append("items must be a list")
        return errors

    seen_keys: set[str] = set()
    status_counts: Counter[str] = Counter()
    normalized_title_to_items: dict[str, list[str]] = defaultdict(list)

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"items[{index}] must be an object")
            continue

        missing_item = sorted(REQUIRED_ITEM_FIELDS - item.keys())
        if missing_item:
            errors.append(f"items[{index}] missing fields: {', '.join(missing_item)}")

        item_key = str(item.get("item_key", "")).strip()
        status = str(item.get("status", "")).strip()
        normalized_title = str(item.get("normalized_title", "")).strip()
        run_dir = str(item.get("run_dir", "")).strip()

        if not item_key:
            errors.append(f"items[{index}] item_key is empty")
        elif item_key in seen_keys:
            errors.append(f"duplicate item_key: {item_key}")
        seen_keys.add(item_key)

        if status not in VALID_STATUSES:
            errors.append(f"{item_key or f'items[{index}]'} has invalid status: {status}")
        else:
            status_counts[status] += 1

        if status in RUN_DIR_REQUIRED_STATUSES and not run_dir:
            errors.append(f"{item_key} status {status} requires run_dir")

        if normalized_title:
            normalized_title_to_items[normalized_title].append(item_key or f"items[{index}]")

        if status == "write_ready" and not str(item.get("write_payload", "")).strip():
            errors.append(f"{item_key} status write_ready requires write_payload")

        if status == "verified" and not str(item.get("written_note_key", "")).strip():
            errors.append(f"{item_key} status verified requires written_note_key")

    for normalized_title, item_keys in sorted(normalized_title_to_items.items()):
        if len(item_keys) <= 1:
            continue
        duplicate_statuses = [
            str(item.get("status", ""))
            for item in items
            if isinstance(item, dict) and str(item.get("normalized_title", "")).strip() == normalized_title
        ]
        if any(status != "blocked_duplicate_normalized_title" for status in duplicate_statuses):
            errors.append(
                "duplicate normalized_title must be blocked: "
                f"{normalized_title} -> {', '.join(item_keys)}"
            )

    counts = data.get("counts", {})
    if isinstance(counts, dict):
        for status, actual in sorted(status_counts.items()):
            if status in counts and counts[status] != actual:
                errors.append(f"counts.{status}={counts[status]} does not match actual {actual}")
        if "discovered" in counts and counts["discovered"] != len(items):
            errors.append(f"counts.discovered={counts['discovered']} does not match actual {len(items)}")
    else:
        errors.append("counts must be an object")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Zotero batch note-writing manifest.")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()

    try:
        data = load_json(args.manifest)
        errors = validate_manifest(data)
    except Exception as exc:
        errors = [f"could not read manifest: {exc}"]

    payload = {"ok": not errors, "errors": errors}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run validator tests**

Run:

```bash
uv run pytest tests/test_zotero_batch_skill_scripts.py::test_validate_manifest_accepts_valid_manifest tests/test_zotero_batch_skill_scripts.py::test_validate_manifest_rejects_unblocked_duplicate_titles -q
```

Expected: both validator tests pass.

---

### Task 3: Create Compact Preview Builder

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/scripts/build_batch_preview.py`

- [ ] **Step 1: Write the preview builder**

Create `skills/zotero-batch-note-writing/scripts/build_batch_preview.py` with this content:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be a JSON object")
    return data


def count_secondary_captured(item: dict[str, Any]) -> int:
    sources = item.get("secondary_sources", [])
    if not isinstance(sources, list):
        return 0
    return sum(1 for source in sources if isinstance(source, dict) and source.get("status") == "captured")


def render_preview(manifest: dict[str, Any]) -> str:
    items = manifest.get("items", [])
    counts = manifest.get("counts", {})
    if not isinstance(items, list):
        items = []
    if not isinstance(counts, dict):
        counts = {}

    lines: list[str] = [
        "# Zotero Batch Write Preview",
        "",
        f"- Batch: `{manifest.get('batch_id', '')}`",
        f"- Target collection: `{manifest.get('target_collection_path', '')}`",
        f"- Generated date: `{manifest.get('generated_date', '')}`",
        f"- State: `{manifest.get('state', '')}`",
        "",
        "## Counts",
        "",
    ]

    for key in [
        "discovered",
        "queued",
        "skipped_existing_summary",
        "skipped_invalid_item",
        "blocked_duplicate_normalized_title",
        "blocked",
        "write_ready",
        "written",
        "verified",
        "failed",
    ]:
        if key in counts:
            lines.append(f"- `{key}`: {counts[key]}")

    lines.extend(
        [
            "",
            "## Write-Ready Items",
            "",
            "| Item Key | Title | Review | Trust | Secondary Captured | Note Title |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
    )

    for item in items:
        if not isinstance(item, dict) or item.get("status") != "write_ready":
            continue
        lines.append(
            "| {item_key} | {title} | {review_status} | {trust_status} | {secondary_count} | {note_title} |".format(
                item_key=item.get("item_key", ""),
                title=str(item.get("title", "")).replace("|", "\\|"),
                review_status=item.get("review_status", ""),
                trust_status=item.get("trust_status", ""),
                secondary_count=count_secondary_captured(item),
                note_title=str(item.get("note_title", "")).replace("|", "\\|"),
            )
        )

    lines.extend(
        [
            "",
            "## Blocked Or Skipped Items",
            "",
            "| Item Key | Status | Reason | Title |",
            "| --- | --- | --- | --- |",
        ]
    )

    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", ""))
        if status in {"write_ready", "written", "verified"}:
            continue
        lines.append(
            "| {item_key} | {status} | {reason} | {title} |".format(
                item_key=item.get("item_key", ""),
                status=status,
                reason=str(item.get("blocked_reason", "")).replace("|", "\\|"),
                title=str(item.get("title", "")).replace("|", "\\|"),
            )
        )

    lines.extend(
        [
            "",
            "## Write Boundary",
            "",
            "No Zotero write has been performed by this preview. Continue only after explicit user confirmation.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a compact Zotero batch write preview.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    args.output.write_text(render_preview(manifest), encoding="utf-8")
    print(str(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run preview test**

Run:

```bash
uv run pytest tests/test_zotero_batch_skill_scripts.py::test_build_batch_preview_is_compact_and_hides_error_detail -q
```

Expected: the preview test passes and confirms `error_detail` text is not printed.

---

### Task 4: Create Write Report Verifier

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/scripts/verify_write_report.py`

- [ ] **Step 1: Write the verifier**

Create `skills/zotero-batch-note-writing/scripts/verify_write_report.py` with this content:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_TAGS = {"codex-summary", "paper-summary"}


def normalize_title(title: str) -> str:
    return " ".join(html.unescape(title).split())


def load_report(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("report root must be a JSON object")
    return data


def verify_report(report: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    verified: list[str] = []
    writes = report.get("writes", [])
    if not isinstance(writes, list):
        return [], ["writes must be a list"]

    for index, write in enumerate(writes):
        if not isinstance(write, dict):
            errors.append(f"writes[{index}] must be an object")
            continue

        item_key = str(write.get("item_key", "")).strip()
        expected_title = normalize_title(str(write.get("expected_note_title", "")))
        expected_tags = set(write.get("expected_note_tags", [])) or REQUIRED_TAGS
        write_response = write.get("write_response", {})
        readback = write.get("readback", {})

        if not item_key:
            errors.append(f"writes[{index}] missing item_key")
            continue
        if not isinstance(write_response, dict):
            errors.append(f"{item_key} write_response must be an object")
            continue
        if not isinstance(readback, dict):
            errors.append(f"{item_key} readback must be an object")
            continue

        note_key = str(write_response.get("key", "")).strip()
        if not note_key:
            errors.append(f"{item_key} write_response.key is empty")
            continue

        child_notes = readback.get("child_notes", [])
        if not isinstance(child_notes, list):
            errors.append(f"{item_key} readback.child_notes must be a list")
            continue

        matched_note = None
        for note in child_notes:
            if isinstance(note, dict) and str(note.get("key", "")).strip() == note_key:
                matched_note = note
                break

        if matched_note is None:
            errors.append(f"{item_key} note key {note_key} not found in readback")
            continue

        actual_title = normalize_title(str(matched_note.get("title", "")))
        if expected_title and actual_title != expected_title:
            errors.append(f"{item_key} title mismatch: expected {expected_title!r}, got {actual_title!r}")
            continue

        actual_tags = set(matched_note.get("tags", []))
        missing_tags = sorted(expected_tags - actual_tags)
        if missing_tags:
            errors.append(f"{item_key} missing tags: {', '.join(missing_tags)}")
            continue

        verified.append(item_key)

    return verified, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Zotero batch write report readback records.")
    parser.add_argument("report", type=Path)
    args = parser.parse_args()

    try:
        report = load_report(args.report)
        verified, errors = verify_report(report)
    except Exception as exc:
        verified, errors = [], [f"could not read report: {exc}"]

    payload = {"ok": not errors, "verified": verified, "errors": errors}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run verifier test**

Run:

```bash
uv run pytest tests/test_zotero_batch_skill_scripts.py::test_verify_write_report_accepts_html_escaped_note_title -q
```

Expected: the verifier accepts escaped Zotero note titles and reports `READY1` as verified.

---

### Task 5: Create Batch Skill Documentation

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/SKILL.md`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/references/manifest-schema.md`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/references/worker-contract.md`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-batch-note-writing/references/failure-modes.md`

- [ ] **Step 1: Create reference directory**

Run:

```bash
mkdir -p skills/zotero-batch-note-writing/references
```

Expected: `skills/zotero-batch-note-writing/references/` exists.

- [ ] **Step 2: Write the skill entrypoint**

Create `skills/zotero-batch-note-writing/SKILL.md` with this content:

```markdown
---
name: zotero-batch-note-writing
description: Use when the user asks to batch summarize Zotero collection items, process papers without Codex summary notes, use parallel workers for local paper analysis, preview notes before Zotero writes, or write multiple Zotero child notes with readback verification.
---

# Zotero Batch Note Writing

## Boundary

This skill orchestrates batch paper-note work for Zotero collections. It does not replace `skills/zotero-paper-summary/SKILL.md`; single-paper extraction, summary gates, note rendering, and write payload preparation still use the existing `uv run zotero-paperread ...` commands.

Persistent Zotero writes are coordinator-only. Worker agents may create local run artifacts but must not call `write_note`, mutate collections, edit Zotero SQLite, or change Better Notes settings.

## Default Workflow

1. Re-check live Zotero MCP tool exposure and resolve the target collection from live Zotero state.
2. Freeze candidates into `manifest.json`; after freeze, resume from the manifest instead of rebuilding the candidate set.
3. Mark existing Codex summaries using child-note title prefix `[Codex Summary]`, tag `codex-summary`, or body footer `Tags: codex-summary, paper-summary`.
4. Block duplicate normalized titles before analysis; do not choose a parent item for the user.
5. Dispatch bounded parallel workers only for local run artifacts.
6. Keep WeChat, news, and blog links as secondary cross-check material; never cite them in `evidence_summary`.
7. Run the central per-item gate chain before preview: `create-run -> prepare-item -> validate-summary-json -> apply-review -> validate-trusted-summary -> next-version-suffix -> finalize-note --html-output -> note-tags -> preview-note -> gate-run -> prepare-write-payload`.
8. Generate `write-preview.md` and stop for explicit user confirmation.
9. After confirmation, serialize `write_note` calls and immediately verify each write with `get_item_details`.
10. Generate `write-report.md` with compact status, note keys, and readback results.

## Required State Model

Use this state progression:

```text
discovered -> skipped_existing_summary / skipped_invalid_item / blocked_duplicate_normalized_title / queued -> prepared -> summarized -> reviewed -> gated -> previewed -> write_ready -> written -> verified
```

`blocked` and `failed` are terminal error states. `verified` is the only successful post-write terminal state.

## Script Helpers

- `scripts/validate_manifest.py <manifest.json>` checks manifest structure, duplicate item keys, duplicate normalized titles, status values, and count consistency.
- `scripts/build_batch_preview.py <manifest.json> --output <write-preview.md>` writes a compact preview without raw error detail.
- `scripts/verify_write_report.py <write-report.json>` verifies write response and readback records, including HTML-escaped note titles.

## References

- Read `references/manifest-schema.md` before freezing or resuming a batch.
- Read `references/worker-contract.md` before dispatching worker agents.
- Read `references/failure-modes.md` when a batch blocks, reports noisy output, sees duplicate titles, or encounters Zotero read/write mismatch.
```

- [ ] **Step 3: Write manifest schema reference**

Create `skills/zotero-batch-note-writing/references/manifest-schema.md` with this content:

```markdown
# Manifest Schema

`manifest.json` is the restart source of truth after candidate freeze. Do not rebuild the candidate set during resume unless the user asks for a fresh audit.

## Top-Level Fields

- `batch_id`: stable batch identifier.
- `target_collection_path`: human-readable Zotero collection path.
- `generated_date`: local date in `YYYY-MM-DD`.
- `state`: batch-level state such as `frozen`, `preview_ready`, `writing`, or `write_verified`.
- `items`: per-item records.
- `counts`: compact status counts.

## Required Item Fields

Each item record must include:

```json
{
  "item_key": "ABC123",
  "title": "Paper title",
  "normalized_title": "paper title",
  "source_collection_path": "CATL/固态电池",
  "status": "queued",
  "run_dir": "runs/YYYY-MM-DD/paper-title-abc123",
  "existing_summary_notes": [],
  "secondary_sources": [],
  "primary_pdf_status": "available",
  "trust_status": "",
  "review_status": "",
  "note_title": "",
  "note_tags": [],
  "write_payload": "",
  "written_note_key": "",
  "blocked_reason": "",
  "error_detail": ""
}
```

Keep `blocked_reason` short. Store long diagnostics in `error_detail`, and do not print `error_detail` in user-facing previews.
```

- [ ] **Step 4: Write worker contract reference**

Create `skills/zotero-batch-note-writing/references/worker-contract.md` with this content:

```markdown
# Worker Contract

Workers are local artifact producers. They are not Zotero writers.

## Allowed

- Read the assigned item record and its `run_dir`.
- Run existing `uv run zotero-paperread ...` commands for one paper.
- Create or update local files inside the assigned `run_dir`.
- Report local gate status and concise blockers to the coordinator.

## Forbidden

- Calling Zotero MCP `write_note`.
- Calling Zotero collection mutation tools.
- Editing Zotero SQLite or Zotero storage metadata.
- Changing Better Notes settings or templates.
- Rebuilding the batch manifest.
- Processing an item outside the assigned write scope.

## Worker Output

A successful worker returns paths for:

- `item-details.json`
- `context.md`
- `figure_context.md`
- `secondary_sources.json`
- `summary.json`
- `review.json`
- `note.md`
- `note.html`
- `gate-report.json`
- `write-payload.json`

Workers must treat WeChat, news, and blog captures as secondary context only.
```

- [ ] **Step 5: Write failure modes reference**

Create `skills/zotero-batch-note-writing/references/failure-modes.md` with this content:

```markdown
# Failure Modes

## Duplicate Normalized Titles

Block every item in the duplicate group as `blocked_duplicate_normalized_title`. Do not choose one item for the user.

## Existing Codex Summary

Mark `skipped_existing_summary` when a child note title starts with `[Codex Summary]`, metadata tags include `codex-summary`, or the note body includes `Tags: codex-summary, paper-summary`. If the user explicitly requests a new version, create a new versioned note title rather than overwriting the old note.

## Noisy Reports

Do not print raw note bodies, raw tracebacks, or chat transcript fragments in `write-preview.md` or `write-report.md`. Use concise reasons and keep long diagnostics in machine-readable JSON.

## Zotero Tool Drift

Re-check live MCP tool exposure before a batch. If expected tools are missing, stop and report the missing tool names instead of inferring Zotero state from memory.

## Collection Drift

Resolve collection paths from live Zotero state before any freeze or collection write. Do not reuse historical collection keys without readback.

## HTML-Escaped Note Titles

When verifying readback, compare normalized titles after HTML unescaping. Zotero note titles can store `<sub>` as `&lt;sub&gt;`.
```

- [ ] **Step 6: Verify skill text is discoverable and compact**

Run:

```bash
python - <<'PY'
from pathlib import Path
path = Path("skills/zotero-batch-note-writing/SKILL.md")
text = path.read_text(encoding="utf-8")
assert "name: zotero-batch-note-writing" in text
assert "description:" in text
assert "write_note" in text
assert "references/manifest-schema.md" in text
assert len(text.splitlines()) < 120
print("skill entrypoint ok")
PY
```

Expected: prints `skill entrypoint ok`.

---

### Task 6: Run Full Verification

**Files:**
- Verify all files created above.

- [ ] **Step 1: Run focused script tests**

Run:

```bash
uv run pytest tests/test_zotero_batch_skill_scripts.py -q
```

Expected: all four focused tests pass.

- [ ] **Step 2: Run project smoke verification**

Run:

```bash
uv run pytest
uv run zotero-paperread --help
```

Expected: pytest passes and CLI help exits 0.

- [ ] **Step 3: Run plan-specific manual checks**

Run:

```bash
python - <<'PY'
from pathlib import Path

required = [
    "skills/zotero-batch-note-writing/SKILL.md",
    "skills/zotero-batch-note-writing/references/manifest-schema.md",
    "skills/zotero-batch-note-writing/references/worker-contract.md",
    "skills/zotero-batch-note-writing/references/failure-modes.md",
    "skills/zotero-batch-note-writing/scripts/validate_manifest.py",
    "skills/zotero-batch-note-writing/scripts/build_batch_preview.py",
    "skills/zotero-batch-note-writing/scripts/verify_write_report.py",
    "tests/test_zotero_batch_skill_scripts.py",
]

missing = [path for path in required if not Path(path).exists()]
assert not missing, missing

skill = Path("skills/zotero-batch-note-writing/SKILL.md").read_text(encoding="utf-8")
assert "Persistent Zotero writes are coordinator-only" in skill
assert "must not call `write_note`" in skill
assert "Generate `write-preview.md` and stop for explicit user confirmation" in skill
print("batch skill file checks ok")
PY
```

Expected: prints `batch skill file checks ok`.

---

## Execution Notes

- Do not call Zotero MCP write tools while implementing this plan.
- Do not edit the existing single-paper skill except if a later review finds an explicit cross-reference is necessary.
- Do not add a repo README for the skill; the skill itself should contain only essential files.
- If implementation reveals that a full batch runner is necessary, stop and write a separate V2 plan instead of expanding this V1.

## Self-Review Checklist

- Spec coverage: the plan creates the batch skill, references, script helpers, fixtures, and tests.
- Safety coverage: the plan preserves preview-before-write, coordinator-only Zotero writes, and worker local-artifact boundaries.
- Scope coverage: the plan avoids collection mutation and avoids running a live Zotero batch.
- Progressive disclosure: `SKILL.md` remains compact and references hold the detailed schema and failure guidance.
