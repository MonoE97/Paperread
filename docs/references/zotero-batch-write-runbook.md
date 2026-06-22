# Zotero Batch Write Runbook

This runbook captures the reusable control pattern for batch Zotero note writing
and historical note-content migrations. It is the operational reference for
future multi-paper or multi-note writes; one-off implementation plans under
`docs/superpowers/plans/` are historical execution records.

## Scope

Use this runbook when a task will write or update more than one Zotero note, or
when a local conversion step prepares content for later Zotero writes.

Do not use it to reorganize Zotero collections, edit Zotero metadata, change
Better Notes configuration, or modify Zotero SQLite directly.

## Durable Artifacts

Create one batch directory under `runs/` before analysis starts:

```text
runs/<YYYY-MM-DD>/<batch-id>/
```

Use these batch-level files:

- `manifest.json`: frozen candidate set, per-item state, run directories, note
  keys, hashes when content migration is involved, and compact errors.
- `write-preview.md`: compact preview table shown before any persistent Zotero
  write.
- `report.md`: final totals and blocked or failed items.

Use one per-paper run directory for summary generation, created with
`uv run zotero-paperread create-run`. Use the returned `run_dir`; do not hand-roll
slugs.

For each candidate item, save the raw MCP `get_item_details(mode="complete")`
response under that run directory and normalize it with `save-item-details`
before calling local preparation commands. Downstream local commands should read
the normalized `item-details.json`, while the raw response remains an audit
artifact.

When MCP omits Zotero `Extra` / `其他`, `save-item-details` may recover it from
the read-only SQLite fallback. A successful immutable SQLite read is provenance
diagnostic data under `_paperread.enrichment.extra.diagnostics`, not a workflow
warning. Missing, unreadable, locked, or item-not-found fallback states remain
warnings and must be carried into the item audit.

## Candidate Freeze

Build the complete candidate set first, then freeze it in `manifest.json`.
After freeze, do not rebuild the candidate list during worker execution or
resume unless the user explicitly asks for a fresh audit.

If discovery finds multiple Zotero entries with the same normalized title, block
that paper before `create-run`; do not assign a worker and do not choose a parent
item on the user's behalf. The user must de-duplicate those Zotero entries first.

For summary-note batches, detect existing Codex summaries with
`get_item_details(mode="complete")`. Treat an item as already summarized when a
child note matches any marker:

- note title starts with `[Codex Summary]`;
- note metadata tags include `codex-summary`;
- note body includes `Tags: codex-summary, paper-summary`.

When a user provides a supplemental webpage for an item, or when
`prepare-item` emits links from Zotero `Extra` into `secondary_sources.json`,
capture each URL into that item's run directory with the CDP helper:

```bash
mkdir -p <run_dir>/secondary_contexts
node skills/zotero-paper-summary/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

Treat `secondary_context.md` and `secondary_contexts/*.md` as cross-check
material only. They must not appear as locators in `evidence_summary`; primary
evidence remains `context.md` and `figure_context.md`. A recovered transient CDP
request may still record `capture_warning` while keeping
`source_status: secondary_context`. Persistent CDP failures write
`source_status: secondary_context_unavailable`; do not use those files as
secondary material.

For historical note-content migrations, store the raw note content and a
SHA-256 hash before conversion. Before updating Zotero, re-read the current note
and compare the current hash with the frozen hash.

## Status Model

Recommended item states for summary-note batches:

```text
discovered
skipped_existing_summary
skipped_invalid_item
queued
prepared
summarized
reviewed
gated
previewed
write_ready
written
verified
blocked
failed
```

Recommended states for content migrations:

```text
discovered
migrate
skipped
blocked
verified
failed
```

`verified`, `skipped`, `blocked`, and `failed` are terminal states. Keep
`blocked_reason` concise and keep full error text in a separate field when it is
needed for audit.

## Worker Boundary

Workers may prepare local run artifacts, extract PDFs and figures, draft
`summary.json`, and run local validation. Workers must not write Zotero notes,
change Zotero collections, edit Zotero metadata, or touch source files outside
their assigned run directories.

The coordinator owns:

- candidate discovery and manifest freeze;
- run directory allocation;
- central quality gates;
- preview presentation;
- all `write_note` calls;
- post-write verification and final reporting.

Cap parallelism explicitly for large batches and queue remaining candidates.
Close completed workers promptly.

## Summary Note Gate

A Zotero summary-note write is allowed only after all checks pass:

```text
summary.json validates
review.json exists
apply-review has merged review.json into summary.json
lint-summary reports no blocking issues
review.json needs_improvement is false
summary.json improvement_status is neither needed nor blocked after apply-review
validate-trusted-summary passes
same-day version suffix has been computed from current item-details.json
note.md and note.html have been finalized with the computed suffix
note tags have been computed with note-tags
preview-note has been shown for note.md and note.html
target Zotero item title has been shown
gate-run reports write_ready with no blockers
prepare-write-payload records parentKey, tags, note_html_path, contentLength, and readback checks
user has confirmed the write-ready preview
```

Recommended per-item command sequence. For single-paper interactive work, prefer
`prepare-write-candidate`; in batch mode the coordinator may run the equivalent
expanded chain for clearer manifest state transitions:

```bash
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread lint-summary <run_dir>/summary.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
uv run zotero-paperread refresh-live-notes <run_dir>/item-details.json --output <run_dir>/item-details.json
VERSION_SUFFIX="$(uv run zotero-paperread next-version-suffix <run_dir>/item-details.json --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE")"
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --generated-date "$GENERATED_DATE" --version-suffix "$VERSION_SUFFIX" --output <run_dir>/note.md --html-output <run_dir>/note.html
uv run zotero-paperread note-tags <run_dir>/summary.json
uv run zotero-paperread preview-note <run_dir>/note.md
uv run zotero-paperread preview-note <run_dir>/note.html
uv run zotero-paperread gate-run <run_dir> --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE" --output <run_dir>/gate-report.json
uv run zotero-paperread prepare-write-payload <run_dir>/gate-report.json --output <run_dir>/write-payload.json
```

The shorter wrapper is:

```bash
uv run zotero-paperread prepare-write-candidate <run_dir> --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE"
```

`prepare-write-candidate` removes any stale `write-payload.json` before running
the gate and writes a new payload only when the run is `write_ready`.
`prepare-write-payload` must write exactly to the gate run directory's
`write-payload.json`; it rejects output paths that are the gate report itself,
the note HTML file, a different filename, or a different run directory.

Write only through Zotero MCP:

```text
write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)
```

Never overwrite an existing Codex summary. Same-day repeats must use
`next-version-suffix` and create a new child note.

After each successful `write_note(action="create", ...)`, verify the new note
with `verify-zotero-note` using the payload's `required_readback_checks`.
The `contentSha256` check uses the project canonical note HTML hash, which
matches Zotero readback's terminal-newline normalization.

## Content Migration Gate

Historical Zotero notes may already be HTML. Do not render the whole note as
Markdown unless it has been classified as plain Markdown.

Safe content migration sequence:

1. Save raw note content under the batch directory.
2. Classify the note content.
3. Convert only local dry-run files.
4. Review converted previews and `report.md`.
5. Stop for explicit user confirmation.
6. Re-read the current Zotero note and compare hashes.
7. Update one note at a time.
8. Re-read after each update and verify the rendered condition.

Update shape:

```text
write_note(action="update", noteKey=<note_key>, content=<converted_html>)
```

Do not pass tags during update. A content migration changes note rendering only;
tag changes are separate side effects.

## Resume Rules

Resume from `manifest.json`.

If execution stops before preview, continue from the first item that is not in a
terminal state. If execution stops after preview but before writes, re-check
current Zotero state and ask before writing. If execution stops during writes,
resume from the first item that is not `verified`.

Before writing after a resume, re-check that another session did not create an
equivalent Codex summary or change the note content being migrated.

## Verification

After implementation or runbook changes, run:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
git diff --check
```

For completed Zotero writes, verify through Zotero MCP readback, not only local
files or command success.
