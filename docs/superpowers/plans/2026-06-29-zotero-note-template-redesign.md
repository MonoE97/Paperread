# Zotero Note Template Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved 0-5 Zotero note layout while keeping `summary.json`, review, evidence, and write gates intact.

**Architecture:** This is a render-layer, validation-layer, and documentation update. The renderer continues to receive the existing rich summary context, but the final Markdown/HTML note only shows the approved 0-5 sections; gate-facing fields such as `trust_status`, `key_results_table`, `result_evidence_notes`, `concept_cards`, `follow_up_keywords`, and `limitations` stay in JSON artifacts. The conservative path is to keep legacy `limitations` as a required audit field and use structured limitation fields only for rendered organization and newer artifacts.

**Tech Stack:** Python, Jinja2, markdown-it-py, Typer CLI, pytest, uv, Zotero MCP for final live writes.

---

## File Structure

- Modify `templates/zotero_note.md.j2`
  - Replace the current 0-7 rendered layout with the approved 0-5 layout.
  - Render `0. 阅读结论` and `1. 速读信息` as Markdown tables.
  - Render `4. 图表导读` as one table, including a one-row `none` fallback when no usable figures exist.
  - Stop rendering `trust_status`, `relevance_to_user`, `quality_score`, result evidence tables, transferable insights, concept cards, and follow-up keywords in the note body.
- Modify `src/zotero_paperread/note.py`
  - Update `REQUIRED_SECTIONS` to the new 0-5 section list.
  - Add forbidden rendered headings/snippets for `validate_note`.
  - Add a helper to synthesize the figure table description from `analysis`, `why_it_matters`, `caption`, `figure_quality_note`, and `image_quality`.
  - Keep `limitations` required for write-ready summaries; do not weaken `validate_trusted_summary`.
- Modify `tests/test_note.py`
  - Update renderer tests to assert the new section order.
  - Add negative assertions for old sections and hidden gate/audit fields.
  - Add table-specific tests for the two quick tables and the figure table.
  - Add a no-figures test that still expects a figure table row.
- Modify `tests/test_cli_note.py`
  - Update CLI render and validation expectations from 0-7 to 0-5.
  - Keep validate-trusted-summary tests proving `limitations`, `follow_up_keywords`, and other audit fields still exist.
- Modify `tests/test_default_workflow_docs.py`
  - Update documentation consistency assertions so README and skill docs describe the same 0-5 layout and the new `verify-zotero-note` command.
- Modify `README.md`
  - Replace old rendered layout documentation and the old `verify-zotero-note` example.
  - State that `trust_status`, result evidence, concepts, and follow-up keywords are audit-only.
- Modify `skills/zotero-paper-summary/SKILL.md`
  - Update the summary writing instructions and final write verification command to match the 0-5 layout.
  - Keep instructions that generate audit-only fields in `summary.json`.
- Use existing run artifacts for dry-run verification:
  - `runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/item-details.json`
  - `runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/metadata.json`
  - `runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/summary.json`
  - `runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/review.json`

## Task 1: Lock the New Render Contract With Tests

**Files:**
- Modify: `tests/test_note.py`

- [ ] **Step 1: Add section-order and hidden-field tests**

Replace the old rendered-section expectation in `test_render_note_contains_required_learning_sections` with:

```python
def test_render_note_contains_required_learning_sections() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-23")

    expected_sections = [
        "## 0. 阅读结论",
        "## 1. 速读信息",
        "## 2. 论文主张",
        "## 3. 方法与设计",
        "## 4. 图表导读",
        "## 5. 边界与机会",
    ]
    for section in expected_sections:
        assert section in note
    positions = [note.index(section) for section in expected_sections]
    assert positions == sorted(positions)

    forbidden_sections = [
        "## 3. 结果可信度",
        "## 6. 我能怎么用",
        "## 7. 术语与检索",
        "## 9. 元数据",
        "## 10. 证据链附录",
        "## 11. 补充优化记录",
    ]
    for section in forbidden_sections:
        assert section not in note

    assert "# [Codex Summary] A Useful Materials Paper - 2026-04-23" in note
    assert "zotero://select/library/items/ABC123" not in note
```

- [ ] **Step 2: Replace the old learning-field render test with two quick-table assertions**

Replace `test_render_note_renders_learning_fields` with:

```python
def test_render_note_uses_decision_and_quick_info_tables() -> None:
    note = render_note(
        METADATA,
        {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS, **LEARNING_FIELDS},
        generated_date="2026-04-23",
    )

    assert "## 0. 阅读结论\n\n| 项目 | 内容 |\n| --- | --- |" in note
    assert "| 30 秒结论 | 本文把动力学采样模型和电子响应模型拆开训练，用于电化学界面长时间尺度采样。 |" in note
    assert "| 主要风险 | Figure 2-4 crop 过小，图像细节不能独立复核。 |" in note
    assert "| 阅读决策 | 强烈建议精读 (strongly_recommended) |" in note

    assert "## 1. 速读信息\n\n| 项目 | 内容 |\n| --- | --- |" in note
    assert "| 论文类型 | 研究论文 (research_article) |" in note
    assert "| 研究对象 | Au(100)/NaCl(aq) electrochemical interface |" in note
    assert "| 核心问题 | 如何加速 finite-field electrochemical interface simulations？ |" in note
    assert "| 核心方法 | FIREANN 学外场相关原子力，MLEDR 学电子密度响应。 |" in note
    assert "| 核心结果 | 实现约 4 个数量级加速，并预测电容、极化和界面水取向。 |" in note
```

- [ ] **Step 3: Add tests proving audit-only fields do not render**

Replace `test_render_note_contains_trust_status_but_hides_audit_appendices` with:

```python
def test_render_note_hides_gate_and_audit_only_fields() -> None:
    note = render_note(
        METADATA,
        {
            **SUMMARY_WITH_FIGURES,
            **TRUSTED_FIELDS,
            **LEARNING_FIELDS,
            "potential_gaps": [
                {
                    "text": "需要真实高面容量软包验证。",
                    "basis": "当前实验仍是扣式或实验室尺度。",
                    "uncertainty": "medium",
                    "locator": "context.md page 7",
                }
            ],
        },
        generated_date="2026-04-23",
    )
    html = render_note_html(note)

    forbidden_snippets = [
        "可信状态",
        "可信 (trusted)",
        "trust_status",
        "与我的研究关系",
        "质量评分",
        "关键结果表",
        "baseline / comparison",
        "结果证据说明",
        "证据质量",
        "可迁移启发",
        "工作流经验",
        "后续问题",
        "核心概念",
        "后续检索关键词",
        "潜在 gap",
        "需要真实高面容量软包验证。",
        "The method uses a learned inverse-design model.",
        "page 3 method section",
        "Method section was too generic.",
    ]
    for snippet in forbidden_snippets:
        assert snippet not in note
        assert snippet not in html
```

- [ ] **Step 4: Add tests for the single figure table**

Add:

```python
def test_render_note_uses_single_figure_table() -> None:
    note = render_note(
        METADATA,
        {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS, **LEARNING_FIELDS},
        generated_date="2026-04-23",
    )

    assert "## 4. 图表导读\n\n| 图 | 图像抽取质量 | 图片描述内容 |\n| --- | --- | --- |" in note
    assert "| Figure 1 | ok |" in note
    assert "图 1 展示了从输入结构到扩散采样再到性质打分的主链路。" in note
    assert "这张图定义了整篇论文的方法对象和信息流。" in note
    assert "### 图表总览" not in note
    assert "### 图表索引" not in note
    assert "### 展开图表" not in note
    assert "证据等级" not in note
```

- [ ] **Step 5: Add a no-figures table fallback test**

Add:

```python
def test_render_note_figure_section_stays_table_when_no_figures() -> None:
    note = render_note(
        METADATA,
        {**SUMMARY, **TRUSTED_FIELDS, **LEARNING_FIELDS, "key_figures": []},
        generated_date="2026-04-23",
    )

    assert "## 4. 图表导读\n\n| 图 | 图像抽取质量 | 图片描述内容 |\n| --- | --- | --- |" in note
    assert "| none | unknown | 未抽取到可用图表；图表导读不可用，请以正文与证据摘要为准。 |" in note
    figure_section = note.split("## 4. 图表导读", maxsplit=1)[1].split("## 5. 边界与机会", maxsplit=1)[0]
    assert "- none" not in figure_section
```

- [ ] **Step 6: Update remaining old-layout renderer tests**

Update these existing tests in `tests/test_note.py` so they no longer require old rendered sections:

```text
test_render_note_uses_reading_thread_sections_without_audit_appendices
test_render_note_separates_dynamic_lists_from_following_headings
test_render_note_renders_recommendations_result_evidence_and_gap_fields
test_render_note_does_not_duplicate_follow_up_questions_as_potential_gaps
test_render_note_old_summary_uses_safe_fallbacks
test_render_note_contains_figure_sections
test_render_note_falls_back_to_ordered_figure_labels_without_caption_number
test_render_note_fallback_figure_labels_ignore_skipped_items
test_render_note_normalizes_common_figure_label_forms
test_render_note_places_trailing_tags_after_reading_sections
test_render_note_ignores_string_values_for_list_sections
test_render_note_keeps_audit_sections_hidden_without_review_or_improvement_blocks
```

Use these replacement rules:

```python
# Old rendered sections become forbidden assertions.
assert "## 3. 结果可信度" not in note
assert "## 6. 我能怎么用" not in note
assert "## 7. 术语与检索" not in note

# Recommendation/result/gap fields stay in summary data but do not render.
assert "### 推荐先读章节" not in note
assert "### 推荐先看图表" not in note
assert "baseline / comparison" not in note
assert "结果证据说明" not in note
assert "潜在 gap" not in note

# Figure-label tests should inspect the single figure table, not expanded h3 headings.
assert "| Figure 2a |" in note
assert "| Scheme 1 |" in note
assert "| Figure 3-4 |" in note
assert "### Figure 2a" not in note

# Tags now follow section 5.
assert note.index("## 5. 边界与机会") < note.index("---\n\nTags: codex-summary, paper-summary")
```

For `test_render_note_ignores_string_values_for_list_sections`, replace the old expected figure-section text with:

```python
assert "| 图 | 图像抽取质量 | 图片描述内容 |" in figure_section
assert "| none | unknown | 未抽取到可用图表；图表导读不可用，请以正文与证据摘要为准。 |" in figure_section
assert "### 图表总览" not in figure_section
assert "### 图表索引" not in figure_section
assert "### 展开图表" not in figure_section
```

- [ ] **Step 7: Run renderer tests and verify RED**

Run:

```bash
uv run pytest tests/test_note.py::test_render_note_contains_required_learning_sections \
  tests/test_note.py::test_render_note_uses_decision_and_quick_info_tables \
  tests/test_note.py::test_render_note_hides_gate_and_audit_only_fields \
  tests/test_note.py::test_render_note_uses_single_figure_table \
  tests/test_note.py::test_render_note_figure_section_stays_table_when_no_figures -q
```

Expected before implementation:

```text
FAILED
```

The failures should mention old headings, missing `## 1. 速读信息`, or the old bullet/expanded figure rendering.

## Task 2: Implement the New Renderer and Validation Contract

**Files:**
- Modify: `src/zotero_paperread/note.py`
- Modify: `templates/zotero_note.md.j2`

- [ ] **Step 1: Update section constants and forbidden body checks**

Replace `REQUIRED_SECTIONS` at the top of `src/zotero_paperread/note.py` with:

```python
REQUIRED_SECTIONS = [
    "0. 阅读结论",
    "1. 速读信息",
    "2. 论文主张",
    "3. 方法与设计",
    "4. 图表导读",
    "5. 边界与机会",
]

FORBIDDEN_RENDERED_HEADINGS = [
    "3. 结果可信度",
    "6. 我能怎么用",
    "7. 术语与检索",
    "9. 元数据",
    "10. 证据链附录",
    "11. 补充优化记录",
]

FORBIDDEN_RENDERED_SNIPPETS = [
    "可信状态",
    "trust_status",
]
```

- [ ] **Step 2: Add figure-description synthesis helpers**

Add this helper near `infer_main_risk_short`:

```python
LOW_CONFIDENCE_IMAGE_QUALITIES = {"poor", "image_too_small", "caption_only", "unknown"}


def build_figure_description(item: dict[str, Any], image_quality: str) -> str:
    analysis = optional_text(item.get("analysis"))
    why_it_matters = optional_text(item.get("why_it_matters"))
    caption = optional_text(item.get("caption"))

    parts: list[str] = []
    if analysis:
        parts.append(analysis)
    elif caption:
        parts.append(f"这张图展示：{caption}")

    if why_it_matters:
        parts.append(f"支撑的核心主张：{why_it_matters}")

    if image_quality in LOW_CONFIDENCE_IMAGE_QUALITIES:
        parts.append("图像抽取质量较低，以上判断仅基于正文/图注证据。")

    return " ".join(parts) if parts else "未提供图表描述。"
```

- [ ] **Step 3: Add the synthesized description to cleaned figures**

In `clean_key_figures`, add `figure_description` to the cleaned item:

```python
        cleaned.append(
            {
                "figure_id": str(item.get("figure_id", "")).strip(),
                "display_label": extract_figure_display_label(caption, fallback_index=fallback_index),
                "caption": caption,
                "page": item.get("page", ""),
                "priority_score": item.get("priority_score", ""),
                "why_it_matters": str(item.get("why_it_matters", "")).strip(),
                "title_short": optional_text(item.get("title_short")),
                "why_it_matters_short": fallback_text(item.get("why_it_matters_short"), item.get("why_it_matters")),
                "evidence_level": safe_choice(item.get("evidence_level"), VALID_EVIDENCE_LEVELS, "unknown"),
                "image_quality": image_quality,
                "figure_quality_note": fallback_text(item.get("figure_quality_note"), image_quality),
                "figure_description": build_figure_description(item, image_quality),
                "analysis": str(item.get("analysis", "")).strip(),
            }
        )
```

- [ ] **Step 4: Update `validate_note` forbidden checks**

Replace `validate_note` with:

```python
def validate_note(note: str) -> list[str]:
    """Return validation errors for a rendered note."""
    errors: list[str] = []
    for section in REQUIRED_SECTIONS:
        if f"## {section}" not in note:
            errors.append(f"missing_section: {section}")
    for section in FORBIDDEN_RENDERED_HEADINGS:
        if f"## {section}" in note:
            errors.append(f"forbidden_section: {section}")
    for snippet in FORBIDDEN_RENDERED_SNIPPETS:
        if snippet in note:
            errors.append(f"forbidden_content: {snippet}")
    if "[Codex Summary]" not in note:
        errors.append("missing_codex_summary_title")
    if "Tags: codex-summary, paper-summary" not in note:
        errors.append("missing_tags")
    return errors
```

- [ ] **Step 5: Replace the template with the approved 0-5 layout**

Replace `templates/zotero_note.md.j2` with:

```jinja
# {{ note_title }}

## 0. 阅读结论

| 项目 | 内容 |
| --- | --- |
| 30 秒结论 | {% if tldr %}{{ tldr | table_cell }}{% else %}{{ one_sentence_summary | table_cell }}{% endif %} |
| 主要风险 | {{ main_risk_short | table_cell }} |
| 阅读决策 | {{ reading_decision | table_cell }} |

## 1. 速读信息

| 项目 | 内容 |
| --- | --- |
| 论文类型 | {{ paper_type | table_cell }} |
| 研究对象 | {{ research_object | table_cell }} |
| 核心问题 | {{ research_question_short | table_cell }} |
| 核心方法 | {{ core_method_short | table_cell }} |
| 核心结果 | {{ core_result_short | table_cell }} |

## 2. 论文主张

### 背景问题

{{ background_problem }}

### 已有缺口

{{ existing_gap }}

### 本文切入点 + 贡献

{{ paper_entry_point }}

{% if contributions %}
{% for item in contributions %}
- {{ item }}
{% endfor %}
{% else %}
- {{ one_sentence_summary }}
{% endif %}

## 3. 方法与设计

### 方法总览

{{ method_overview }}

### 模块拆解

{% if method_modules %}
| 模块 | 输入 | 目标 | 输出 | 作用 |
| --- | --- | --- | --- | --- |
{% for item in method_modules %}
| {{ item.name | table_cell }} | {{ item.input | table_cell }} | {{ item.target | table_cell }} | {{ item.output | table_cell }} | {{ item.role | table_cell }} |
{% endfor %}
{% else %}
- none
{% endif %}

{% if workflow_steps %}
### Workflow

{{ workflow_steps }}
{% endif %}

### 技术细节

{% if technical_details %}
{% for item in technical_details %}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

## 4. 图表导读

| 图 | 图像抽取质量 | 图片描述内容 |
| --- | --- | --- |
{% if key_figures %}
{% for item in key_figures %}
| {{ item.display_label | table_cell }} | {{ item.image_quality | table_cell }} | {{ item.figure_description | table_cell }} |
{% endfor %}
{% else %}
| none | unknown | 未抽取到可用图表；图表导读不可用，请以正文与证据摘要为准。 |
{% endif %}

## 5. 边界与机会

### 作者明示局限

{% if author_stated_limitations %}
{% for item in author_stated_limitations %}
- {{ item.text }}{% if item.locator %} ({{ item.locator }}){% endif %}
{% endfor %}
{% elif limitations %}
{% for item in limitations %}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

### LLM 推断限制

{% if inferred_limits %}
{% for item in inferred_limits %}
- {{ item.text }}{% if item.basis %} (basis: {{ item.basis }}){% endif %}{% if item.locator %} ({{ item.locator }}){% endif %}
{% endfor %}
{% else %}
- none
{% endif %}

### 适用机会与边界

{% if applicability_limits %}
{% for item in applicability_limits %}
- {{ item }}
{% endfor %}
{% else %}
- none
{% endif %}

---

Tags: {{ note_labels | join(', ') }}
```

- [ ] **Step 6: Run the targeted renderer tests and verify GREEN**

Run the same command from Task 1 Step 6.

Expected:

```text
5 passed
```

## Task 3: Update CLI Validation Tests and Keep Audit Gates Intact

**Files:**
- Modify: `tests/test_note.py`
- Modify: `tests/test_cli_note.py`
- Modify: `src/zotero_paperread/note.py` only if tests expose a regression

- [ ] **Step 1: Update validate-note expectations**

Replace the old validation assertion in `tests/test_note.py` that expects `missing_section: 7. 术语与检索` with:

```python
def test_validate_note_reports_missing_new_sections_and_forbidden_old_sections() -> None:
    old_note = """# [Codex Summary] Old - 2026-04-23

## 0. 阅读结论

## 1. 论文主张

## 2. 方法与设计

## 3. 结果可信度

## 4. 图表导读

## 5. 边界与机会

## 6. 我能怎么用

## 7. 术语与检索

---

Tags: codex-summary, paper-summary
"""

    errors = validate_note(old_note)

    assert "missing_section: 1. 速读信息" in errors
    assert "missing_section: 3. 方法与设计" in errors
    assert "forbidden_section: 3. 结果可信度" in errors
    assert "forbidden_section: 6. 我能怎么用" in errors
    assert "forbidden_section: 7. 术语与检索" in errors
```

- [ ] **Step 2: Update CLI render assertions**

In `tests/test_cli_note.py`, replace assertions that require `## 7. 术语与检索` with:

```python
assert "## 1. 速读信息" in note
assert "## 5. 边界与机会" in note
assert "## 7. 术语与检索" not in note
assert "可信状态" not in note
```

For CLI tests that assert validation output, expect new sections and forbidden old sections:

```python
assert "missing_section: 1. 速读信息" in result.output
assert "forbidden_section: 3. 结果可信度" in result.output
```

- [ ] **Step 3: Keep trusted-summary gate expectations unchanged**

Confirm tests still prove these fields are required in `summary.json`:

```python
def test_validate_trusted_summary_still_requires_audit_only_fields() -> None:
    summary = {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS, **LEARNING_FIELDS}
    summary["limitations"] = []
    summary["follow_up_keywords"] = []

    errors = note_module.validate_trusted_summary(summary)

    assert "limitations must contain at least one item" in errors
    assert "follow_up_keywords must contain at least one item" in errors
```

If this test already exists under a different name, update its expected rendered headings only; do not remove the audit-field assertions.

- [ ] **Step 4: Run note and CLI tests**

Run:

```bash
uv run pytest tests/test_note.py tests/test_cli_note.py -q
```

Expected:

```text
passed
```

## Task 4: Update README and Skill Documentation

**Files:**
- Modify: `README.md`
- Modify: `skills/zotero-paper-summary/SKILL.md`
- Modify: `tests/test_default_workflow_docs.py`

- [ ] **Step 1: Update README verify command**

Replace the README `verify-zotero-note` example with:

```bash
uv run zotero-paperread verify-zotero-note <note_key> \
  --expected-parent <item_key> \
  --expected-title "<payload noteTitle>" \
  --required-heading "0. 阅读结论" \
  --required-heading "1. 速读信息" \
  --required-heading "2. 论文主张" \
  --required-heading "3. 方法与设计" \
  --required-heading "4. 图表导读" \
  --required-heading "5. 边界与机会" \
  --forbidden-heading "3. 结果可信度" \
  --forbidden-heading "6. 我能怎么用" \
  --forbidden-heading "7. 术语与检索" \
  --forbidden-heading "9. 元数据" \
  --forbidden-heading "10. 证据链附录" \
  --forbidden-heading "11. 补充优化记录" \
  --expected-tag codex-summary \
  --expected-tag paper-summary \
  --expected-content-sha256 <payload required_readback_checks.contentSha256> \
  --min-content-length <payload required_readback_checks.contentLengthAtLeast>
```

- [ ] **Step 2: Update README layout prose**

Replace the rendered-note paragraph with:

```markdown
The rendered note is a compact 0-5 reading card. It opens with `## 0. 阅读结论`, a table containing the 30-second conclusion, main risk, and reading decision. `## 1. 速读信息` is a second table containing paper type, research object, core problem, core method, and core result. The body then follows `## 2. 论文主张`, `## 3. 方法与设计`, `## 4. 图表导读`, and `## 5. 边界与机会`. Trust status, quality score, key result tables, baseline/comparison notes, result evidence notes, concept cards, follow-up keywords, metadata, evidence chains, review status, and improvement notes remain in JSON artifacts and gate reports instead of being rendered as Zotero note sections.
```

- [ ] **Step 3: Update `skills/zotero-paper-summary/SKILL.md` rendered layout list**

Replace the old rendered-layout list with:

```markdown
当前 Zotero note 正文只渲染 0-5：

1. `0. 阅读结论`：表格展示 `30 秒结论`、`主要风险`、`阅读决策`。不要渲染 `trust_status`。
2. `1. 速读信息`：表格展示 `论文类型`、`研究对象`、`核心问题`、`核心方法`、`核心结果`。
3. `2. 论文主张`：`背景问题`、`已有缺口`、`本文切入点 + 贡献`。
4. `3. 方法与设计`：方法总览、模块拆解、Workflow、技术细节。
5. `4. 图表导读`：只保留一张表，列为 `图`、`图像抽取质量`、`图片描述内容`。
6. `5. 边界与机会`：作者明示局限、LLM 推断限制、适用机会与边界。
7. trailing `Tags:` 行保持现有标签策略，至少包含 `codex-summary, paper-summary`。

`key_results_table`、`baseline_or_comparison`、`result_evidence_notes`、`concept_cards`、`follow_up_keywords`、`limitations`、`trust_status` 继续写入 `summary.json`，用于审查、lint、gate 和未来扩展，但不渲染到 note 正文。
```

- [ ] **Step 4: Update skill write verification command**

Replace the `verify-zotero-note` command in `skills/zotero-paper-summary/SKILL.md` with the same required/forbidden heading set from Step 1.

- [ ] **Step 5: Update documentation tests**

In `tests/test_default_workflow_docs.py`, update tests that scan README/skill docs so they require:

```python
assert "0. 阅读结论" in text
assert "1. 速读信息" in text
assert "5. 边界与机会" in text
assert "--required-heading \"1. 速读信息\"" in text
assert "--forbidden-heading \"3. 结果可信度\"" in text
assert "--forbidden-heading \"6. 我能怎么用\"" in text
assert "--forbidden-heading \"7. 术语与检索\"" in text
```

Do not assert that old section names are absent globally from README/skill docs, because they may appear in forbidden-heading examples or migration notes.

- [ ] **Step 6: Run documentation tests**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected:

```text
passed
```

## Task 5: Run End-to-End Dry-Run Verification

**Files:**
- Read: `runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/metadata.json`
- Read: `runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/summary.json`
- Write generated outputs in the same run directory only through project CLI commands.

- [ ] **Step 1: Regenerate note Markdown and HTML from the existing run**

Run:

```bash
RUN_DIR="runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries"
uv run zotero-paperread finalize-note "$RUN_DIR/metadata.json" "$RUN_DIR/summary.json" \
  --output "$RUN_DIR/note.md" \
  --html-output "$RUN_DIR/note.html"
```

Expected:

```text
Wrote note Markdown: runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/note.md
note_valid
Wrote note HTML: runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/note.html
```

The command should exit with status 0 and update `note.md` plus `note.html`.

- [ ] **Step 2: Validate the generated note**

Run:

```bash
RUN_DIR="runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries"
uv run zotero-paperread validate-note "$RUN_DIR/note.md"
```

Expected:

```text
note_valid
```

- [ ] **Step 3: Spot-check rendered structure**

Run:

```bash
RUN_DIR="runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries"
rg -n "## [0-9]\\. |可信状态|结果可信度|我能怎么用|术语与检索|图表索引|展开图表" "$RUN_DIR/note.md"
```

Expected headings should include only:

```text
## 0. 阅读结论
## 1. 速读信息
## 2. 论文主张
## 3. 方法与设计
## 4. 图表导读
## 5. 边界与机会
```

The command must not show `可信状态`, `结果可信度`, `我能怎么用`, `术语与检索`, `图表索引`, or `展开图表`.

- [ ] **Step 4: Confirm HTML still contains tables**

Run:

```bash
RUN_DIR="runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries"
rg -n "<table>|<th>项目</th>|<th>图</th>" "$RUN_DIR/note.html"
```

Expected: at least one `<table>` match, one `项目` table header match, and one `图` table header match.

- [ ] **Step 5: Confirm HTML hides forbidden rendered content**

Run:

```bash
RUN_DIR="runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries"
uv run python - <<'PY'
from pathlib import Path

run_dir = Path("runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries")
html = (run_dir / "note.html").read_text(encoding="utf-8")
forbidden = ["可信状态", "trust_status", "结果可信度", "我能怎么用", "术语与检索", "潜在 gap"]
present = [item for item in forbidden if item in html]
if present:
    raise SystemExit(f"forbidden rendered content present: {present}")
PY
```

Expected:

```text
no stdout; exit status 0
```

## Task 6: Full Verification and Commit

**Files:**
- Verify all modified files.
- Commit after all tests pass.

- [ ] **Step 1: Run project verification commands**

Run:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

Expected:

```text
passed
```

`zotero-paperread --help` should exit 0 and print command help. `extract-pdf` should exit 0 and create `/tmp/zotero-paperread-extract.json`.

- [ ] **Step 2: Check diff hygiene**

Run:

```bash
git diff --check
git status --short --branch --untracked-files=all
```

Expected:

```text
no stdout; exit status 0
```

`git diff --check` should print nothing and exit 0. `git status` should show only the intentional files from this plan plus generated run `note.md` and `note.html` if Task 5 updated them.

- [ ] **Step 3: Commit**

Run:

```bash
git add templates/zotero_note.md.j2 src/zotero_paperread/note.py tests/test_note.py tests/test_cli_note.py tests/test_default_workflow_docs.py README.md skills/zotero-paper-summary/SKILL.md docs/superpowers/specs/2026-06-29-zotero-note-template-redesign.md docs/superpowers/plans/2026-06-29-zotero-note-template-redesign.md
git add runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/note.md runs/2026-06-23/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/note.html
git commit -m "feat: shorten zotero note template"
```

Expected:

```text
commit output starts with "[main " and includes "feat: shorten zotero note template"
```

Do not push without explicit user confirmation.

## Self-Review

- Spec coverage:
  - 0/1 tables are covered by Task 1 and Task 2.
  - Removed rendered `结果可信度`, `我能怎么用`, `术语与检索`, `trust_status`, `potential_gaps`, and audit-only fields are covered by Task 1, Task 2, Task 3, Task 4, and Task 5.
  - `图表导读` single-table behavior, low-quality caveat synthesis, and no-figures fallback are covered by Task 1 and Task 2.
  - `limitations` compatibility and unchanged write gate are covered by Task 3.
  - README and skill command drift are covered by Task 4.
  - Dry-run and full verification are covered by Task 5 and Task 6.
- Placeholder scan:
  - No disallowed placeholder markers or unbounded edge-handling instructions remain.
  - Every code-changing step includes concrete code or concrete replacement text.
- Type consistency:
  - Template uses context keys that already exist in `render_note` except `figure_description`.
  - Task 2 defines `figure_description` before the template uses it.
  - Validation uses plain string lists and the existing `validate_note` return type.
