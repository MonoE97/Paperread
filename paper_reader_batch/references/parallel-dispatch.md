# Parallel Dispatch

paper_reader_batch uses two execution modes.

## Main Mode: Outer-Agent Parallel Dispatch

1. Run `uv run paper_reader_batch validate <batch_run> --paper-reader-root <paper_reader_root>`.
2. Run `uv run paper_reader_batch next <batch_run> --limit <N>`.
3. For each returned assignment, generate a worker prompt with `uv run paper_reader_batch worker-prompt <batch_run> <item_id>`.
4. Dispatch one worker per assignment in the outer agent environment.
5. Each worker runs `$paper_reader` and writes an item result JSON.
6. The controller records each result with `uv run paper_reader_batch record-result <batch_run> <item_id> --result <result_json>`.
7. Repeat `next` until no pending items remain.
8. Run `uv run paper_reader_batch report <batch_run>`.

## Local PDF Worker Rule

For `input_type=pdf_path`, the worker must use `$paper_reader` local PDF workflow. It must not search Zotero, inspect Zotero duplicates, call `refresh-live-notes`, create `write-payload.json`, run `next-write`, or write Zotero.

## Zotero Worker Rule

For `input_type=zotero_item` or `input_type=zotero_title`, the worker may prepare a Zotero note candidate. It must stop on exact duplicate normalized titles, run the single-paper write gate, and return a `write_payload` path only when the gate is `write_ready`. It must not call Zotero MCP `write_note`.

## Serial Write Rule

The controller must process Zotero writes with `uv run paper_reader_batch next-write <batch_run> --limit 1`. Before calling MCP, show the target Zotero item plus the `note.md` and `note.html` preview that will be written. Then call MCP `write_note`, run `$paper_reader verify-zotero-note`, and `record-write`. Parallel write is intentionally unsupported.

`next-write` intentionally rejects values other than `--limit 1`. Preparing Zotero candidates is parallel; writing Zotero notes is serial because each write has an external side effect and must be immediately verified before the next write.

Before each MCP `write_note` call, show the target Zotero item title or key plus the `note.md` and `note.html` preview contents from the candidate run. The write call must use the `note.html` contents, not Markdown.

## Fallback Mode: Local PDF Pre-Extraction

When outer-agent parallelism is unavailable, run `uv run paper_reader_batch prepare-local-pdfs <batch_run> --concurrency <N> --paper-reader-root <paper_reader_root>` to prepare local PDF analysis bundles in parallel. A single agent then runs `next` and `worker-prompt`; prompts for prepared PDF items must continue from `prepared_analysis_dir` and must not run `prepare-pdf` again unless the prepared bundle is missing or unreadable. Re-running `prepare-local-pdfs` only processes pending PDF items; use `retry-failed` before retrying failed or interrupted work.

The fallback calls `$paper_reader prepare-pdf --json-output <temp-json>` and
reads that file as the stable machine result. Stdout JSON and `run.json`
recovery are compatibility fallbacks. `run.json` recovery is valid only when the
manifest says `status=prepared` and the expected analysis artifacts are
readable; an initialized but incomplete run must stay failed or pending for
retry.
