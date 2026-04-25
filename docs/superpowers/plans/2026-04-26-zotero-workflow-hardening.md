# Zotero Workflow Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Zotero-first paper summary workflow deterministic enough that a normal paper can be found, prepared, reviewed, versioned, previewed, and written to Zotero without manual metadata reconstruction or unchecked quality assumptions.

**Architecture:** Keep semantic judgment in the Codex skill, but move repeatable gates into Python utilities and CLI commands. Treat Zotero MCP as the source of item details; when a tool is not visible, solve it as Codex tool discovery, not as missing Zotero metadata. Keep all Zotero writes behind explicit write intent and `write_note`.

**Tech Stack:** Python 3 via `uv`, Typer CLI, Jinja2 templates, PyMuPDF (`fitz`), pytest, Zotero MCP (`cookjohn/zotero-mcp` streamable HTTP).

---

## Root Cause Map

1. **MCP tool discovery was mistaken for MCP capability absence.**
   - Observed: initial visible tools did not include `get_item_details`.
   - Verified: Zotero MCP plugin `cookjohn/zotero-mcp` v1.4.7 contains and exposes `get_item_details`; `tool_search` loads it into Codex.
   - Root cause: the skill did not require a preflight `tool_search` step for the complete tool set.

2. **The workflow relied on manual item detail reconstruction.**
   - Observed: `item-details.json` was manually assembled from `search_library`, local Zotero storage, and PDF text.
   - Correct path: use `get_item_details(itemKey, mode="complete")`, which returns DOI, URL, abstract, notes, attachment path, content type, filename, and file size.

3. **Existing note detection and same-day versioning are procedural.**
   - Observed: existing `[Codex Summary]` note detection used approximate note fulltext search.
   - Root cause: no deterministic parser consumes `get_item_details().notes` and no CLI step wires the computed version suffix into `finalize-note`.

4. **Write intent vocabulary is under-specified.**
   - Observed: user intended "输出笔记" to mean "write to Zotero", but the skill only listed "写入/写回/创建 note/保存到 Zotero" style triggers.
   - Root cause: natural-language write intent is a project convention but not documented in the skill.

5. **Review and trusted-note gates are not enforced by code.**
   - Observed: `summary.json` can pass `validate-summary-json` with only a top-level object, and `finalize-note` can render a note with weak/default trusted fields.
   - Root cause: schema validation, review merge, and write-readiness validation are still agent discipline, not deterministic commands.

6. **Figure evidence quality is not guarded.**
   - Observed: high-priority figure crops were visually useless while `figure_context.md` still ranked them highly.
   - Root cause: figure ranking uses caption/geometry signals, but does not assess rendered image quality before allowing figure evidence to be trusted.

7. **Template list formatting had a rendering defect.**
   - Observed: review bullets were rendered onto the same line before the local template patch.
   - Root cause: Jinja trimming removed expected line breaks in `templates/zotero_note.md.j2`.

## File Structure

- Modify `skills/zotero-paper-summary/SKILL.md`
  - Owns the agent-facing workflow: tool discovery, write intent semantics, existing note policy, and write gate order.
- Modify `README.md`
  - Owns user-facing workflow documentation and command examples.
- Create `src/zotero_paperread/zotero_details.py`
  - Pure parsing helpers for Zotero MCP item detail payloads: existing Codex note title extraction, attachment path readiness, and same-day version suffix selection.
- Create `tests/test_zotero_details.py`
  - Unit tests for parsing `get_item_details` payloads without calling Zotero.
- Modify `src/zotero_paperread/note.py`
  - Add strict trusted summary validation helpers while keeping legacy rendering permissive.
- Modify `src/zotero_paperread/cli.py`
  - Add version-suffix, strict validation, and review-merge commands.
- Create `src/zotero_paperread/review.py`
  - Deterministic merge/gate helpers for `review.json` and `summary.json`.
- Create `tests/test_review.py`
  - Unit tests for review application and write-gate decisions.
- Modify `src/zotero_paperread/figures.py`
  - Add rendered-image quality assessment and propagate warnings into selected figure records.
- Extend `tests/test_figures.py`
  - Cover tiny, text-only, formula-strip, blank, and low-information figure crop detection.
- Modify `templates/zotero_note.md.j2`
  - Keep review and improvement list bullets separated.
- Extend `tests/test_note.py`
  - Assert review/improvement bullets remain separated in rendered Markdown.

---

### Task 0: Prepare Feature Branch or Worktree

**Files:**
- No file changes.

- [ ] **Step 1: Inspect the current branch and dirty state**

Run:

```bash
git branch --show-current
git status --short
```

Expected:

```text
<current-branch-name>
```

The status may include pre-existing local edits. Do not revert or stage unrelated edits.

- [ ] **Step 2: Enter a feature branch if still on `main`**

If Step 1 prints `main`, run:

```bash
git switch -c codex/zotero-workflow-hardening
```

Expected:

```text
Switched to a new branch 'codex/zotero-workflow-hardening'
```

If Step 1 already prints a non-`main` feature branch, stay on it and record the branch name in the task notes.

- [ ] **Step 3: Verify branch readiness**

Run:

```bash
git branch --show-current
git status --short
```

Expected:

```text
codex/zotero-workflow-hardening
```

or another non-`main` feature branch. This satisfies the project rule that feature development happens on a branch or worktree before the first commit.

---

### Task 1: Update Skill and README Workflow Contract

**Files:**
- Modify: `skills/zotero-paper-summary/SKILL.md`
- Modify: `README.md`

- [ ] **Step 1: Document Zotero MCP tool discovery preflight**

In `skills/zotero-paper-summary/SKILL.md`, under `## 工具边界`, replace the current Zotero MCP bullets with:

````markdown
- 开始前先用 `tool_search` 精确加载 Zotero MCP 工具，至少查询：

```text
zotero mcp search_library get_item_details get_content write_note annotations
```

- 必需读工具：
  - `zotero-mcp search_library`
  - `zotero-mcp get_item_details`
  - `zotero-mcp get_content` 或本项目 `prepare-item` 的 PDF 抽取路径
- 必需写工具：
  - 只有显式写入时才调用 `zotero-mcp write_note`
- 如果 `get_item_details` 初始不可见，不要手工拼 metadata；先用 `tool_search` 重新加载。只有 `tool_search` 后仍不可用，才停止并说明这是 Codex App 工具发现/注入问题。
````

- [ ] **Step 2: Add "输出笔记" as explicit write intent**

In `skills/zotero-paper-summary/SKILL.md`, under `## 目标`, replace the write-intent sentence with:

```markdown
把 Zotero 中的一篇论文转换为中文结构化研究笔记。默认只 dry-run；当用户明确要求“输出笔记”“写入笔记”“写回 Zotero”“创建 note”“保存到 Zotero”等动作时，执行分析、预览并创建 Zotero 子笔记。
```

Also add this example under natural-language write intent:

```text
请对 Zotero 中的 <paper title> 文章进行分析并输出笔记
```

- [ ] **Step 3: Replace manual item detail fallback with `get_item_details`**

In `skills/zotero-paper-summary/SKILL.md`, step 3 should say:

```markdown
3. 获取条目详情：
   - 调用 `get_item_details(itemKey=<item_key>, mode="complete")`。
   - 保存原始返回到 `<run_dir>/item-details.json`。
   - 如果返回中 `attachments[].path` 已有本地 PDF 路径，直接交给 `prepare-item`。
   - 如果没有 PDF path，但有 PDF attachment key，先报告 `missing_pdf_path_in_item_details`，不要直接猜 Zotero storage 路径；只有用户明确要求排障时才进行本机路径探测。
```

- [ ] **Step 4: Update README safety and normal command sequence**

In `README.md`, add a short "MCP Tool Discovery" section after "Codex Workflow":

```markdown
## MCP Tool Discovery

Codex App may lazy-load MCP tool schemas. Before running a Zotero note workflow, load the full Zotero tool set with a targeted tool search for `search_library`, `get_item_details`, `get_content`, and `write_note`. If `get_item_details` is not initially visible, treat that as a tool discovery issue, not as missing Zotero metadata.
```

- [ ] **Step 5: Verify docs**

Run:

```bash
rg -n "tool_search|get_item_details|输出笔记|missing_pdf_path_in_item_details" README.md skills/zotero-paper-summary/SKILL.md
```

Expected:

```text
README.md:<line>:...get_item_details...
skills/zotero-paper-summary/SKILL.md:<line>:...tool_search...
skills/zotero-paper-summary/SKILL.md:<line>:...输出笔记...
```

- [ ] **Step 6: Commit**

```bash
git add README.md skills/zotero-paper-summary/SKILL.md
git commit -m "docs: clarify zotero mcp discovery workflow"
```

---

### Task 2: Add Deterministic Zotero Item Detail Parsing

**Files:**
- Create: `src/zotero_paperread/zotero_details.py`
- Create: `tests/test_zotero_details.py`
- Modify: `src/zotero_paperread/cli.py`
- Modify: `tests/test_cli_note.py`

- [ ] **Step 1: Write failing tests for Codex note title extraction**

Create `tests/test_zotero_details.py`:

```python
from zotero_paperread.zotero_details import (
    codex_summary_titles_from_details,
    next_version_suffix_from_details,
    primary_pdf_path_from_details,
)


def test_codex_summary_titles_from_html_notes() -> None:
    details = {
        "notes": [
            "<h1>[Codex Summary] Paper A - 2026-04-26</h1><p>body</p>",
            "<h1>Manual note</h1>",
            "<h2>[Codex Summary] Paper A - 2026-04-26 (v2)</h2>",
        ]
    }

    assert codex_summary_titles_from_details(details) == [
        "[Codex Summary] Paper A - 2026-04-26",
        "[Codex Summary] Paper A - 2026-04-26 (v2)",
    ]


def test_codex_summary_titles_from_markdown_notes() -> None:
    details = {
        "notes": [
            "# [Codex Summary] Paper B - 2026-04-26\n\nBody",
            "No heading",
        ]
    }

    assert codex_summary_titles_from_details(details) == [
        "[Codex Summary] Paper B - 2026-04-26"
    ]


def test_next_version_suffix_from_details() -> None:
    details = {
        "notes": [
            "<h1>[Codex Summary] Paper A - 2026-04-26</h1>",
            "<h1>[Codex Summary] Paper A - 2026-04-26 (v2)</h1>",
        ]
    }

    assert next_version_suffix_from_details(
        details,
        paper_title="Paper A",
        generated_date="2026-04-26",
    ) == " (v3)"


def test_primary_pdf_path_from_details() -> None:
    details = {
        "attachments": [
            {
                "key": "SUPP",
                "filename": "supporting-information.pdf",
                "title": "Supporting Information",
                "contentType": "application/pdf",
                "path": "/tmp/supporting-information.pdf",
            },
            {
                "key": "MAIN",
                "filename": "paper.pdf",
                "title": "PDF",
                "contentType": "application/pdf",
                "path": "/tmp/paper.pdf",
            },
        ]
    }

    assert primary_pdf_path_from_details(details) == "/tmp/paper.pdf"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_zotero_details.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'zotero_paperread.zotero_details'
```

- [ ] **Step 3: Implement parsing helpers**

Create `src/zotero_paperread/zotero_details.py`:

```python
from __future__ import annotations

import html
import re
from typing import Any

from zotero_paperread.note import next_same_day_version_suffix
from zotero_paperread.workflow import select_pdf_attachment

HTML_HEADING_RE = re.compile(
    r"<h[1-6][^>]*>\s*(?P<title>\[Codex Summary\].*?)\s*</h[1-6]>",
    re.IGNORECASE | re.DOTALL,
)
MARKDOWN_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s+(?P<title>\[Codex Summary\].*?)\s*$",
    re.MULTILINE,
)
TAG_RE = re.compile(r"<[^>]+>")


def _clean_title(value: str) -> str:
    text = html.unescape(value)
    text = TAG_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def codex_summary_titles_from_details(details: dict[str, Any]) -> list[str]:
    notes = details.get("notes", [])
    if not isinstance(notes, list):
        return []

    titles: list[str] = []
    for note in notes:
        if not isinstance(note, str):
            continue
        for match in HTML_HEADING_RE.finditer(note):
            title = _clean_title(match.group("title"))
            if title:
                titles.append(title)
        for match in MARKDOWN_HEADING_RE.finditer(note):
            title = _clean_title(match.group("title"))
            if title:
                titles.append(title)
    return titles


def next_version_suffix_from_details(
    details: dict[str, Any],
    *,
    paper_title: str,
    generated_date: str,
) -> str:
    return next_same_day_version_suffix(
        codex_summary_titles_from_details(details),
        paper_title=paper_title,
        generated_date=generated_date,
    )


def primary_pdf_path_from_details(details: dict[str, Any]) -> str:
    attachments = details.get("attachments", [])
    if not isinstance(attachments, list):
        return ""
    selected = select_pdf_attachment(attachments)
    return str(selected.get("path", "")) if selected else ""
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
uv run pytest tests/test_zotero_details.py -q
```

Expected:

```text
4 passed
```

- [ ] **Step 5: Add failing CLI test for version suffix computation**

Append to `tests/test_cli_note.py`:

```python
def test_next_version_suffix_command_reads_item_details(tmp_path: Path) -> None:
    details_path = tmp_path / "item-details.json"
    write_json(
        details_path,
        {
            "notes": [
                "<h1>[Codex Summary] Paper A - 2026-04-26</h1>",
                "<h1>[Codex Summary] Paper A - 2026-04-26 (v2)</h1>",
            ]
        },
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "next-version-suffix",
            str(details_path),
            "--paper-title",
            "Paper A",
            "--generated-date",
            "2026-04-26",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == " (v3)\n"
```

- [ ] **Step 6: Run CLI test and verify failure**

Run:

```bash
uv run pytest tests/test_cli_note.py::test_next_version_suffix_command_reads_item_details -q
```

Expected:

```text
Error: No such command 'next-version-suffix'
```

- [ ] **Step 7: Add CLI command**

In `src/zotero_paperread/cli.py`, import:

```python
from zotero_paperread.zotero_details import next_version_suffix_from_details
```

Add command near the other note-preparation commands:

```python
@app.command("next-version-suffix")
def next_version_suffix_command(
    details_json: Path,
    paper_title: str = typer.Option(..., "--paper-title", help="Exact Zotero item title."),
    generated_date: str = typer.Option(..., "--generated-date", help="Note date in YYYY-MM-DD form."),
) -> None:
    """Compute the same-day note title suffix from Zotero item details."""
    suffix = next_version_suffix_from_details(
        read_json_or_exit(details_json, label="details JSON"),
        paper_title=paper_title,
        generated_date=generated_date,
    )
    typer.echo(suffix)
```

- [ ] **Step 8: Run parser and CLI tests**

Run:

```bash
uv run pytest tests/test_zotero_details.py tests/test_cli_note.py::test_next_version_suffix_command_reads_item_details -q
uv run zotero-paperread --help
```

Expected:

```text
pytest: all tests pass
help: command list includes next-version-suffix
```

- [ ] **Step 9: Commit**

```bash
git add src/zotero_paperread/zotero_details.py tests/test_zotero_details.py src/zotero_paperread/cli.py tests/test_cli_note.py
git commit -m "feat: parse zotero item details for note versioning"
```

---

### Task 3: Add Strict Trusted Summary Validation

**Files:**
- Modify: `src/zotero_paperread/note.py`
- Modify: `src/zotero_paperread/cli.py`
- Modify: `tests/test_cli_note.py`

- [ ] **Step 1: Add failing CLI tests**

Append to `tests/test_cli_note.py`:

```python
def test_validate_trusted_summary_fails_without_review_gate(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_json(
        summary_path,
        {
            "one_sentence_summary": "ok",
            "paper_type": "research_article",
            "trust_status": "usable_with_caveats",
            "review_status": "not_reviewed",
            "evidence_summary": [],
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["validate-trusted-summary", str(summary_path)])

    assert result.exit_code == 1
    assert "trusted_summary_invalid:" in result.stdout
    assert "review_status must be passed or passed_with_caveats" in result.stdout
    assert "evidence_summary must contain at least one claim" in result.stdout


def test_validate_trusted_summary_fails_empty_core_content(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_json(
        summary_path,
        {
            "one_sentence_summary": "",
            "abstract_translation": "",
            "key_points": [],
            "research_question": "",
            "method": "",
            "experiments": "",
            "contributions": [],
            "limitations": [],
            "ai4s_relevance": "",
            "follow_up_keywords": [],
            "paper_type": "method_paper",
            "trust_status": "usable_with_caveats",
            "trust_rationale": "Evidence was checked.",
            "review_status": "passed",
            "evidence_summary": [
                {
                    "claim": "The method is supported.",
                    "evidence": [{"type": "text", "locator": "context.md page 2", "summary": "method evidence"}],
                    "confidence": "high",
                }
            ],
            "improvement_status": "not_needed",
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["validate-trusted-summary", str(summary_path)])

    assert result.exit_code == 1
    assert "one_sentence_summary is required" in result.stdout
    assert "method is required" in result.stdout
    assert "key_points must contain at least one item" in result.stdout


def test_validate_trusted_summary_passes_ready_summary(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    write_json(
        summary_path,
        {
            "one_sentence_summary": "This paper proposes a field-aware ML workflow.",
            "abstract_translation": "本文提出一个有限电场机器学习工作流。",
            "key_points": ["Field-aware forces", "Charge response model"],
            "research_question": "How can finite-field interface simulations be accelerated?",
            "method": "The method combines force learning and charge-response learning.",
            "experiments": "The paper validates the workflow on Au/NaCl interfaces.",
            "contributions": ["ML finite-field dynamics", "ML charge response"],
            "limitations": ["Single benchmark chemistry"],
            "ai4s_relevance": "The decomposition is useful for field-driven AI4S simulations.",
            "follow_up_keywords": ["finite-field MD"],
            "paper_type": "method_paper",
            "trust_status": "usable_with_caveats",
            "trust_rationale": "Text extraction is complete and figure evidence is caveated.",
            "review_status": "passed_with_caveats",
            "evidence_summary": [
                {
                    "claim": "The method is supported.",
                    "evidence": [{"type": "text", "locator": "context.md page 2", "summary": "method evidence"}],
                    "confidence": "high",
                }
            ],
            "review_issues": [],
            "improvement_status": "completed",
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["validate-trusted-summary", str(summary_path)])

    assert result.exit_code == 0
    assert "trusted_summary_valid" in result.stdout
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_cli_note.py::test_validate_trusted_summary_fails_without_review_gate tests/test_cli_note.py::test_validate_trusted_summary_fails_empty_core_content tests/test_cli_note.py::test_validate_trusted_summary_passes_ready_summary -q
```

Expected:

```text
Error: No such command 'validate-trusted-summary'
```

- [ ] **Step 3: Add validator in `note.py`**

Add to `src/zotero_paperread/note.py`:

```python
WRITE_READY_REVIEW_STATUSES = {"passed", "passed_with_caveats"}
REQUIRED_WRITE_READY_TEXT_FIELDS = {
    "one_sentence_summary": "one_sentence_summary is required",
    "abstract_translation": "abstract_translation is required",
    "research_question": "research_question is required",
    "method": "method is required",
    "experiments": "experiments is required",
    "ai4s_relevance": "ai4s_relevance is required",
}
REQUIRED_WRITE_READY_LIST_FIELDS = {
    "key_points": "key_points must contain at least one item",
    "contributions": "contributions must contain at least one item",
    "limitations": "limitations must contain at least one item",
    "follow_up_keywords": "follow_up_keywords must contain at least one item",
}


def validate_trusted_summary(summary: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    paper_type = safe_choice(summary.get("paper_type"), VALID_PAPER_TYPES, "unknown")
    if paper_type == "unknown":
        errors.append("paper_type must be a known paper type")

    trust_status = safe_choice(summary.get("trust_status"), VALID_TRUST_STATUSES, "needs_manual_review")
    if trust_status in {"metadata_only", "needs_manual_review"}:
        errors.append("trust_status is not write-ready")

    review_status = safe_choice(summary.get("review_status"), VALID_REVIEW_STATUSES, "not_reviewed")
    if review_status not in WRITE_READY_REVIEW_STATUSES:
        errors.append("review_status must be passed or passed_with_caveats")

    if not str(summary.get("trust_rationale", "")).strip():
        errors.append("trust_rationale is required")

    for field_name, error_message in REQUIRED_WRITE_READY_TEXT_FIELDS.items():
        value = flatten_inline_markdown_text(str(summary.get(field_name, "")))
        if not value:
            errors.append(error_message)

    for field_name, error_message in REQUIRED_WRITE_READY_LIST_FIELDS.items():
        if not clean_string_list(summary.get(field_name, [])):
            errors.append(error_message)

    evidence = clean_evidence_summary(summary)
    if not evidence:
        errors.append("evidence_summary must contain at least one claim")
    for index, item in enumerate(evidence, start=1):
        if not item["evidence"]:
            errors.append(f"evidence_summary[{index}] must include at least one evidence locator")

    improvement_status = safe_choice(
        summary.get("improvement_status"),
        VALID_IMPROVEMENT_STATUSES,
        "needed",
    )
    if improvement_status in {"needed", "blocked"}:
        errors.append("improvement_status must not be needed or blocked for write-through")

    return errors
```

- [ ] **Step 4: Add CLI command**

In `src/zotero_paperread/cli.py`, import:

```python
from zotero_paperread.note import render_note, validate_note, validate_trusted_summary
```

Add command after `validate-summary-json`:

```python
@app.command("validate-trusted-summary")
def validate_trusted_summary_command(summary_json: Path) -> None:
    """Validate semantic write-readiness fields in summary JSON."""
    errors = validate_trusted_summary(read_json_or_exit(summary_json, label="summary JSON"))
    if errors:
        for error in errors:
            console.print(f"trusted_summary_invalid: {error}")
        raise typer.Exit(1)
    console.print("trusted_summary_valid")
```

- [ ] **Step 5: Run tests and verify pass**

Run:

```bash
uv run pytest tests/test_cli_note.py -q
```

Expected:

```text
all tests pass
```

- [ ] **Step 6: Commit**

```bash
git add src/zotero_paperread/note.py src/zotero_paperread/cli.py tests/test_cli_note.py
git commit -m "feat: validate trusted summary write readiness"
```

---

### Task 4: Add Deterministic Review Merge Gate

**Files:**
- Create: `src/zotero_paperread/review.py`
- Create: `tests/test_review.py`
- Modify: `src/zotero_paperread/cli.py`
- Modify: `README.md`
- Modify: `skills/zotero-paper-summary/SKILL.md`

- [ ] **Step 1: Write failing review merge tests**

Create `tests/test_review.py`:

```python
from zotero_paperread.review import apply_review_to_summary, review_allows_write


def test_apply_review_to_summary_copies_gate_fields() -> None:
    summary = {"one_sentence_summary": "ok", "review_status": "not_reviewed"}
    review = {
        "review_status": "passed_with_caveats",
        "review_issues": [{"severity": "low", "issue": "minor", "suggested_fix": "none"}],
        "trust_status_recommendation": "usable_with_caveats",
        "needs_improvement": False,
        "improvement_requests": [],
    }

    updated = apply_review_to_summary(summary, review)

    assert updated["review_status"] == "passed_with_caveats"
    assert updated["review_issues"] == review["review_issues"]
    assert updated["trust_status"] == "usable_with_caveats"
    assert updated["improvement_status"] == "not_needed"


def test_apply_review_to_summary_clears_stale_improvement_state() -> None:
    summary = {
        "one_sentence_summary": "ok",
        "review_status": "failed",
        "improvement_status": "needed",
        "improvement_notes": [{"issue": "Old issue", "action": "", "source": "previous review"}],
    }
    review = {
        "review_status": "passed",
        "review_issues": [],
        "trust_status_recommendation": "trusted",
        "needs_improvement": False,
        "improvement_requests": [],
    }

    updated = apply_review_to_summary(summary, review)

    assert updated["review_status"] == "passed"
    assert updated["improvement_status"] == "not_needed"
    assert updated["improvement_notes"] == []


def test_apply_review_to_summary_marks_needed_improvement() -> None:
    summary = {"one_sentence_summary": "ok"}
    review = {
        "review_status": "failed",
        "review_issues": [{"severity": "high", "issue": "missing evidence"}],
        "trust_status_recommendation": "needs_manual_review",
        "needs_improvement": True,
        "improvement_requests": ["Add evidence locators."],
    }

    updated = apply_review_to_summary(summary, review)

    assert updated["review_status"] == "failed"
    assert updated["trust_status"] == "needs_manual_review"
    assert updated["improvement_status"] == "needed"
    assert updated["improvement_notes"] == [
        {
            "issue": "Add evidence locators.",
            "action": "",
            "source": "review.json",
        }
    ]


def test_review_allows_write() -> None:
    assert review_allows_write({"review_status": "passed", "needs_improvement": False}) is True
    assert review_allows_write({"review_status": "passed_with_caveats", "needs_improvement": False}) is True
    assert review_allows_write({"review_status": "failed", "needs_improvement": False}) is False
    assert review_allows_write({"review_status": "passed", "needs_improvement": True}) is False
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_review.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'zotero_paperread.review'
```

- [ ] **Step 3: Implement review helpers**

Create `src/zotero_paperread/review.py`:

```python
from __future__ import annotations

from copy import deepcopy
from typing import Any

WRITE_READY_REVIEW_STATUSES = {"passed", "passed_with_caveats"}


def _clean_review_issues(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    issues: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue", "")).strip()
        if not issue:
            continue
        issues.append(
            {
                "severity": str(item.get("severity", "")).strip() or "medium",
                "issue": issue,
                "suggested_fix": str(item.get("suggested_fix", "")).strip(),
            }
        )
    return issues


def apply_review_to_summary(summary: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(summary)
    review_status = str(review.get("review_status", "not_reviewed")).strip() or "not_reviewed"
    needs_improvement = bool(review.get("needs_improvement", False))
    trust_recommendation = str(review.get("trust_status_recommendation", "")).strip()

    updated["review_status"] = review_status
    updated["review_issues"] = _clean_review_issues(review.get("review_issues", []))
    if trust_recommendation:
        updated["trust_status"] = trust_recommendation

    if needs_improvement:
        updated["improvement_status"] = "needed"
        requests = review.get("improvement_requests", [])
        if not isinstance(requests, list):
            requests = [str(requests)]
        updated["improvement_notes"] = [
            {"issue": str(request).strip(), "action": "", "source": "review.json"}
            for request in requests
            if str(request).strip()
        ]
    elif updated.get("improvement_status") == "completed":
        updated["improvement_status"] = "completed"
        updated["improvement_notes"] = updated.get("improvement_notes", [])
    else:
        updated["improvement_status"] = "not_needed"
        updated["improvement_notes"] = []

    return updated


def review_allows_write(review: dict[str, Any]) -> bool:
    return (
        str(review.get("review_status", "")).strip() in WRITE_READY_REVIEW_STATUSES
        and bool(review.get("needs_improvement", False)) is False
    )
```

- [ ] **Step 4: Add CLI command `apply-review`**

In `src/zotero_paperread/cli.py`, import:

```python
from zotero_paperread.review import apply_review_to_summary
```

Add command:

```python
@app.command("apply-review")
def apply_review_command(
    summary_json: Path,
    review_json: Path,
    output: Path | None = typer.Option(None, "--output", "-o", help="Write updated summary JSON."),
) -> None:
    """Apply review gate fields to summary JSON deterministically."""
    summary = read_json_or_exit(summary_json, label="summary JSON")
    review = read_json_or_exit(review_json, label="review JSON")
    updated = apply_review_to_summary(summary, review)
    target = output or summary_json
    target.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    console.print(f"Wrote reviewed summary JSON: {target}")
```

- [ ] **Step 5: Add CLI smoke test**

Append to `tests/test_cli_note.py`:

```python
def test_apply_review_command_updates_summary(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    review_path = tmp_path / "review.json"
    write_json(summary_path, {"one_sentence_summary": "ok", "review_status": "not_reviewed"})
    write_json(
        review_path,
        {
            "review_status": "passed",
            "review_issues": [],
            "trust_status_recommendation": "trusted",
            "needs_improvement": False,
            "improvement_requests": [],
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["apply-review", str(summary_path), str(review_path)])

    assert result.exit_code == 0
    updated = json.loads(summary_path.read_text(encoding="utf-8"))
    assert updated["review_status"] == "passed"
    assert updated["trust_status"] == "trusted"
    assert updated["improvement_status"] == "not_needed"
    assert updated["improvement_notes"] == []
```

- [ ] **Step 6: Update workflow docs**

In `skills/zotero-paper-summary/SKILL.md`, after generating `review.json`, require:

```bash
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
PAPER_TITLE="<paper title>"
GENERATED_DATE="<YYYY-MM-DD>"
VERSION_SUFFIX="$(uv run zotero-paperread next-version-suffix <run_dir>/item-details.json --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE")"
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --generated-date "$GENERATED_DATE" --version-suffix "$VERSION_SUFFIX" --output <run_dir>/note.md
```

In `README.md`, add the same sequence under Trusted Notes.

- [ ] **Step 7: Verify**

Run:

```bash
uv run pytest tests/test_review.py tests/test_cli_note.py -q
uv run zotero-paperread --help
```

Expected:

```text
all tests pass
```

- [ ] **Step 8: Commit**

```bash
git add src/zotero_paperread/review.py src/zotero_paperread/cli.py tests/test_review.py tests/test_cli_note.py README.md skills/zotero-paper-summary/SKILL.md
git commit -m "feat: apply review gates before zotero write"
```

---

### Task 5: Add Figure Crop Visual Quality Gate

**Files:**
- Modify: `src/zotero_paperread/figures.py`
- Modify: `tests/test_figures.py`
- Modify: `src/zotero_paperread/workflow.py`

- [ ] **Step 1: Add tests for tiny, text-only, and plot-like crop quality**

Append to `tests/test_figures.py`:

```python
from zotero_paperread.figures import assess_image_quality


def test_assess_image_quality_flags_tiny_image(tmp_path: Path) -> None:
    image_path = tmp_path / "tiny.png"
    doc = fitz.open()
    page = doc.new_page(width=40, height=20)
    page.draw_rect(fitz.Rect(0, 0, 40, 20), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
    pix = page.get_pixmap()
    pix.save(image_path)
    doc.close()

    quality = assess_image_quality(image_path)

    assert quality["status"] == "poor"
    assert "image_too_small" in quality["warnings"]


def test_assess_image_quality_flags_normal_size_text_only_header(tmp_path: Path) -> None:
    image_path = tmp_path / "article-header.png"
    doc = fitz.open()
    page = doc.new_page(width=300, height=180)
    page.insert_text((24, 48), "Article", fontsize=24)
    pix = page.get_pixmap()
    pix.save(image_path)
    doc.close()

    quality = assess_image_quality(image_path)

    assert quality["status"] == "poor"
    assert "image_content_area_too_sparse" in quality["warnings"]


def test_assess_image_quality_flags_formula_strip(tmp_path: Path) -> None:
    image_path = tmp_path / "formula-strip.png"
    doc = fitz.open()
    page = doc.new_page(width=300, height=180)
    page.insert_textbox(
        fitz.Rect(24, 82, 276, 108),
        "E = E_0 - q V + O(V^2)",
        fontsize=14,
    )
    pix = page.get_pixmap()
    pix.save(image_path)
    doc.close()

    quality = assess_image_quality(image_path)

    assert quality["status"] == "poor"
    assert "image_content_area_too_sparse" in quality["warnings"]


def test_assess_image_quality_accepts_normal_plot_like_image(tmp_path: Path) -> None:
    image_path = tmp_path / "plot.png"
    doc = fitz.open()
    page = doc.new_page(width=300, height=180)
    page.draw_rect(fitz.Rect(20, 20, 280, 160), color=(0, 0, 0), fill=(0.96, 0.96, 0.96))
    page.draw_line(fitz.Point(40, 140), fitz.Point(260, 50), color=(0.1, 0.2, 0.8), width=2)
    page.draw_line(fitz.Point(40, 120), fitz.Point(260, 100), color=(0.8, 0.2, 0.1), width=2)
    pix = page.get_pixmap()
    pix.save(image_path)
    doc.close()

    quality = assess_image_quality(image_path)

    assert quality["status"] == "ok"
    assert quality["warnings"] == []
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_figures.py::test_assess_image_quality_flags_tiny_image tests/test_figures.py::test_assess_image_quality_flags_normal_size_text_only_header tests/test_figures.py::test_assess_image_quality_flags_formula_strip tests/test_figures.py::test_assess_image_quality_accepts_normal_plot_like_image -q
```

Expected:

```text
ImportError: cannot import name 'assess_image_quality'
```

- [ ] **Step 3: Implement quality helper**

Add to `src/zotero_paperread/figures.py`:

```python
def _content_pixel_bbox(pixmap: fitz.Pixmap) -> tuple[int, int, int, int, int]:
    width = int(pixmap.width)
    height = int(pixmap.height)
    component_count = int(pixmap.n)
    samples = bytes(pixmap.samples)
    min_x = width
    min_y = height
    max_x = -1
    max_y = -1
    content_pixels = 0

    if component_count <= 0:
        return 0, 0, 0, 0, 0

    for pixel_index in range(width * height):
        offset = pixel_index * component_count
        if offset + 2 >= len(samples):
            break
        r, g, b = samples[offset], samples[offset + 1], samples[offset + 2]
        is_content = min(r, g, b) < 245 or (max(r, g, b) - min(r, g, b)) > 15
        if not is_content:
            continue
        x = pixel_index % width
        y = pixel_index // width
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)
        content_pixels += 1

    if content_pixels == 0:
        return 0, 0, 0, 0, 0
    return min_x, min_y, max_x, max_y, content_pixels


def assess_image_quality(image_path: Path) -> dict[str, Any]:
    path = Path(image_path)
    warnings: list[str] = []
    try:
        pixmap = fitz.Pixmap(str(path))
    except Exception:
        return {"status": "poor", "warnings": ["image_unreadable"], "width": 0, "height": 0}

    width = int(pixmap.width)
    height = int(pixmap.height)
    if width < 120 or height < 80:
        warnings.append("image_too_small")

    min_x, min_y, max_x, max_y, content_pixels = _content_pixel_bbox(pixmap)
    total_pixels = max(width * height, 1)
    content_ratio = content_pixels / total_pixels
    if content_pixels:
        bbox_area = (max_x - min_x + 1) * (max_y - min_y + 1)
        content_bbox_area_ratio = bbox_area / total_pixels
    else:
        content_bbox_area_ratio = 0.0

    if content_ratio < 0.002:
        warnings.append("image_low_information")
    if content_bbox_area_ratio < 0.12 and content_ratio < 0.04:
        warnings.append("image_content_area_too_sparse")

    return {
        "status": "poor" if warnings else "ok",
        "warnings": warnings,
        "width": width,
        "height": height,
        "content_ratio": round(content_ratio, 6),
        "content_bbox_area_ratio": round(content_bbox_area_ratio, 6),
    }
```

- [ ] **Step 4: Attach quality to selected figures**

In `extract_figures()`, after selecting figures:

```python
    selected = ranking_pool[: max(top_k, 0)]
    for item in selected:
        quality = assess_image_quality(Path(item["image_path"]))
        item["visual_quality"] = quality
        if quality["warnings"]:
            item["needs_fallback"] = True
            item["fallback_reason"] = item["fallback_reason"] or "visual_quality"
            warnings.extend(f"figure_visual_quality:{item['figure_id']}:{warning}" for warning in quality["warnings"])
```

Then deduplicate warnings before return:

```python
    warnings = list(dict.fromkeys(warnings))
```

- [ ] **Step 5: Surface visual quality in figure context**

In `src/zotero_paperread/workflow.py`, inside `build_figure_context_markdown()`, add:

```python
                        f"- Visual Quality: {json.dumps(figure.get('visual_quality', {}), ensure_ascii=False, sort_keys=True)}",
```

right after `Needs Fallback`.

- [ ] **Step 6: Verify figure tests**

Run:

```bash
uv run pytest tests/test_figures.py tests/test_workflow.py -q
```

Expected:

```text
all tests pass
```

- [ ] **Step 7: Commit**

```bash
git add src/zotero_paperread/figures.py src/zotero_paperread/workflow.py tests/test_figures.py
git commit -m "feat: flag poor figure crops before analysis"
```

---

### Task 6: Lock Template Bullet Formatting

**Files:**
- Modify: `templates/zotero_note.md.j2`
- Modify: `tests/test_note.py`

- [ ] **Step 1: Add regression test for review bullet separation**

Append to `tests/test_note.py`:

```python
def test_render_note_separates_review_issue_bullets() -> None:
    note = render_note(
        METADATA,
        {
            **SUMMARY_WITH_FIGURES,
            **TRUSTED_FIELDS,
            "review_issues": [
                {"severity": "medium", "issue": "First issue.", "suggested_fix": "Fix first."},
                {"severity": "low", "issue": "Second issue.", "suggested_fix": "Fix second."},
            ],
            "improvement_notes": [
                {"issue": "First improvement.", "action": "Done.", "source": "review.json"},
                {"issue": "Second improvement.", "action": "Done.", "source": "review.json"},
            ],
        },
        generated_date="2026-04-26",
    )

    assert "- medium: First issue. 建议: Fix first.\n\n- low: Second issue." in note
    assert "- First improvement.: Done. (source: review.json)\n\n- Second improvement." in note
```

- [ ] **Step 2: Run test**

Run:

```bash
uv run pytest tests/test_note.py::test_render_note_separates_review_issue_bullets -q
```

Expected:

```text
PASS
```

If it fails, apply the existing local template fix:

```jinja2
- {{ item.severity }}: {{ item.issue }}{% if item.suggested_fix %} 建议: {{ item.suggested_fix }}{% endif %}{{ "\n" }}
```

and:

```jinja2
- {{ item.issue }}{% if item.action %}: {{ item.action }}{% endif %}{% if item.source %} (source: {{ item.source }}){% endif %}{{ "\n" }}
```

- [ ] **Step 3: Verify note tests**

Run:

```bash
uv run pytest tests/test_note.py -q
```

Expected:

```text
all tests pass
```

- [ ] **Step 4: Commit**

```bash
git add templates/zotero_note.md.j2 tests/test_note.py
git commit -m "fix: preserve review bullets in rendered notes"
```

---

### Task 7: Update Write-Through Runbook and End-to-End Verification

**Files:**
- Modify: `README.md`
- Modify: `skills/zotero-paper-summary/SKILL.md`
- Optional test fixture update: `tests/test_end_to_end_dry_run.py`

- [ ] **Step 1: Define the final write-through gate order**

In both `README.md` and `skills/zotero-paper-summary/SKILL.md`, document this order:

```bash
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
PAPER_TITLE="<paper title>"
GENERATED_DATE="<YYYY-MM-DD>"
VERSION_SUFFIX="$(uv run zotero-paperread next-version-suffix <run_dir>/item-details.json --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE")"
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --generated-date "$GENERATED_DATE" --version-suffix "$VERSION_SUFFIX" --output <run_dir>/note.md
uv run zotero-paperread preview-note <run_dir>/note.md
```

Then write to Zotero only if:

```text
review_status is passed or passed_with_caveats
needs_improvement is false
validate-trusted-summary passes
same-day version suffix has been computed from current item-details.json
preview-note has been shown
target Zotero item title has been shown
```

- [ ] **Step 2: Add a "Known MCP behavior" note**

Add:

```markdown
`get_item_details` is available in `cookjohn/zotero-mcp` 1.4.7. If Codex does not show it initially, run a targeted tool search before assuming the MCP server lacks the tool.
```

- [ ] **Step 3: Verify project commands**

Run:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

Expected:

```text
pytest: all tests pass
help: command list includes apply-review, validate-trusted-summary, and next-version-suffix
extract-pdf: Wrote extraction JSON: /tmp/zotero-paperread-extract.json
```

- [ ] **Step 4: Commit**

```bash
git add README.md skills/zotero-paper-summary/SKILL.md tests/test_end_to_end_dry_run.py
git commit -m "docs: define strict zotero write-through gate"
```

---

## Self-Review

**Spec coverage:** This plan covers branch/worktree readiness, MCP discovery, item detail retrieval, existing note/versioning, write intent, review merge, strict summary validation, figure quality, template formatting, and final write-through verification.

**Placeholder scan:** No task relies on "TBD" or vague "add validation" wording; each code task names exact files, tests, implementation snippets, and commands.

**Type consistency:** Functions introduced here are consistently named:
- `codex_summary_titles_from_details`
- `next_version_suffix_from_details`
- `next_version_suffix_command`
- `primary_pdf_path_from_details`
- `validate_trusted_summary`
- `apply_review_to_summary`
- `review_allows_write`
- `assess_image_quality`

**Important caveat:** This plan does not patch the Zotero MCP plugin. The plugin already exposes `get_item_details`; the fix is to make Codex workflow load and use it reliably.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-26-zotero-workflow-hardening.md`.

Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh worker per task, review between tasks, faster isolation.
2. **Inline Execution** - execute tasks in this session using executing-plans, with checkpoints after each task.
