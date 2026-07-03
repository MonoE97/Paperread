---
name: paperread-batch
description: Use when the user asks to analyze multiple papers from Zotero collections, Zotero titles, PDF folders, or PDF paths by dispatching each item to $paperread and producing a resumable batch report while keeping PDF items local-only.
---

# Paperread Batch

Paperread Batch orchestrates multiple paper reads. It does not perform deep
single-paper analysis itself. Each paper must be dispatched to `$paperread`,
which remains the owner of extraction, evidence rules, summary schema, note
rendering, and Zotero write gates.

## Setup

Run setup commands from the installed `paperread-batch` skill root:

```bash
uv --version
uv sync --locked
uv run paperread-batch --help
```

`$paperread` must also be installed and available. Batch validation checks the
batch manifest, run directory, and configured `paperread` skill root before
dispatch.

For Zotero-backed batch items, Zotero Desktop and `zotero-mcp-plugin` must be
installed and enabled before dispatch. Use the plugin's Streamable HTTP endpoint
from Zotero preferences, normally `http://127.0.0.1:23120/mcp`.

## Typical Use

- Zotero collection or multiple Zotero titles: use `$paperread-batch` to build a
  manifest, dispatch each item to `$paperread`, then use `next-write` and
  `record-write` for verified Zotero note creation.
- Local PDF folder or multiple PDF paths: use `$paperread-batch` to dispatch
  each PDF to `$paperread` local PDF workflow and generate a batch report; PDF
  items remain local-output only and skip Zotero lookup or duplicate checks.

## Routing

Use `references/batch-workflow.md` for all batch workflows:

- Zotero collection.
- Multiple Zotero titles or title fragments.
- Local PDF folder.
- Multiple local PDF paths.

PDF folder and PDF path items are local-only: do not run Zotero lookup, duplicate checks, next-write, or Zotero write-through for them. Manifest builders store these items as `pdf_path` with `expected_output=local_note`.
An existing directory path passed through `$paperread` should be routed here
instead of being treated as a Zotero title fragment.

Default Codex concurrency is 3. Claude-compatible fallback is sequential. The
default write policy is `zotero_write`: Zotero-backed items must proceed from
prepared candidates to MCP `write_note`, read-only verification, and
`record-write`. Pass `--write-policy prepare_only` only for an explicit dry-run.
PDF items remain local-output only. The batch CLI must not call Zotero MCP
directly; the outer agent performs the write step from `next-write`. Per-paper
30-second report entries must be extracted from each single-paper note's
`30 秒结论` row, with structured fallback only when that row is unavailable.
