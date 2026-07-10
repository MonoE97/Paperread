# Summary And Review Shape — Paper Reader 2.0 Target Contract

This reference defines the binding Paper Reader 2.0 target schemas `paper_reader.summary.v2`, `paper_reader.review.v2`, and immutable `paper_reader.review-package.v2`. All use strict Pydantic v2 models with `extra=forbid`; unknown fields, implicit coercion, V1/unversioned artifacts and schema guessing are rejected. V1 artifacts are historical-only and fail with `unsupported_run_schema` before locks or mutation.

The summary separates fields that block review sealing from fields that improve the rendered note. The agent should fill both layers for high-quality notes, but only `gate-required` fields are mandatory for sealing. Review validation resolves every render fallback before Chinese-first prose lint, locator validation and sealing, so omitted optional fields cannot bypass checks.

## `summary.json`

- `schema_version` must be exactly `paper_reader.summary.v2`.
- `summary_id` is the immutable summary identity.
- `run_id` binds the summary to one `paper_reader.run.v2`.
- `created_at` is the RFC3339 UTC creation time.
- `evidence_digest` is the canonical SHA-256 of the evidence manifest used by the summary.
- The complete summary content is hashed canonically and bound into the sealed review package. Any post-review change invalidates sealing and requires a new review.

## `gate-required`

- `paper_type`: one of `research_article`, `review`, `perspective`, `benchmark`, `method_paper`, `dataset_paper`, `theory_paper`
- `trust_status`: usually `usable_with_caveats`; use `trusted` only with strong extraction evidence
- `review_status`: `passed` or `passed_with_caveats` after review is applied
- `improvement_status`: `not_needed` or `completed`
- `trust_rationale`
- `one_sentence_summary`
- `abstract_translation`
- `research_question`
- `method`
- `experiments`
- `ai4s_relevance`
- `key_points`
- `contributions`
- `limitations`
- `follow_up_keywords`
- `evidence_summary`

`evidence_summary` entries must use canonical locators: `context.md page <N>`, `context.md page <N> section <Section Name>`, `context.md page <N> section <Section Name> table_candidate <N>`, or `figure_context.md <figure_id>`. Bare `context.md` / `figure_context.md`, prose locators such as `page 3 method section`, `section_context.md`, and secondary context paths are invalid.

Rendered note prose is Chinese-first. Paper titles, author names, institution names, formulas, method names, abbreviations, units, evidence locators, code-like keys, and tag keys may remain in English.

## `quality-recommended`

These fields are rendered prominently by the note template or make the review more useful. In other words, missing quality-recommended fields do not by themselves block review sealing, but their resolved fallbacks can render as `未知`, `无`, or weaker text and are still subject to Chinese-first lint and evidence checks:

- `tldr`: preferred source for the `30 秒结论` row; falls back to `one_sentence_summary`
- `research_object`
- `research_question_short`: falls back to `research_question`
- `core_method_short`: falls back to `method`
- `core_result_short`: falls back to `one_sentence_summary`
- `main_risk_short`: falls back to extraction warnings, review issues, or `无`
- `reading_decision`
- `background_problem`
- `existing_gap`
- `paper_entry_point`
- `method_overview`: falls back to `method`
- `method_modules`
- `workflow_steps`
- `technical_details`
- `key_figures`
- `author_stated_limitations`: falls back to `limitations` in the rendered note
- `inferred_limits`
- `applicability_limits`
- `note_labels`

## `review.json`

- `schema_version` must be exactly `paper_reader.review.v2`.

Minimum fields:

- `schema_version`: exactly `paper_reader.review.v2`
- `review_id`: immutable review identity
- `run_id`: the reviewed V2 run identity
- `created_at`: RFC3339 UTC creation time
- `summary_sha256`: canonical SHA-256 of the exact reviewed summary
- `evidence_digest`: canonical SHA-256 of the reviewed evidence manifest
- `review_status`
- `needs_improvement`
- `review_issues`
- `trust_status_recommendation`
- `improvement_requests`

`uv run paper_reader review validate <run_dir>` validates summary, review, canonical evidence membership and fully resolved render prose. `uv run paper_reader review seal <run_dir>` may then publish one immutable `paper_reader.review-package.v2` that binds exact summary/review/evidence hashes. Failed review, changed summary hash, invalid locator or rendered English prose blocks sealing and candidate construction.

## Minimal write-ready example

Use this as a shape reference, then replace every prose field with paper-specific Chinese content and evidence-backed claims:

```json
{
  "schema_version": "paper_reader.summary.v2",
  "summary_id": "summary_example_001",
  "run_id": "run_example_001",
  "created_at": "2026-07-10T09:30:00Z",
  "evidence_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "paper_type": "method_paper",
  "trust_status": "usable_with_caveats",
  "review_status": "passed_with_caveats",
  "improvement_status": "completed",
  "trust_rationale": "正文抽取覆盖摘要、方法和结果页；图表证据需要结合图注复核。",
  "one_sentence_summary": "本文提出一个用于材料论文阅读的结构化证据整理流程。",
  "abstract_translation": "摘要说明该方法把论文正文、图表和人工复核结果组织成可追溯笔记。",
  "research_question": "如何让单篇论文阅读笔记同时保留结论、证据和限制条件？",
  "method": "方法先抽取 PDF 正文和图表，再生成结构化 summary，并通过 review 与 lint 门禁。",
  "method_modules": [
    {
      "name": "PDF extraction",
      "input": "论文 PDF",
      "target": "正文与页码证据",
      "output": "context.md",
      "role": "提供主要证据来源"
    }
  ],
  "workflow_steps": [
    "抽取 PDF 正文和图表候选。",
    "基于 context.md 写 summary.json。",
    "运行 review validate、中文 lint 和 review seal 门禁。"
  ],
  "technical_details": ["证据 locator 使用 canonical 格式。"],
  "experiments": "示例论文用正文页和图表候选验证笔记结构。",
  "ai4s_relevance": "该流程适合材料与 AI4S 论文的快速归档和复盘。",
  "key_points": ["结构化总结", "证据 locator", "写入前门禁"],
  "key_figures": [
    {
      "figure_id": "fig_p1_1",
      "caption": "Figure 1. Workflow overview.",
      "analysis": "图 1 展示从 PDF 抽取到笔记生成的流程。",
      "why_it_matters": "它支撑方法模块之间的顺序关系。",
      "image_quality": "ok",
      "evidence_level": "caption_only"
    }
  ],
  "contributions": ["把阅读结论和证据链放在同一份结构化笔记中。"],
  "limitations": ["示例结构不能替代对具体论文的人工判断。"],
  "author_stated_limitations": [
    {
      "text": "作者未在示例中讨论所有失败模式。",
      "source_type": "author_stated",
      "locator": "context.md page 1 section Abstract"
    }
  ],
  "inferred_limits": [
    {
      "text": "如果 PDF 抽取质量差，证据定位会变弱。",
      "source_type": "inferred",
      "basis": "抽取依赖页面文本质量。",
      "locator": "context.md page 1 section Abstract"
    }
  ],
  "applicability_limits": ["适用于单篇论文，不适合直接代表综述级结论。"],
  "follow_up_keywords": ["paper reading", "evidence locator"],
  "evidence_summary": [
    {
      "claim": "该流程把正文证据和笔记结论显式连接。",
      "evidence": [
        {
          "type": "text",
          "locator": "context.md page 1 section Abstract",
          "summary": "摘要描述了从论文抽取到结构化笔记的流程。"
        }
      ],
      "confidence": "medium"
    }
  ]
}
```

## Minimal review example

This example reviews the exact summary and evidence identities above:

```json
{
  "schema_version": "paper_reader.review.v2",
  "review_id": "review_example_001",
  "run_id": "run_example_001",
  "created_at": "2026-07-10T09:35:00Z",
  "summary_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "evidence_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "review_status": "passed_with_caveats",
  "needs_improvement": false,
  "review_issues": [
    {
      "severity": "medium",
      "issue": "图表证据仍需结合图注复核。",
      "suggested_fix": "在候选构建前复核关键图表定位。"
    }
  ],
  "trust_status_recommendation": "usable_with_caveats",
  "improvement_requests": []
}
```
