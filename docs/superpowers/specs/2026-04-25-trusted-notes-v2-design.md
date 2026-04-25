# Trusted Notes V2 Design

## Summary

This spec defines the next quality layer for the Zotero-first paper summary workflow. The goal is to move from "a useful structured summary" toward "a long-term trusted reading note" that the user can safely read before opening the full paper.

The design covers two phases:

1. **Phase 1: Trust and Evidence Minimum Layer**
   - classify paper type
   - assign a bounded trust status
   - attach evidence pointers to important claims
   - render a compact `## 可信度与证据` section in the final note

2. **Phase 2: Note Quality Review Layer**
   - review the generated note for unsupported claims, generic limitations, paper-type mismatch, and figure-analysis drift
   - emit a structured review result
   - use that result to downgrade trust status or block write-through when necessary

This spec does not implement section-by-section summarization, Better Notes graph integration, or relation graph construction. Those are later layers. This spec strengthens the credibility of each single-paper note first.

---

## Context

The current workflow already does the following well:

- finds a Zotero item through `zotero-mcp`
- extracts metadata and PDF text into a run directory
- extracts and ranks key figures
- asks Codex to produce a figure-aware Chinese research note
- validates the note structure
- writes a Zotero child note only when the user explicitly asks
- avoids duplicate analysis by stopping when an existing Codex summary note is detected
- renders a small set of normalized note labels at the end of the note

The remaining gap is trust. A note can be complete in structure while still being weak as a long-term research record. It may overstate contributions, miss a caveat, summarize from abstract-level language only, or treat background knowledge as if it were proven by the paper.

Trusted Notes V2 adds evidence and review discipline without making the workflow heavy.

---

## Goals

1. Make the note state how trustworthy it is.
2. Make important conclusions traceable to page-level or figure-level evidence.
3. Make paper type explicit so reviews, perspectives, theory papers, benchmarks, and research articles are not judged by the same template.
4. Add a second-pass quality review before writing to Zotero.
5. Keep the final note readable; do not expose bulky internal audit JSON unless needed.

## Non-Goals

1. No Better Notes API integration.
2. No graph or related-paper relation work.
3. No section-by-section summarization in this phase.
4. No external database.
5. No full citation extraction system.
6. No claim verification against sources outside the paper.

---

## Core Concepts

## Paper Type

Every summary should classify the analyzed paper into one of these values:

- `research_article`
- `review`
- `perspective`
- `benchmark`
- `method_paper`
- `dataset_paper`
- `theory_paper`
- `unknown`

The paper type affects how trust is judged.

Examples:

- A `review` is not penalized for lacking original experiments.
- A `theory_paper` is not penalized for lacking experimental figures.
- A `research_article` should identify method, evidence, results, and limitations.
- A `benchmark` should explain datasets, metrics, baselines, and evaluation scope.

## Trust Status

Every note receives exactly one trust status:

- `trusted`
- `usable_with_caveats`
- `metadata_only`
- `needs_manual_review`

### Meaning

`trusted`

The note has enough extracted text and figure or page evidence to support its major claims. It can be used as the first reading surface for the paper.

`usable_with_caveats`

The note is useful but should not be treated as fully reliable. Common causes include truncated extraction, weak figure coverage, missing experiment details, or review issues that are not fatal.

`metadata_only`

The workflow did not have enough full-text evidence. The note is based mostly on metadata, abstract, or limited text.

`needs_manual_review`

The note may contain unsupported or conflicting claims, or extraction quality was too weak. The user should open the paper before relying on the note.

## Evidence Summary

Evidence summary is a compact claim-to-evidence list. It does not need to quote the paper verbatim. It should mostly use paraphrased pointers.

Each evidence item points to one or more of:

- page number
- section name when detectable
- figure id from `figure_context.md`
- caption text or short caption paraphrase

Example:

```json
{
  "claim": "The proposed workflow uses a neural surrogate to accelerate inverse metasurface design.",
  "evidence": [
    {
      "type": "text",
      "locator": "page 3, Method section",
      "summary": "The method description explains the learned mapping from target response to structure parameters."
    },
    {
      "type": "figure",
      "locator": "p4-f1",
      "summary": "The framework figure shows the model loop and optimization path."
    }
  ],
  "confidence": "high"
}
```

## Note Quality Review

The review layer is a second pass over:

- `context.md`
- `figure_context.md`
- `summary.json`
- rendered `note.md`

It checks whether the note is faithful to the extracted evidence.

This review is not a peer review of the paper. It is a quality check on the generated note.

---

## Summary Contract Additions

The generated `summary.json` should add these fields:

```json
{
  "paper_type": "research_article",
  "trust_status": "trusted",
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
  "review_issues": []
}
```

## Field Rules

### `paper_type`

Must be one of the values listed in `Paper Type`.

If the source does not clearly identify the type, use `unknown` and explain uncertainty in `trust_rationale`.

### `trust_status`

Must be one of:

- `trusted`
- `usable_with_caveats`
- `metadata_only`
- `needs_manual_review`

### `evidence_summary`

Should contain 2 to 5 high-value claims.

Each claim should be one of:

- central method claim
- main result claim
- key limitation claim
- important figure-supported claim
- paper-scope claim for reviews or perspectives

Each claim must have at least one evidence item.

### `trust_rationale`

One to three sentences explaining why this trust status was assigned.

### `review_status`

Allowed values:

- `not_reviewed`
- `passed`
- `passed_with_caveats`
- `failed`

Before Phase 2 runs, the value is `not_reviewed`.

After Phase 2 runs, the value must be one of the other three.

### `review_issues`

A list of compact issue objects:

```json
{
  "severity": "medium",
  "issue": "The limitations section is too generic.",
  "suggested_fix": "Tie the limitation to dataset size or experimental coverage if supported by the paper."
}
```

Allowed severities:

- `low`
- `medium`
- `high`

---

## Trust Assignment Rules

## Automatic Downgrade Rules

The trust status cannot be `trusted` if any of these are true:

- no PDF attachment is available
- no full text is extracted
- extraction is based only on metadata or abstract
- `review_status` is `failed`
- a high-severity review issue exists

The trust status should usually be no higher than `usable_with_caveats` if any of these are true:

- extraction was truncated before important sections
- figure extraction failed for a figure-heavy paper
- the paper type is `unknown`
- important evidence is page-only and not tied to specific methods, results, or figures
- the review pass found medium-severity issues

The trust status should be `metadata_only` when:

- the note is based on metadata, title, abstract, or Zotero fields only
- no reliable PDF text is available

The trust status should be `needs_manual_review` when:

- the note contains unsupported claims
- the summary conflicts with extracted text or figures
- paper type is misclassified in a way that changes the interpretation
- the review pass identifies a high-severity issue

## Positive Signals for `trusted`

A note can be `trusted` only when:

- enough full text was extracted to cover the main method and result sections
- the main claims have page-level or figure-level evidence
- limitations are specific to the paper
- figure analysis is grounded in extracted figure context when figures are used
- the review pass is `passed` or `passed_with_caveats` with only low-severity issues

---

## Rendered Note Contract

The final note should add a new section:

```md
## 可信度与证据
```

This section should appear near the top of the note, after `## 元数据` and before `## 核心结论`. It should be visible before the reader reaches the detailed summary.

## Rendering Format

Example:

```md
## 可信度与证据

- **论文类型**: research_article
- **可信状态**: trusted
- **审查状态**: passed_with_caveats
- **判断依据**: 正文和关键图均可支持主要方法与实验结论，但 PDF 只抽取前 15 页。

### 关键证据

- 结论: The method uses a learned inverse-design model for metasurface control.
  - 证据: page 3 method description; figure p4-f1
- 结论: The reported experiments support power allocation across output channels.
  - 证据: page 6 results section; figure p7-f1
```

## Display Rules

- Do not render long quotes.
- Do not render raw JSON.
- Do not render more than 5 evidence claims.
- Evidence can be paraphrased.
- Use page and figure locators whenever possible.

---

## Phase 2 Review Contract

Phase 2 adds a second-pass review step. The review can initially live in the Codex skill workflow instead of a Python CLI command.

## Review Inputs

The reviewer reads:

- `context.md`
- `figure_context.md`
- `summary.json`
- rendered `note.md`

## Review Output

The review output is saved as:

```text
review.json
```

Shape:

```json
{
  "review_status": "passed_with_caveats",
  "review_issues": [
    {
      "severity": "medium",
      "issue": "The limitations section is generic.",
      "suggested_fix": "Tie limitations to dataset size, experimental setting, or model generalization if supported."
    }
  ],
  "trust_status_recommendation": "usable_with_caveats"
}
```

## Review Checks

The review must check:

1. whether each major claim has evidence
2. whether limitations are paper-specific
3. whether the paper type is plausible
4. whether background is incorrectly presented as this paper's contribution
5. whether figure analysis uses real extracted figures
6. whether the note overstates performance or novelty
7. whether extraction warnings should downgrade trust

## Write-Through Gate

When the user asks to write to Zotero:

- if `review_status` is `passed` or `passed_with_caveats`, writing may proceed after preview
- if `review_status` is `failed`, stop and report the review issues
- if `review_status` is `not_reviewed`, run the review before writing

This keeps write-through notes from entering Zotero before the quality pass happens.

---

## Workflow Integration

## Current Flow

Current V2 flow:

1. search Zotero item
2. create run directory
3. save `item-details.json`
4. prepare bundle
5. generate `summary.json`
6. finalize note
7. preview note
8. write to Zotero only when explicitly requested

## Trusted Notes Flow

Updated flow:

1. search Zotero item
2. stop if an existing Codex summary note is found unless user explicitly asks to continue
3. create run directory
4. save `item-details.json`
5. prepare bundle
6. generate `summary.json` with `paper_type`, `trust_status`, and `evidence_summary`
7. finalize note
8. run the note quality review
9. update `summary.json` or write `review.json`
10. finalize note again if review changes visible fields
11. preview note
12. write to Zotero only when explicitly requested and review gate allows it

---

## File-Level Impact

Expected implementation areas:

- `skills/zotero-paper-summary/SKILL.md`
  - require trusted-note fields in `summary.json`
  - add second-pass review instructions
  - add write-through gate based on review status

- `src/zotero_paperread/note.py`
  - add trust fields to render context
  - validate `## 可信度与证据`
  - keep defaults safe for legacy summaries

- `templates/zotero_note.md.j2`
  - render `## 可信度与证据`

- `tests/test_note.py`
  - verify trust section rendering and validation

- `tests/test_cli_note.py`
  - verify `finalize-note` works with trusted-note fields

Possible later implementation areas:

- `src/zotero_paperread/review.py`
  - if the review layer becomes deterministic enough for Python validation

- `tests/test_review.py`
  - if `review.py` is added

---

## Acceptance Criteria

The Trusted Notes V2 design is satisfied when:

1. generated summaries include `paper_type`, `trust_status`, `evidence_summary`, `trust_rationale`, `review_status`, and `review_issues`
2. rendered notes contain `## 可信度与证据`
3. `validate-note` requires the trust section
4. existing summaries without trust fields still render with conservative defaults
5. write-through analysis runs or checks the review step before writing to Zotero
6. failed review status blocks Zotero write unless the user explicitly overrides in a later design

---

## Deferred Work

These are intentionally deferred:

- section-level summarization
- `evidence_map.json` as a full standalone artifact
- deterministic Python `review-note` command
- Better Notes integration
- paper relation graph
- external source verification

This spec keeps the next implementation focused: make each single-paper note visibly trustworthy before making the system broader.
