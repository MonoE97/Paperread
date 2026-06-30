# Paperread

Paperread is a clone-and-run literature reading workflow for Codex or Claude. It uses the local `paperread` CLI to extract evidence from a paper PDF, then guides an agent to write a Chinese-first structured reading note.

## What It Does

Paperread supports two public v1 inputs:

- **Zotero title**: find a paper in Zotero through Zotero MCP, prepare deterministic evidence artifacts, render `note.md` and `note.html`, and create a new Zotero child note only after explicit write intent.
- **local PDF path**: run the same extraction, summary, review, lint, and render rules on a PDF that is not in Zotero, then write local outputs beside the PDF without writing Zotero.

Both workflows use full-PDF extraction by default. Evidence locators in `summary.json` should cite `context.md` or `figure_context.md`; `section_context.md` is only a navigation aid.

## Public V1 Setup

Clone this repository, install `uv`, then run from the repo root:

```bash
uv sync
uv run paperread --help
```

Zotero title workflows also require Zotero Desktop plus a working Zotero MCP server. Local PDF path workflows do not require Zotero.

## Use As A Repo-Local Skill

The only public workflow bundle is `skill/`, and its skill name is `paperread`. Public v1 is repo-local: clone this repository, run `uv sync`, and execute commands from the repo root. Do not copy `skill/` by itself and expect the workflow to run, because it depends on this repository's Python package, templates, lockfile, and CLI.

Point your agent at `skill/SKILL.md`. The skill routes a local PDF path to `skill/references/pdf-path-workflow.md`; otherwise it treats the input as a Zotero title or title fragment and uses `skill/references/zotero-workflow.md`.

## Zotero Title Workflow

Use this path when the input is a Zotero title or title fragment.

High-level flow:

1. Search Zotero through MCP and stop on duplicate normalized titles.
2. Create a run directory with `create-run`.
3. Save the raw MCP response, then normalize it with `save-item-details`.
4. Run `prepare-item` to generate `metadata.json`, `extract.json`, `context.md`, `section_context.md`, optional `figures.json`, and optional `figure_context.md`.
5. If `secondary_sources.json` lists Extra/web URLs, capture them as cross-check-only context:

```bash
mkdir -p <run_dir>/secondary_contexts
node skill/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

Captured secondary files use `source_status: secondary_context` when usable. Unavailable captures use `source_status: secondary_context_unavailable`, including warnings such as `navigation_timeout`. Secondary context must not cite secondary context in `evidence_summary`; it is only for cross-checking and background.

6. The agent reads the evidence artifacts and writes `summary.json` plus `review.json`.
7. Run the deterministic review chain:

```bash
uv run paperread validate-summary-json <run_dir>/summary.json
uv run paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run paperread lint-summary <run_dir>/summary.json
uv run paperread validate-trusted-summary <run_dir>/summary.json
```

8. Prepare a write candidate only when Zotero output is requested:

```bash
uv run paperread prepare-write-candidate <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD
```

9. Preview the target Zotero item, `note.md`, and `note.html`.
10. After explicit write intent, create a new Zotero child note through Zotero MCP `write_note`.
11. Verify the created note with `verify-zotero-note`.

## Local PDF Path Workflow

Use this path when the input is an existing local PDF path.

```bash
uv run paperread prepare-pdf "/path/to/paper.pdf"
```

The first run creates `<pdf_stem>_analysis/` beside the PDF and targets `<pdf_stem>_note.md`. Repeated runs create `<pdf_stem>_analysis_v2/`, `<pdf_stem>_note_v2.md`, and higher suffixes without overwriting previous outputs.

The agent writes `summary.json` and `review.json` inside the analysis directory, then runs:

```bash
uv run paperread validate-summary-json <analysis_dir>/summary.json
uv run paperread apply-review <analysis_dir>/summary.json <analysis_dir>/review.json
uv run paperread lint-summary <analysis_dir>/summary.json
uv run paperread validate-trusted-summary <analysis_dir>/summary.json
uv run paperread prepare-local-note-candidate <analysis_dir> --generated-date YYYY-MM-DD
```

`prepare-local-note-candidate` writes `note.md`, `note.html`, preview files, `note-tags.json`, `local-gate-report.json`, and the final Markdown note beside the PDF. This workflow is local-output only.

## Privacy And Local Outputs

Generated paper data is private by default. `.gitignore` excludes common local outputs:

- `runs/`
- `papers/`
- `<pdf_stem>_analysis/` and `<pdf_stem>_analysis_vN/`
- `<pdf_stem>_note.md` and `<pdf_stem>_note_vN.md`
- extracted text, summary JSON, and generated note files
- agent/session state such as `.superpowers/` and `.worktrees/`

Do not commit paper PDFs, extracted text, Zotero metadata, generated notes, review reports, or local run artifacts unless you have reviewed them intentionally.

## Verification

Run these before considering a change complete:

```bash
uv run pytest
uv run paperread --help
uv run paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/paperread-extract.json
```

Codex users who have the bundled `skill-creator` validator available can optionally run its local `quick_validate.py` script against `skill/`. That validator is not required to use this repository.

## Safety Boundaries

- Default to dry-run and preview before writing.
- Zotero writes are allowed only through Zotero MCP `write_note` and only after explicit user intent.
- Zotero local API and SQLite are read-only in this project.
- PDF path workflow must not write Zotero, call `refresh-live-notes`, or create `write-payload.json`.
- Rendered note prose should be Chinese-first while preserving paper titles, names, formulas, method names, units, evidence locators, and tag keys.
