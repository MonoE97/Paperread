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
- `experiments`
- `ai4s_relevance`
- `key_points`
- `contributions`
- `limitations`
- `follow_up_keywords`
- `trust_rationale`
- `evidence_summary`

`evidence_summary` entries must cite `context.md` or `figure_context.md` locators.

## `review.json`

Minimum fields:

- `review_status`
- `needs_improvement`
- `review_issues`
- `trust_status_recommendation`
- `improvement_requests`

After writing `review.json`, run `apply-review` before linting and trusted-summary validation.
