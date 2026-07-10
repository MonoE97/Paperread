# Parallel Dispatch — Paper Reader Batch 2.0 Target Contract

paper_reader_batch uses two execution modes under the journal-and-lease contract. Every mutation requires `--request-id UUID`, holds `.run.lock`, validates the append-only hash-chain and manifest binding, and commits an event before replacing the reconstructable `state.json` snapshot.

## Main Mode: Outer-Agent Parallel Dispatch

1. Run `uv run paper_reader_batch run validate <batch_run> --paper-reader-root <paper_reader_root>`.
2. Claim up to the manifest concurrency through `uv run paper_reader_batch worker claim <batch_run> --request-id UUID`. Default Codex concurrency is 3.
3. Dispatch one outer-agent worker per leased assignment. The assignment binds item, claim id, exact attempt, worker identity, lease token and expiry; worker leases default to 900 seconds.
4. Renew long work with `uv run paper_reader_batch worker renew <batch_run> <item_id> --request-id UUID`.
5. Each worker runs `$paper_reader` and produces a strict `paper_reader_batch.worker-result.v2` referring to immutable single-paper artifacts.
6. Finish with `uv run paper_reader_batch worker finish <batch_run> <item_id> --result <result_json> --request-id UUID`.
7. Use grouped `worker release` only before side effects; failed/blocked work requires explicit `worker retry`. Reject stale lease token, wrong attempt, source drift and same-PDF mutual exclusion violations.
8. Repeat claims until no eligible work remains, then run `uv run paper_reader_batch run report <batch_run>`.

## Local PDF Worker Rule

For `input_type=pdf_path`, the worker must use `$paper_reader` local PDF workflow. It must not search Zotero, inspect Zotero duplicates, create Zotero authorization, enter the write lane, or write Zotero. The exact source absolute path/size/SHA-256 and claimed attempt must match the finished result.

## Zotero Worker Rule

For `input_type=zotero_item` or `input_type=zotero_title`, the worker may return an immutable `paper_reader.candidate.v2`. It must stop on duplicate normalized titles, bind a sealed review package whose resolved rendered note passed the Chinese-first gate, bind exact source/parent/title/content/tags/hashes, and must not create write authority by itself or call Zotero MCP `write_note`.

## Recoverable Serial Write Rule

The controller claims exactly one Zotero candidate with `uv run paper_reader_batch write claim <batch_run> --request-id UUID`; write leases default to 120 seconds and bind writer, item, claim id and lease token. `write preview` verifies that claim and shows only the immutable candidate target plus exact `note.md` / `note.html`; no authorization exists yet. Parallel write is unsupported.

After candidate preview, the controller must obtain the user's explicit real-write intent. Only then may the external agent run `$paper_reader zotero authorize` for that candidate with the external claim id. Before `write begin`, that immutable authorization must have at least 30 seconds remaining and match the claim id, lease token and candidate digest. Begin atomically consumes its nonce and commits `write.started` before returning the exact MCP envelope. A same-request replay has `replayed=true` and must not be sent again; a new request id cannot create a second start.

Only the external agent may send that envelope through MCP `write_note` at most once. Then it runs `$paper_reader` read-only verification and commits `paper_reader_batch.write-result.v2`. Any crash/error/expiry after `write.started` becomes uncertain, never queued, and must use read-only `write reconcile` before retry. Exact parent + title + canonical hash gives one -> written, zero -> retry-confirmation-required, many -> blocked.

## Fallback Mode: Local PDF Pre-Extraction

When outer-agent parallelism is unavailable, use `uv run paper_reader_batch local-prepare claim <batch_run> --request-id UUID` for fallback pre-extraction. Local-prepare leases default to 900 seconds and support grouped renew/finish/release/retry. A single reader later continues only from the exact V2 preparation attempt returned by `$paper_reader`.

Local preparation may delegate only to an explicitly configured `--paper-reader-root` grouped V2 command and must validate its returned identity/schema. Finish requires the exact attempt plus source absolute path/size/SHA-256; never recover by glob, filename stem, mtime, V1 manifest or stdout guessing. Worker and local-prepare lanes share same-PDF mutual exclusion.
