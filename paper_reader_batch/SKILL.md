---
name: paper_reader_batch
description: Use when the user asks to analyze multiple papers from Zotero collections, Zotero titles, PDF folders, or PDF paths by dispatching each item to $paper_reader and producing a resumable batch report while keeping PDF items local-only.
---

# paper_reader_batch

paper_reader_batch orchestrates multiple paper reads. It does not perform deep
single-paper analysis itself. Each paper must be dispatched to `$paper_reader`,
which remains the owner of extraction, evidence rules, summary schema, note
rendering, and Zotero write gates.

## Setup

Run setup commands from the installed `paper_reader_batch` skill root:

```bash
uv --version
uv sync --locked
uv run paper_reader_batch --help
```

`$paper_reader` must also be installed and available. Batch validation checks the
batch manifest, run directory, and configured `paper_reader` skill root before
dispatch.

For Zotero-backed batch items, Zotero Desktop and `zotero-mcp-plugin` must be
installed and enabled before dispatch. Use the plugin's Streamable HTTP endpoint
from Zotero preferences, normally `http://127.0.0.1:23120/mcp`.

## Typical Use

- Zotero collection or multiple Zotero titles: use `$paper_reader_batch` to build a
  manifest, dispatch each item to `$paper_reader`, then use `next-write` and
  `record-write` for verified Zotero note creation.
- Local PDF folder or multiple PDF paths: use `$paper_reader_batch` to dispatch
  each PDF to `$paper_reader` local PDF workflow and generate a batch report; PDF
  items remain local-output only and skip Zotero lookup or duplicate checks.

## Routing

Use `references/batch-workflow.md` for all batch workflows:

- Zotero collection.
- Multiple Zotero titles or title fragments.
- Local PDF folder.
- Multiple local PDF paths.

Use `references/parallel-dispatch.md` for concurrency, worker prompt,
fallback pre-extraction, and serial write rules. Use
`references/worker-result-contract.md` when constructing or validating item
result, local prepare result, or write result JSON.

PDF folder and PDF path items are local-only: do not run Zotero lookup, duplicate checks, next-write, or Zotero write-through for them. Manifest builders store these items as `pdf_path` with `expected_output=local_note`.
An existing directory path passed through `$paper_reader` should be routed here
instead of being treated as a Zotero title fragment.

Default Codex concurrency is 3. When outer-agent parallelism is unavailable,
use `prepare-local-pdfs` as the fallback pre-extraction path for local PDF
items, then continue deep reading from the prepared bundles. `prepare-local-pdfs`
uses `$paper_reader prepare-pdf --json-output` as the stable machine channel; if
it must recover from `run.json`, accept only a manifest with `status=prepared`
and readable core analysis artifacts. Initialized or partial local PDF bundles
are not prepared bundles. The default write policy is `zotero_write`:
Zotero-backed items must proceed from prepared candidates to MCP `write_note`,
read-only verification, and `record-write`. Pass `--write-policy prepare_only`
only for an explicit dry-run. PDF items remain local-output only; a pure local
PDF batch report should be read as `effective_write_policy=local_only`. The
batch CLI must not call Zotero MCP directly; the outer agent performs the write
step from `next-write`. Per-paper 30-second report entries must be extracted
from each single-paper note's `30 秒结论` row, with structured fallback only
when that row is unavailable.
