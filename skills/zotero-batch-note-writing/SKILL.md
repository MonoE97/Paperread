---
name: zotero-batch-note-writing
description: Use when the user asks to batch summarize Zotero collection items, process papers without Codex summary notes, use parallel workers for local paper analysis, preview notes before Zotero writes, or write multiple Zotero child notes with readback verification.
---

# Zotero Batch Note Writing

Use this skill to coordinate a batch run across Zotero collection items. It is orchestration only: single-paper extraction, summary gates, rendering, and write-payload preparation still use the existing `zotero-paperread` CLI / `skills/zotero-paper-summary/SKILL.md`.

## Boundary

- Persistent Zotero writes are coordinator-only.
- Workers may create local run artifacts but must not call `write_note`, mutate collections, edit Zotero SQLite, or change Better Notes settings.
- Default to preview-first. Do not write to Zotero until the user explicitly confirms the generated preview.
- Keep batch artifacts compact and resumable. Treat `manifest.json` as the source of truth after candidate freeze.

## Default Workflow

1. Re-check live Zotero MCP tool exposure and resolve target collection from live state.
2. Freeze candidates into `manifest.json`; resume from manifest after freeze.
3. Mark existing Codex summaries via `[Codex Summary]`, `codex-summary`, or `Tags: codex-summary, paper-summary`.
4. Block duplicate normalized titles before analysis; do not choose parent item for user.
5. Dispatch bounded parallel workers only for local run artifacts.
6. Treat `section_context.md` as a navigation aid for sections and table/value candidates; it is not a canonical evidence source and must not be used as an `evidence_summary` locator source. Valid locators should look like `context.md page 3 section Methods`, `context.md page 6 section Results table_candidate 1`, or `figure_context.md fig_p4_1`.
7. Treat WeChat/news/blog links as secondary cross-check material; never cite them in `evidence_summary`.
8. Run central per-item gate chain: `create-run -> prepare-item -> validate-summary-json -> apply-review -> lint-summary -> validate-trusted-summary -> next-version-suffix -> finalize-note --html-output -> note-tags -> preview-note -> gate-run -> prepare-write-payload`.
9. Generate `write-preview.md` and stop for explicit user confirmation.
10. After confirmation, serialize `write_note` and verify each with `get_item_details`.
11. Generate compact `write-report.md`.

## State Model

Required progression:

`discovered -> skipped_existing_summary / skipped_invalid_item / blocked_duplicate_normalized_title / queued -> prepared -> summarized -> reviewed -> gated -> previewed -> write_ready -> written -> verified`

Terminal states:

- `blocked` and `failed` are terminal error states.
- `verified` is the successful post-write terminal state.

## Script Helpers

- `scripts/validate_manifest.py`: validate frozen manifest shape and state values.
- `scripts/build_batch_preview.py`: generate compact `write-preview.md` from write-ready items.
- `scripts/verify_write_report.py`: verify compact report consistency after serialized writes.

## References

- [Manifest schema](references/manifest-schema.md)
- [Worker contract](references/worker-contract.md)
- [Failure modes](references/failure-modes.md)
