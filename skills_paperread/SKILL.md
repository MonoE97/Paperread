---
name: paperread
description: Use when the user asks to analyze a paper either by Zotero title/title fragment or by local PDF path, producing a Chinese structured reading note with evidence-grounded summary, review, and local or Zotero output gates.
---

# Paperread

This is the repo-local v1 paper reading skill bundle for this repository. It is not a standalone global skill installation. Use it after cloning this repo, installing `uv`, running `uv sync`, and executing commands from the repo root.

Do not copy this directory by itself and expect the workflow to run. The skill depends on the repository's Python package, templates, lockfile, and `zotero-paperread` CLI.

## Entry Routing

- If the user input is a local PDF path and the path exists with suffix `.pdf`, use the local PDF path workflow in `references/pdf-path-workflow.md`.
- Otherwise treat the input as a Zotero title or title fragment and use `references/zotero-workflow.md`.
- For both modes, use full-PDF extraction by default. Pass `--max-pages` only when the user explicitly asks for debugging or a shortened preview.
- For both modes, the CLI creates deterministic evidence artifacts; the agent writes `summary.json` and `review.json` after reading `context.md`, `section_context.md`, and `figure_context.md` when available.

## Shared Rules

- Run commands from the repo root with `uv run zotero-paperread ...`.
- Final evidence locators in `summary.json` must cite `context.md` or `figure_context.md`, not `section_context.md`.
- Rendered note prose should be Chinese-first while preserving titles, names, formulas, method names, units, evidence locators, and tag keys.
- Always run the review and gate sequence before treating a note as ready.
