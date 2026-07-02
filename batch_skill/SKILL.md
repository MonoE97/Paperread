---
name: paperread-batch
description: Use when the user asks to analyze multiple papers from Zotero collections, Zotero titles, PDF folders, or PDF paths by dispatching each item to $paperread and producing a resumable prepare-only batch report.
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

## Routing

Use `references/batch-workflow.md` for all batch workflows:

- Zotero collection.
- Multiple Zotero titles or title fragments.
- Local PDF folder.
- Multiple local PDF paths.

Default Codex concurrency is 3. Claude-compatible fallback is sequential. The
default write policy is `prepare_only`; batch runs must not write Zotero notes.
They must not call Zotero MCP `write_note`. Per-paper 30-second report entries
must be extracted from each single-paper note's `30 秒结论` row, with structured
fallback only when that row is unavailable.
