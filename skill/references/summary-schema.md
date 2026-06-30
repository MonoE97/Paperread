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
