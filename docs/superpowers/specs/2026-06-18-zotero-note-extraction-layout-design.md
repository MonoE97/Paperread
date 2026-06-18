# Zotero Note Extraction and Layout Design

**Status:** Approved for implementation planning.

**Date:** 2026-06-18

**Scope decision:** Optimize the Zotero-first paper note content, layout, and extraction quality only. Do not create a ResearchWiki-style `wiki/`, `synthesis/`, or `memory/` layer in this project.

## Summary

This design upgrades `Zotero_paperread` from plain PDF text plus a structured note into a two-stage reading workflow:

1. Build a more structured source context from the paper PDF.
2. Render a two-layer Zotero child note from that source context.

The first layer improves evidence quality by adding section-aware and table/value-aware context. The second layer improves the final note so the first screen supports quick reading decisions, while later sections preserve study notes, figures, limitations, and evidence appendices.

The workflow remains Zotero-first:

- Zotero access stays behind `zotero-mcp`.
- The project still writes only Zotero child notes when explicitly requested.
- `note.md` remains the audit artifact.
- `note.html` remains the write payload for Zotero.
- Better Notes remains optional display software, not a runtime dependency.
- ResearchWiki is used as a design reference for evidence, claim, method, and gap discipline, not as a file-system target.

## Context

The current project already has several strong pieces:

- `prepare-item` creates a run directory with metadata, PDF text, secondary-source metadata, figure output, and figure context.
- `summary.json` includes learning-note fields such as `method_modules`, `key_results_table`, `concept_cards`, `workflow_lessons`, and `reading_decision`.
- The note renderer produces `note.md` and Zotero-ready `note.html`.
- The write gate validates trust status, review status, evidence locators, note tags, and note preview before real Zotero writes.
- Figure extraction already records provenance, confidence, quality warnings, and evidence tiers.

The main gap is that text extraction remains mostly plain text. `context.md` is useful, but evidence locators are coarse, often page-level only. This makes it harder for generated notes to explain where a method detail, limitation, result, or numeric comparison came from.

ResearchWiki suggests a useful principle: a reading note should not only summarize a paper, it should preserve reusable claims, methods, gaps, uncertainty, and source evidence. For this project, the right adaptation is not to create an Obsidian-style wiki. The right adaptation is to make Zotero notes more evidence-aware and more reusable while staying inside the current run-directory workflow.

## Goals

1. Add section-aware PDF extraction without breaking old `extract.json` consumers.
2. Add conservative table/value candidates for scientific results and numeric comparisons.
3. Generate a new `section_context.md` artifact to help source-grounded summarization while preserving canonical evidence locators.
4. Make evidence locators more precise and stable.
5. Redesign the Zotero note as a two-layer reading note:
   - early compact decision layer
   - later detailed learning and evidence layer
6. Keep all new summary fields optional so old runs still render.
7. Keep write-through gates strict and Zotero write behavior unchanged.

## Non-Goals

1. No ResearchWiki `wiki/`, `synthesis/`, or `memory/` directories.
2. No `knowledge_units.json` in this phase.
3. No Obsidian or Better Notes graph synchronization.
4. No cross-run relation graph or related-paper state.
5. No database or persistent index.
6. No broad batch collection workflow changes.
7. No direct Zotero SQLite writes.
8. No Zotero write behavior changes.

## Architecture

The new high-level workflow is:

```text
item-details.json
  -> prepare-item
  -> extract.json
  -> context.md
  -> section_context.md
  -> figures.json
  -> figure_context.md
  -> summary.json
  -> review.json
  -> note.md / note.html
  -> gate-run
```

`context.md` stays as the full-text fallback. `section_context.md` is an additional summarization aid, not a replacement and not a new canonical evidence source. If section detection is weak or fails, the workflow still works with page-level `context.md` locators and records a warning.

## Structured Extraction

### Backward Compatibility

`extract.json` keeps these existing top-level fields:

```json
{
  "pdf_path": "",
  "page_count": 0,
  "extracted_pages": 0,
  "text": "",
  "warnings": []
}
```

New fields are additive:

```json
{
  "pages": [],
  "sections": [],
  "table_candidates": []
}
```

Old commands, old tests, and old run directories should remain valid.

### Page Records

Each page record should be deterministic and lightweight:

```json
{
  "page": 1,
  "text": "Extracted page text.",
  "char_count": 1234,
  "warnings": []
}
```

Page warnings may include:

- `empty_page_text`
- `short_page_text`
- `possible_ocr_needed`

These warnings are extraction diagnostics. They are not automatically write blockers, but they should influence `trust_status` and `trust_rationale`.

### Section Records

The extractor should identify common scientific paper sections conservatively:

```json
{
  "kind": "methods",
  "title": "Methods",
  "start_page": 3,
  "end_page": 5,
  "text": "Aggregated section text.",
  "confidence": "high"
}
```

Allowed section `kind` values for the first implementation:

- `abstract`
- `introduction`
- `background`
- `methods`
- `experimental`
- `computational`
- `results`
- `discussion`
- `conclusion`
- `limitations`
- `references`
- `acknowledgements`
- `unknown`

The detection strategy should be rule-based:

- match standalone headings from extracted text lines
- support numeric prefixes such as `1 Introduction` or `2. Methods`
- normalize common variants such as `Materials and methods`, `Experimental section`, `Computational details`, `Results and discussion`
- include materials and battery paper headings such as `Electrochemical performance`, `Ionic conductivity`, `Characterization`, and `DFT calculations`

The extractor should avoid aggressive classification. If a heading is ambiguous, use `confidence: "low"` or `kind: "unknown"` rather than pretending certainty.

### Table and Value Candidates

The first implementation should not attempt perfect table reconstruction. It should emit conservative candidates that help Codex find results, baselines, and numeric comparisons.

Example:

```json
{
  "page": 6,
  "section": "Results",
  "text": "Conductivity ... 1.2 mS cm-1 ... activation energy ...",
  "signals": ["conductivity", "activation energy", "baseline"],
  "confidence": "medium",
  "locator": "context.md page 6 section Results table_candidate 1"
}
```

Candidate signals should include general scientific and AI4S/materials terms:

- `accuracy`
- `mae`
- `rmse`
- `r2`
- `speedup`
- `baseline`
- `ablation`
- `conductivity`
- `ionic conductivity`
- `activation energy`
- `diffusion barrier`
- `capacity`
- `cycle life`
- `rate performance`
- `energy density`
- `voltage`
- `bandgap`
- `formation energy`
- `ehull`

The confidence rule should be conservative:

- `high`: clear table-like text plus multiple numeric/result signals
- `medium`: paragraph or line block with numeric/result signals
- `low`: weak numeric signal, useful only as a search hint

Low-confidence table candidates must not be treated as strong evidence without supporting text or figure context.

## Section Context Artifact

`prepare-item` should write `section_context.md` when `extract.json` contains page, section, or table-candidate data.

Suggested format:

```md
# Section Context

## Extraction Summary

- PDF Path: <path>
- Page Count: <N>
- Extracted Pages: <N>
- Section Count: <N>
- Table Candidate Count: <N>

## Sections

### Methods

- Kind: methods
- Pages: 3-5
- Confidence: high
- Locator: context.md page 3 section Methods

<section text excerpt or full section text>

## Table / Value Candidates

### Candidate 1

- Locator: context.md page 6 section Results table_candidate 1
- Confidence: medium
- Signals: conductivity, activation energy

<candidate text>
```

`section_context.md` should be listed in `prepare-item` output and added to `run.json` when a manifest exists.

## Evidence Locator Contract

The workflow should prefer these locator forms:

```text
context.md page 3 section Methods
context.md page 6 section Results table_candidate 1
figure_context.md fig_p4_1
```

Locator rules:

1. Trusted evidence locators remain limited to `context.md` and `figure_context.md`.
2. Secondary contexts remain cross-check material and must not be cited in `evidence_summary`.
3. `section_context.md` may help Codex find section and table/value candidates, but final `evidence_summary` locators should cite the canonical source form such as `context.md page 3 section Methods`.
4. Low-confidence section and table candidates can be cited only through canonical `context.md ...` locators and with caveats in the evidence summary.
5. `lint-summary` should reject secondary context locators and malformed trusted locators.

## Summary Contract Additions

The renderer should keep old fields and add optional new fields:

```json
{
  "recommended_sections": [],
  "recommended_figures": [],
  "baseline_or_comparison": [],
  "result_evidence_notes": [],
  "author_stated_limitations": [],
  "inferred_limits": [],
  "potential_gaps": [],
  "evidence_quality_summary": ""
}
```

### `recommended_sections`

Short list of sections worth reading first:

```json
[
  {
    "section": "Methods",
    "locator": "context.md page 3 section Methods",
    "reason": "Best source for the model design and assumptions."
  }
]
```

### `recommended_figures`

Short list of key figures worth opening:

```json
[
  {
    "figure_id": "fig_p4_1",
    "locator": "figure_context.md fig_p4_1",
    "reason": "Shows the overall workflow and evidence chain."
  }
]
```

### `baseline_or_comparison`

Comparison targets, baselines, or reference systems:

```json
[
  {
    "target": "DFT baseline",
    "result": "Lower MAE on formation energy prediction.",
    "locator": "context.md page 6 section Results table_candidate 1"
  }
]
```

### `result_evidence_notes`

Short evidence-quality notes for major results:

```json
[
  {
    "result": "Ionic conductivity improved at room temperature.",
    "evidence": "Reported in the Results section with table-like numeric comparison.",
    "locator": "context.md page 6 section Results table_candidate 1",
    "confidence": "medium"
  }
]
```

### `author_stated_limitations`

Limitations stated by the paper authors. These should be separated from inferred limits.

Preferred object form:

```json
[
  {
    "text": "The authors state that only one material family was evaluated.",
    "locator": "context.md page 8 section Discussion",
    "source_type": "author_stated"
  }
]
```

### `inferred_limits`

Limits inferred by Codex or the reader from scope, dataset, missing experiments, or weak extraction. These must not be presented as author claims.

Preferred object form:

```json
[
  {
    "text": "Generalization to sulfide solid electrolytes is not established.",
    "basis": "The experiments cover oxide examples only.",
    "locator": "context.md page 6 section Results",
    "source_type": "inferred"
  }
]
```

### `potential_gaps`

Follow-up research gaps suggested by the paper. Each gap should include either paper evidence or an explicit uncertainty label.

Preferred object form:

```json
[
  {
    "text": "Whether the workflow transfers to reactive battery interfaces remains open.",
    "basis": "The paper validates non-reactive interface examples.",
    "locator": "context.md page 7 section Results",
    "uncertainty": "AI inference"
  }
]
```

### `evidence_quality_summary`

A compact explanation of whether the note relies on full text, section context, figure context, weak extraction, or metadata-only evidence.

## Note Layout

The final note should become a two-layer reading note.

### Proposed Sections

```md
# [Codex Summary] <title> - <date>

## 0. 速读决策
## 1. 论文核心
## 2. 方法怎么做
## 3. 结果是否站得住
## 4. 图表导读
## 5. 局限、适用边界与潜在 gap
## 6. 可迁移启发
## 7. 术语与概念卡片
## 8. 后续检索关键词
## 9. 元数据
## 10. 证据链附录
## 11. 补充优化记录

Tags: codex-summary, paper-summary, ...
```

### Section 0: `速读决策`

This section should be compact and action-oriented:

- 30 second takeaway
- reading decision
- relevance to the user's AI4S / materials / battery work
- trust status
- main risk
- recommended sections
- recommended figures

Avoid a large table as the only first-screen experience. Tables are still useful, but the section should prioritize quick human triage.

### Section 1: `论文核心`

This section should explain:

- research object
- core problem
- existing gap
- paper entry point
- one-sentence contribution

### Section 2: `方法怎么做`

This section should preserve the existing method module table and workflow:

- method overview
- method modules
- workflow
- assumptions and technical details

### Section 3: `结果是否站得住`

This section should force evidence-aware result reading:

- key results table
- baseline or comparison targets
- table/value evidence notes
- evidence-quality summary
- caveats when results come from weak extraction or low-confidence table candidates

### Section 4: `图表导读`

This section keeps the current figure overview, figure index, and expanded key-figure analysis. It should continue to respect figure evidence tiers and visual-quality warnings.

### Section 5: `局限、适用边界与潜在 gap`

This section must separate:

- author-stated limitations
- inferred limits
- potential gaps and follow-up questions

Codex-inferred limits and gaps must not be written as if they were paper-authored claims.

### Sections 6-11

These sections preserve the current learning-note value:

- transferable insight
- workflow lessons
- concept cards
- follow-up keywords
- metadata
- evidence chain
- improvement notes

The evidence chain remains near the end so provenance is available without interrupting the reading flow.

## Lint and Gate Changes

`lint-summary` should be extended to check:

1. secondary context is not used as trusted evidence
2. malformed trusted locators are flagged
3. `author_stated_limitations` entries, when using object form, have `source_type: "author_stated"`
4. `inferred_limits` entries, when using object form, have `source_type: "inferred"`
5. `workflow_steps` remains readable as multi-line Markdown or normalized list text

Semantic checks that require paper understanding should stay in the review pass, not in deterministic lint. In particular, the review pass should check whether low-confidence table candidates are over-claimed and whether inferred limits are written as paper-authored limitations.

`validate_trusted_summary` should remain focused on write-through readiness. It should not require every new optional field, but write-ready notes should still require core evidence, trust, review, and improvement fields.

## Implementation Phases

### Phase 1: Structured Extraction

Files likely to change:

- `src/zotero_paperread/pdf_extract.py`
- `src/zotero_paperread/workflow.py`
- `src/zotero_paperread/cli.py`
- `tests/test_pdf_extract.py`
- `tests/test_workflow.py`
- `README.md`
- `skills/zotero-paper-summary/SKILL.md`

Deliverables:

- additive `pages`, `sections`, and `table_candidates` in `extract.json`
- `section_context.md`
- run manifest support for `section_context_md`
- docs and skill updates explaining that Codex should read `section_context.md` when available, while final evidence locators remain `context.md ...` or `figure_context.md ...`

### Phase 2: Note Layout and Summary Contract

Files likely to change:

- `src/zotero_paperread/note.py`
- `src/zotero_paperread/summary_lint.py`
- `templates/zotero_note.md.j2`
- `tests/test_note.py`
- `tests/test_cli_note.py`
- `tests/test_summary_lint.py`
- `README.md`
- `skills/zotero-paper-summary/SKILL.md`

Deliverables:

- new two-layer note section order
- safe cleaning/rendering for optional new fields
- old-summary compatibility
- summary lint checks for canonical evidence locator rules and structured limitation fields
- docs and skill updates for the new note structure

## Verification

Phase 1 focused verification:

```bash
uv run pytest tests/test_pdf_extract.py tests/test_workflow.py -q
```

Phase 2 focused verification:

```bash
uv run pytest tests/test_note.py tests/test_cli_note.py tests/test_summary_lint.py -q
```

Final project verification:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

No verification step should write to Zotero.

## Risks and Mitigations

### Section Detection Is Wrong

Mitigation:

- keep section detection conservative
- record confidence
- allow page-level fallback
- make low-confidence section locators visible as caveats

### Table Candidates Are Noisy

Mitigation:

- call them candidates, not extracted truth
- include confidence
- require summary/review to cite supporting text or caveat weak candidates

### Note Becomes Too Long

Mitigation:

- keep decision material in the first section
- move evidence and audit material to later sections
- keep recommendation lists short

### ResearchWiki Scope Creep

Mitigation:

- no wiki directories
- no knowledge-unit export in this phase
- no cross-run synthesis state
- no Obsidian-specific links

### Old Runs Break

Mitigation:

- all new fields are optional
- Python render context supplies defaults
- `extract.json` additions are backward compatible
- tests cover old summary rendering

## Open Decisions

No product decisions remain open for this implementation plan. The user selected:

- optimize Zotero note and extraction quality only
- use a two-stage plan
- prioritize section-aware extraction before table/value candidates
- use a two-layer note layout
- implement the full two-stage approach rather than a template-only update
