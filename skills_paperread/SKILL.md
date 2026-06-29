---
name: paperread
description: Use when the user asks to analyze a paper either by Zotero title/title fragment or by local PDF path, producing a Chinese structured reading note with evidence-grounded summary, review, and local or Zotero output gates.
---

# Paperread

This is the repo-local v1 paper reading skill bundle for this repository. It is not a standalone global skill installation. Use it from the repo root after cloning this repo, installing `uv`, and running `uv sync`.

## Entry Routing

- If the user input is a local PDF path and the path exists with suffix `.pdf`, use the local PDF path workflow in `references/pdf-path-workflow.md`.
- Otherwise treat the input as a Zotero title or title fragment and use `references/zotero-workflow.md`.
- For both modes, the CLI creates deterministic evidence artifacts; the agent still reads `context.md`, `section_context.md`, and `figure_context.md` to author `summary.json` and `review.json`.

## Shared Rules

- Run commands from the repo root with `uv run zotero-paperread ...`.
- Use full-PDF extraction by default. Pass `--max-pages` only for explicit debugging or shortened previews.
- Final evidence locators in `summary.json` must cite `context.md` or `figure_context.md`, not `section_context.md`.
- Rendered note prose should be Chinese-first while preserving titles, names, formulas, method names, units, evidence locators, and tag keys.
- Always run the review and gate sequence before treating a note as ready.
