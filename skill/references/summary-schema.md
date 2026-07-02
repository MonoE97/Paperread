# Summary And Review Shape

This reference lists the minimum fields the agent must write before gates can pass.

## `summary.json`

Required write-ready fields:

- `paper_type`: one of `research_article`, `review`, `perspective`, `benchmark`, `method_paper`, `dataset_paper`, `theory_paper`
- `trust_status`: usually `usable_with_caveats`; use `trusted` only with strong extraction evidence
- `review_status`: `passed` or `passed_with_caveats` after review is applied
- `improvement_status`: `not_needed` or `completed`
- `one_sentence_summary`
- `abstract_translation`
- `research_question`
- `method`
- `method_modules`
- `workflow_steps`
- `technical_details`
- `experiments`
- `ai4s_relevance`
- `key_points`
- `key_figures`
- `contributions`
- `limitations`
- `author_stated_limitations`
- `inferred_limits`
- `applicability_limits`
- `follow_up_keywords`
- `trust_rationale`
- `evidence_summary`

`evidence_summary` entries must use canonical locators: `context.md page <N>`, `context.md page <N> section <Section Name>`, `context.md page <N> section <Section Name> table_candidate <N>`, or `figure_context.md <figure_id>`. Bare `context.md` / `figure_context.md`, prose locators such as `page 3 method section`, `section_context.md`, and secondary context paths are invalid.

Rendered note prose is Chinese-first. Paper titles, author names, institution names, formulas, method names, abbreviations, units, evidence locators, code-like keys, and tag keys may remain in English.

## `review.json`

Minimum fields:

- `review_status`
- `needs_improvement`
- `review_issues`
- `trust_status_recommendation`
- `improvement_requests`

After writing `review.json`, run `apply-review` before linting and trusted-summary validation.

## Minimal write-ready example

Use this as a shape reference, then replace every prose field with paper-specific Chinese content and evidence-backed claims:

```json
{
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
    "运行 review、lint 和 trusted-summary 门禁。"
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
