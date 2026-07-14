---
name: paper_reader
description: Use when the user asks to analyze a paper by Zotero title/title fragment, local PDF path, or local directory path under the Paper Reader 2.0 contract, producing a Chinese structured note with immutable review, candidate, publication, authorization, and verification boundaries.
---

# paper_reader

paper_reader is a self-contained paper reading skill. This file defines the released Paper Reader 2.0 runtime contract and its grouped CLI. Run bundled commands from the installed skill root with `uv run paper_reader ...` after synchronization with `uv sync --locked`.

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

- Zotero title or title fragment: use `$paper_reader` with the paper title. The agent uses the read-only `scripts/discover-zotero-item.py` helper (or the equivalent injected-tool procedure), saves the exact search inventory, selected item details and authoritative parent identity as a provenance-preserving discovery bundle, initializes a V2 run, prepares immutable evidence, validates and seals a review package, builds and previews an immutable candidate, creates a short-lived immutable authorization only after explicit write intent, lets the external agent call MCP `write_note` at most once, then verifies or reconciles read-only.
- Local PDF path: use `$paper_reader` with a `.pdf` path. The grouped workflow reserves `<pdf_stem>_analysis/` and `<pdf_stem>_note.md`, prepares immutable evidence, seals review, builds an immutable candidate and publishes with no-replace semantics. It never searches Zotero and never creates a Zotero authorization.
- Local directory path: route to `$paper_reader_batch` with the local PDF folder workflow. Directory input is not a Zotero title fragment.

## Paper Reader 2.0 Grouped CLI

The public grouped CLI is:

```text
uv run paper_reader route
uv run paper_reader run init-local
uv run paper_reader run init-zotero
uv run paper_reader run prepare
uv run paper_reader run status
uv run paper_reader run validate
uv run paper_reader review validate
uv run paper_reader review seal
uv run paper_reader candidate build
uv run paper_reader local publish
uv run paper_reader zotero authorize
uv run paper_reader zotero verify
uv run paper_reader zotero reconcile
uv run paper_reader maintenance
```

Active V2 schema identifiers are `paper_reader.run.v2`, `paper_reader.summary.v2`, `paper_reader.review.v2`, `paper_reader.review-package.v2`, `paper_reader.candidate.v2`, `paper_reader.write-authorization.v2`, `paper_reader.verification.v2`, `paper_reader.reconciliation.v2`, and `paper_reader.command-result.v2`. Every model is strict and uses `extra=forbid`; V2 code must not coerce, guess or accept unknown fields.

Operational commands emit exactly one `paper_reader.command-result.v2` JSON object on stdout and diagnostics on stderr. V1/unversioned artifacts are historical-only and must fail before locks, output allocation, network access or writes with `unsupported_run_schema`; there are no compatibility aliases, migration loaders, schema guessing or hidden V1 fallbacks.

## Entry Routing

- If the user input resolves to an existing local path with suffix `.pdf`, use the local PDF path workflow in `references/pdf-path-workflow.md`.
- If the user input resolves to an existing local directory path, delegate to `$paper_reader_batch` and its local PDF folder workflow. If `$paper_reader_batch` is unavailable, ask the user to install or enable it; do not fall back to Zotero title search.
- Local PDF path and directory path inputs skip Zotero lookup and duplicate checks, including same-title or same-DOI checks.
- Existing local paths are not Zotero title fragments.
- Only non-path text should be treated as a Zotero title or title fragment and use `references/zotero-workflow.md`.
- For both modes, use full-PDF extraction by default. Use the V2 `--preview-pages` option only when the user explicitly asks for debugging or a shortened preview; preview evidence can never produce a candidate.
- For both modes, the CLI creates deterministic immutable evidence artifacts; it does not replace the agent's paper-reading step. The agent prepares `paper_reader.summary.v2` and `paper_reader.review.v2` after reading `context.md`, `section_context.md`, and `figure_context.md` when available, then seals `paper_reader.review-package.v2` before candidate construction.

## Shared Rules

- Final evidence locators in `summary.json` must use canonical forms: `context.md page <N>`, `context.md page <N> section <Section Name>`, `context.md page <N> section <Section Name> table_candidate <N>`, or `figure_context.md <figure_id>`. Do not use bare `context.md` / `figure_context.md`, prose locators such as `page 3 method section`, `section_context.md`, or secondary context paths.
- Secondary context is cross-check material only and must not be cited in `evidence_summary`.
- Rendered note prose should be Chinese-first while preserving titles, names, formulas, method names, units, evidence locators, and tag keys.
- Run `uv run python scripts/lint-summary.py <run_dir>/summary.json` before computing the review's summary hash; this early lint does not replace strict `review validate` or `review seal` gates.
- Always seal review before candidate build; candidates and authorizations are immutable and hash-bound. Any source, evidence, review, target, note, tag or hash change requires rebuilding the successor artifact.
- Zotero writes are allowed only through the external agent and Zotero MCP `write_note` after explicit user write intent and a valid immutable authorization. The CLI itself must not call MCP `write_note`.
- Local PDF path analysis is local-output only; it must not call Zotero lookup, duplicate-check, write, or live-note refresh commands.
