---
name: paper_reader
description: Use when the user asks to analyze a paper either by Zotero title/title fragment, local PDF path, or local directory path, producing a Chinese structured reading note with evidence-grounded summary, review, and local or Zotero output gates.
---

# paper_reader

paper_reader is a self-contained paper reading skill. Run bundled CLI commands from the installed skill root with `uv run paper_reader ...` after the skill environment has been synchronized with `uv sync --locked`.

## Environment Setup

Run setup commands from the installed skill root:

```bash
uv --version
uv sync --locked
uv run paper_reader --help
```

If `uv sync --locked` reports that Python `>=3.13` is unavailable, run `uv python install 3.13` from the skill root, then retry `uv sync --locked`. If `uv` itself is missing, stop and ask the user to install `uv` first; do not use `pip`, `conda`, or system Python as a substitute.

For Zotero title workflows, Zotero Desktop and Zotero MCP must already be installed and enabled. Use the Zotero MCP plugin from https://github.com/cookjohn/zotero-mcp#readme (`zotero-mcp-plugin` installed in Zotero via `Tools -> Add-ons`) and configure the local Streamable HTTP endpoint, normally `http://127.0.0.1:23120/mcp`.

## Typical Use

- Zotero title or title fragment: use `$paper_reader` with the paper title. The agent searches Zotero via Zotero MCP, creates a run, prepares evidence artifacts, writes `summary.json` and `review.json`, renders `note.md` and `note.html`, previews the target, writes only through MCP `write_note` after explicit write intent, and verifies the created note.
- Local PDF path: use `$paper_reader` with a `.pdf` path. `prepare-pdf` prepares `<pdf_stem>_analysis/` beside the PDF and reserves `<pdf_stem>_note.md` as the final-note target. The agent must write `summary.json` and `review.json`, run the deterministic review/lint/trusted-summary chain, then run `prepare-local-note-candidate` to write the final local Markdown note. The workflow never searches Zotero for matching items and never writes Zotero.
- Local directory path: route to `$paper_reader_batch` with the local PDF folder workflow. Directory input is not a Zotero title fragment.

## Entry Routing

- If the user input resolves to an existing local path with suffix `.pdf`, use the local PDF path workflow in `references/pdf-path-workflow.md`.
- If the user input resolves to an existing local directory path, delegate to `$paper_reader_batch` and its local PDF folder workflow. If `$paper_reader_batch` is unavailable, ask the user to install or enable it; do not fall back to Zotero title search.
- Local PDF path and directory path inputs skip Zotero lookup and duplicate checks, including same-title or same-DOI checks.
- Existing local paths are not Zotero title fragments.
- Only non-path text should be treated as a Zotero title or title fragment and use `references/zotero-workflow.md`.
- For both modes, use full-PDF extraction by default. Pass `--max-pages` only when the user explicitly asks for debugging or a shortened preview.
- For both modes, the CLI creates deterministic evidence artifacts; it does not replace the agent's paper-reading step. The agent writes `summary.json` and `review.json` after reading `context.md`, `section_context.md`, and `figure_context.md` when available.

## Shared Rules

- Final evidence locators in `summary.json` must use canonical forms: `context.md page <N>`, `context.md page <N> section <Section Name>`, `context.md page <N> section <Section Name> table_candidate <N>`, or `figure_context.md <figure_id>`. Do not use bare `context.md` / `figure_context.md`, prose locators such as `page 3 method section`, `section_context.md`, or secondary context paths.
- Secondary context is cross-check material only and must not be cited in `evidence_summary`.
- Rendered note prose should be Chinese-first while preserving titles, names, formulas, method names, units, evidence locators, and tag keys.
- Always run the review and gate sequence before treating a note as ready.
- Zotero writes are allowed only through Zotero MCP `write_note` after explicit user write intent.
- Local PDF path analysis is local-output only; it must not call Zotero lookup, duplicate-check, write, or live-note refresh commands.
