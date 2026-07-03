# Paperread Batch Skill Design

Date: 2026-07-02
Status: implemented and merged to `main` on 2026-07-03

Implementation note: the deterministic `paperread-batch` layer now lives in
`batch_skill/`, with root docs and validators updated for the two-skill install
model. This file is now an implemented design record. Public release still
requires copied-install verification outside the repository.

Follow-up note on 2026-07-03: Zotero-backed batch items now default to verified
`zotero_write`. The batch CLI still does not call Zotero MCP directly; it emits
pending writes with `next-write`, and the outer agent records verified writes
with `record-write`.

## Goal

Add a second skill, `paperread-batch`, for batch paper reading orchestration.
The existing single-paper `paperread` skill remains the only owner of deep
paper reading quality: extraction, evidence rules, summary schema, note
template, review gates, Zotero write payloads, and local PDF note rendering.

The batch skill is a scheduler and reporter. It accepts multiple paper inputs,
normalizes them into a batch manifest, dispatches individual items to
`$paperread`, tracks progress for resume, and produces a lightweight batch
report. It must not copy single-paper prompts, parsing logic, summary schema,
note templates, or write gates.

The first version is Codex-first. It is designed around a small pool of
parallel single-paper agents, with default concurrency 3. Claude-compatible use
can run the same manifest sequentially, but parallel execution is not promised
outside Codex.

## First Principles

Batch reading has three separate problems:

1. Reading one paper well.
2. Running many independent paper reads reliably.
3. Summarizing the run enough for the user to act on it.

Only the first problem requires the full single-paper prompt, schema, evidence
contract, and note rendering logic. Rebuilding those inside a batch skill would
create drift and reduce quality. The batch skill should therefore outsource one
paper at a time to `$paperread` and own only the second and third problems:
queueing, concurrency, resume, item-level failure handling, and a run report.

The batch run must be recoverable from files, not from chat memory. A batch can
be interrupted, resumed, partially retried, or inspected later only if the
manifest, item results, and state transitions are durable artifacts.

Zotero writing must remain explicit and separated from deterministic batch CLI
code. Batch mode now defaults eligible Zotero-backed items to verified
`zotero_write`: the scheduler prepares candidates, emits pending write records,
the outer agent calls Zotero MCP `write_note`, and `record-write` consumes the
read-only verification report from the successful single-paper run.

## Confirmed Decisions

- Use an independent `paperread-batch` skill instead of expanding `paperread`
  with a batch mode.
- Keep the current source `skill/` directory for the single-paper skill. It is
  still installed as `paperread`.
- Add a new source directory, `batch_skill/`, for the batch skill. It is
  installed as `paperread-batch`.
- Do not rename the existing `skill/` source directory in the first batch
  version. Renaming it would churn README, AGENTS, validators, install commands,
  and the already-verified self-contained skill contract without improving batch
  behavior.
- Support four equally important input forms in v1:
  - Zotero collection.
  - Multiple Zotero titles or title fragments.
  - Local PDF folder.
  - Multiple local PDF paths.
- Use a unified manifest as the internal batch input.
- Normalize all executable items to one of three v1 item types:
  `zotero_item`, `zotero_title`, or `pdf_path`. A Zotero collection is an input
  source, not a worker item type; collection entries become `zotero_item`
  records before dispatch.
- Default write policy is `zotero_write` for Zotero-backed items; manifest
  builders keep `--write-policy prepare_only` for explicit dry-run.
- Default Codex concurrency is 3.
- Single-paper artifacts stay in `paperread/runs/...`. The batch run stores
  only indexes, state, item result summaries, and reports.
- The final v1 report is lightweight: counts, per-paper status, each paper's
  30-second takeaway extracted from that paper's own rendered quick-read note,
  candidate output paths, and failure reasons. Research synthesis across the
  batch is out of scope for v1.

## Repository Shape

The source repository is:

```text
Paperread/
  AGENTS.md
  README.md
  README.zh-CN.md
  docs/superpowers/specs/
  docs/superpowers/scripts/
  skill/          # source for installed skill name: paperread
  batch_skill/    # source for installed skill name: paperread-batch
```

Installed skill shape:

```text
~/.codex/skills/
  paperread/
  paperread-batch/
```

`skill/` continues to satisfy the existing self-contained single-paper skill
contract. `batch_skill/` is a separate self-contained skill source with its own
`SKILL.md`, `references/`, scripts, tests, dependency metadata, and validation
script.

Do not put `README.md`, `INSTALLATION_GUIDE.md`, `QUICK_REFERENCE.md`, or
`CHANGELOG.md` inside either installed skill. Root README files remain the human
installation entry point.

## Skill Boundary

### `paperread`

The single-paper skill owns:

- Zotero title or title-fragment workflow.
- Local PDF path workflow.
- Full-PDF extraction defaults.
- Evidence artifacts such as `context.md`, `section_context.md`, and
  `figure_context.md`.
- Canonical evidence locator validation.
- `summary.json` and `review.json` contract.
- Note rendering from `templates/zotero_note.md.j2`.
- `prepare-write-candidate` for Zotero title workflow.
- `prepare-local-note-candidate` for local PDF workflow.
- Zotero write gate, write payload, and read-only write verification.

### `paperread-batch`

The batch skill owns:

- Batch input normalization.
- `manifest.json` creation and validation.
- `state.json` initialization, validation, and resume.
- Work queue selection.
- Codex-first parallel dispatch instructions.
- Item result recording and aggregation.
- Batch report generation.
- Retry planning for failed or interrupted items.

It must not own:

- Single-paper extraction logic.
- Summary schema.
- Evidence locator rules.
- Note template.
- Zotero write payload construction.
- Zotero writes.
- Local PDF note rendering.

## Input Model

V1 supports four input types.

### Zotero Collection

The user provides a Zotero collection identity. The batch skill expands it into
`zotero_item` records before dispatch. Expansion should preserve enough metadata
for traceability, including collection name or key, Zotero item key when
available, title, and original order if the source provides one.

Duplicate normalized titles should be detected during manifest validation when
enough metadata is available. If exact duplicate Zotero items are found, the
manifest should mark them as blocked or require the single-paper workflow to
stop according to the current `paperread` Zotero duplicate rule.

### Multiple Zotero Titles

The user provides several titles or title fragments. Each becomes a manifest
item of type `zotero_title`. Exact matching and duplicate handling remain part
of the `$paperread` single-paper workflow.

### Local PDF Folder

The user provides a directory. V1 scans direct child files whose suffix is
`.pdf` case-insensitively by default. Recursive scanning is an explicit option
because accidental recursive scans can pull in old analysis directories,
downloads, or unrelated papers.

The manifest stores absolute PDF paths to make resume independent of the
current shell directory.

### Multiple PDF Paths

The user provides several file paths. The manifest stores each as an absolute
path after validating existence and `.pdf` suffix.

## Manifest

`manifest.json` is the immutable plan for one batch run. If the user wants to
add or remove papers after the run starts, create a new batch or explicitly
derive a new manifest version. Do not silently mutate the manifest during
execution.

Recommended shape:

```json
{
  "schema_version": "paperread-batch.manifest.v1",
  "created_at": "2026-07-02T10:00:00+08:00",
  "batch_title": "solid-state-electrolytes",
  "default_concurrency": 3,
  "write_policy": "zotero_write",
  "source_summary": {
    "source_type": "mixed",
    "description": "Zotero collection plus local PDFs"
  },
  "items": [
    {
      "item_id": "001",
      "input_type": "zotero_item",
      "input": {
        "item_key": "CABS9KGA",
        "title": "Polyanion-stabilized amorphous halide electrolytes..."
      },
      "expected_output": "zotero_note_candidate"
    },
    {
      "item_id": "002",
      "input_type": "zotero_title",
      "input": {
        "title": "A title or title fragment"
      },
      "expected_output": "zotero_note_candidate"
    },
    {
      "item_id": "003",
      "input_type": "pdf_path",
      "input": {
        "path": "/abs/path/paper.pdf"
      },
      "expected_output": "local_note"
    }
  ]
}
```

`item_id` should be stable and human-readable. Numeric IDs such as `001`, `002`,
and `003` are sufficient for v1 as long as they are unique within the manifest.
IDs must be safe local filenames: letters, numbers, underscore, dot, and hyphen
only, with no leading punctuation or path separators.

`expected_output` should be one of:

- `zotero_note_candidate` for Zotero-backed inputs.
- `local_note` for PDF-only inputs.

`input_type` should be one of:

- `zotero_item`: a Zotero-backed item already resolved from a collection or
  read-only inventory step.
- `zotero_title`: a title or title fragment that `$paperread` must resolve.
- `pdf_path`: a local PDF path.

## State

`state.json` is mutable and represents the current execution state.

Batch statuses:

- `pending`
- `running`
- `completed`
- `completed_pending_writes`
- `completed_with_failures`
- `blocked`

Item statuses:

- `pending`
- `running`
- `succeeded`
- `failed`
- `skipped`
- `blocked`
- `interrupted`

Each item state should record:

- `item_id`
- `input_type`
- `status`
- `attempt_count`
- `worker_id`
- `started_at`
- `completed_at`
- `write_status`
- `write_completed_at`
- `paperread_run_dir`
- `summary_json`
- `note_md`
- `note_html`
- `gate_report`
- `write_payload`
- `verify_report`
- `local_note_path`
- `zotero_note_key`
- `zotero_parent_key`
- `content_sha256`
- `thirty_second_takeaway`
- `takeaway_source_type`
- `takeaway_source_path`
- `takeaway_source_sha256`
- `failure_reason`
- `resume_decision`

The state file is the scheduler's merged view. Workers should not edit it
directly. The scheduler should write `state.json` with a temporary file and
atomic rename so interrupted writes do not leave partial JSON.

## Item Result Files

To avoid concurrent writes to `state.json`, each worker writes one item result
file:

```text
batch_skill/runs/YYYY-MM-DD/<batch-slug>/
  items/
    001.json
    002.json
```

Recommended result shape:

```json
{
  "schema_version": "paperread-batch.item-result.v1",
  "item_id": "001",
  "worker_id": "worker-001",
  "attempt_count": 1,
  "status": "succeeded",
  "paperread_run_dir": "/abs/path/to/paperread/runs/2026-07-02/paper-slug",
  "summary_json": "/abs/path/to/summary.json",
  "note_md": "/abs/path/to/note.md",
  "note_html": "/abs/path/to/note.html",
  "gate_report": "/abs/path/to/gate-report.json",
  "write_payload": "/abs/path/to/write-payload.json",
  "local_note_path": "",
  "failure_reason": ""
}
```

For local PDF items, `write_payload` stays empty and `local_note_path` should
point to the final Markdown note beside the PDF.

`worker_id` and `attempt_count` must match the current dispatched assignment.
This prevents stale workers from overwriting a later retry.

`thirty_second_takeaway` is not supplied by the worker as trusted data. During
`record-result`, the batch layer derives it from the single-paper note's own
quick-read result, specifically the `30 秒结论` row under `## 0. 阅读结论` in the
rendered note. If that row is unavailable, `record-result` falls back to the
single-paper structured fields that feed that row (`tldr`, then
`one_sentence_summary`) and records the fallback in `takeaway_source_type`.

## Batch Run Directory

The batch skill stores its own run artifacts under its installed skill root:

```text
<paperread-batch-skill-root>/
  runs/
    YYYY-MM-DD/
      <batch-slug>/
        manifest.json
        state.json
        items/
          001.json
          001.write.json
          002.json
        batch-report.json
        batch-report.md
```

The single-paper artifacts remain wherever `$paperread` creates them, usually
under the installed `paperread/runs/YYYY-MM-DD/<title-slug>/` for Zotero title
workflow or beside the source PDF for local PDF workflow. The batch state stores
absolute paths to those artifacts.

This index-only design keeps ownership clear. The batch run is not a copy of
the single-paper run; it is a ledger that points to single-paper evidence and
candidate outputs.

## Execution Model

V1 uses Codex-first parallel scheduling.

1. Build or receive `manifest.json`.
2. Validate manifest, the batch run directory, and `$paperread` availability.
3. Initialize `state.json`.
4. Ask the batch CLI for the next available work items, up to the concurrency
   limit.
5. Dispatch each selected item to a separate `$paperread` single-paper worker.
6. Each worker completes the single-paper workflow and writes an item result
   file.
7. The batch scheduler records each result into `state.json` using atomic
   writes.
8. When a worker slot opens, dispatch the next pending item.
9. When no pending or running items remain, generate reports.
10. For Zotero-backed `pending_write` items, use `next-write` to emit one
    prepared candidate, perform MCP `write_note` outside the batch CLI, verify
    the note read-only, and record the result with `record-write`.

Default concurrency is 3. The concurrency setting is a scheduler limit, not a
promise that every host supports real parallel agent execution.

Claude fallback uses the same manifest and item result contract but processes
items sequentially.

## Worker Contract

Each worker receives one manifest item and must follow these rules:

- For `zotero_item` or `zotero_title`, use `$paperread` Zotero title
  workflow.
- For `pdf_path`, use `$paperread` local PDF path workflow.
- Use full-PDF extraction unless the user explicitly requested shortened
  debugging or preview.
- Do not write Zotero notes inside the single-paper worker.
- For Zotero items, stop after `prepare-write-candidate` succeeds or fails; the
  batch write stage handles MCP write-through later.
- For local PDF items, stop after `prepare-local-note-candidate` succeeds or
  fails.
- Record the actual `paperread_run_dir`, key output paths, `worker_id`, and
  `attempt_count`.
- Do not write a trusted `thirty_second_takeaway` in the result file. The batch
  scheduler extracts it from the completed single-paper note's existing
  `30 秒结论` row during `record-result`. Do not ask the batch worker to
  summarize the paper again, and do not infer the takeaway from the title alone.
- Let `record-result` compute the takeaway source path and hash so the batch
  report can be traced back to the exact single-paper artifact used.
- Write exactly one result file under the batch run's `items/` directory.

Single-paper failure does not stop the whole batch unless the failure proves a
system-wide blocker.

## Deterministic CLI

Even though the reading work is agent-driven, `paperread-batch` needs a
deterministic CLI for durable state. The CLI should manage manifests, state,
result ingestion, and reports. It should not perform deep reading.

Proposed command surface:

```bash
uv run paperread-batch init --manifest manifest.json --output <batch_run>
uv run paperread-batch validate <batch_run>
uv run paperread-batch next <batch_run> --limit 3
uv run paperread-batch record-result <batch_run> <item_id> --result <batch_run>/items/<item_id>.json
uv run paperread-batch next-write <batch_run> --limit 1
uv run paperread-batch record-write <batch_run> <item_id> --result write-result.json
uv run paperread-batch report <batch_run>
uv run paperread-batch resume <batch_run>
uv run paperread-batch retry-failed <batch_run>
```

`init` must fail with an explicit `batch_run_exists` error when the selected
output directory already contains `manifest.json` or `state.json`. Re-running
`init` must not reset an existing run; the operator should use `resume`,
`retry-failed`, or a new output directory.

Optional manifest builders can be added around the same manifest schema:

```bash
uv run paperread-batch manifest from-pdf-folder "/abs/path/to/folder" --batch-title "folder batch" --output manifest.json
uv run paperread-batch manifest from-pdf-paths paths.txt --batch-title "paths batch" --output manifest.json
uv run paperread-batch manifest from-zotero-titles titles.txt --batch-title "titles batch" --output manifest.json
uv run paperread-batch manifest from-zotero-collection "<collection>" --items-json collection-items.json --batch-title "collection batch" --output manifest.json
uv run paperread-batch manifest from-zotero-titles titles.txt --batch-title "dry run" --write-policy prepare_only --output manifest.json
```

The Zotero collection builder may require MCP access or a read-only inventory
step. It must not write Zotero.

`validate` must fail early if the batch run directory is not writable, the
manifest uses an unknown item type, or `$paperread` is not installed or cannot
be invoked in the current agent environment.

## Resume Rules

Resume must be file-backed and conservative.

- `succeeded` items are not rerun by default.
- `failed` items are not rerun unless `retry-failed` is requested.
- `skipped` items stay skipped unless explicitly reset.
- `running` and `interrupted` items are inspected before any status downgrade:
  - If a complete item result file exists under `items/<item_id>.json`, record
    it through the same `record-result` validation path.
  - Zotero-backed items can be recovered as succeeded only when `summary.json`,
    `note.md`, `note.html`, and `gate-report.json` are readable; if the gate is
    write-ready, `write-payload.json` must also exist.
  - PDF items can be recovered as succeeded only when `summary.json`, the local
    gate report, and the final local note path are readable.
- Remaining `running` items without a valid archived result are then marked
  `interrupted`; `retry-failed` is the explicit path that moves interrupted
  items back to `pending`.
- The manifest is not silently edited during resume.

## Reporting

`batch-report.json` is the machine-readable aggregate. `batch-report.md` is the
human report.

Required report content:

- Batch title.
- Created time and report time.
- Input source summary.
- Configured concurrency.
- Write policy.
- Total item count.
- Counts by item status.
- Counts by expected output type.
- Per-item table with:
  - item id
  - input type
  - status
  - title or input label
  - 30-second takeaway copied from the single-paper quick-read note
  - key output paths
  - failure reason
  - write status
  - takeaway source artifact

Write status values:

- `prepared_not_written`
- `pending_write`
- `written`
- `not_applicable`
- `blocked`
- `failed`

The report should avoid long paper summaries. V1 is a run report, not a
literature review. The batch report does not create new per-paper summaries; it
only extracts the already-rendered 30-second quick-read result from each
successful single-paper note.

## Error Handling

Failures should be classified so the scheduler can continue when appropriate.

### Item Failure

Examples:

- PDF cannot be parsed.
- Zotero title has duplicate normalized matches.
- The single-paper gate fails.
- `summary.json` is missing or invalid.

Action: mark the item failed, store `failure_reason`, continue other items.

### System Blocker

Examples:

- `$paperread` is not installed or not discoverable.
- `uv sync --locked` fails for the required skill environment.
- Zotero MCP is unavailable and all remaining items require Zotero access.
- The batch run directory is not writable.

Action: mark the batch `blocked` and stop dispatching new items.

### Interruption

Examples:

- The Codex session stops while workers are running.
- The process is interrupted after a worker completed but before state merge.

Action: recover from item result files and single-paper output paths before
deciding whether to rerun.

## Safety Boundaries

- Batch default is `zotero_write` for Zotero-backed items.
- Batch CLI code must not call Zotero MCP `write_note`; the outer agent performs
  the write from `next-write` and records read-only verification with
  `record-write`.
- `prepare_only` remains available as an explicit dry-run write policy.
- PDF-only items never create `write-payload.json`.
- Local PDF items remain local-output only.
- Batch reports can link to `write-payload.json`; they must not treat its
  existence as permission to write.
- Batch must not cite secondary context as canonical evidence; that rule remains
  enforced by `paperread`.
- Machine-readable state may store absolute local paths. Human reports should
  mark paths as local-only and prefer concise display labels where possible, so
  reports are not accidentally treated as portable or share-safe artifacts.

## Testing Strategy

The first implementation should test deterministic batch behavior without
requiring real LLM reading.

Required test groups:

- Manifest validation:
  - Valid Zotero title manifest.
  - Valid PDF path manifest.
  - Mixed manifest.
  - Duplicate `item_id`.
  - Path-like or unsafe `item_id`.
  - Missing or invalid required fields.
- Manifest builders:
  - PDF folder scan ignores non-PDF files and accepts `.pdf` suffixes
    case-insensitively.
  - PDF folder scan is non-recursive by default.
  - PDF paths are made absolute.
- State transitions:
  - `init` refuses to overwrite an existing run directory containing
    `manifest.json` or `state.json`.
  - `pending -> running -> succeeded`.
  - `pending -> running -> failed`.
  - Illegal transitions are rejected.
  - Batch status becomes `completed_with_failures` when appropriate.
- Item result ingestion:
  - Valid result updates state.
  - Result item id must match target item id.
  - Result `worker_id` and `attempt_count` must match the current assignment.
  - Missing critical output paths are handled according to status.
  - Worker-supplied takeaway text and hash are ignored or rejected in favor of
    artifact-derived values.
- Resume:
  - Existing `succeeded` is preserved.
  - Running item with archived result file is recovered before other running
    items are marked interrupted.
  - Interrupted item without result is not treated as succeeded.
- Report:
  - Markdown and JSON reports are deterministic.
  - Counts match state.
  - 30-second takeaways appear only for completed items.
  - 30-second takeaways are extracted from rendered single-paper note artifacts
    or explicitly marked as structured-field fallback.
  - Takeaway source path and source hash are recorded.
- Quick-read extraction:
  - Extracts the `30 秒结论` table row from a rendered single-paper `note.md`.
  - Falls back to `tldr`, then `one_sentence_summary`, only when the rendered
    note is unavailable or does not contain the row.
  - Does not call an LLM or create a new per-paper summary.
- Skill validation:
  - `batch_skill/SKILL.md` has `name: paperread-batch`.
  - No forbidden in-skill docs exist.
  - Required references, scripts, tests, pyproject, and lockfile exist.

Integration testing with real `$paperread` workers can come after deterministic
tests. It should use small fixtures and dry-run or prepare-only paths.

## Documentation Updates

Root docs now explain that this repo contains two installable skill sources:

- `skill/` -> install as `paperread`.
- `batch_skill/` -> install as `paperread-batch`.

`README.md`, `README.zh-CN.md`, `AGENTS.md`, and root validators were updated
with the `batch_skill/` implementation. The active install contract is now the
two-skill model documented in those root files; this design document records the
rationale and constraints behind that implemented contract.

The batch skill's `SKILL.md` should be lean:

- State that it orchestrates multiple papers by dispatching to `$paperread`.
- State that `$paperread` must be installed and available.
- Route detailed workflow rules to `references/batch-workflow.md`.
- State default concurrency 3 for Codex.
- State default `zotero_write` policy and explicit `prepare_only` dry-run.
- State Claude sequential fallback.

## Implemented Order

The implementation followed this order after the review corrections:

1. Scaffold `batch_skill/` as a self-contained installable skill named
   `paperread-batch`.
2. Implement manifest schema and builders for `zotero_item`, `zotero_title`,
   and `pdf_path`.
3. Implement state schema, legal transitions, item result ingestion, and atomic
   JSON/Markdown writes.
4. Implement `$paperread` availability validation as a preflight gate.
5. Implement quick-read extraction from rendered single-paper notes:
   - first parse the `30 秒结论` row in `note.md`;
   - fall back to structured `tldr`;
   - then fall back to `one_sentence_summary`;
   - record source type, source path, and source hash.
6. Implement report generation from state and item results. The report copies
   per-paper quick-read results; it does not summarize papers again.
7. Write `references/batch-workflow.md` to describe Codex-first parallel
   dispatch and Claude sequential fallback.
8. Add deterministic tests for schema validation, state transitions, atomic
   writes, quick-read extraction, resume, and report generation.
9. Only after the deterministic layer is stable, connect the real `$paperread`
   worker contract and run prepare-only integration checks.

## Non-Goals For V1

- Do not rename existing `skill/` to `paperread/`.
- Do not copy single-paper prompt/schema/template logic into `batch_skill/`.
- Do not let batch CLI directly call Zotero MCP or write without verification.
- Do not make research synthesis the default report.
- Do not require all inputs to be Zotero-backed.
- Do not require all inputs to be PDF-backed.
- Do not build a background daemon, database, or external queue.
- Do not add global dependencies or system configuration.

## Future Extensions

Potential later work:

- `synthesize` command that reads all successful single-paper `summary.json`
  files and creates a research-level cross-paper analysis.
- Richer write approval UX for Zotero-backed successful items.
- Priority scheduling.
- Per-source concurrency limits.
- Recursive PDF folder scanning with ignore rules.
- Better duplicate grouping across Zotero and local PDFs.
- HTML report.
- Export package containing the batch report plus selected single-paper notes.

These extensions should not be included in the first implementation unless the
core scheduler and report contracts are already stable.

## Implementation Status

The design was implemented with these constraints:

- `paperread-batch` is a separate installed skill.
- `skill/` remains the source directory for `paperread` in v1.
- `batch_skill/` is the source directory for `paperread-batch`.
- Zotero-backed batch reads default to verified `zotero_write`; `prepare_only`
  remains the explicit dry-run policy.
- Parallel execution is Codex-first with sequential fallback.
- Batch stores indexes and reports, not copies of complete single-paper runs.
- V1 report is operational, not a literature synthesis.
- V1 30-second report entries are copied from single-paper quick-read notes, not
  regenerated by the batch layer.
