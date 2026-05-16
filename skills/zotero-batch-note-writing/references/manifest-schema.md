# Manifest Schema

`manifest.json` is the restart truth source after candidate freeze. Do not rebuild the candidate list during resume unless the user explicitly asks for a fresh live audit. Discovery, worker dispatch, preview, write, and readback verification all update or consume this file.

The validator for this file is `skills/zotero-batch-note-writing/scripts/validate_manifest.py`. A manifest following this reference should pass that validator.

## Required Top-Level Fields

The first four fields below are validated as non-empty strings.

- `batch_id`: non-empty batch/run identifier.
- `target_collection_path`: Zotero collection path used for the frozen candidate list.
- `generated_date`: non-empty date string for the manifest generation date, for example `2026-05-11`.
- `state`: non-empty batch-level coordinator state such as `frozen`, `analyzing`, `preview_ready`, `writing`, or `completed`.
- `items`: list of item records; never reorder to hide failures.
- `counts`: compact status counts for audit and resume checks.

## Required Item Fields

Every item must include all fields below. Use empty strings or empty lists for fields that are not applicable yet; do not omit fields.

```json
{
  "item_key": "ABCD1234",
  "title": "Paper title",
  "normalized_title": "paper title",
  "source_collection_path": "CATL/固态电池",
  "status": "write_ready",
  "run_dir": "runs/2026-05-11/example-paper-abcd1234",
  "existing_summary_notes": [],
  "secondary_sources": [
    {
      "kind": "wechat",
      "url": "https://mp.weixin.qq.com/s/example",
      "status": "captured"
    }
  ],
  "primary_pdf_status": "available",
  "trust_status": "trusted",
  "review_status": "passed",
  "note_title": "[Codex Summary] Paper title - 2026-05-11",
  "note_tags": ["codex-summary", "paper-summary"],
  "write_payload": "runs/2026-05-11/example-paper-abcd1234/write-payload.json",
  "written_note_key": "",
  "blocked_reason": "",
  "error_detail": ""
}
```

Required string item fields:

`item_key`, `title`, `normalized_title`, `source_collection_path`, `status`, `run_dir`, `primary_pdf_status`, `trust_status`, `review_status`, `note_title`, `write_payload`, `written_note_key`, `blocked_reason`, `error_detail`

Required list item fields:

`existing_summary_notes`, `secondary_sources`, `note_tags`

## Valid Statuses

Valid item statuses are:

`discovered`, `skipped_existing_summary`, `skipped_invalid_item`, `blocked_duplicate_normalized_title`, `queued`, `prepared`, `summarized`, `reviewed`, `gated`, `previewed`, `write_ready`, `written`, `verified`, `blocked`, `failed`

`run_dir` must be non-empty for:

`queued`, `prepared`, `summarized`, `reviewed`, `gated`, `previewed`, `write_ready`, `written`, `verified`

`write_ready` additionally requires:

- non-empty `write_payload`
- non-empty `note_title`
- non-empty `note_tags` with only non-empty string tags

`verified` additionally requires non-empty `written_note_key`.

## Counts

`counts.discovered`, when present, must equal `len(items)`. Any present status count must match the number of items with that exact `status`.

Example:

```json
{
  "discovered": 2,
  "queued": 0,
  "skipped_existing_summary": 1,
  "skipped_invalid_item": 0,
  "blocked_duplicate_normalized_title": 0,
  "blocked": 0,
  "write_ready": 1,
  "written": 0,
  "verified": 0,
  "failed": 0
}
```

## Duplicate Titles

`normalized_title` must be non-empty. If multiple items share the same `normalized_title`, every item in that duplicate group must have status `blocked_duplicate_normalized_title`; otherwise validation fails.

## Diagnostics

Keep `blocked_reason` short enough for user previews, such as `primary_pdf_missing`, `duplicate_normalized_title`, or `gate_not_write_ready`. Put long tracebacks, tool responses, and detailed diagnostics in `error_detail`; do not copy them into `write-preview.md` or compact user reports.
