# Zotero Note Reading Layout Redesign

**Status:** Approved for implementation.

**Date:** 2026-06-18

## Summary

Redesign the rendered Zotero reading note from an audit-heavy 0-11 outline into a cleaner 0-7 reading outline. The goal is to reduce repetition, remove rendered metadata/audit appendices, and make the note read as a paper-reading workflow: decide whether to read, understand the paper's claim, inspect methods and evidence, read figures, identify boundaries, and turn the paper into personal research input.

This is a render-layer redesign. It must not delete trusted summary fields, weaken gates, or change Zotero write semantics.

## Problems Found in the Current Scheme

1. **Audit content leaks into the reading note.** Sections `9. 元数据`, `10. 证据链附录`, and `11. 补充优化记录` are useful for reproducibility but not useful while reading in Zotero. Zotero already owns metadata, and run artifacts already retain evidence/review details.
2. **Core results repeat too many times.** In the Polyanion note, the same values such as `1.5 mS cm^-1`, `2.4 wt% Li`, and NCM811 cycling performance appear in the quick decision, result table, baseline/evidence notes, figure explanation, and evidence appendix.
3. **Figure sections partly duplicate result sections.** Figure explanations should tell the reader what question each figure answers and how it supports the argument, not restate the full result table.
4. **Limitations, applicability, and gaps are split across too many sections.** This makes caveats such as the Li-In / Li6PS5Cl cell architecture appear repeatedly instead of forming one clear "where this does and does not apply" section.
5. **Visual companion artifacts are not ignored.** `.superpowers/brainstorm/...` is local preview state and should not appear in `git status`.

## Approved Direction

Use the "reading thread" layout:

```text
0. 阅读结论
1. 论文主张
2. 方法与设计
3. 结果可信度
4. 图表导读
5. 边界与机会
6. 我能怎么用
7. 术语与检索
---
Tags: ...
```

Remove these rendered sections:

- `9. 元数据`
- `10. 证据链附录`
- `11. 补充优化记录`

These fields remain in JSON artifacts and gates. They are not deleted from the schema.

## Section Design

### `0. 阅读结论`

Purpose: one-screen reading decision.

Fields:

- `tldr` or `one_sentence_summary`
- `reading_decision`
- `relevance_to_user`
- `trust_status`
- `main_risk_short`
- `recommended_sections`
- `recommended_figures`

Rule: do not fully expand numerical evidence here. One short value proposition is acceptable, but detailed numbers belong in `3. 结果可信度`.

### `1. 论文主张`

Purpose: explain what problem the paper is trying to solve.

Fields:

- `background_problem`
- `existing_gap`
- `paper_entry_point`
- `one_sentence_summary`
- `abstract_translation`

Rule: contribution is rendered here as part of the paper's claim, not repeated later as a separate contribution list.

### `2. 方法与设计`

Purpose: explain how the paper is built.

Fields:

- `method_overview`
- `method_modules`
- `workflow_steps`
- `technical_details`

Rule: method details should not become a second result section. Keep numerical outcomes out unless they define experimental settings.

### `3. 结果可信度`

Purpose: the single home for key numbers, baselines, comparisons, locators, and evidence quality.

Fields:

- `key_results_table`
- `baseline_or_comparison`
- `result_evidence_notes`
- `evidence_quality_summary`

Rule: this is the only section that fully expands core results and source locators. Other sections can refer to results briefly but should not duplicate the result table.

### `4. 图表导读`

Purpose: explain how to read the paper's important figures.

Fields:

- `figure_overview`
- `key_figures`

Rule: figures explain role and reading order, not a second copy of the result table. If figure extraction is weak, say that clearly and keep the analysis text/caption-grounded.

### `5. 边界与机会`

Purpose: combine limits, applicability, and research gaps into one logical chain.

Fields:

- `author_stated_limitations`
- `inferred_limits`
- `applicability_limits`
- `potential_gaps`

Rule: order the section as "what the authors explicitly limit", "what Codex infers", "where it applies", and "what this opens up". Do not repeat the same caveat across all four lists unless each occurrence adds a distinct function.

### `6. 我能怎么用`

Purpose: turn the paper into personal research input.

Fields:

- `transferable_insight`
- `workflow_lessons`
- `follow_up_questions`

Rule: this section may be more opinionated than earlier sections, but it must not contradict the evidence caveats.

### `7. 术语与检索`

Purpose: lightweight lookup tail.

Fields:

- `concept_cards`
- `follow_up_keywords`
- `note_labels`

Rule: keep this compact. Render at most the cleaned concept cards already accepted by the renderer and avoid turning the note tail into a long glossary.

## Audit-Only Fields

These fields stay available for validation, gate checks, and artifact review, but are not rendered as dedicated note sections:

- Zotero/item metadata: `key`, `title`, `creators`, `date`, `doi`, `url`, `zotero_url`, `generated_date`
- `evidence_summary`
- `review_status`
- `improvement_status`
- `improvement_notes`

The canonical audit path remains the run directory:

- `item-details.json`
- `extract.json`
- `context.md`
- `section_context.md`
- `figures.json`
- `figure_context.md`
- `summary.json`
- `review.json`
- `gate-report.json`
- `write-payload.json`

## Compatibility Requirements

1. Existing `summary.json` files must still render if they include the current schema.
2. `evidence_summary` must remain required/validated where gates currently require it.
3. `validate-summary-json`, `apply-review`, `lint-summary`, `validate-trusted-summary`, `gate-run`, and `prepare-write-payload` must continue to work.
4. Zotero writes must still use generated `note.html`.
5. Old Zotero notes must not be overwritten automatically. Rewriting old notes remains an explicit per-paper action.
6. `.superpowers/` must be ignored so visual companion state does not pollute git status.

## Testing Requirements

1. Renderer tests must prove that the new note contains the 0-7 sections.
2. Renderer tests must prove that rendered notes do not contain `## 9. 元数据`, `## 10. 证据链附录`, or `## 11. 补充优化记录`.
3. CLI tests must expect the new section names and still confirm Markdown/HTML generation.
4. Gate/lint tests must still prove `evidence_summary` is validated even though it is no longer rendered.
5. Regenerating the Polyanion note from its existing run artifacts must produce the new 0-7 reading layout and no rendered audit sections.

## Out of Scope

- Deleting fields from `summary.json`
- Redesigning extraction, table detection, or figure extraction
- Changing Zotero MCP write behavior
- Automatically migrating existing Zotero notes
- Creating a ResearchWiki filesystem layer

## Self-Review

- No placeholders remain.
- Scope is a single implementation plan: note render layout, validation/tests, skill documentation, and one example regeneration/write.
- The design preserves evidence safety: `evidence_summary` remains gate-visible even when no longer rendered.
- The design explicitly handles the visual companion `.superpowers/` git noise found during brainstorming.
