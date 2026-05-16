# Failure Modes

## Duplicate Normalized Titles

If two or more candidate items share the same `normalized_title`, block them before analysis as `blocked_duplicate_normalized_title`. Do not choose the parent item for the user and do not let workers analyze either copy until the duplicate is resolved.

## Existing Codex Summary

Detect existing Codex summaries through `[Codex Summary]`, `codex-summary`, or `Tags: codex-summary, paper-summary`. In default batch mode, mark the item `skipped_existing_summary`. If the user explicitly asks for a new version, keep the item eligible and let the coordinator compute the next versioned note title.

## Noisy Reports

Reports become unauditable when they include note bodies, raw tracebacks, or long tool responses. Keep `write-preview.md` and `write-report.md` compact. Store short user-facing causes in `blocked_reason` and long diagnostics in `error_detail`.

## Zotero Tool Drift

Live Zotero MCP tool exposure can change between sessions. Re-check available tools at the start of a batch run and before write-through. If write or readback tools are missing, stop before persistent actions and record the tool gap.

## Collection Drift

Resolve the collection from live state before freezing candidates. After `manifest.json` is frozen, resume from the manifest rather than silently re-enumerating the collection. If the user wants a fresh audit, create a new manifest or explicitly record the refresh.

## HTML-Escaped Note Titles

Readback may expose note titles with HTML entities. Verify note identity by comparing normalized decoded titles, parent item key, note tags, and written note key instead of relying on raw escaped title text alone.

## Readback Parent Drift

Include the parent item identity in write readback records when the tool response exposes it. `verify_write_report.py` accepts either `readback.item_key` or `readback.parent_item_key`; when present, it must match the intended `write.item_key` so a successful note readback cannot be attributed to the wrong Zotero parent item.
