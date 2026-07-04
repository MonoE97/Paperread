# Batch Workflow

Use this workflow when the user asks to analyze multiple papers.

Paperread Batch is a scheduler and reporter. It must dispatch each paper to
`$paperread` for single-paper analysis. It must not copy single-paper prompts,
summary schema, note templates, evidence locator rules, or Zotero write gates.

## Prerequisites

Install both skills before running a batch: `paperread` for single-paper work
and `paperread-batch` for scheduling. Zotero-backed batch items also require
Zotero Desktop and `zotero-mcp-plugin` with the integrated MCP server enabled.
Use the local Streamable HTTP endpoint from the plugin preferences, normally
`http://127.0.0.1:23120/mcp`.

## Inputs

Supported input sources:

- Zotero collection, expanded to `zotero_item` manifest entries.
- Multiple Zotero titles or title fragments, stored as `zotero_title`.
- Local PDF folder, scanned non-recursively by default into `pdf_path`.
- Multiple local PDF paths, stored as `pdf_path`.

PDF folder and PDF path items are local-only: do not run Zotero lookup,
duplicate checks, next-write, or Zotero write-through for them. The manifest
must keep them as `input_type=pdf_path` with `expected_output=local_note`.
This applies even when Zotero contains an item with the same title, DOI, or
attachment.

For Zotero collection input, the collection argument must match the read-only
inventory's `collection.key` or `collection.name`. A mismatch stops before
manifest creation.

Manifest `item_id` values must be file-name safe: letters, numbers,
underscore, dot, and hyphen only, with no leading punctuation or path
separators.

## Run Directory

Initialize a batch run after creating a manifest:

```bash
uv run paperread-batch init --manifest manifest.json
```

Without `--output`, `init` allocates `runs/YYYY-MM-DD/<batch-slug>/` under the
installed `paperread-batch` skill root. Pass `--output <batch_run_dir>` only
when the user explicitly wants a custom location.

`init` refuses to write into a directory that already contains `manifest.json`
or `state.json`. Use the existing run with `resume` or `retry-failed`, or choose
a new output directory.

## Execution

Default Codex concurrency is 3. Use `references/parallel-dispatch.md` for the
controller loop and worker prompt contract. When a worker finishes, record the
result, then dispatch the next pending item. If outer-agent parallelism is
unavailable, use the local PDF pre-extraction fallback only for `pdf_path`
items, then continue deep reading sequentially from the prepared
`prepared_analysis_dir` bundles.

## Result Ingestion

Each worker result must include the dispatched `item_id`, `worker_id`, and
`attempt_count`. `record-result` rejects stale results that do not match the
current running or interrupted item assignment.

Workers record artifact paths, not final report conclusions. During result
ingestion, `paperread-batch` derives `thirty_second_takeaway`,
`takeaway_source_type`, `takeaway_source_path`, and `takeaway_source_sha256`
from the rendered single-paper note and `summary.json`.

## Resume

Use `resume` after an interrupted session. It first records complete archived
worker results found at `items/<item_id>.json` for running or interrupted
assignments, then marks the remaining running assignments as interrupted.

## Zotero Write Stage

The default write policy is `zotero_write`. After all eligible Zotero-backed
items have successful single-paper candidates, list pending writes:

```bash
uv run paperread-batch next-write <batch_run_dir> --limit 1
```

For each returned item, read `write_payload` and `note_html`, then call Zotero
MCP `write_note(action="create", parentKey=<payload parentKey>,
content=<contents of note.html>, tags=<payload tags>)`. Verify the created note
with `$paperread` `verify-zotero-note` using the payload's required readback
checks. Record the verified write:

```bash
uv run paperread-batch record-write <batch_run_dir> <item_id> --result write-result.json
```

`write-result.json` must use schema
`paperread-batch.write-result.v1`, include `status: "written"`, the Zotero
`note_key`, `parent_key`, `contentSha256`, and a local `verify_report` path whose
JSON has `status: "passed"`, matching `noteKey`, `parentKey`, and
`contentSha256`.

## Safety

Batch CLI code must not call Zotero MCP `write_note`; it only schedules,
records, and reports. The outer agent performs Zotero writes through MCP from
`next-write`, then records the read-only verification with `record-write`.
Pass manifest builders `--write-policy prepare_only` for an explicit dry-run.
PDF items remain local-output only and are excluded from `next-write`.

## Reporting

The batch report is operational, not a literature synthesis. The per-paper
`thirty_second_takeaway` is extracted from that paper's rendered single-paper
note row `30 秒结论`. If the note row is unavailable, fallback to `tldr`, then
`one_sentence_summary`, and record the fallback source.
