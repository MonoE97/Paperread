# Learning Note Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the Zotero paper summary note from a machine-traceable summary into a layered learning note that supports quick triage, study, comparison, reuse, and evidence review.

**Architecture:** Keep semantic extraction and research judgment in the Codex skill, then render it through deterministic Python normalization plus a Jinja2 Markdown template. The renderer must remain permissive for old `summary.json` files, while `validate_trusted_summary()` continues to enforce strict write-through gates.

**Tech Stack:** Python 3.13 via `uv`, Jinja2 with `StrictUndefined`, Typer CLI, pytest, Markdown rendered into Zotero child notes through Zotero MCP.

---

## Plan Review

The previous plan direction is technically sound but needed eight corrections before implementation.

1. **Do not add new `trust_status` values.**
   - Current code accepts only `trusted`, `usable_with_caveats`, `metadata_only`, and `needs_manual_review`.
   - The user examples include `needs_review`, `low_confidence`, and `failed`, but those would break current validators.
   - Implementation must keep existing `trust_status` values. Use `review_status=failed` for failed review states.

2. **Do not rely on Jinja `default()` for new-field safety.**
   - The renderer uses `StrictUndefined`, so every template variable must be present in `render_note()` context.
   - Missing new fields must be handled in `src/zotero_paperread/note.py`, not scattered through the template.

3. **Do not attempt brittle deterministic extraction for semantic learning fields.**
   - Fields such as `concept_cards`, `workflow_lessons`, `key_results_table`, and `reading_decision` require paper understanding.
   - Python should clean, normalize, and safely render these fields; `skills/zotero-paper-summary/SKILL.md` should instruct Codex to generate them.

4. **Do not drop old fields that disappear from the new outline.**
   - `abstract_translation` should render as `### 摘要翻译` under `## 1. 论文解决了什么问题？`.
   - `experiments` should render as `### 实验与证据摘要` under `## 3. 关键结果与数值`.
   - This preserves old summaries without adding top-level sections that conflict with the requested 0-12 structure.

5. **Fix evidence line formatting before changing the evidence template.**
   - Current `format_evidence_line()` returns a Markdown bullet prefix (`"  - 证据: ..."`).
   - The new Claim-Evidence appendix template renders `- {{ evidence.line }}`.
   - If the line formatter is not changed, the final note will contain malformed nested bullets such as `-   - 证据: ...`.

6. **Support `workflow_steps` as both string and list.**
   - The requested field contract allows `workflow_steps` to be a string or a list.
   - Python should normalize a list into a numbered Markdown workflow instead of silently skipping it.

7. **Prefer specific visual-quality warnings over generic `poor`.**
   - `figures.py` stores `visual_quality.status` as `poor` or `ok`, but specific warnings include `image_too_small`.
   - The figure index should show `image_too_small` when that warning is present, not collapse it to `poor`.

8. **Do not assume the implementation starts from a fully clean worktree.**
   - This plan file itself may be an untracked change when implementation begins.
   - Task 0 must treat the plan document as an expected pre-existing planning artifact, while still protecting unrelated user edits.

## File Structure

- Modify `src/zotero_paperread/note.py`
  - Owns required section names, enum compatibility, field cleaning, fallback generation, and render context.
- Modify `templates/zotero_note.md.j2`
  - Owns the final 0-12 note layout and Markdown/Jinja rendering.
- Modify `tests/test_note.py`
  - Owns note rendering tests for old summaries, new learning fields, evidence placement, and validation.
- Modify `tests/test_cli_note.py`
  - Owns CLI-level assertions that rendered and finalized notes use the new section names.
- Modify `skills/zotero-paper-summary/SKILL.md`
  - Owns the agent-facing `summary.json` generation contract and learning-note field instructions.
- Modify `README.md`
  - Owns user-facing documentation for the new rendered note structure and field compatibility.

## Implementation Constraints

- Keep `render_note()` permissive for old `summary.json` files.
- Keep `validate_trusted_summary()` strict for Zotero write-through.
- Do not write to Zotero during implementation or tests.
- Do not introduce new runtime dependencies.
- Use `uv run` for all commands.
- Do not run `git push`, `git rebase`, or destructive git commands.

---

### Task 0: Prepare Branch Context

**Files:**
- No file changes.

- [ ] **Step 1: Inspect branch and worktree**

Run:

```bash
git branch --show-current
git status --short
```

Expected:

```text
main
```

`git status --short` may include this plan document:

```text
?? docs/superpowers/plans/2026-04-27-learning-note-template.md
```

If unrelated user edits appear, leave them untouched and record them in the execution notes.

- [ ] **Step 2: Create a local feature branch when executing the implementation**

Run:

```bash
git switch -c codex/learning-note-template
```

Expected:

```text
Switched to a new branch 'codex/learning-note-template'
```

- [ ] **Step 3: Verify branch context**

Run:

```bash
git branch --show-current
```

Expected:

```text
codex/learning-note-template
```

---

### Task 1: Add Rendering Tests For The New Note Contract

**Files:**
- Modify `tests/test_note.py`

- [ ] **Step 1: Add a new learning-summary fixture**

Append this fixture near `SUMMARY_WITH_FIGURES` and `TRUSTED_FIELDS`:

```python
LEARNING_FIELDS = {
    "research_object": "Au(100)/NaCl(aq) electrochemical interface",
    "research_question_short": "如何加速 finite-field electrochemical interface simulations？",
    "core_method_short": "FIREANN 学外场相关原子力，MLEDR 学电子密度响应。",
    "core_result_short": "实现约 4 个数量级加速，并预测电容、极化和界面水取向。",
    "relevance_to_user": "对 AI4S、电池界面模拟和 learned observable workflow 有直接参考价值。",
    "reading_decision": "strongly_recommended",
    "main_risk_short": "Figure 2-4 crop 过小，图像细节不能独立复核。",
    "tldr": "本文把动力学采样模型和电子响应模型拆开训练，用于电化学界面长时间尺度采样。",
    "background_problem": "电化学界面需要同时描述电势、电解液极化、离子吸附和界面水取向。",
    "existing_gap": "finite-field AIMD 成本高，经典力场难以描述电子响应。",
    "paper_entry_point": "用外场相关机器学习力场和电子密度响应模型替代昂贵的 AIMD 采样。",
    "method_overview": "方法由 FIREANN 力场和 MLEDR 电子密度响应模型组成。",
    "method_modules": [
        {
            "name": "FIREANN",
            "input": "原子结构 + 外场",
            "target": "外场相关原子力",
            "output": "MLMD 力场",
            "role": "加速界面结构采样",
        },
        {
            "name": "MLEDR",
            "input": "原子结构 + 外场 + ghost atoms",
            "target": "电子密度响应",
            "output": "charge response field",
            "role": "计算表面电荷和 Helmholtz capacitance",
        },
    ],
    "workflow_steps": "1. 生成 AIMD 数据。\n2. 训练 FIREANN。\n3. 训练 MLEDR。\n4. 执行 MLMD。\n5. 积分得到电化学可观测量。",
    "technical_details": [
        "训练体系为 Au(100)/5.5 M NaCl(aq)。",
        "MLMD 使用 0.5 fs timestep。",
    ],
    "key_results_table": [
        {
            "result": "加速效果",
            "value": "约 4 个数量级",
            "meaning": "支持 ns 级界面采样",
        },
        {
            "result": "最大 Helmholtz capacitance",
            "value": "约 20.8 μF/cm²",
            "meaning": "0 V 附近出现最大电容",
        },
    ],
    "applicability_limits": [
        "适合研究需要外场、电势、界面极化和电子响应的电化学界面体系。",
        "不能直接推广到复杂电极、多组分电解液、真实 SEI 或反应性界面。",
    ],
    "transferable_insight": "把科学问题拆成动力学采样模型和可观测量响应模型。",
    "workflow_lessons": [
        "用 field-conditioned ML potential 学习外场下的结构动力学。",
        "用单独 response model 学习电子密度、电荷、极化或谱学响应。",
    ],
    "follow_up_questions": [
        "该 framework 能否迁移到电池 SEI / 电解液分解界面？",
        "MLEDR 是否可以替换为 charge density foundation model？",
    ],
    "concept_cards": [
        {
            "term": "finite-field molecular dynamics",
            "short_definition": "在周期体系中施加外电场的分子动力学方法。",
            "role_in_paper": "提供 constant-potential-like 全电池模拟框架。",
            "related_keywords": ["finite field", "electric field", "electrochemical interface"],
        },
        {
            "term": "MLEDR",
            "short_definition": "用机器学习预测电子密度响应的模型。",
            "role_in_paper": "从结构和外场预测 charge response。",
            "related_keywords": ["electron density response", "charge response", "learned observable"],
        },
    ],
}
```

- [ ] **Step 2: Extend `SUMMARY_WITH_FIGURES` for figure index fields**

Change the first `key_figures` item to include the new optional fields:

```python
        {
            "figure_id": "fig_p1_1",
            "title_short": "Overall pipeline",
            "caption": "Figure 1. Overall pipeline.",
            "page": 1,
            "priority_score": 5.2,
            "why_it_matters_short": "定义方法对象和信息流",
            "why_it_matters": "这张图定义了整篇论文的方法对象和信息流。",
            "evidence_level": "medium",
            "image_quality": "ok",
            "figure_quality_note": "图像质量可用于辅助理解，结论仍以正文为准。",
            "analysis": "图 1 展示了从输入结构到扩散采样再到性质打分的主链路。",
        }
```

- [ ] **Step 3: Replace required section assertions**

Replace `test_render_note_contains_required_sections()` with:

```python
def test_render_note_contains_required_learning_sections() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-23")

    expected_sections = [
        "## 0. 速读卡片",
        "## 1. 论文解决了什么问题？",
        "## 2. 方法框架",
        "## 3. 关键结果与数值",
        "## 4. 图表导读",
        "## 5. 贡献、局限与适用边界",
        "## 6. 对 AI4S / 电池 / 材料研究的启发",
        "## 7. 术语与概念卡片",
        "## 8. 后续检索关键词",
        "## 9. 元数据",
        "## 10. 自动抽取质量报告",
        "## 11. 证据链附录",
        "## 12. 补充优化记录",
    ]
    for section in expected_sections:
        assert section in note

    assert "# [Codex Summary] A Useful Materials Paper - 2026-04-23" in note
    assert "zotero://select/library/items/ABC123" in note
```

- [ ] **Step 4: Add a test for learning fields**

Append:

```python
def test_render_note_renders_learning_fields() -> None:
    note = render_note(
        METADATA,
        {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS, **LEARNING_FIELDS},
        generated_date="2026-04-23",
    )

    assert "| 是否值得精读 | strongly_recommended |" in note
    assert "| 与我的研究关系 | 对 AI4S、电池界面模拟和 learned observable workflow 有直接参考价值。 |" in note
    assert "### 30 秒结论" in note
    assert "| FIREANN | 原子结构 + 外场 | 外场相关原子力 | MLMD 力场 | 加速界面结构采样 |" in note
    assert "| 加速效果 | 约 4 个数量级 | 支持 ns 级界面采样 |" in note
    assert "| fig_p1_1 | 1 | 定义方法对象和信息流 | medium | ok |" in note
    assert "### fig_p1_1：Overall pipeline" in note
    assert "### finite-field molecular dynamics" in note
    assert "- **相关关键词**: finite field, electric field, electrochemical interface" in note
```

- [ ] **Step 5: Add a test for old summary fallback behavior**

Append:

```python
def test_render_note_old_summary_uses_safe_fallbacks() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-23")

    assert "| 研究对象 | unknown |" in note
    assert "| 核心问题 | 如何更可靠地预测材料性质？ |" in note
    assert "| 核心结果 | 这篇论文提出一种用于材料发现的机器学习框架。 |" in note
    assert "### 摘要翻译" in note
    assert "本文摘要的中文翻译。" in note
    assert "### 实验与证据摘要" in note
    assert "实验覆盖多个材料数据集。" in note
    assert "## 7. 术语与概念卡片\n\n- none" in note
```

- [ ] **Step 6: Add tests for workflow list normalization and visual-quality warning mapping**

Append:

```python
def test_render_note_accepts_workflow_steps_as_list() -> None:
    note = render_note(
        METADATA,
        {**SUMMARY, "workflow_steps": ["生成 AIMD 数据", "训练 FIREANN", "训练 MLEDR"]},
        generated_date="2026-04-23",
    )

    assert "### Workflow" in note
    assert "1. 生成 AIMD 数据\n2. 训练 FIREANN\n3. 训练 MLEDR" in note


def test_render_note_prefers_specific_visual_quality_warning() -> None:
    summary = {
        **SUMMARY,
        "figure_overview": "图表总览。",
        "key_figures": [
            {
                "figure_id": "fig_p1_1",
                "caption": "Figure 1. Overall pipeline.",
                "page": 1,
                "why_it_matters": "测试图作用。",
                "visual_quality": {"status": "poor", "warnings": ["image_too_small"]},
                "analysis": "图像质量不足，只能基于正文和 caption 分析。",
            }
        ],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")

    assert "| fig_p1_1 | 1 | 测试图作用。 | unknown | image_too_small |" in note
```

- [ ] **Step 7: Add a test that evidence and quality information are placed only in rear sections**

Append:

```python
def test_render_note_places_quality_and_evidence_in_rear_sections() -> None:
    note = render_note(
        METADATA,
        {
            **SUMMARY_WITH_FIGURES,
            **TRUSTED_FIELDS,
            "extraction_warnings": ["figure_visual_quality:fig_p1_1:image_too_small"],
        },
        generated_date="2026-04-23",
    )

    front_matter = note.split("## 10. 自动抽取质量报告", maxsplit=1)[0]
    assert "figure_visual_quality:fig_p1_1:image_too_small" not in front_matter
    assert "## 11. 证据链附录" in note
    assert "### Claim 1" in note
    assert "**结论**: The method uses a learned inverse-design model." in note
    assert "- page 3 method section: The method section describes the learned mapping" in note
    assert "\n-   - 证据:" not in note
    assert "\n  - 证据:" not in note
    assert note.index("## 10. 自动抽取质量报告") < note.index("## 11. 证据链附录")
```

- [ ] **Step 8: Run the new tests and confirm they fail before implementation**

Run:

```bash
uv run pytest tests/test_note.py::test_render_note_contains_required_learning_sections tests/test_note.py::test_render_note_renders_learning_fields tests/test_note.py::test_render_note_old_summary_uses_safe_fallbacks tests/test_note.py::test_render_note_accepts_workflow_steps_as_list tests/test_note.py::test_render_note_prefers_specific_visual_quality_warning tests/test_note.py::test_render_note_places_quality_and_evidence_in_rear_sections -q
```

Expected: failures mentioning old section names, missing new fields, or missing new rendered content.

---

### Task 2: Add Note Field Normalization And Fallbacks

**Files:**
- Modify `src/zotero_paperread/note.py`
- Test `tests/test_note.py`

- [ ] **Step 1: Update required sections**

Replace `REQUIRED_SECTIONS` with:

```python
REQUIRED_SECTIONS = [
    "0. 速读卡片",
    "1. 论文解决了什么问题？",
    "2. 方法框架",
    "3. 关键结果与数值",
    "4. 图表导读",
    "5. 贡献、局限与适用边界",
    "6. 对 AI4S / 电池 / 材料研究的启发",
    "7. 术语与概念卡片",
    "8. 后续检索关键词",
    "9. 元数据",
    "10. 自动抽取质量报告",
    "11. 证据链附录",
    "12. 补充优化记录",
]
```

- [ ] **Step 2: Add enum constants for new optional fields**

Add below existing status constants:

```python
VALID_READING_DECISIONS = {
    "strongly_recommended",
    "recommended",
    "skim_only",
    "not_priority",
    "unknown",
}
VALID_EVIDENCE_LEVELS = {
    "high",
    "medium",
    "low",
    "text_only",
    "caption_only",
    "image_unverified",
    "unknown",
}
VALID_IMAGE_QUALITIES = {
    "good",
    "ok",
    "poor",
    "image_too_small",
    "caption_only",
    "unknown",
}
```

- [ ] **Step 3: Add safe text helpers**

Add after `clean_required_text()`:

```python
def safe_text(value: Any, default: str = "unknown") -> str:
    if not isinstance(value, str):
        return default
    text = flatten_inline_markdown_text(value)
    return text if text else default


def optional_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def fallback_text(primary: Any, fallback: Any, default: str = "unknown") -> str:
    primary_text = flatten_inline_markdown_text(primary) if isinstance(primary, str) else ""
    if primary_text:
        return primary_text
    fallback_text_value = flatten_inline_markdown_text(fallback) if isinstance(fallback, str) else ""
    if fallback_text_value:
        return fallback_text_value
    return default


def clean_workflow_steps(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    items = clean_string_list(value)
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))
```

- [ ] **Step 4: Change evidence line formatting for the new appendix**

Replace `format_evidence_line()` with:

```python
def format_evidence_line(locator: str, summary: str) -> str:
    locator = flatten_inline_markdown_text(locator)
    summary = flatten_inline_markdown_text(summary)
    if locator and summary:
        return f"{locator}: {summary}"
    return locator or summary
```

This makes the new template line `- {{ evidence.line }}` render as a single clean bullet, not a nested bullet.

- [ ] **Step 5: Add list-of-dict cleaners**

Add after `clean_string_list()`:

```python
def clean_method_modules(value: Any) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for item in safe_list(value)[:8]:
        if not isinstance(item, dict):
            continue
        name = safe_text(item.get("name"))
        if name == "unknown":
            continue
        cleaned.append(
            {
                "name": name,
                "input": safe_text(item.get("input")),
                "target": safe_text(item.get("target")),
                "output": safe_text(item.get("output")),
                "role": safe_text(item.get("role")),
            }
        )
    return cleaned


def clean_key_results_table(value: Any) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for item in safe_list(value)[:12]:
        if not isinstance(item, dict):
            continue
        result = safe_text(item.get("result"))
        if result == "unknown":
            continue
        cleaned.append(
            {
                "result": result,
                "value": safe_text(item.get("value")),
                "meaning": safe_text(item.get("meaning")),
            }
        )
    return cleaned


def clean_concept_cards(value: Any) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in safe_list(value)[:8]:
        if not isinstance(item, dict):
            continue
        term = safe_text(item.get("term"))
        if term == "unknown":
            continue
        cleaned.append(
            {
                "term": term,
                "short_definition": safe_text(item.get("short_definition")),
                "role_in_paper": safe_text(item.get("role_in_paper")),
                "related_keywords": clean_string_list(item.get("related_keywords", [])),
            }
        )
    return cleaned
```

- [ ] **Step 6: Add risk fallback helper**

Add after `clean_issue_list()`:

```python
def infer_main_risk_short(summary: dict[str, Any], review_issues: list[dict[str, str]]) -> str:
    explicit = optional_text(summary.get("main_risk_short"))
    if explicit:
        return explicit

    warnings = clean_string_list(summary.get("extraction_warnings", []))
    if warnings:
        return warnings[0]

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    ranked_issues = sorted(
        review_issues,
        key=lambda item: severity_rank.get(item.get("severity", "medium"), 1),
    )
    if ranked_issues:
        return ranked_issues[0]["issue"]

    return "none"
```

- [ ] **Step 7: Add figure image-quality normalization and extend `clean_key_figures()`**

Add this helper before `clean_key_figures()`:

```python
def normalize_figure_image_quality(item: dict[str, Any]) -> str:
    explicit = item.get("image_quality")
    if isinstance(explicit, str) and explicit in VALID_IMAGE_QUALITIES:
        return explicit

    visual_quality = item.get("visual_quality", {})
    if isinstance(visual_quality, dict):
        warnings = visual_quality.get("warnings", [])
        if isinstance(warnings, list):
            for warning in warnings:
                if isinstance(warning, str) and warning in VALID_IMAGE_QUALITIES:
                    return warning
        status = visual_quality.get("status")
        if isinstance(status, str) and status in VALID_IMAGE_QUALITIES:
            return status

    return "unknown"
```

Replace the returned dict inside `clean_key_figures()` with:

```python
        why_it_matters = str(item.get("why_it_matters", "")).strip()
        image_quality = normalize_figure_image_quality(item)
        cleaned.append(
            {
                "figure_id": str(item.get("figure_id", "")).strip(),
                "title_short": optional_text(item.get("title_short")),
                "caption": str(item.get("caption", "")).strip(),
                "page": item.get("page", ""),
                "priority_score": item.get("priority_score", ""),
                "why_it_matters_short": fallback_text(
                    item.get("why_it_matters_short"),
                    why_it_matters,
                ),
                "why_it_matters": why_it_matters,
                "evidence_level": safe_choice(
                    item.get("evidence_level"),
                    VALID_EVIDENCE_LEVELS,
                    "unknown",
                ),
                "image_quality": image_quality,
                "figure_quality_note": fallback_text(
                    item.get("figure_quality_note"),
                    image_quality,
                ),
                "analysis": str(item.get("analysis", "")).strip(),
            }
        )
```

- [ ] **Step 8: Extend `render_note()` context**

Inside `render_note()`, compute cleaned intermediate values before `context`:

```python
    review_issues = clean_issue_list(summary)
    extraction_warnings = clean_string_list(summary.get("extraction_warnings", []))
```

Then add these context entries:

```python
        "research_object": safe_text(summary.get("research_object")),
        "research_question_short": fallback_text(
            summary.get("research_question_short"),
            summary.get("research_question"),
        ),
        "core_method_short": safe_text(summary.get("core_method_short")),
        "core_result_short": fallback_text(
            summary.get("core_result_short"),
            summary.get("one_sentence_summary"),
        ),
        "relevance_to_user": safe_text(summary.get("relevance_to_user")),
        "reading_decision": safe_choice(
            summary.get("reading_decision"),
            VALID_READING_DECISIONS,
            "unknown",
        ),
        "main_risk_short": infer_main_risk_short(summary, review_issues),
        "tldr": optional_text(summary.get("tldr")),
        "background_problem": safe_text(summary.get("background_problem")),
        "existing_gap": safe_text(summary.get("existing_gap")),
        "paper_entry_point": safe_text(summary.get("paper_entry_point")),
        "method_overview": fallback_text(summary.get("method_overview"), summary.get("method")),
        "method_modules": clean_method_modules(summary.get("method_modules", [])),
        "workflow_steps": clean_workflow_steps(summary.get("workflow_steps")),
        "technical_details": clean_string_list(summary.get("technical_details", [])),
        "key_results_table": clean_key_results_table(summary.get("key_results_table", [])),
        "applicability_limits": clean_string_list(summary.get("applicability_limits", [])),
        "transferable_insight": fallback_text(
            summary.get("transferable_insight"),
            summary.get("ai4s_relevance"),
        ),
        "workflow_lessons": clean_string_list(summary.get("workflow_lessons", [])),
        "follow_up_questions": clean_string_list(summary.get("follow_up_questions", [])),
        "concept_cards": clean_concept_cards(summary.get("concept_cards", [])),
```

Also replace the existing context values for `review_issues` and `extraction_warnings` with the intermediate values:

```python
        "review_issues": review_issues,
        "extraction_warnings": extraction_warnings,
```

Also use safe text fallbacks for old scalar fields that now appear in the front matter:

```python
        "quality_score": safe_text(summary.get("quality_score")),
        "one_sentence_summary": safe_text(summary.get("one_sentence_summary"), "No one-sentence summary provided."),
        "abstract_translation": optional_text(summary.get("abstract_translation")),
        "research_question": optional_text(summary.get("research_question")),
        "method": optional_text(summary.get("method")),
        "figure_overview": optional_text(summary.get("figure_overview")),
        "experiments": optional_text(summary.get("experiments")),
        "ai4s_relevance": optional_text(summary.get("ai4s_relevance")),
```

- [ ] **Step 9: Run focused tests**

Run:

```bash
uv run pytest tests/test_note.py::test_render_note_renders_learning_fields tests/test_note.py::test_render_note_old_summary_uses_safe_fallbacks tests/test_note.py::test_render_note_accepts_workflow_steps_as_list tests/test_note.py::test_render_note_prefers_specific_visual_quality_warning -q
```

Expected: tests still fail until the template is rewritten, but errors should no longer be caused by undefined Jinja variables after Task 3 is complete.

---

### Task 3: Rewrite The Jinja Note Template

**Files:**
- Modify `templates/zotero_note.md.j2`
- Test `tests/test_note.py`
- Test `tests/test_cli_note.py`

- [ ] **Step 1: Replace the full template with the learning-note structure**

Replace `templates/zotero_note.md.j2` with:

```jinja
# {{ note_title }}

> {{ one_sentence_summary }}

## 0. 速读卡片

| 项目 | 内容 |
|---|---|
| 论文类型 | {{ paper_type }} |
| 研究对象 | {{ research_object }} |
| 核心问题 | {{ research_question_short }} |
| 核心方法 | {{ core_method_short }} |
| 核心结果 | {{ core_result_short }} |
| 可信状态 | {{ trust_status }} |
| 质量评分 | {{ quality_score }} |
| 最大风险 | {{ main_risk_short }} |
| 是否值得精读 | {{ reading_decision }} |
| 与我的研究关系 | {{ relevance_to_user }} |

{% if tldr %}
### 30 秒结论

{{ tldr }}
{% endif %}

## 1. 论文解决了什么问题？

### 背景问题

{{ background_problem }}

### 现有方法瓶颈

{{ existing_gap }}

### 本文切入点

{{ paper_entry_point }}

{% if abstract_translation %}
### 摘要翻译

{{ abstract_translation }}
{% endif %}

## 2. 方法框架

### 方法总览

{{ method_overview }}

### 模块拆解

{% if method_modules %}
| 模块 | 输入 | 学习/计算目标 | 输出 | 作用 |
|---|---|---|---|---|
{% for item in method_modules -%}
| {{ item.name }} | {{ item.input }} | {{ item.target }} | {{ item.output }} | {{ item.role }} |
{% endfor %}
{% else %}
- none
{% endif %}

{% if workflow_steps %}
### Workflow

{{ workflow_steps }}
{% endif %}

### 关键技术细节

{% if technical_details %}
{% for item in technical_details -%}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

## 3. 关键结果与数值

### 结果总览

{% if key_results_table %}
| 结果 | 数值/现象 | 意义 |
|---|---|---|
{% for item in key_results_table -%}
| {{ item.result }} | {{ item.value }} | {{ item.meaning }} |
{% endfor %}
{% else %}
- none
{% endif %}

### 主要发现

{% if key_points %}
{% for item in key_points -%}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

{% if experiments %}
### 实验与证据摘要

{{ experiments }}
{% endif %}

## 4. 图表导读

{% if figure_overview %}
### 图表总览

{{ figure_overview }}
{% endif %}

### 关键图表索引

{% if key_figures %}
| 图 | 页码 | 作用 | 证据等级 | 图像质量 |
|---|---:|---|---|---|
{% for item in key_figures -%}
| {{ item.figure_id }} | {{ item.page }} | {{ item.why_it_matters_short }} | {{ item.evidence_level }} | {{ item.image_quality }} |
{% endfor %}

{% for item in key_figures -%}
### {{ item.figure_id }}{% if item.title_short %}：{{ item.title_short }}{% endif %}

- **Caption**: {{ item.caption }}
- **Page**: {{ item.page }}
- **Why it matters**: {{ item.why_it_matters }}
- **图像/抽取质量**: {{ item.figure_quality_note }}

{{ item.analysis }}

{% endfor %}
{% else %}
- none
{% endif %}

## 5. 贡献、局限与适用边界

### 主要贡献

{% if contributions %}
{% for item in contributions -%}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

### 局限与风险

{% if limitations %}
{% for item in limitations -%}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

### 适用边界

{% if applicability_limits %}
{% for item in applicability_limits -%}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

## 6. 对 AI4S / 电池 / 材料研究的启发

### 可迁移思想

{{ transferable_insight }}

### 可借鉴的 workflow

{% if workflow_lessons %}
{% for item in workflow_lessons -%}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

### 可继续追问的问题

{% if follow_up_questions %}
{% for item in follow_up_questions -%}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

## 7. 术语与概念卡片

{% if concept_cards %}
{% for item in concept_cards -%}
### {{ item.term }}

- **一句话解释**: {{ item.short_definition }}
- **本文中的作用**: {{ item.role_in_paper }}
- **相关关键词**: {{ item.related_keywords | join(', ') }}

{% endfor %}
{% else %}
- none
{% endif %}

## 8. 后续检索关键词

{% if follow_up_keywords %}
{% for item in follow_up_keywords -%}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

## 9. 元数据

| 字段 | 内容 |
|---|---|
| Zotero Key | {{ key }} |
| 标题 | {{ title }} |
| 作者 | {{ creators }} |
| 日期 | {{ date }} |
| DOI | {{ doi }} |
| URL | {{ url }} |
| Zotero 链接 | {{ zotero_url }} |
| 标签 | {{ note_labels | join(', ') }} |

## 10. 自动抽取质量报告

### 抽取告警

{% if extraction_warnings %}
{% for item in extraction_warnings -%}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

### 审查问题

{% if review_issues %}
{% for item in review_issues -%}
- **{{ item.severity }}**: {{ item.issue }}{% if item.suggested_fix %}
  建议: {{ item.suggested_fix }}{% endif %}
{% endfor %}
{% else %}
- none
{% endif %}

### 可信度判断依据

{{ trust_rationale }}

### 审查状态

- **审查状态**: {{ review_status }}
- **改进状态**: {{ improvement_status }}

## 11. 证据链附录

{% if evidence_summary %}
{% for item in evidence_summary %}
### Claim {{ loop.index }}

**结论**: {{ item.claim }}

**证据**:
{% if item.evidence %}
{% for evidence in item.evidence %}
- {{ evidence.line }}
{% endfor %}
{% else %}
- none
{% endif %}

{% endfor %}
{% else %}
- none
{% endif %}

## 12. 补充优化记录

{% if improvement_notes %}
{% for item in improvement_notes %}
- {{ item.issue }}{% if item.action %}: {{ item.action }}{% endif %}{% if item.source %}
  source: {{ item.source }}{% endif %}
{% endfor %}
{% else %}
- none
{% endif %}

---

Tags: {{ note_labels | join(', ') }}
```

- [ ] **Step 2: Run focused rendering tests**

Run:

```bash
uv run pytest tests/test_note.py::test_render_note_contains_required_learning_sections tests/test_note.py::test_render_note_renders_learning_fields tests/test_note.py::test_render_note_old_summary_uses_safe_fallbacks tests/test_note.py::test_render_note_accepts_workflow_steps_as_list tests/test_note.py::test_render_note_prefers_specific_visual_quality_warning tests/test_note.py::test_render_note_places_quality_and_evidence_in_rear_sections -q
```

Expected:

```text
6 passed
```

- [ ] **Step 3: Update tests that still reference old headings**

Replace old heading references in `tests/test_note.py`:

```python
"## 关键图片总览"
"## 可信度与证据"
"## 抽取告警"
"## 关键证据"
"## 审查问题"
```

with new section references:

```python
"## 4. 图表导读"
"## 10. 自动抽取质量报告"
"## 11. 证据链附录"
```

For evidence-section string splitting, use:

```python
evidence_section = note.split("## 11. 证据链附录\n\n", maxsplit=1)[1].split(
    "\n\n## 12. 补充优化记录",
    maxsplit=1,
)[0].strip()
```

Update assertions that previously expected evidence bullets like:

```python
"  - 证据: page 3 method section; ..."
```

to the new Claim-Evidence appendix format:

```python
"- page 3 method section: The method section describes the learned mapping from target response to structure parameters."
```

Also assert malformed nested bullets are absent:

```python
assert "\n-   - 证据:" not in evidence_section
assert "\n  - 证据:" not in evidence_section
```

- [ ] **Step 4: Update CLI note tests that assert old section names**

In `tests/test_cli_note.py`, update:

```python
assert "## 核心结论" in output_path.read_text(encoding="utf-8")
```

to:

```python
rendered = output_path.read_text(encoding="utf-8")
assert "## 0. 速读卡片" in rendered
assert "## 11. 证据链附录" in rendered
```

Also update:

```python
assert "## 可信度与证据" in note
```

to:

```python
assert "## 10. 自动抽取质量报告" in note
assert "## 11. 证据链附录" in note
```

- [ ] **Step 5: Run note and CLI note tests**

Run:

```bash
uv run pytest tests/test_note.py tests/test_cli_note.py -q
```

Expected:

`pytest` exits with code 0 and reports every test in `tests/test_note.py` and `tests/test_cli_note.py` as passed.

---

### Task 4: Update The Summary Generation Skill Contract

**Files:**
- Modify `skills/zotero-paper-summary/SKILL.md`

- [ ] **Step 1: Extend the required `summary.json` example**

In the JSON example under "生成 summary JSON", add these optional fields before `quality_score`:

```json
  "research_object": "",
  "research_question_short": "",
  "core_method_short": "",
  "core_result_short": "",
  "relevance_to_user": "",
  "reading_decision": "unknown",
  "main_risk_short": "",
  "tldr": "",
  "background_problem": "",
  "existing_gap": "",
  "paper_entry_point": "",
  "method_overview": "",
  "method_modules": [
    {
      "name": "",
      "input": "",
      "target": "",
      "output": "",
      "role": ""
    }
  ],
  "workflow_steps": "",
  "technical_details": [],
  "key_results_table": [
    {
      "result": "",
      "value": "",
      "meaning": ""
    }
  ],
  "applicability_limits": [],
  "transferable_insight": "",
  "workflow_lessons": [],
  "follow_up_questions": [],
  "concept_cards": [
    {
      "term": "",
      "short_definition": "",
      "role_in_paper": "",
      "related_keywords": []
    }
  ],
```

- [ ] **Step 2: Extend each `key_figures` object in the example**

Change the `key_figures` object shape to:

```json
    {
      "figure_id": "",
      "title_short": "",
      "caption": "",
      "page": 0,
      "priority_score": 0,
      "why_it_matters_short": "",
      "why_it_matters": "",
      "evidence_level": "unknown",
      "image_quality": "unknown",
      "figure_quality_note": "",
      "analysis": ""
    }
```

- [ ] **Step 3: Add field generation rules**

Add these bullets to the analysis requirements:

```markdown
   - `reading_decision` 必须从 `strongly_recommended`、`recommended`、`skim_only`、`not_priority`、`unknown` 中选择。
   - `trust_status` 仍只能从 `trusted`、`usable_with_caveats`、`metadata_only`、`needs_manual_review` 中选择；不要输出 `needs_review`、`low_confidence` 或 `failed`。
   - `main_risk_short` 只写一个最主要风险；完整 warning 仍写入 `extraction_warnings` 或 `review_issues`。
   - `workflow_steps` 可以是 Markdown 字符串，也可以是字符串列表；如果是列表，渲染器会转换成编号 workflow。
   - `method_modules` 优先用于 AI4S、计算材料、电池、模拟、方法论文；没有模块化方法时留空数组。
   - `key_results_table` 优先收集 RMSE、MAE、加速倍数、电容、能量密度、循环寿命、倍率性能、diffusion barrier、conductivity 和 out-of-distribution performance。
   - `concept_cards` 建议 3-8 个，优先选择读懂论文必须掌握的术语、方法缩写和可继续检索的关键词。
   - `workflow_lessons` 应提炼可迁移科研 workflow，而不是重复论文结论。
   - `evidence_level` 必须从 `high`、`medium`、`low`、`text_only`、`caption_only`、`image_unverified`、`unknown` 中选择。
   - `image_quality` 必须从 `good`、`ok`、`poor`、`image_too_small`、`caption_only`、`unknown` 中选择。
   - 如果 `figure_context.md` 或 `figures.json` 中已有 `visual_quality.warnings`，优先把 `image_too_small` 这类具体 warning 写入 `image_quality`，不要只写泛化的 `poor`。
   - 当图像质量为 `poor`、`image_too_small` 或 `caption_only` 时，`figure_quality_note` 和 `analysis` 必须说明图分析不依赖像素读图，只基于正文或 caption。
```

- [ ] **Step 4: Run a documentation grep check**

Run:

```bash
rg -n "reading_decision|method_modules|key_results_table|concept_cards|evidence_level|image_quality" skills/zotero-paper-summary/SKILL.md
```

Expected: all six field names appear in the skill.

---

### Task 5: Update User Documentation

**Files:**
- Modify `README.md`

- [ ] **Step 1: Replace the old rendered-note paragraph**

Replace the paragraph that begins with "The rendered note includes `## 可信度与证据` near the top" with:

```markdown
The rendered note is a layered learning note. It opens with `## 0. 速读卡片` so the first screen shows paper type, research object, core problem, core method, core result, trust status, main risk, reading decision, and relevance to AI4S / battery / materials research. The main body then follows problem, method, results, figures, contributions, limits, transferable workflows, concept cards, and follow-up keywords. Metadata, extraction warnings, review issues, trust rationale, evidence chains, and improvement notes are kept in rear sections (`## 9` through `## 12`) so provenance remains available without interrupting the reading flow.
```

- [ ] **Step 2: Add a short compatibility note**

Add below that paragraph:

```markdown
New learning-note fields such as `method_modules`, `key_results_table`, `concept_cards`, `workflow_lessons`, and `reading_decision` are optional. Old `summary.json` files still render through safe fallbacks: `method_overview` falls back to `method`, `core_result_short` falls back to `one_sentence_summary`, and `transferable_insight` falls back to `ai4s_relevance`.
```

- [ ] **Step 3: Run a documentation grep check**

Run:

```bash
rg -n "速读卡片|method_modules|key_results_table|concept_cards|transferable_insight" README.md
```

Expected: all five terms appear.

---

### Task 6: Full Verification

**Files:**
- No direct file edits.

- [ ] **Step 1: Run the project test suite**

Run:

```bash
uv run pytest
```

Expected:

`pytest` exits with code 0 and reports the full project test suite as passed.

- [ ] **Step 2: Verify CLI help still works**

Run:

```bash
uv run zotero-paperread --help
```

Expected: Typer help output listing the `zotero-paperread` commands without traceback.

- [ ] **Step 3: Run the PDF extraction smoke command**

Because this change touches rendered note behavior that can be used after PDF extraction, run the project-required extraction smoke command:

```bash
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

Expected: command exits successfully and writes `/tmp/zotero-paperread-extract.json`.

- [ ] **Step 4: Inspect final changed files**

Run:

```bash
git status --short
git diff -- src/zotero_paperread/note.py templates/zotero_note.md.j2 tests/test_note.py tests/test_cli_note.py skills/zotero-paper-summary/SKILL.md README.md
```

Expected: diffs are limited to the planned files and show no unrelated rewrites.

---

## Self-Review

**Spec coverage:** The plan covers the 0-12 section order,速读卡片, evidence-chain后置, metadata表格化, method_modules, key_results_table, figure index plus detail expansion, applicability_limits, AI4S workflow lessons, concept_cards, centralized extraction quality report, optional-field fallback, and old-field compatibility.

**Risk review:** The biggest implementation risk is treating semantic fields as deterministic extraction. This plan avoids that by putting semantic generation into `skills/zotero-paper-summary/SKILL.md` and keeping Python responsible for safe normalization and rendering. The plan also handles three renderer-specific risks: evidence lines no longer carry their own Markdown bullet prefix, `workflow_steps` accepts string or list input, and figure `image_quality` preserves specific warnings such as `image_too_small`.

**Compatibility review:** Existing `trust_status` values are preserved. Old `summary.json` fields still render. `StrictUndefined` remains enabled, so missing context keys will surface in tests.

**Verification review:** The plan includes focused failing tests, focused passing tests, full `uv run pytest`, CLI help, and the project-required PDF extraction smoke command.
