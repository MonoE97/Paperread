# Zotero Note Workflow Smoothing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the single-paper Zotero note workflow smoother and less error-prone by hard-stopping duplicate Zotero titles, saving MCP item details deterministically, capturing secondary web context consistently, making figure evidence boundaries explicit, aggregating gate status, and preparing safe write payloads.

**Architecture:** Keep semantic paper analysis and final Zotero writes in the agent workflow, but move repeatable serialization, linting, gate reporting, figure evidence classification, and write-payload preparation into deterministic project code. Do not add a direct Zotero write command in this first pass; the CLI will prepare `write-payload.json`, and the agent will still call `zotero-mcp write_note` only after explicit write intent and gate success.

**Tech Stack:** Python 3 via `uv`, Typer CLI, pytest, Jinja2 templates, PyMuPDF (`fitz`), local Node.js script for CDP secondary-source capture, Zotero MCP through Codex tool injection for real writes.

---

## Review And Optimization Notes

The previous proposal is directionally correct, but needs these corrections before implementation:

1. **Duplicate Zotero item handling must be a hard stop, not a selection UX.** If exact or normalized-title search finds more than one same-title Zotero item, stop before `create-run`, tell the user duplicate Zotero entries exist, and ask them to de-duplicate first. Do not let the agent choose among duplicate items.

2. **Collection inspection is intentionally out of scope for this plan.** It was useful in a prior run, but the user did not approve it for this first implementation round. Do not add SQLite collection logic in this plan.

3. **Direct `write-note-from-run` is too broad for the first pass.** The local CLI cannot call the Codex App-injected `mcp__zotero_mcp__` tool. Adding direct HTTP MCP write support would create a second write path and complicate safety. First implement `prepare-write-payload`, `gate-run`, and readback checklist; keep actual writes in the existing agent-mediated `write_note` step.

4. **Secondary web context must be permanently marked as secondary.** `capture-secondary-url` should output stable files for audit and cross-checking, but `evidence_summary` must still only cite `context.md` and `figure_context.md`.

5. **Figure evidence needs machine-readable boundaries.** Current `figure_context.md` has caption confidence and visual quality, but the downstream agent needs a clear tier: `pixel_verified`, `caption_text_grounded`, or `not_usable`.

## File Structure

- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`
  - Owns the agent-facing workflow rules, including duplicate-title hard stop, stable skill path guidance, secondary context boundaries, gate command order, and write payload usage.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`
  - Owns user-facing workflow documentation and command examples.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
  - Adds `save-item-details`, `lint-summary`, `gate-run`, and `prepare-write-payload`.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/zotero_item_io.py`
  - Parses MCP raw tool outputs and writes raw plus normalized item details.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/summary_lint.py`
  - Performs stronger summary/render lint checks that are stricter than `validate-summary-json` but less semantic than human review.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/gate.py`
  - Aggregates write-readiness status, version suffix, note paths, tags, review status, and blockers into a single report.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/write_payload.py`
  - Builds safe local write payload metadata without writing to Zotero.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/figures.py`
  - Adds figure evidence-tier classification.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/workflow.py`
  - Emits evidence tier and important-but-not-selected figure hints in `figure_context.md`.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/scripts/capture-secondary-url.mjs`
  - Captures secondary URL content through the local CDP proxy, with WeChat-compatible behavior.
- Create tests:
  - `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_zotero_item_io.py`
  - `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_summary_lint.py`
  - `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_gate.py`
  - `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_write_payload.py`
- Extend tests:
  - `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_note.py`
  - `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_figures.py`
  - `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_workflow.py`
  - `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_default_workflow_docs.py`

---

### Task 0: Prepare Branch And Baseline

**Files:**
- No file changes.

- [ ] **Step 1: Inspect current branch and status**

Run:

```bash
git branch --show-current
git status --short
```

Expected: branch name prints, and any dirty files are understood before editing.

- [ ] **Step 2: Create a feature branch if still on `main`**

Run only if Step 1 prints `main`:

```bash
git switch -c codex/zotero-note-workflow-smoothing
```

Expected:

```text
Switched to a new branch 'codex/zotero-note-workflow-smoothing'
```

- [ ] **Step 3: Run baseline tests**

Run:

```bash
uv run pytest
uv run zotero-paperread --help
```

Expected:

```text
165 passed
```

and the CLI help lists the existing commands before new commands are added.

---

### Task 1: Update Workflow Contract And Duplicate-Title Hard Stop

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_default_workflow_docs.py`

- [ ] **Step 1: Add failing docs test for duplicate hard stop and stable skill entry**

Append to `tests/test_default_workflow_docs.py`:

```python
def test_single_paper_workflow_documents_duplicate_title_hard_stop() -> None:
    skill = Path("skills/zotero-paper-summary/SKILL.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "same normalized title" in skill
    assert "stop before create-run" in skill
    assert "请先在 Zotero 中去重" in skill
    assert "duplicate Zotero entries" in readme
    assert "do not choose among duplicate items" in readme


def test_single_paper_workflow_avoids_plugin_hash_paths() -> None:
    skill = Path("skills/zotero-paper-summary/SKILL.md").read_text(encoding="utf-8")

    assert "/plugins/cache/openai-curated/superpowers/" not in skill
    assert "rg --files -g 'SKILL.md'" in skill
```

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: fails because the new wording is not documented yet.

- [ ] **Step 2: Update skill search rules**

In `skills/zotero-paper-summary/SKILL.md`, replace the current Step 1 search bullets with this exact policy:

```markdown
1. 搜索 Zotero 条目：
   - 先使用标题 exact 搜索；如果 0 个匹配，再使用 contains 搜索作为发现入口。
   - 0 个匹配：停止，告诉用户没有找到。
   - 多个 exact 匹配且标题归一化后相同：stop before create-run，停止分析和写入，告诉用户 Zotero 中存在 duplicate Zotero entries，请先在 Zotero 中去重；不要在重复条目中帮用户选择一个。
   - 多个 contains 匹配但不是同一 normalized title：列出候选标题、作者、年份和 key，停止，要求用户提供更精确标题或 item key；不要写入。
   - 1 个匹配：继续。
   - normalized title 比较至少忽略大小写、连续空白、常见 dash 变体和首尾空格；如果仍有多个同题条目，视为重复条目。
```

Also add this under the skill path guidance:

```markdown
如果历史记忆或用户消息给出旧 skill 绝对路径，不要依赖插件缓存 hash。先用当前项目内稳定入口：

```bash
rg --files -g 'SKILL.md' skills /Users/jwxi/.codex/skills | rg 'zotero|paper'
```

优先使用当前 repo 的 `skills/zotero-paper-summary/SKILL.md`。
```

- [ ] **Step 3: Update README duplicate policy**

Add a short subsection under the single-paper workflow:

```markdown
### Duplicate Zotero Entries

If a title search finds duplicate Zotero entries with the same normalized title, the workflow stops before `create-run`. The agent must not choose among duplicate items because writing to the wrong parent item is harder to recover from than asking the user to de-duplicate first.

The user-facing message should be direct: duplicate Zotero entries exist; please de-duplicate in Zotero first, then rerun the workflow. If a broad `contains` search finds several different titles, the workflow asks for a more exact title or item key instead.
```

- [ ] **Step 4: Run docs tests**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: passes.

- [ ] **Step 5: Commit**

Run:

```bash
git add skills/zotero-paper-summary/SKILL.md README.md tests/test_default_workflow_docs.py
git commit -m "docs: hard stop duplicate zotero title workflow"
```

Expected: commit succeeds.

---

### Task 2: Add MCP Item Details Serialization

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/zotero_item_io.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_zotero_item_io.py`
- Extend: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_note.py`

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_zotero_item_io.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotero_paperread.zotero_item_io import (
    normalize_item_details_payload,
    write_item_details_files,
)


def test_normalize_item_details_accepts_plain_item_object() -> None:
    payload = {
        "key": "ABC123",
        "title": "Example Paper",
        "attachments": [{"key": "PDF1", "contentType": "application/pdf", "path": "/tmp/a.pdf"}],
        "notes": [],
    }

    assert normalize_item_details_payload(payload)["key"] == "ABC123"


def test_normalize_item_details_accepts_mcp_text_response() -> None:
    item = {"key": "ABC123", "title": "Example Paper", "attachments": [], "notes": []}
    payload = [{"type": "text", "text": json.dumps(item)}]

    assert normalize_item_details_payload(payload)["title"] == "Example Paper"


def test_normalize_item_details_rejects_missing_key() -> None:
    with pytest.raises(ValueError, match="item details missing key"):
        normalize_item_details_payload({"title": "No Key"})


def test_write_item_details_files_writes_raw_and_normalized(tmp_path: Path) -> None:
    item = {"key": "ABC123", "title": "Example Paper", "attachments": [], "notes": []}
    raw_path = tmp_path / "item-details.raw.json"
    normalized_path = tmp_path / "item-details.json"

    result = write_item_details_files(item, normalized_path=normalized_path, raw_path=raw_path)

    assert result["item_key"] == "ABC123"
    assert normalized_path.exists()
    assert raw_path.exists()
    assert json.loads(normalized_path.read_text(encoding="utf-8"))["key"] == "ABC123"
```

Run:

```bash
uv run pytest tests/test_zotero_item_io.py -q
```

Expected: fails because `zotero_item_io.py` does not exist.

- [ ] **Step 2: Implement item details IO helper**

Create `src/zotero_paperread/zotero_item_io.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def normalize_item_details_payload(payload: Any) -> dict[str, Any]:
    """Return a Zotero item-details dict from raw MCP or plain JSON payload."""
    raw = payload
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and raw[0].get("type") == "text":
        text = raw[0].get("text")
        if not isinstance(text, str):
            raise ValueError("mcp text payload is not a string")
        raw = json.loads(text)

    if not isinstance(raw, dict):
        raise ValueError("item details payload must be a JSON object")

    key = str(raw.get("key", "")).strip()
    if not key:
        raise ValueError("item details missing key")
    title = str(raw.get("title", "")).strip()
    if not title:
        raise ValueError("item details missing title")

    normalized = dict(raw)
    attachments = normalized.get("attachments", [])
    if not isinstance(attachments, list):
        normalized["attachments"] = []
    notes = normalized.get("notes", [])
    if not isinstance(notes, list):
        normalized["notes"] = []
    return normalized


def write_item_details_files(
    payload: Any,
    *,
    normalized_path: Path,
    raw_path: Path | None = None,
) -> dict[str, Any]:
    """Write raw and normalized Zotero item details for a run bundle."""
    normalized = normalize_item_details_payload(payload)
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if raw_path is not None:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "item_key": normalized["key"],
        "title": normalized["title"],
        "normalized_path": str(normalized_path),
        "raw_path": str(raw_path) if raw_path is not None else None,
    }
```

- [ ] **Step 3: Add CLI command test**

Append to `tests/test_cli_note.py`:

```python
def test_save_item_details_command_writes_normalized_and_raw(tmp_path: Path) -> None:
    input_path = tmp_path / "mcp-response.json"
    output_path = tmp_path / "run" / "item-details.json"
    raw_output_path = tmp_path / "run" / "item-details.raw.json"
    input_path.write_text(
        json.dumps(
            [{"type": "text", "text": json.dumps({"key": "ABC123", "title": "Example Paper"})}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "save-item-details",
            str(input_path),
            "--output",
            str(output_path),
            "--raw-output",
            str(raw_output_path),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["key"] == "ABC123"
    assert raw_output_path.exists()
```

Run:

```bash
uv run pytest tests/test_cli_note.py::test_save_item_details_command_writes_normalized_and_raw -q
```

Expected: fails because the CLI command does not exist.

- [ ] **Step 4: Implement CLI command**

In `src/zotero_paperread/cli.py`, import:

```python
from zotero_paperread.zotero_item_io import write_item_details_files
```

Add command before `prepare-item`:

```python
@app.command("save-item-details")
def save_item_details_command(
    input_json: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write normalized item details JSON."),
    raw_output: Path | None = typer.Option(None, "--raw-output", help="Optionally write raw MCP payload JSON."),
) -> None:
    """Save raw MCP item details as normalized run item-details.json."""
    payload = json.loads(input_json.read_text(encoding="utf-8"))
    result = write_item_details_files(payload, normalized_path=output, raw_path=raw_output)
    typer.echo(json.dumps(result, ensure_ascii=False))
```

- [ ] **Step 5: Run tests**

Run:

```bash
uv run pytest tests/test_zotero_item_io.py tests/test_cli_note.py::test_save_item_details_command_writes_normalized_and_raw -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/zotero_paperread/zotero_item_io.py src/zotero_paperread/cli.py tests/test_zotero_item_io.py tests/test_cli_note.py
git commit -m "feat: save zotero item details from mcp payload"
```

Expected: commit succeeds.

---

### Task 3: Add Secondary URL Capture Script

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/scripts/capture-secondary-url.mjs`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_default_workflow_docs.py`

- [ ] **Step 1: Add docs test for secondary context boundary**

Append to `tests/test_default_workflow_docs.py`:

```python
def test_secondary_context_is_documented_as_non_evidence() -> None:
    skill = Path("skills/zotero-paper-summary/SKILL.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "capture-secondary-url" in skill
    assert "source_status: secondary_context" in skill
    assert "evidence_summary" in skill
    assert "must not cite secondary context" in readme
```

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py::test_secondary_context_is_documented_as_non_evidence -q
```

Expected: fails.

- [ ] **Step 2: Create CDP capture script**

Create `skills/zotero-paper-summary/scripts/capture-secondary-url.mjs`:

```javascript
#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const args = process.argv.slice(2);
const url = args[0];
const outputIndex = args.indexOf("--output");
const output = outputIndex >= 0 ? args[outputIndex + 1] : null;

if (!url || !output) {
  console.error("usage: capture-secondary-url.mjs <url> --output <path>");
  process.exit(2);
}

async function request(path, options = {}) {
  const response = await fetch(`http://localhost:3456${path}`, options);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return await response.json();
}

function markdownEscape(text) {
  return String(text || "").replace(/\r\n/g, "\n").trim();
}

const target = await request(`/new?url=${encodeURIComponent(url)}`);
const targetId = target.targetId;

try {
  const expression = `(() => {
    const pick = (selector) => document.querySelector(selector);
    const meta = (name) => document.querySelector(\`meta[property="\${name}"], meta[name="\${name}"]\`)?.content || "";
    const article = pick("#js_content") || pick(".rich_media_content") || pick("article") || document.body;
    const clone = article.cloneNode(true);
    clone.querySelectorAll("script,style,noscript,iframe,svg").forEach((node) => node.remove());
    const text = clone.innerText || "";
    return JSON.stringify({
      title: document.title || meta("og:title"),
      description: meta("og:description") || meta("description"),
      finalUrl: location.href,
      text
    });
  })()`;
  const result = await request(`/eval?target=${encodeURIComponent(targetId)}`, {
    method: "POST",
    body: expression,
  });
  const data = JSON.parse(result.value);
  const capturedAt = new Date().toISOString();
  const body = `# Secondary Context

- source_url: ${url}
- final_url: ${data.finalUrl}
- title: ${markdownEscape(data.title)}
- captured_at: ${capturedAt}
- capture_method: chrome_cdp
- source_status: secondary_context
- usage_boundary: cross-check only; must not be cited in evidence_summary

## Description

${markdownEscape(data.description) || "_No description._"}

## Text

${markdownEscape(data.text) || "_No text captured._"}
`;
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, body, "utf8");
  console.log(JSON.stringify({ output, targetId, title: data.title, textLength: data.text.length }));
} finally {
  await request(`/close?target=${encodeURIComponent(targetId)}`).catch(() => null);
}
```

- [ ] **Step 3: Update skill and README**

In the skill, add a secondary-source section:

```markdown
## 二级材料 capture

当用户提供微信公众号、新闻稿、博客或其他网页作为补充材料时，先用二级材料 capture，不要把网页正文混入 PDF 主证据。

```bash
node skills/zotero-paper-summary/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_context.md
```

微信公众号默认使用 Chrome CDP。输出文件必须包含 `source_status: secondary_context`。`evidence_summary` must not cite secondary context；它只能用于 cross-check、补充阅读背景和提示后续问题。
```

Add the same boundary to README.

- [ ] **Step 4: Run docs tests**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: passes.

- [ ] **Step 5: Commit**

Run:

```bash
git add skills/zotero-paper-summary/scripts/capture-secondary-url.mjs skills/zotero-paper-summary/SKILL.md README.md tests/test_default_workflow_docs.py
git commit -m "feat: document secondary url capture boundary"
```

Expected: commit succeeds.

---

### Task 4: Add Figure Evidence Tiers

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/figures.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/workflow.py`
- Extend: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_figures.py`
- Extend: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_workflow.py`

- [ ] **Step 1: Add failing figure-tier unit tests**

Append to `tests/test_figures.py`:

```python
from zotero_paperread.figures import classify_figure_evidence_tier


def test_classify_figure_evidence_tier_marks_source_pdf_as_pixel_verified() -> None:
    figure = {
        "source": "pdf-figure",
        "caption_confidence": 0.9,
        "visual_quality": {"status": "ok", "warnings": []},
    }

    assert classify_figure_evidence_tier(figure)["tier"] == "pixel_verified"


def test_classify_figure_evidence_tier_marks_embedded_low_caption_as_text_grounded() -> None:
    figure = {
        "source": "embedded-image",
        "caption_confidence": 0.56,
        "visual_quality": {"status": "ok", "warnings": []},
    }

    result = classify_figure_evidence_tier(figure)

    assert result["tier"] == "caption_text_grounded"
    assert "embedded-image" in result["reason"]


def test_classify_figure_evidence_tier_marks_tiny_image_not_usable() -> None:
    figure = {
        "source": "embedded-image",
        "caption_confidence": 0.2,
        "visual_quality": {"status": "poor", "warnings": ["image_too_small"]},
    }

    result = classify_figure_evidence_tier(figure)

    assert result["tier"] == "not_usable"
    assert "image_too_small" in result["reason"]
```

Run:

```bash
uv run pytest tests/test_figures.py::test_classify_figure_evidence_tier_marks_source_pdf_as_pixel_verified tests/test_figures.py::test_classify_figure_evidence_tier_marks_embedded_low_caption_as_text_grounded tests/test_figures.py::test_classify_figure_evidence_tier_marks_tiny_image_not_usable -q
```

Expected: fails because the function does not exist.

- [ ] **Step 2: Implement evidence tier classifier**

In `src/zotero_paperread/figures.py`, add:

```python
def classify_figure_evidence_tier(figure: dict[str, Any]) -> dict[str, str]:
    """Classify whether figure analysis can rely on pixels, caption/text, or neither."""
    source = str(figure.get("source", ""))
    caption_confidence = float(figure.get("caption_confidence") or 0.0)
    visual_quality = figure.get("visual_quality") if isinstance(figure.get("visual_quality"), dict) else {}
    warnings = visual_quality.get("warnings", []) if isinstance(visual_quality, dict) else []
    warning_text = ",".join(str(item) for item in warnings)

    if any(item in {"image_too_small", "image_low_information", "image_unreadable"} for item in warnings):
        return {"tier": "not_usable", "reason": f"visual quality warning: {warning_text}"}

    if source in {"pdf-figure", "arxiv-source", "deterministic-pdf"} and caption_confidence >= 0.75:
        return {"tier": "pixel_verified", "reason": "source and caption confidence are strong enough for visual cross-checking"}

    if caption_confidence > 0 or source == "embedded-image":
        return {"tier": "caption_text_grounded", "reason": f"{source or 'unknown source'} requires text/caption-grounded analysis"}

    return {"tier": "not_usable", "reason": "missing caption and weak source provenance"}
```

In `extract_figures`, after assigning `visual_quality`, add:

```python
tier = classify_figure_evidence_tier(item)
item["evidence_tier"] = tier["tier"]
item["evidence_tier_reason"] = tier["reason"]
```

- [ ] **Step 3: Extend workflow figure context**

In `src/zotero_paperread/workflow.py`, inside `build_figure_context_markdown`, add lines after `Visual Quality`:

```python
f"- Evidence Tier: {figure.get('evidence_tier', 'unknown')}",
f"- Analysis Boundary: {figure.get('evidence_tier_reason', '')}",
```

- [ ] **Step 4: Add workflow context test**

Append to `tests/test_workflow.py`:

```python
def test_figure_context_includes_evidence_tier() -> None:
    payload = {
        "arxiv_id": None,
        "candidate_count": 1,
        "pdf_path": "/tmp/paper.pdf",
        "source_attempts": [],
        "warnings": [],
        "selected_figures": [
            {
                "figure_id": "p1-f1",
                "caption": "Figure 1. Overview.",
                "caption_confidence": 0.56,
                "page": 1,
                "source": "embedded-image",
                "image_path": "/tmp/fig.png",
                "priority_score": 1.0,
                "needs_fallback": False,
                "visual_quality": {"status": "ok", "warnings": []},
                "evidence_tier": "caption_text_grounded",
                "evidence_tier_reason": "embedded-image requires text/caption-grounded analysis",
            }
        ],
    }

    context = workflow.build_figure_context_markdown(payload)

    assert "Evidence Tier: caption_text_grounded" in context
    assert "Analysis Boundary: embedded-image requires text/caption-grounded analysis" in context
```

Run:

```bash
uv run pytest tests/test_figures.py tests/test_workflow.py::test_figure_context_includes_evidence_tier -q
```

Expected: passes.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/zotero_paperread/figures.py src/zotero_paperread/workflow.py tests/test_figures.py tests/test_workflow.py
git commit -m "feat: classify figure evidence tiers"
```

Expected: commit succeeds.

---

### Task 5: Add Summary Lint

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/summary_lint.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_summary_lint.py`
- Extend: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_note.py`

- [ ] **Step 1: Write failing summary lint tests**

Create `tests/test_summary_lint.py`:

```python
from __future__ import annotations

from zotero_paperread.summary_lint import lint_summary


def test_lint_summary_flags_single_line_numbered_workflow() -> None:
    summary = {
        "workflow_steps": "1. First. 2. Second. 3. Third.",
        "evidence_summary": [],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "workflow_steps_single_line_numbered_list" for issue in issues)


def test_lint_summary_flags_secondary_context_evidence_locator() -> None:
    summary = {
        "workflow_steps": "1. First.\n2. Second.",
        "evidence_summary": [
            {
                "claim": "Claim",
                "evidence": [
                    {"type": "text", "locator": "secondary_context.md", "summary": "Not allowed"}
                ],
            }
        ],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "secondary_context_used_as_evidence" for issue in issues)


def test_lint_summary_flags_low_quality_figure_without_note() -> None:
    summary = {
        "workflow_steps": "1. First.\n2. Second.",
        "evidence_summary": [],
        "key_figures": [
            {"figure_id": "fig1", "image_quality": "image_too_small", "figure_quality_note": ""}
        ],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "low_quality_figure_missing_quality_note" for issue in issues)
```

Run:

```bash
uv run pytest tests/test_summary_lint.py -q
```

Expected: fails because `summary_lint.py` does not exist.

- [ ] **Step 2: Implement summary lint**

Create `src/zotero_paperread/summary_lint.py`:

```python
from __future__ import annotations

import re
from typing import Any

LOW_QUALITY_IMAGE_VALUES = {"poor", "image_too_small", "caption_only"}


def lint_summary(summary: dict[str, Any]) -> list[dict[str, str]]:
    """Return non-fatal summary issues that should be fixed before write-through."""
    issues: list[dict[str, str]] = []

    workflow_steps = summary.get("workflow_steps")
    if isinstance(workflow_steps, str) and "\n" not in workflow_steps and re.search(r"\b1\..*\b2\.", workflow_steps):
        issues.append(
            {
                "code": "workflow_steps_single_line_numbered_list",
                "message": "workflow_steps looks like a numbered list but has no line breaks",
            }
        )

    for claim_index, claim in enumerate(summary.get("evidence_summary", []) or []):
        if not isinstance(claim, dict):
            continue
        for evidence_index, evidence in enumerate(claim.get("evidence", []) or []):
            if not isinstance(evidence, dict):
                continue
            locator = str(evidence.get("locator", ""))
            if locator.startswith(("secondary_context", "wechat-context")):
                issues.append(
                    {
                        "code": "secondary_context_used_as_evidence",
                        "message": f"evidence_summary[{claim_index}].evidence[{evidence_index}] cites secondary context",
                    }
                )

    for index, figure in enumerate(summary.get("key_figures", []) or []):
        if not isinstance(figure, dict):
            continue
        image_quality = str(figure.get("image_quality", ""))
        figure_quality_note = str(figure.get("figure_quality_note", "")).strip()
        if image_quality in LOW_QUALITY_IMAGE_VALUES and not figure_quality_note:
            issues.append(
                {
                    "code": "low_quality_figure_missing_quality_note",
                    "message": f"key_figures[{index}] has {image_quality} without figure_quality_note",
                }
            )

    return issues
```

- [ ] **Step 3: Add CLI command test**

Append to `tests/test_cli_note.py`:

```python
def test_lint_summary_command_reports_issues(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "workflow_steps": "1. First. 2. Second.",
                "evidence_summary": [],
                "key_figures": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["lint-summary", str(summary_path)])

    assert result.exit_code == 1
    assert "workflow_steps_single_line_numbered_list" in result.stdout
```

Run:

```bash
uv run pytest tests/test_cli_note.py::test_lint_summary_command_reports_issues -q
```

Expected: fails because the CLI command does not exist.

- [ ] **Step 4: Implement CLI command**

In `src/zotero_paperread/cli.py`, import:

```python
from zotero_paperread.summary_lint import lint_summary
```

Add command:

```python
@app.command("lint-summary")
def lint_summary_command(summary_json: Path) -> None:
    """Run non-fatal summary lint checks used before write-through."""
    issues = lint_summary(read_json_or_exit(summary_json, label="summary JSON"))
    if issues:
        typer.echo(json.dumps({"status": "failed", "issues": issues}, ensure_ascii=False, indent=2))
        raise typer.Exit(1)
    typer.echo(json.dumps({"status": "passed", "issues": []}, ensure_ascii=False))
```

- [ ] **Step 5: Run tests**

Run:

```bash
uv run pytest tests/test_summary_lint.py tests/test_cli_note.py::test_lint_summary_command_reports_issues -q
```

Expected: passes.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/zotero_paperread/summary_lint.py src/zotero_paperread/cli.py tests/test_summary_lint.py tests/test_cli_note.py
git commit -m "feat: lint summary before zotero write"
```

Expected: commit succeeds.

---

### Task 6: Add Gate-Run Report

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/gate.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_gate.py`
- Extend: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_note.py`

- [ ] **Step 1: Write failing gate tests**

Create `tests/test_gate.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from zotero_paperread.gate import build_gate_report


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_build_gate_report_passes_ready_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_json(run_dir / "summary.json", {
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
    })
    write_json(run_dir / "review.json", {"review_status": "passed_with_caveats", "needs_improvement": False})
    write_json(run_dir / "item-details.json", {"key": "ABC123", "title": "Example Paper", "notes": []})
    (run_dir / "note.md").write_text("# note", encoding="utf-8")
    (run_dir / "note.html").write_text("<h1>note</h1>", encoding="utf-8")

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
```

Run:

```bash
uv run pytest tests/test_gate.py -q
```

Expected: fails because `gate.py` does not exist.

- [ ] **Step 2: Implement gate report**

Create `src/zotero_paperread/gate.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zotero_paperread.note import build_note_labels, validate_trusted_summary
from zotero_paperread.summary_lint import lint_summary
from zotero_paperread.zotero_details import next_version_suffix_from_details


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_gate_report(run_dir: Path, *, paper_title: str, generated_date: str) -> dict[str, Any]:
    """Build a single write-readiness report for a run directory."""
    run_dir = Path(run_dir)
    blockers: list[str] = []

    summary_path = run_dir / "summary.json"
    review_path = run_dir / "review.json"
    item_details_path = run_dir / "item-details.json"
    note_md_path = run_dir / "note.md"
    note_html_path = run_dir / "note.html"

    summary = _read_json(summary_path) if summary_path.exists() else {}
    review = _read_json(review_path) if review_path.exists() else {}
    item_details = _read_json(item_details_path) if item_details_path.exists() else {}

    if not summary_path.exists():
        blockers.append("missing summary.json")
    if not review_path.exists():
        blockers.append("missing review.json")
    if not item_details_path.exists():
        blockers.append("missing item-details.json")
    if not note_md_path.exists():
        blockers.append("missing note.md")
    if not note_html_path.exists():
        blockers.append("missing note.html")

    trusted_errors = validate_trusted_summary(summary) if summary else ["summary.json unavailable"]
    blockers.extend(f"trusted summary: {error}" for error in trusted_errors)

    lint_issues = lint_summary(summary) if summary else []
    blockers.extend(f"summary lint: {issue['code']}" for issue in lint_issues)

    if review.get("needs_improvement") is not False:
        blockers.append("review.json needs_improvement is not false")

    version_suffix = ""
    if item_details:
        version_suffix = next_version_suffix_from_details(
            item_details,
            paper_title=paper_title,
            generated_date=generated_date,
        )

    tags = build_note_labels(summary) if summary else []

    return {
        "status": "blocked" if blockers else "write_ready",
        "blockers": blockers,
        "run_dir": str(run_dir),
        "parentKey": str(item_details.get("key", "")),
        "paper_title": paper_title,
        "generated_date": generated_date,
        "version_suffix": version_suffix,
        "note_title": f"[Codex Summary] {paper_title} - {generated_date}{version_suffix}",
        "note_md_path": str(note_md_path),
        "note_html_path": str(note_html_path),
        "tags": tags,
        "review_status": summary.get("review_status"),
        "improvement_status": summary.get("improvement_status"),
        "trust_status": summary.get("trust_status"),
    }
```

- [ ] **Step 3: Add CLI command**

In `src/zotero_paperread/cli.py`, import:

```python
from zotero_paperread.gate import build_gate_report
```

Add command:

```python
@app.command("gate-run")
def gate_run_command(
    run_dir: Path,
    paper_title: str = typer.Option(..., "--paper-title", help="Paper title used in the generated note title."),
    generated_date: str = typer.Option(..., "--generated-date", help="Generated note date in YYYY-MM-DD form."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write gate report JSON."),
) -> None:
    """Aggregate run write-readiness into one report."""
    report = build_gate_report(run_dir, paper_title=paper_title, generated_date=generated_date)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    typer.echo(payload)
    if report["status"] != "write_ready":
        raise typer.Exit(1)
```

- [ ] **Step 4: Add CLI test**

Append to `tests/test_cli_note.py`:

```python
def test_gate_run_command_writes_blocked_report(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(json.dumps({"review_status": "not_reviewed"}), encoding="utf-8")
    report_path = tmp_path / "gate-report.json"

    result = runner.invoke(
        app,
        [
            "gate-run",
            str(run_dir),
            "--paper-title",
            "Example Paper",
            "--generated-date",
            "2026-05-06",
            "--output",
            str(report_path),
        ],
    )

    assert result.exit_code == 1
    assert report_path.exists()
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "blocked"
```

Run:

```bash
uv run pytest tests/test_gate.py tests/test_cli_note.py::test_gate_run_command_writes_blocked_report -q
```

Expected: passes.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/zotero_paperread/gate.py src/zotero_paperread/cli.py tests/test_gate.py tests/test_cli_note.py
git commit -m "feat: add zotero run gate report"
```

Expected: commit succeeds.

---

### Task 7: Add Safe Write Payload Preparation

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/write_payload.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_write_payload.py`
- Extend: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_note.py`

- [ ] **Step 1: Write failing write-payload tests**

Create `tests/test_write_payload.py`:

```python
from __future__ import annotations

import json
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
```

Run:

```bash
uv run pytest tests/test_write_payload.py -q
```

Expected: fails because `write_payload.py` does not exist.

- [ ] **Step 2: Implement write payload builder**

Create `src/zotero_paperread/write_payload.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any


def build_write_payload(gate_report: dict[str, Any]) -> dict[str, Any]:
    """Prepare safe metadata for a Zotero write_note call without writing."""
    if gate_report.get("status") != "write_ready":
        raise ValueError("gate report is not write_ready")
    note_html_path = Path(str(gate_report.get("note_html_path", "")))
    content = note_html_path.read_text(encoding="utf-8")
    title_prefix = str(gate_report.get("note_title", ""))[:120]
    parent_key = str(gate_report.get("parentKey", ""))
    tags = list(gate_report.get("tags", []))
    return {
        "action": "create",
        "parentKey": parent_key,
        "note_html_path": str(note_html_path),
        "contentLength": len(content),
        "titlePrefix": title_prefix,
        "contentPreview": content[:240],
        "tags": tags,
        "required_readback_checks": {
            "parentKey": parent_key,
            "tags": tags,
            "titlePrefix": title_prefix,
            "contentLengthAtLeast": max(len(content) - 20, 0),
        },
    }
```

- [ ] **Step 3: Add CLI command**

In `src/zotero_paperread/cli.py`, import:

```python
from zotero_paperread.write_payload import build_write_payload
```

Add command:

```python
@app.command("prepare-write-payload")
def prepare_write_payload_command(
    gate_report_json: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write write-payload JSON."),
) -> None:
    """Prepare a safe local write payload summary without writing to Zotero."""
    gate_report = read_json_or_exit(gate_report_json, label="gate report JSON")
    try:
        payload = build_write_payload(gate_report)
    except ValueError as exc:
        console.print(str(exc), soft_wrap=True)
        raise typer.Exit(1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
```

- [ ] **Step 4: Add CLI test**

Append to `tests/test_cli_note.py`:

```python
def test_prepare_write_payload_command_writes_payload(tmp_path: Path) -> None:
    note_html = tmp_path / "note.html"
    note_html.write_text("<h1>Title</h1>", encoding="utf-8")
    gate_report = tmp_path / "gate-report.json"
    gate_report.write_text(
        json.dumps(
            {
                "status": "write_ready",
                "parentKey": "ABC123",
                "note_html_path": str(note_html),
                "tags": ["codex-summary"],
                "note_title": "[Codex Summary] Title - 2026-05-06",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "write-payload.json"

    result = runner.invoke(app, ["prepare-write-payload", str(gate_report), "--output", str(output)])

    assert result.exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["parentKey"] == "ABC123"
```

Run:

```bash
uv run pytest tests/test_write_payload.py tests/test_cli_note.py::test_prepare_write_payload_command_writes_payload -q
```

Expected: passes.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/zotero_paperread/write_payload.py src/zotero_paperread/cli.py tests/test_write_payload.py tests/test_cli_note.py
git commit -m "feat: prepare zotero write payload metadata"
```

Expected: commit succeeds.

---

### Task 8: Update Skill And README With New Commands

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_default_workflow_docs.py`

- [ ] **Step 1: Add docs test for new command chain**

Append to `tests/test_default_workflow_docs.py`:

```python
def test_docs_show_smoothed_write_gate_command_chain() -> None:
    skill = Path("skills/zotero-paper-summary/SKILL.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    for text in (skill, readme):
        assert "save-item-details" in text
        assert "lint-summary" in text
        assert "gate-run" in text
        assert "prepare-write-payload" in text
        assert "write_note" in text
        assert "prepare-write-payload does not write to Zotero" in text
```

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py::test_docs_show_smoothed_write_gate_command_chain -q
```

Expected: fails until docs are updated.

- [ ] **Step 2: Update skill workflow**

In `skills/zotero-paper-summary/SKILL.md`, update the command chain so the final write-through sequence is:

```bash
uv run zotero-paperread save-item-details <mcp-response.json> --output <run_dir>/item-details.json --raw-output <run_dir>/item-details.raw.json
uv run zotero-paperread prepare-item <run_dir>/item-details.json --workdir <run_dir>
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread lint-summary <run_dir>/summary.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
PAPER_TITLE="<confirmed Zotero item title>"
GENERATED_DATE="$(date +%F)"
VERSION_SUFFIX="$(uv run zotero-paperread next-version-suffix "$PAPER_TITLE" --date "$GENERATED_DATE")"
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --generated-date "$GENERATED_DATE" --version-suffix "$VERSION_SUFFIX" --output <run_dir>/note.md --html-output <run_dir>/note.html
uv run zotero-paperread gate-run <run_dir> --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE" --output <run_dir>/gate-report.json
uv run zotero-paperread prepare-write-payload <run_dir>/gate-report.json --output <run_dir>/write-payload.json
```

Add:

```markdown
`prepare-write-payload does not write to Zotero`; it only prepares metadata for the agent-side `write_note` call and readback checklist. Real writes still happen only through `zotero-mcp write_note`.
```

- [ ] **Step 3: Update README command examples**

Add a matching command section in README and clarify:

```markdown
`prepare-write-payload does not write to Zotero`. It records `parentKey`, tags, `note_html_path`, `contentLength`, and readback checks. The actual write remains an explicit `zotero-mcp write_note` action performed by the agent after the gate report is `write_ready`.
```

- [ ] **Step 4: Run docs tests**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: passes.

- [ ] **Step 5: Commit**

Run:

```bash
git add skills/zotero-paper-summary/SKILL.md README.md tests/test_default_workflow_docs.py
git commit -m "docs: document smoothed zotero note gate"
```

Expected: commit succeeds.

---

### Task 9: Final Verification

**Files:**
- No planned file edits unless failures expose a bug in the current task set.

- [ ] **Step 1: Run full test suite**

Run:

```bash
uv run pytest
```

Expected:

```text
passed
```

- [ ] **Step 2: Run CLI help**

Run:

```bash
uv run zotero-paperread --help
```

Expected: command list includes:

```text
save-item-details
lint-summary
gate-run
prepare-write-payload
```

- [ ] **Step 3: Run fixture PDF extraction**

Run:

```bash
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

Expected:

```text
Wrote extraction JSON: /tmp/zotero-paperread-extract.json
```

- [ ] **Step 4: Inspect final status**

Run:

```bash
git status --short
```

Expected: only intended tracked changes are present if not committed; otherwise clean.

---

## Self-Review

- **Spec coverage:** Covers accepted items 1, 4, 5, 6, 7, and 8. Item 2 is intentionally narrowed to duplicate-title hard stop. Item 3 collection inspection is intentionally out of scope.
- **Write safety:** No new direct Zotero write path is introduced. `prepare-write-payload` only creates local metadata and readback checks.
- **Evidence safety:** Secondary context is explicitly blocked from `evidence_summary`; figure evidence receives machine-readable tiers.
- **Implementation scope:** Plan is sequential and testable. The only browser automation piece is a local skill script; deterministic Python tests cover the durable behavior.
