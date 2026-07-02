# Batch Workflow

Use this workflow when the user asks to analyze multiple papers.

Paperread Batch is a scheduler and reporter. It must dispatch each paper to
`$paperread` for single-paper analysis. It must not copy single-paper prompts,
summary schema, note templates, evidence locator rules, or Zotero write gates.

## Inputs

Supported input sources:

- Zotero collection, expanded to `zotero_item` manifest entries.
- Multiple Zotero titles or title fragments, stored as `zotero_title`.
- Local PDF folder, scanned non-recursively by default into `pdf_path`.
- Multiple local PDF paths, stored as `pdf_path`.

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

Default Codex concurrency is 3. When a worker finishes, dispatch the next
pending item. Claude-compatible fallback is sequential execution of the same
manifest.

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

## Safety

The default write policy is `prepare_only`. Batch execution must not call
Zotero MCP `write_note`. Zotero-backed items may prepare write candidates
through `$paperread`; PDF items remain local-output only.

## Reporting

The batch report is operational, not a literature synthesis. The per-paper
`thirty_second_takeaway` is extracted from that paper's rendered single-paper
note row `30 秒结论`. If the note row is unavailable, fallback to `tldr`, then
`one_sentence_summary`, and record the fallback source.
