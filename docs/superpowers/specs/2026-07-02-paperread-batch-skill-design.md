# Paperread Batch Skill Design

Date: 2026-07-02
Status: design for review

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

Zotero writing must remain explicit and separate. Batch mode can prepare write
candidates, but it must not create Zotero notes by default. A later write phase
must require a separate user action and should consume the prepared payloads
from successful single-paper runs.

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
- Default write policy is `prepare_only`.
- Default Codex concurrency is 3.
- Single-paper artifacts stay in `paperread/runs/...`. The batch run stores
  only indexes, state, item result summaries, and reports.
- The final v1 report is lightweight: counts, per-paper status, each paper's
  30-second takeaway, candidate output paths, and failure reasons. Research
  synthesis across the batch is out of scope for v1.

## Repository Shape

The source repository should become:

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
paper items before dispatch. Expansion should preserve enough metadata for
traceability, including collection name or key, Zotero item key when available,
title, and original order if the source provides one.

Duplicate normalized titles should be detected during manifest validation when
enough metadata is available. If exact duplicate Zotero items are found, the
manifest should mark them as blocked or require the single-paper workflow to
stop according to the current `paperread` Zotero duplicate rule.

### Multiple Zotero Titles

The user provides several titles or title fragments. Each becomes a manifest
item of type `zotero_title`. Exact matching and duplicate handling remain part
of the `$paperread` single-paper workflow.

### Local PDF Folder

The user provides a directory. V1 scans direct child `*.pdf` files by default.
Recursive scanning is an explicit option because accidental recursive scans can
pull in old analysis directories, downloads, or unrelated papers.

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
  "write_policy": "prepare_only",
  "source_summary": {
    "source_type": "mixed",
    "description": "Zotero collection plus local PDFs"
  },
  "items": [
    {
      "item_id": "001",
      "input_type": "zotero_title",
      "input": "Polyanion-stabilized amorphous halide electrolytes...",
      "expected_output": "zotero_note_candidate"
    },
    {
      "item_id": "002",
      "input_type": "pdf_path",
      "input": "/abs/path/paper.pdf",
      "expected_output": "local_note"
    }
  ]
}
```

`item_id` should be stable and human-readable. Numeric IDs such as `001`, `002`,
and `003` are sufficient for v1 as long as they are unique within the manifest.

`expected_output` should be one of:

- `zotero_note_candidate` for Zotero-backed inputs.
- `local_note` for PDF-only inputs.

## State

`state.json` is mutable and represents the current execution state.

Batch statuses:

- `pending`
- `running`
- `completed`
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
- `paperread_run_dir`
- `summary_json`
- `note_md`
- `note_html`
- `gate_report`
- `write_payload`
- `local_note_path`
- `thirty_second_takeaway`
- `failure_reason`
- `resume_decision`

The state file is the scheduler's merged view. Workers should not edit it
directly.

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
  "status": "succeeded",
  "paperread_run_dir": "/abs/path/to/paperread/runs/2026-07-02/paper-slug",
  "summary_json": "/abs/path/to/summary.json",
  "note_md": "/abs/path/to/note.md",
  "note_html": "/abs/path/to/note.html",
  "gate_report": "/abs/path/to/gate-report.json",
  "write_payload": "/abs/path/to/write-payload.json",
  "local_note_path": "",
  "thirty_second_takeaway": "这篇论文研究...核心结论是...主要风险是...",
  "failure_reason": ""
}
```

For local PDF items, `write_payload` stays empty and `local_note_path` should
point to the final Markdown note beside the PDF.

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
2. Validate manifest.
3. Initialize `state.json`.
4. Ask the batch CLI for the next available work items, up to the concurrency
   limit.
5. Dispatch each selected item to a separate `$paperread` single-paper worker.
6. Each worker completes the single-paper workflow and writes an item result
   file.
7. The batch scheduler records each result into `state.json`.
8. When a worker slot opens, dispatch the next pending item.
9. When no pending or running items remain, generate reports.

Default concurrency is 3. The concurrency setting is a scheduler limit, not a
promise that every host supports real parallel agent execution.

Claude fallback uses the same manifest and item result contract but processes
items sequentially.

## Worker Contract

Each worker receives one manifest item and must follow these rules:

- For `zotero_collection_item` or `zotero_title`, use `$paperread` Zotero title
  workflow.
- For `pdf_path`, use `$paperread` local PDF path workflow.
- Use full-PDF extraction unless the user explicitly requested shortened
  debugging or preview.
- Do not write Zotero notes.
- For Zotero items, stop after `prepare-write-candidate` succeeds or fails.
- For local PDF items, stop after `prepare-local-note-candidate` succeeds or
  fails.
- Record the actual `paperread_run_dir` and key output paths.
- Derive `thirty_second_takeaway` from the completed single-paper
  `summary.json`; do not infer it from the title alone.
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
uv run paperread-batch report <batch_run>
uv run paperread-batch retry-failed <batch_run>
```

Optional manifest builders can be added around the same manifest schema:

```bash
uv run paperread-batch manifest from-pdf-folder "/abs/path/to/folder" --output manifest.json
uv run paperread-batch manifest from-pdf-paths paths.txt --output manifest.json
uv run paperread-batch manifest from-zotero-titles titles.txt --output manifest.json
uv run paperread-batch manifest from-zotero-collection "<collection>" --output manifest.json
```

The Zotero collection builder may require MCP access or a read-only inventory
step. It must not write Zotero.

## Resume Rules

Resume must be file-backed and conservative.

- `succeeded` items are not rerun by default.
- `failed` items are not rerun unless `retry-failed` is requested.
- `skipped` items stay skipped unless explicitly reset.
- `running` items on startup are treated as interrupted.
- Interrupted items are inspected:
  - If a complete item result file exists, record it.
  - If single-paper output paths can prove completion, record a recovered
    result.
  - Otherwise mark the item `interrupted` or move it back to `pending`,
    depending on the chosen CLI option.
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
  - 30-second takeaway
  - key output paths
  - failure reason
  - write status

Write status values:

- `prepared_not_written`
- `not_applicable`
- `blocked`
- `failed`

The report should avoid long paper summaries. V1 is a run report, not a
literature review.

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

- Batch default is `prepare_only`.
- Batch must not call Zotero MCP `write_note` in v1 default execution.
- Zotero writes, if added later as a phase, must require explicit user intent
  and must consume the single-paper `write-payload.json` and `note.html`.
- PDF-only items never create `write-payload.json`.
- Local PDF items remain local-output only.
- Batch reports can link to `write-payload.json`; they must not treat its
  existence as permission to write.
- Batch must not cite secondary context as canonical evidence; that rule remains
  enforced by `paperread`.

## Testing Strategy

The first implementation should test deterministic batch behavior without
requiring real LLM reading.

Required test groups:

- Manifest validation:
  - Valid Zotero title manifest.
  - Valid PDF path manifest.
  - Mixed manifest.
  - Duplicate `item_id`.
  - Missing or invalid required fields.
- Manifest builders:
  - PDF folder scan ignores non-PDF files.
  - PDF folder scan is non-recursive by default.
  - PDF paths are made absolute.
- State transitions:
  - `pending -> running -> succeeded`.
  - `pending -> running -> failed`.
  - Illegal transitions are rejected.
  - Batch status becomes `completed_with_failures` when appropriate.
- Item result ingestion:
  - Valid result updates state.
  - Result item id must match target item id.
  - Missing critical output paths are handled according to status.
- Resume:
  - Existing `succeeded` is preserved.
  - Interrupted `running` item with result file is recovered.
  - Interrupted item without result is not treated as succeeded.
- Report:
  - Markdown and JSON reports are deterministic.
  - Counts match state.
  - 30-second takeaways appear only for completed items.
- Skill validation:
  - `batch_skill/SKILL.md` has `name: paperread-batch`.
  - No forbidden in-skill docs exist.
  - Required references, scripts, tests, pyproject, and lockfile exist.

Integration testing with real `$paperread` workers can come after deterministic
tests. It should use small fixtures and dry-run or prepare-only paths.

## Documentation Updates

Root docs should eventually explain that this repo contains two installable
skill sources:

- `skill/` -> install as `paperread`.
- `batch_skill/` -> install as `paperread-batch`.

`README.md`, `README.zh-CN.md`, `AGENTS.md`, and root validators should be
updated only when `batch_skill/` is implemented. This design document records
the intended direction but does not itself change the current install contract.

The batch skill's `SKILL.md` should be lean:

- State that it orchestrates multiple papers by dispatching to `$paperread`.
- State that `$paperread` must be installed and available.
- Route detailed workflow rules to `references/batch-workflow.md`.
- State default concurrency 3 for Codex.
- State prepare-only write policy.
- State Claude sequential fallback.

## Non-Goals For V1

- Do not rename existing `skill/` to `paperread/`.
- Do not copy single-paper prompt/schema/template logic into `batch_skill/`.
- Do not implement automatic Zotero writing.
- Do not make research synthesis the default report.
- Do not require all inputs to be Zotero-backed.
- Do not require all inputs to be PDF-backed.
- Do not build a background daemon, database, or external queue.
- Do not add global dependencies or system configuration.

## Future Extensions

Potential later work:

- `synthesize` command that reads all successful single-paper `summary.json`
  files and creates a research-level cross-paper analysis.
- Explicit `write-approved` phase for Zotero-backed successful items.
- Priority scheduling.
- Per-source concurrency limits.
- Recursive PDF folder scanning with ignore rules.
- Better duplicate grouping across Zotero and local PDFs.
- HTML report.
- Export package containing the batch report plus selected single-paper notes.

These extensions should not be included in the first implementation unless the
core scheduler and report contracts are already stable.

## Implementation Readiness

The design is ready for an implementation plan when these constraints are
accepted:

- `paperread-batch` is a separate installed skill.
- `skill/` remains the source directory for `paperread` in v1.
- `batch_skill/` is the source directory for `paperread-batch`.
- Batch reads are prepare-only by default.
- Parallel execution is Codex-first with sequential fallback.
- Batch stores indexes and reports, not copies of complete single-paper runs.
- V1 report is operational, not a literature synthesis.
