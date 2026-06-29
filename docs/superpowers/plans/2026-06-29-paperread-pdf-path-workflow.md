# Paperread PDF Path Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans, superpowers:test-driven-development, and superpowers:verification-before-completion. Implement behavior with failing tests first. After each small task, review from first principles before moving on.

**Goal:** Extend the current Zotero-first paper reading workflow so a direct local PDF path receives the same evidence extraction, summary, review, and note rendering treatment, while writing PDF outputs next to the source PDF and keeping Zotero writes strictly isolated to the Zotero workflow.

**Architecture:** Split the workflow into source adapters, a shared evidence bundle builder, and separate finalization gates. Zotero title input keeps the existing Zotero write gate. PDF path input uses a local-only gate and never creates a Zotero write payload.

**Tech Stack:** Python 3.13, Typer CLI, PyMuPDF, Jinja2, pytest, uv, repo-local Codex/Claude skill instructions.

**Implementation Status:** Completed on 2026-06-29 on branch `codex/paperread-pdf-workflow`. The shipped shape uses one repo-local `skills_paperread/` bundle, not `.agents/skills/paperread` or `.claude/skills/paperread`.

**Verification Evidence:** Full regression suite passed (`uv run pytest`: 304 passed). Project smoke commands passed (`uv run zotero-paperread --help`, `uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json`, `git diff --check`). The live PDF path workflow produced `/Users/jwxi/Downloads/1-s2.0-S2405829725006877-main_analysis/` and `/Users/jwxi/Downloads/1-s2.0-S2405829725006877-main_note.md` with `local_ready` and no `write-payload.json`. The live Zotero workflow created and verified child note `K6CXJC2Z` under parent item `JSYJ6TXS`.

---

## Requirements

- Direct PDF input such as `/path/to/paper.pdf` creates versioned local outputs beside the PDF:
  - first run: `/path/to/paper_analysis/` and `/path/to/paper_note.md`
  - repeated runs: `/path/to/paper_analysis_v2/` and `/path/to/paper_note_v2.md`, then `_v3`, etc.
- PDF analysis must reuse the same deterministic extraction and rendering pipeline used by Zotero entries: `metadata.json`, `extract.json`, `context.md`, `section_context.md`, optional `figures.json`, optional `figure_context.md`, `summary.json`, `review.json`, `note.md`, and `note.html`.
- PDF workflow must not write Zotero, call `refresh-live-notes`, require `item-details.json`, or generate `write-payload.json`.
- PDF metadata defaults to the filename stem. PDF document metadata and first-page text may provide candidates, but explicit CLI overrides win: `--title`, `--authors`, `--date`, `--doi`, `--url`.
- The agent skill must state that semantic analysis is still agent-authored: CLI commands create deterministic evidence and gates; Codex/Claude reads `context.md` / `figure_context.md` and writes `summary.json` / `review.json`.
- Create one repo-local canonical skill bundle at `skills_paperread/`. Do not create `.agents/skills/paperread` or `.claude/skills/paperread` in v1. This folder is not claimed to be a formally installed OpenAI/Claude skill directory; it is the v1 repo-local source of truth for agents working from this checkout.
- Update README, AGENTS, and repo-local workflow docs so first external users can clone the repo, install `uv`, run `uv sync`, and use the workflows from the repo root.

## Task 1: Plan Doc Review Before Code

**Files:**
- Modify: `docs/superpowers/plans/2026-06-29-paperread-pdf-path-workflow.md`

- [ ] Review this plan from first principles:
  - What problem is the user actually solving?
  - Does any task duplicate the Zotero and PDF analysis logic?
  - Is any PDF path capable of triggering a Zotero write or payload?
  - Does the skill promise standalone installability that v1 does not provide?
  - Are repeated runs safe for user files?
- [ ] Patch this plan before implementation if any answer is weak.

## Task 2: Add Failing Tests for PDF Output Allocation and Metadata

**Files:**
- Create/modify: `tests/test_pdf_workflow.py`
- Modify after red: `src/zotero_paperread/pdf_workflow.py`

- [ ] Add a failing test that `allocate_pdf_output_paths(Path("/tmp/Paper One.pdf"))` returns `/tmp/Paper One_analysis` and `/tmp/Paper One_note.md` when neither exists.
- [ ] Add a failing test that existing first-run paths produce `_v2` paths and do not delete the originals.
- [ ] Add a failing test that metadata built from a PDF path uses the filename stem by default and explicit overrides for title/authors/date/doi/url when provided.
- [ ] Run only the new tests and verify they fail for missing symbols.
- [ ] Implement the minimum `pdf_workflow.py` functions to pass.
- [ ] Re-run the targeted tests.
- [ ] First-principles review: confirm the implementation preserves user files and does not infer a stronger title than it knows.

## Task 3: Add Shared Evidence Bundle Builder

**Files:**
- Modify: `src/zotero_paperread/workflow.py`
- Modify: `tests/test_workflow.py`
- Modify: `tests/test_pdf_workflow.py`

- [ ] Add failing tests that both Zotero item details and direct PDF metadata can produce the same core artifact set in a bundle directory.
- [ ] Extract the existing `prepare_item_bundle` internals into a shared function that accepts normalized metadata, PDF path, optional item details, secondary sources, and missing-PDF warning policy.
- [ ] Keep `prepare_item_bundle` as the Zotero adapter and prove existing tests still pass.
- [ ] Add `prepare_pdf_bundle` using the shared builder.
- [ ] First-principles review: confirm source adapters differ only in input/output policy, not in analysis behavior.

## Task 4: Add Local PDF Gate and Candidate Finalizer

**Files:**
- Create: `src/zotero_paperread/local_gate.py`
- Create: `src/zotero_paperread/local_candidate.py`
- Create/modify: `tests/test_local_gate.py`
- Modify: `src/zotero_paperread/cli.py`

- [ ] Add failing tests that local gate blocks when `summary.json`, `review.json`, `note.md`, or `note.html` is missing.
- [ ] Add failing tests that local gate blocks on trusted-summary validation, summary lint issues, or `review.json needs_improvement is not false`.
- [ ] Add a failing test that local gate ready output includes `analysis_dir`, `final_note_path`, `note_md_path`, `note_html_path`, tags, and status `local_ready`, but never `parentKey` or `write_payload_path`.
- [ ] Add a failing test that `prepare_local_note_candidate` renders `note.md` and `note.html`, writes previews and tags, runs local gate, and copies `note.md` to the versioned final note path.
- [ ] Implement minimal local gate and candidate finalizer.
- [ ] First-principles review: confirm PDF completion requires the same semantic quality gates as Zotero, minus Zotero-specific parent/live-note checks.

## Task 5: Add CLI Commands

**Files:**
- Modify: `src/zotero_paperread/cli.py`
- Modify: `tests/test_cli_pdf_workflow.py`

- [ ] Add failing CLI tests for:
  - `prepare-pdf <pdf_path>` creates the analysis dir, writes `run.json`, and prints JSON with `analysis_dir`, `final_note_path`, and bundle paths.
  - repeated `prepare-pdf` creates `_v2` outputs.
  - `prepare-pdf` accepts metadata overrides.
  - `local-gate-run <analysis_dir>` writes/prints local gate reports.
  - `prepare-local-note-candidate <analysis_dir> --generated-date YYYY-MM-DD` refuses to generate a Zotero payload.
- [ ] Implement CLI commands:
  - `prepare-pdf`
  - `local-gate-run`
  - `prepare-local-note-candidate`
- [ ] First-principles review: confirm command names communicate side effects and output locations.

## Task 6: Create `skills_paperread/`

**Files:**
- Create: `skills_paperread/SKILL.md`
- Create: `skills_paperread/references/summary-schema.md`
- Create: `skills_paperread/references/zotero-workflow.md`
- Create: `skills_paperread/references/pdf-path-workflow.md`
- Create/modify: `tests/test_default_workflow_docs.py`

- [ ] Add docs tests that `skills_paperread/SKILL.md` contains both entry modes: Zotero title and local PDF path.
- [ ] Add docs tests that `skills_paperread` states repo-local v1 prerequisites: clone repo, install `uv`, run `uv sync`, run commands from repo root.
- [ ] Add docs tests that PDF path workflow explicitly forbids `write-payload.json`, `refresh-live-notes`, and Zotero writes rather than instructing the agent to use them.
- [ ] Add docs tests that `skills_paperread` does not claim standalone global skill installation.
- [ ] Write a concise `SKILL.md` with routing instructions:
  - if input path exists and suffix is `.pdf`, use PDF path workflow reference;
  - otherwise use Zotero workflow reference.
- [ ] Move long schema and workflow rules into one-level reference files.
- [ ] First-principles review: confirm the skill does not overpromise standalone installation and tells the agent where semantic authoring begins.

## Task 7: Update User-Facing Docs

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify as needed: `docs/github-publication.md`
- Modify as needed: existing `skills/zotero-paper-summary/SKILL.md`

- [ ] Document the two input modes and their output contracts.
- [ ] Document v1 public-use setup: clone repo, install `uv`, `uv sync`, operate from repo root.
- [ ] Document that PDF local notes are Markdown files beside the PDF and Zotero notes are versioned Zotero child notes.
- [ ] Preserve existing Zotero write-through guardrails and update wording only where needed.
- [ ] First-principles review: confirm docs make the first public version usable without implying global skill installation.

## Task 8: End-to-End PDF Validation

**Files/Artifacts:**
- PDF fixture path: `/Users/jwxi/Downloads/1-s2.0-S2405829725006877-main.pdf`
- Expected generated artifacts beside the PDF, with `_vN` suffix if needed.

- [ ] Run `uv run zotero-paperread prepare-pdf /Users/jwxi/Downloads/1-s2.0-S2405829725006877-main.pdf`.
- [ ] Read generated `context.md`, `section_context.md`, and `figure_context.md` if available.
- [ ] Write source-grounded `summary.json` and `review.json`.
- [ ] Run `validate-summary-json -> apply-review -> lint-summary -> validate-trusted-summary` before finalizing the local note.
- [ ] Run `uv run zotero-paperread prepare-local-note-candidate <analysis_dir> --generated-date 2026-06-29`.
- [ ] Preview/inspect the generated final note beside the PDF.
- [ ] First-principles review: confirm evidence locators cite only `context.md` / `figure_context.md`, the final note is in the requested location, and no Zotero artifacts were produced.

## Task 9: End-to-End Zotero Validation and Write

**Input title:**
`Low-cost high-air-stability argyrodite electrolyte delivering excellent interface compatibility in all-solid-state lithium metal batteries`

**Files/Artifacts:**
- Existing Zotero run directory under `runs/2026-06-29/...` or collision-suffixed equivalent.

- [ ] Use Zotero MCP tool discovery for `search_library`, `get_item_details`, and `write_note`.
- [ ] Search exact title and stop if duplicate normalized titles exist.
- [ ] Save raw item details, normalize with `save-item-details`, and run `prepare-item`.
- [ ] Generate source-grounded `summary.json` and `review.json`.
- [ ] Run `validate-summary-json -> apply-review -> lint-summary -> validate-trusted-summary` before preparing the write candidate.
- [ ] Run `prepare-write-candidate` and show target item plus `note.md` / `note.html` previews.
- [ ] Because the user explicitly requested the Zotero test write, call only `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`.
- [ ] Run `verify-zotero-note` using payload readback checks.
- [ ] First-principles review: confirm this path used Zotero write gates and never reused PDF local gate as proof of Zotero write readiness.

## Task 10: Final Review, Verification, and Commit

**Files:**
- All modified files.

- [ ] Run full verification:
  - `uv run pytest`
  - `uv run zotero-paperread --help`
  - `uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json`
- [ ] Run adversarial code review from first principles:
  - Can any PDF path write Zotero?
  - Can any stale local note be mistaken for a fresh note?
  - Does any gate pass without semantic review?
  - Are public-use docs honest about repo-local setup?
  - Are existing Zotero write guarantees intact?
- [ ] Fix all actionable findings and rerun relevant tests.
- [ ] Inspect `git diff --check` and `git status --short`.
- [ ] Create a local commit. Do not push.

## First-Principles Review Notes

- The core problem is not "add a PDF command"; it is "preserve one trusted paper-reading method across two source types." Therefore the implementation must share bundle/render/review code and isolate only source/output adapters.
- The PDF path workflow is local-output only. Any use of Zotero parent keys, live-note refresh, or write payloads in the PDF path is a design bug, not just a test failure.
- `skills_paperread/` is intentionally repo-local in v1 because the workflow depends on this repo's CLI, templates, and tests. Public docs must instruct users to clone the repo and run from the repo root.
- Metadata extracted from a PDF is not identity truth. The filename stem plus explicit user overrides are more honest than aggressive title inference.
- A local note is not ready merely because `note.md` renders. It must pass the same semantic review fields, summary lint, trusted-summary validation, and rendered-note validation as Zotero notes, minus Zotero-specific live parent checks.
