---
name: paperread
description: Use when the user asks to analyze a paper either by Zotero title/title fragment or by local PDF path, producing a Chinese structured reading note with evidence-grounded summary, review, and local or Zotero output gates.
---

# Paperread

Paperread is a self-contained paper reading skill. Run bundled CLI commands from the installed skill root with `uv run paperread ...` after the skill environment has been synchronized with `uv sync --locked`.

## Environment Setup

Run setup commands from the installed skill root:

```bash
uv --version
uv sync --locked
uv run paperread --help
```

If `uv sync --locked` reports that Python `>=3.13` is unavailable, run `uv python install 3.13` from the skill root, then retry `uv sync --locked`. If `uv` itself is missing, stop and ask the user to install `uv` first; do not use `pip`, `conda`, or system Python as a substitute.

## Entry Routing

- If the user input is a local PDF path and the path exists with suffix `.pdf`, use the local PDF path workflow in `references/pdf-path-workflow.md`.
- Otherwise treat the input as a Zotero title or title fragment and use `references/zotero-workflow.md`.
- For both modes, use full-PDF extraction by default. Pass `--max-pages` only when the user explicitly asks for debugging or a shortened preview.
- For both modes, the CLI creates deterministic evidence artifacts; the agent writes `summary.json` and `review.json` after reading `context.md`, `section_context.md`, and `figure_context.md` when available.

## Shared Rules

- Final evidence locators in `summary.json` must use canonical forms: `context.md page <N>`, `context.md page <N> section <Section Name>`, `context.md page <N> section <Section Name> table_candidate <N>`, or `figure_context.md <figure_id>`. Do not use bare `context.md` / `figure_context.md`, prose locators such as `page 3 method section`, `section_context.md`, or secondary context paths.
- Secondary context is cross-check material only and must not be cited in `evidence_summary`.
- Rendered note prose should be Chinese-first while preserving titles, names, formulas, method names, units, evidence locators, and tag keys.
- Always run the review and gate sequence before treating a note as ready.
- Zotero writes are allowed only through Zotero MCP `write_note` after explicit user write intent.
- Local PDF path analysis is local-output only; it must not call Zotero write or live-note refresh commands.
