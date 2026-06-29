# Zotero Note Template Redesign

**Status:** Design approved; implementation not started.

**Date:** 2026-06-29

## Summary

Redesign the rendered Zotero child note from the current 0-7 reading-thread layout into a shorter 0-5 reading card. The goal is to reduce rendered note noise while preserving the existing extraction, review, evidence, and write-gate discipline.

This is a render-layer and validation-layer redesign. It must not weaken `summary.json` generation, trusted evidence checks, Zotero write gates, or readback verification.

## First-Principles Goal

The Zotero note should help the reader recover judgment quickly when reopening a paper:

1. Is this paper worth reading?
2. What does it claim, and how is the work designed?
3. What are the main figures telling me?
4. Where should I be careful when citing or reusing it?

The final note should not display every useful extraction field. Some fields are valuable for generation quality, review, and audit, but create noise when rendered into Zotero.

## Approved Direction

Use the following final rendered structure:

```text
0. 阅读结论
1. 速读信息
2. 论文主张
3. 方法与设计
4. 图表导读
5. 边界与机会
---
Tags: ...
```

Remove these rendered sections from future notes:

- `3. 结果可信度`
- `6. 我能怎么用`
- `7. 术语与检索`
- `9. 元数据`
- `10. 证据链附录`
- `11. 补充优化记录`

These removed sections do not imply deleted data fields. The corresponding data remains available in `summary.json`, `review.json`, `gate-report.json`, and other run artifacts.

## Rendered Note Design

### `0. 阅读结论`

Purpose: first-screen reading decision.

Render as a two-column Markdown table:

| 项目 | 内容 |
| --- | --- |
| 30 秒结论 | `tldr` if present, otherwise `one_sentence_summary` |
| 主要风险 | `main_risk_short` |
| 阅读决策 | `reading_decision` display label |

Rules:

- Do not render `trust_status` in the note body.
- Do not render `relevance_to_user` in this section.
- Keep `主要风险` to the most important evidence or extrapolation risk.

### `1. 速读信息`

Purpose: compact paper identity and result snapshot.

Render as a two-column Markdown table:

| 项目 | 内容 |
| --- | --- |
| 论文类型 | `paper_type` display label |
| 研究对象 | `research_object` |
| 核心问题 | `research_question_short` |
| 核心方法 | `core_method_short` |
| 核心结果 | `core_result_short` |

Rules:

- Do not render `quality_score`.
- Do not expand detailed evidence, baselines, or result locators here.
- If a short field is absent, use existing safe fallbacks.

### `2. 论文主张`

Purpose: explain the paper's argument without repeating the quick tables.

Rendered subsections:

```text
背景问题
已有缺口
本文切入点 + 贡献
```

Field mapping:

- `background_problem`
- `existing_gap`
- `paper_entry_point`
- `contributions`
- fallback contribution text: `one_sentence_summary`

Rules:

- Merge the old "本文切入点" and "一句话贡献" into one subsection.
- If `contributions` contains items, render them as a short bullet list after `paper_entry_point`.
- If `contributions` is empty, render `one_sentence_summary` as the contribution text.
- Do not render `abstract_translation` in the final Zotero note.

### `3. 方法与设计`

Purpose: preserve the current method-reading framework.

Rendered subsections:

```text
方法总览
模块拆解
Workflow
技术细节
```

Field mapping:

- `method_overview`
- `method_modules`
- `workflow_steps`
- `technical_details`

Rules:

- Keep the current `method_modules` table: `模块 / 输入 / 目标 / 输出 / 作用`.
- Render `Workflow` only when `workflow_steps` is non-empty.
- Keep `technical_details` focused on details needed to understand or reproduce the method.
- Do not turn this into a second result section.

### `4. 图表导读`

Purpose: make figures scannable without duplicating result sections.

Render as one table only:

| 图 | 图像抽取质量 | 图片描述内容 |
| --- | --- | --- |
| Fig. 1 | `image_quality` | synthesized description |
| Fig. 2 | `image_quality` | synthesized description |

Field mapping:

- `key_figures[].figure_id`
- `key_figures[].image_quality`
- `key_figures[].figure_quality_note`
- `key_figures[].caption`
- `key_figures[].why_it_matters`
- `key_figures[].analysis`

`图片描述内容` must synthesize:

1. what the figure shows;
2. which core claim it supports;
3. if extraction quality is low, that the interpretation is based only on text/caption evidence.

Rules:

- Remove the old `图表总览`, `图表索引`, and per-figure expanded subsections.
- Do not render `priority_score`, `evidence_level`, or page number unless they are already naturally included in the description.
- If `figure_context.md` has low confidence or image quality warnings, the row must be conservative.

### `5. 边界与机会`

Purpose: keep limitations and applicability in one place.

Rendered subsections:

```text
作者明示局限
LLM 推断限制
适用机会与边界
```

Field mapping:

- `author_stated_limitations`
- `inferred_limits`
- `applicability_limits`
- optional support from `potential_gaps` only when it improves boundary clarity

Rules:

- Use the heading `LLM 推断限制`, not `Codex 推断限制`.
- Do not present inferred limits as author-stated claims.
- `适用机会与边界` should answer where the paper's claims/design lessons apply and where they should not be extrapolated.
- Do not absorb the old `我能怎么用` section into this section.

### `Tags`

Purpose: keep Zotero note tagging behavior unchanged.

Render:

```text
---

Tags: codex-summary, paper-summary, ...
```

Field mapping:

- fixed labels: `codex-summary`, `paper-summary`
- inferred labels: normalized `note_labels`, up to the existing project limit

## Audit-Only Fields

Continue generating these fields in `summary.json`, but do not render them into the final Zotero note body:

- `abstract_translation`
- `ai4s_relevance`
- `baseline_or_comparison`
- `concept_cards`
- `evidence_quality_summary`
- `evidence_summary`
- `experiments`
- `extraction_warnings`
- `follow_up_keywords`
- `follow_up_questions`
- `key_results_table`
- `quality_score`
- `result_evidence_notes`
- `review_issues`
- `review_status`
- `transferable_insight`
- `trust_rationale`
- `trust_status`
- `workflow_lessons`

Rationale:

- These fields improve generation, review, and future extensibility.
- They are useful in run artifacts and gates.
- Rendering them all into Zotero makes the note harder to scan.

## Validation Rules

`validate-note` should require the new headings:

```text
0. 阅读结论
1. 速读信息
2. 论文主张
3. 方法与设计
4. 图表导读
5. 边界与机会
```

Validation and write-readback checks should forbid old rendered headings:

```text
3. 结果可信度
6. 我能怎么用
7. 术语与检索
9. 元数据
10. 证据链附录
11. 补充优化记录
```

Important nuance:

- The old `3. 结果可信度` heading is forbidden as a rendered section name.
- The data that powered it is still generated and validated.
- New `3. 方法与设计` is the only allowed section 3 heading.

## Gate Requirements

The shorter rendered note must not relax any write gate.

Keep these behaviors:

1. `validate-trusted-summary` still requires trusted summary fields, including `evidence_summary`.
2. `lint-summary` still validates canonical locators in trusted evidence fields.
3. `review.json` still records review status, issues, and whether improvement is needed.
4. `prepare-write-candidate` still runs live note refresh, version suffix calculation, note finalization, preview generation, `gate-run`, and `prepare-write-payload`.
5. Real Zotero writes still use only `zotero-mcp write_note(action="create", ...)` with `note.html` content.
6. `verify-zotero-note` still checks parent, title, required headings, forbidden headings, tags, minimum content length, and canonical `contentSha256`.

Core principle: the note body gets shorter; the audit trail does not get weaker.

## Compatibility Requirements

Existing `summary.json` artifacts should still render through safe fallbacks:

- `tldr` missing -> `one_sentence_summary`
- `research_question_short` missing -> `research_question`
- `core_method_short` missing -> `method`
- `core_result_short` missing -> `one_sentence_summary`
- `method_overview` missing -> `method`
- `key_figures[].figure_quality_note` missing -> `image_quality`
- `contributions` missing or empty -> `one_sentence_summary`

Old Zotero notes are not automatically migrated. Historical note migration remains a separate explicit workflow.

## Implementation Boundaries

In scope for the later implementation plan:

- Update `templates/zotero_note.md.j2`.
- Update renderer cleaning helpers only where necessary for the new table rows and figure description synthesis.
- Update required and forbidden heading checks.
- Update tests to expect the 0-5 rendered layout.
- Update README / repo skill docs that describe the current note layout.
- Regenerate a dry-run note from an existing run artifact to verify the new layout.

Out of scope:

- Changing PDF extraction.
- Changing `summary.json` schema by deleting fields.
- Weakening trusted summary validation.
- Changing Zotero write semantics.
- Automatically migrating existing Zotero notes.
- Adding a new database, index, or ResearchWiki layer.

## Testing Requirements

Implementation should verify:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

Additional targeted checks:

1. Render a note from an existing run artifact and confirm only the new 0-5 sections appear.
2. Confirm `note.html` table output is valid and readable.
3. Confirm `validate-note` passes with the new headings.
4. Confirm `gate-run` and `prepare-write-candidate` do not depend on old required headings.
5. Confirm `verify-zotero-note` can be called with the new required headings and old forbidden headings.
6. Confirm `summary.json` still retains audit-only fields such as `key_results_table`, `evidence_summary`, `concept_cards`, and `follow_up_keywords`.

## User-Approved Decisions

1. Use the "方案 B" design: keep the current method/design framework but make the final Zotero note much shorter.
2. Remove rendered `结果可信度`; keep `key_results_table`, `baseline_or_comparison`, and `result_evidence_notes` in `summary.json`.
3. Remove rendered `我能怎么用`; do not absorb it into another section.
4. Remove rendered `术语与检索`; keep `concept_cards` and `follow_up_keywords` in `summary.json`.
5. Render both `0. 阅读结论` and `1. 速读信息` as tables.
6. Render `4. 图表导读` as one table only.
7. Do not render `trust_status` in the note body.
8. Use `LLM 推断限制` as the inferred-limits subsection heading.

## Self-Review

- No placeholders remain.
- Scope is limited to note rendering, validation, docs, tests, and one dry-run verification path.
- The design preserves evidence safety by keeping audit-only fields and trusted evidence gates.
- The design explicitly avoids historical Zotero note migration.
- The design does not contradict the project write boundary: real Zotero writes still require explicit user intent and `zotero-mcp write_note`.
