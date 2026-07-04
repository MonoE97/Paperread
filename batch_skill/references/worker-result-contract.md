# Worker Result Contract

Worker result files are durable handoff artifacts between the outer agent and `paperread-batch`. They must use absolute paths and must describe artifacts created by `$paperread`, not synthesized batch-level conclusions.

## Zotero Candidate Success

```json
{
  "schema_version": "paperread-batch.item-result.v1",
  "item_id": "001",
  "worker_id": "worker-001",
  "attempt_count": 1,
  "status": "succeeded",
  "paperread_run_dir": "/abs/path/to/paperread/run",
  "summary_json": "/abs/path/to/summary.json",
  "note_md": "/abs/path/to/note.md",
  "note_html": "/abs/path/to/note.html",
  "gate_report": "/abs/path/to/gate-report.json",
  "write_payload": "/abs/path/to/write-payload.json",
  "local_note_path": "",
  "local_gate_report": "",
  "failure_reason": ""
}
```

`write_payload` must be present only when `gate_report` is `write_ready`. The batch CLI records the path and later emits it through `next-write`; it must not call Zotero MCP `write_note`.

## Local PDF Success

```json
{
  "schema_version": "paperread-batch.item-result.v1",
  "item_id": "003",
  "worker_id": "worker-003",
  "attempt_count": 1,
  "status": "succeeded",
  "paperread_run_dir": "/abs/path/to/Paper_analysis",
  "summary_json": "/abs/path/to/Paper_analysis/summary.json",
  "note_md": "",
  "note_html": "",
  "gate_report": "",
  "write_payload": "",
  "local_note_path": "/abs/path/to/Paper_note.md",
  "local_gate_report": "/abs/path/to/Paper_analysis/local-gate-report.json",
  "failure_reason": ""
}
```

Local PDF results are local-output only. They must not contain a `write_payload`.

## Failure

```json
{
  "schema_version": "paperread-batch.item-result.v1",
  "item_id": "001",
  "worker_id": "worker-001",
  "attempt_count": 1,
  "status": "failed",
  "failure_reason": "duplicate Zotero title"
}
```

## Local Prepare Fallback Result

```json
{
  "schema_version": "paperread-batch.local-prepare-result.v1",
  "item_id": "003",
  "status": "prepared",
  "analysis_dir": "/abs/path/to/Paper_analysis",
  "final_note_path": "/abs/path/to/Paper_note.md",
  "manifest_path": "/abs/path/to/Paper_analysis/run.json",
  "failure_reason": ""
}
```

## Verified Zotero Write Result

```json
{
  "schema_version": "paperread-batch.write-result.v1",
  "item_id": "001",
  "status": "written",
  "note_key": "ABC12345",
  "parent_key": "PARENT1",
  "contentSha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "verify_report": "/abs/path/to/verify-report.json"
}
```

The `verify_report` JSON must have `status: "passed"` and matching `noteKey`, `parentKey`, and `contentSha256`.
