# Trusted Notes V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add trust status, evidence summaries, a quality-review gate, and one bounded source-grounded improvement pass to the existing Zotero-first paper summary workflow.

**Architecture:** Keep deterministic rendering in `zotero_paperread` and keep semantic review in the Codex skill. Python will render and validate trusted-note fields with safe legacy defaults. The Codex skill will generate `summary.json`, produce `review.json`, optionally perform one improvement pass from existing run artifacts, then preview and write to Zotero only when the review gate allows it.

**Tech Stack:** Python 3.13, `uv`, Typer, Jinja2, pytest, Codex skill orchestration, Zotero MCP.

---

## Source Spec

Implement from:

`/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/superpowers/specs/2026-04-25-trusted-notes-v2-design.md`

This plan covers Phase 1 and Phase 2 from that spec:

- Phase 1: trust and evidence minimum layer
- Phase 2: note quality review layer with one bounded improvement pass

It does not implement section-level summarization, Better Notes integration, graph relations, or a deterministic Python `review-note` command.

---

## File Structure

- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/note.py`: add trusted-note defaults, validation, and render context helpers.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/templates/zotero_note.md.j2`: render `## 可信度与证据` near the top of the note.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_note.py`: test trust section rendering, legacy defaults, and validation.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_note.py`: test `finalize-note` with trusted-note fields.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`: require trusted-note fields, quality review, one improvement pass, and write-through gating.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`: document trusted-note quality workflow.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/superpowers/specs/2026-04-25-trusted-notes-v2-design.md`: keep the spec aligned if implementation clarifies wording.

---

### Task 1: Render Trusted Note Fields

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_note.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/note.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/templates/zotero_note.md.j2`

- [ ] **Step 1: Add failing tests for trust section rendering**

Use `apply_patch` to update `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_note.py` with:

```python
TRUSTED_FIELDS = {
    "paper_type": "research_article",
    "trust_status": "trusted",
    "trust_rationale": "正文和关键图支持主要方法与实验结论。",
    "review_status": "passed_with_caveats",
    "evidence_summary": [
        {
            "claim": "The method uses a learned inverse-design model.",
            "evidence": [
                {
                    "type": "text",
                    "locator": "page 3 method section",
                    "summary": "The method section describes the learned mapping from target response to structure parameters.",
                },
                {
                    "type": "figure",
                    "locator": "fig_p1_1",
                    "summary": "The framework figure shows the optimization loop.",
                },
            ],
            "confidence": "high",
        }
    ],
    "review_issues": [
        {
            "severity": "low",
            "issue": "Figure evidence is available but page evidence is brief.",
            "suggested_fix": "Keep caveat in trust rationale.",
        }
    ],
    "improvement_status": "completed",
    "improvement_notes": [
        {
            "issue": "Method section was too generic.",
            "action": "Added page-grounded method detail.",
            "source": "context.md",
        }
    ],
}


def test_render_note_contains_trust_and_evidence_section() -> None:
    note = render_note(METADATA, {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS}, generated_date="2026-04-23")

    assert "## 可信度与证据" in note
    assert "- **论文类型**: research_article" in note
    assert "- **可信状态**: trusted" in note
    assert "- **审查状态**: passed_with_caveats" in note
    assert "- **改进状态**: completed" in note
    assert "The method uses a learned inverse-design model." in note
    assert "page 3 method section" in note
    assert "fig_p1_1" in note
    assert "Method section was too generic." in note
```

- [ ] **Step 2: Run the new test and confirm it fails**

Run:

```bash
uv run pytest tests/test_note.py::test_render_note_contains_trust_and_evidence_section -q
```

Expected: FAIL because the template does not yet render `## 可信度与证据`.

- [ ] **Step 3: Implement trusted-note render helpers**

Use `apply_patch` to update `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/note.py`:

```python
VALID_PAPER_TYPES = {
    "research_article",
    "review",
    "perspective",
    "benchmark",
    "method_paper",
    "dataset_paper",
    "theory_paper",
    "unknown",
}
VALID_TRUST_STATUSES = {"trusted", "usable_with_caveats", "metadata_only", "needs_manual_review"}
VALID_REVIEW_STATUSES = {"not_reviewed", "passed", "passed_with_caveats", "failed"}
VALID_IMPROVEMENT_STATUSES = {"not_needed", "needed", "completed", "blocked"}


def safe_choice(value: Any, allowed: set[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def clean_evidence_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    items = summary.get("evidence_summary", [])
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim", "")).strip()
        if not claim:
            continue
        evidence_items = item.get("evidence", [])
        if not isinstance(evidence_items, list):
            evidence_items = []
        cleaned_evidence = []
        for evidence in evidence_items[:3]:
            if not isinstance(evidence, dict):
                continue
            locator = str(evidence.get("locator", "")).strip()
            evidence_summary = str(evidence.get("summary", "")).strip()
            evidence_type = str(evidence.get("type", "")).strip() or "text"
            if locator or evidence_summary:
                cleaned_evidence.append(
                    {
                        "type": evidence_type,
                        "locator": locator,
                        "summary": evidence_summary,
                    }
                )
        cleaned.append(
            {
                "claim": claim,
                "evidence": cleaned_evidence,
                "confidence": str(item.get("confidence", "")).strip() or "unknown",
            }
        )
    return cleaned


def clean_issue_list(summary: dict[str, Any]) -> list[dict[str, str]]:
    items = summary.get("review_issues", [])
    if not isinstance(items, list):
        return []
    cleaned = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue", "")).strip()
        if not issue:
            continue
        cleaned.append(
            {
                "severity": str(item.get("severity", "")).strip() or "medium",
                "issue": issue,
                "suggested_fix": str(item.get("suggested_fix", "")).strip(),
            }
        )
    return cleaned


def clean_improvement_notes(summary: dict[str, Any]) -> list[dict[str, str]]:
    items = summary.get("improvement_notes", [])
    if not isinstance(items, list):
        return []
    cleaned = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue", "")).strip()
        action = str(item.get("action", "")).strip()
        if not issue and not action:
            continue
        cleaned.append(
            {
                "issue": issue,
                "action": action,
                "source": str(item.get("source", "")).strip(),
            }
        )
    return cleaned
```

Then add these fields to the render context in `render_note()`:

```python
"paper_type": safe_choice(summary.get("paper_type"), VALID_PAPER_TYPES, "unknown"),
"trust_status": safe_choice(summary.get("trust_status"), VALID_TRUST_STATUSES, "usable_with_caveats"),
"trust_rationale": summary.get("trust_rationale", "") or "未提供可信度判断依据。",
"review_status": safe_choice(summary.get("review_status"), VALID_REVIEW_STATUSES, "not_reviewed"),
"review_issues": clean_issue_list(summary),
"evidence_summary": clean_evidence_summary(summary),
"improvement_status": safe_choice(summary.get("improvement_status"), VALID_IMPROVEMENT_STATUSES, "not_needed"),
"improvement_notes": clean_improvement_notes(summary),
```

Also add `"可信度与证据"` to `REQUIRED_SECTIONS`.

- [ ] **Step 4: Render the trust section in the template**

Use `apply_patch` to insert this section in `/Users/jwxi/Desktop/AIflow/Zotero_paperread/templates/zotero_note.md.j2` after `## 元数据` and before `## 核心结论`:

```jinja
## 可信度与证据

- **论文类型**: {{ paper_type }}
- **可信状态**: {{ trust_status }}
- **审查状态**: {{ review_status }}
- **改进状态**: {{ improvement_status }}
- **判断依据**: {{ trust_rationale }}

### 关键证据

{% if evidence_summary -%}
{% for item in evidence_summary -%}
- 结论: {{ item.claim }}
{% for evidence in item.evidence -%}
  - 证据: {{ evidence.locator }}{% if evidence.summary %}; {{ evidence.summary }}{% endif %}
{% endfor %}
{% endfor %}
{% else -%}
- none
{% endif %}

{% if review_issues -%}
### 审查问题

{% for item in review_issues -%}
- {{ item.severity }}: {{ item.issue }}{% if item.suggested_fix %} 建议: {{ item.suggested_fix }}{% endif %}
{% endfor %}
{% endif %}

{% if improvement_notes -%}
### 补充优化记录

{% for item in improvement_notes -%}
- {{ item.issue }}{% if item.action %}: {{ item.action }}{% endif %}{% if item.source %} (source: {{ item.source }}){% endif %}
{% endfor %}
{% endif %}
```

- [ ] **Step 5: Run the note tests**

Run:

```bash
uv run pytest tests/test_note.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

Run:

```bash
git add src/zotero_paperread/note.py templates/zotero_note.md.j2 tests/test_note.py
git commit -m "feat: render trust and evidence note section"
```

Expected: one local commit.

---

### Task 2: Keep CLI Finalization Compatible

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_note.py`

- [ ] **Step 1: Add a CLI test for trusted-note summaries**

Use `apply_patch` to add this test to `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_note.py`:

```python
def test_finalize_note_command_accepts_trusted_note_fields(tmp_path: Path) -> None:
    metadata_path = tmp_path / "metadata.json"
    summary_path = tmp_path / "summary.json"
    output_path = tmp_path / "note.md"
    write_json(metadata_path, {"key": "ABC123", "title": "Paper", "creators": "A", "date": "2026"})
    write_json(
        summary_path,
        {
            "one_sentence_summary": "一句话总结。",
            "abstract_translation": "摘要翻译。",
            "key_points": ["要点"],
            "research_question": "问题",
            "method": "方法",
            "figure_overview": "关键图片概览。",
            "key_figures": [],
            "experiments": "实验",
            "contributions": ["贡献"],
            "limitations": ["局限"],
            "ai4s_relevance": "启发",
            "follow_up_keywords": ["keyword"],
            "note_labels": ["deep_learning"],
            "quality_score": "8/10",
            "extraction_warnings": [],
            "paper_type": "research_article",
            "trust_status": "trusted",
            "trust_rationale": "证据充分。",
            "review_status": "passed",
            "evidence_summary": [
                {
                    "claim": "The method is supported by the method section.",
                    "evidence": [{"type": "text", "locator": "page 3", "summary": "method evidence"}],
                    "confidence": "high",
                }
            ],
            "review_issues": [],
            "improvement_status": "not_needed",
            "improvement_notes": [],
        },
    )
    runner = CliRunner()

    result = runner.invoke(app, ["finalize-note", str(metadata_path), str(summary_path), "--output", str(output_path)])

    assert result.exit_code == 0
    note = output_path.read_text(encoding="utf-8")
    assert "## 可信度与证据" in note
    assert "note_valid" in result.stdout
```

- [ ] **Step 2: Run the CLI note test**

Run:

```bash
uv run pytest tests/test_cli_note.py::test_finalize_note_command_accepts_trusted_note_fields -q
```

Expected: PASS after Task 1.

- [ ] **Step 3: Run all CLI note tests**

Run:

```bash
uv run pytest tests/test_cli_note.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit Task 2**

Run:

```bash
git add tests/test_cli_note.py
git commit -m "test: cover trusted note finalization"
```

Expected: one test-only commit.

---

### Task 3: Update Skill Workflow for Review and Improvement

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`

- [ ] **Step 1: Update the summary JSON contract in the skill**

Use `apply_patch` to update `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md` so the summary JSON example includes:

```json
"paper_type": "research_article",
"trust_status": "usable_with_caveats",
"evidence_summary": [
  {
    "claim": "",
    "evidence": [
      {
        "type": "text",
        "locator": "page 1",
        "summary": ""
      }
    ],
    "confidence": "high"
  }
],
"trust_rationale": "",
"review_status": "not_reviewed",
"review_issues": [],
"improvement_status": "not_needed",
"improvement_notes": []
```

- [ ] **Step 2: Add review instructions to the skill**

Use `apply_patch` to add a new workflow step after the first `finalize-note` step:

```markdown
9. 二次质量审查：
   - 阅读 `<run_dir>/context.md`、`<run_dir>/figure_context.md`、`<run_dir>/summary.json` 和 `<run_dir>/note.md`。
   - 生成 `<run_dir>/review.json`。
   - 审查必须检查：
     - 主要结论是否有 page 或 figure 证据
     - 局限是否具体而不是泛泛而谈
     - 论文类型是否合理
     - 是否把背景知识写成本论文贡献
     - 图分析是否来自真实 `figure_context.md`
     - 是否因抽取告警需要降级可信状态
```

- [ ] **Step 3: Add one-pass improvement instructions to the skill**

Use `apply_patch` to add:

```markdown
10. 补充优化：
   - 如果 `review.json` 中 `needs_improvement` 为 true，允许一次补充优化。
   - 只允许重读当前 run 目录中的 `context.md`、`figure_context.md`、`extract.json`、`figures.json`。
   - 可以更新 `summary.json` 中的方法、局限、证据、可信状态、审查状态和 `improvement_notes`。
   - 不允许使用外部知识补证据。
   - 补充后必须重新运行 `finalize-note`，再做一次质量审查。
   - 自动补充最多一次。
```

- [ ] **Step 4: Add write-through gate instructions**

Use `apply_patch` to update the write step:

```markdown
- 写入 Zotero 前必须满足：
  - `review_status` 为 `passed` 或 `passed_with_caveats`
  - 没有待处理的 `needs_improvement`
  - 已完成 `preview-note`
- 如果 `review_status` 为 `failed`，停止并报告审查问题，不写入 Zotero。
```

- [ ] **Step 5: Update README trusted-note section**

Use `apply_patch` to add a short section to `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`:

```markdown
## Trusted Notes

The workflow now asks Codex to classify paper type, assign trust status, attach compact evidence pointers, and run a second-pass note quality review before Zotero write-through. If review finds fixable omissions, Codex may perform one bounded improvement pass by re-reading only the current run directory artifacts.
```

- [ ] **Step 6: Verify docs mention the review and improvement gate**

Run:

```bash
rg -n "二次质量审查|补充优化|review.json|needs_improvement|Trusted Notes|可信度与证据" skills/zotero-paper-summary/SKILL.md README.md
```

Expected: matching lines in both files.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add skills/zotero-paper-summary/SKILL.md README.md
git commit -m "docs: define trusted note review workflow"
```

Expected: one docs commit.

---

### Task 4: Verification Gate

**Files:**
- Read: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/AGENTS.md`
- Read: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/superpowers/specs/2026-04-25-trusted-notes-v2-design.md`
- Read: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/superpowers/plans/2026-04-25-trusted-notes-v2.md`

- [ ] **Step 1: Run the full test suite**

Run:

```bash
uv run pytest
```

Expected: all tests pass.

- [ ] **Step 2: Run CLI help**

Run:

```bash
uv run zotero-paperread --help
```

Expected: command list prints successfully.

- [ ] **Step 3: Run the minimal PDF extraction check**

Run:

```bash
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

Expected: writes `/tmp/zotero-paperread-extract.json`.

- [ ] **Step 4: Run spec and plan consistency checks**

Run:

```bash
rg -n "TO[D]O|TB[D]|implement late[r]|fill in detail[s]" docs/superpowers/specs/2026-04-25-trusted-notes-v2-design.md docs/superpowers/plans/2026-04-25-trusted-notes-v2.md
rg -n "paper_type|trust_status|evidence_summary|review_status|improvement_status|可信度与证据" docs/superpowers/specs/2026-04-25-trusted-notes-v2-design.md docs/superpowers/plans/2026-04-25-trusted-notes-v2.md skills/zotero-paper-summary/SKILL.md README.md
```

Expected:
- first command has no matches
- second command shows relevant references across spec, plan, skill, and README

- [ ] **Step 5: Commit any final fixes**

Run:

```bash
git status --short
```

Expected: no unexpected files. If final fixes were needed, commit them with a specific message.

Do not push unless the user explicitly asks.
