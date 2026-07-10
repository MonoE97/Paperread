# Batch Workflow — Paper Reader Batch 2.0 Target Contract

Use this workflow when the user asks to analyze multiple papers. It is the binding grouped-CLI target contract for staged Paper Reader Batch 2.0 implementation, not a claim that the full runtime is already complete before the implementation and release tasks finish.

paper_reader_batch is a scheduler and reporter. It must dispatch each paper to
`$paper_reader` for single-paper analysis. It must not copy single-paper prompts,
summary schema, note templates, evidence locator rules, or Zotero write gates.

Every active artifact is strict V2 with `extra=forbid`. V1/unversioned/unknown artifacts are historical-only and must fail read-only before lock or mutation with `unsupported_run_schema`; aliases, migration, schema guessing and hidden fallback are forbidden.

## Prerequisites

Install both skills before running a batch: `paper_reader` for single-paper work
and `paper_reader_batch` for scheduling. Zotero-backed batch items also require
Zotero Desktop and `zotero-mcp-plugin` with the integrated MCP server enabled.
Use the local Streamable HTTP endpoint from the plugin preferences, normally
`http://127.0.0.1:23120/mcp`.

## Inputs

Supported input sources:

- Zotero collection, expanded to `zotero_item` manifest entries.
- Multiple Zotero titles or title fragments, stored as `zotero_title`.
- Local PDF folder, scanned non-recursively by default into `pdf_path`.
- Multiple local PDF paths, stored as `pdf_path`.

PDF folder and PDF path items are local-only: do not run Zotero lookup, duplicate checks, or Zotero write-through for them. The manifest
must keep them as `input_type=pdf_path` with `expected_output=local_note`.
This applies even when Zotero contains an item with the same title, DOI, or
attachment.

For Zotero collection input, the collection argument must match the read-only
inventory's `collection.key` or `collection.name`. A mismatch stops before
manifest creation.

Manifest `item_id` values must be file-name safe: letters, numbers,
underscore, dot, and hyphen only, with no leading punctuation or path
separators.

## Run Directory

`paper_reader_batch.manifest.v2` binds normalized inputs, write policy, concurrency and exact source identities. Build/validate it through the manifest group, then initialize:

```bash
uv run paper_reader_batch manifest ...
uv run paper_reader_batch run init --manifest manifest.json --request-id UUID
```

Without a custom output, initialization allocates `runs/YYYY-MM-DD/<batch-slug>/` under the installed `paper_reader_batch` skill root. It rejects duplicate normalized PDF paths and duplicate resolved Zotero item keys.

The append-only hash-chain `events/<20-digit-seq>.json` is the source of truth. `state.json` is a reconstructable snapshot, never independent authority. Mutation holds `.run.lock`, verifies manifest SHA and journal continuity, applies request fingerprint/idempotency, writes any content-addressed result, commits the event, and only then atomically replaces the snapshot. Journal gaps or hash mismatch return `journal_corrupt`; stale snapshots replay; orphan result files are ignored.

Every state-mutating command requires `--request-id UUID`. Reusing the same UUID with the same fingerprint returns an idempotent replay; reusing it for another request is rejected.

Manifest builders and default `run init` execute before a run journal exists. They use a skill-root request receipt under a global no-follow allocation lock: the receipt binds the exact UUID, canonical request fingerprint and reserved output/run target before publication. Crash recovery resumes only that receipt and target; scanning or globbing runs to rediscover a result is forbidden.

## Execution

Default Codex concurrency is 3. Use `references/parallel-dispatch.md` for worker/local-prepare lease commands and the controller loop. Manifest concurrency sets claim defaults but never weakens safety limits.

```bash
uv run paper_reader_batch worker claim <batch_run_dir> --worker-id <worker_id> --request-id UUID
uv run paper_reader_batch worker prompt <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>
uv run paper_reader_batch worker renew <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --request-id UUID
uv run paper_reader_batch worker finish <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --result <result.json> --request-id UUID
uv run paper_reader_batch worker release <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --acknowledge-no-side-effects --request-id UUID
uv run paper_reader_batch worker retry <batch_run_dir> <item_id> --request-id UUID
```

Worker claim returns and binds `claim_id`, `lease_token`, `attempt_id`, worker id and item id. `worker prompt` validates that exact live identity and returns the deterministic outer-agent instruction without mutating state or dispatching an LLM. Worker leases default to 900 seconds. Every renew/finish/release must present the same claim id, lease token and exact attempt; stale lease tokens or cross-item values are rejected. Release additionally requires `--acknowledge-no-side-effects` and is forbidden after any external side effect or single-paper artifact. Failed/blocked items require explicit retry. Duplicate resolved PDFs use same-PDF mutual exclusion across worker and local-prepare lanes.

If outer-agent parallelism is unavailable, use the `local-prepare` group as fallback pre-extraction for `pdf_path` items only, then continue deep reading from the exact claimed prepare attempt:

```bash
uv run paper_reader_batch local-prepare claim <batch_run_dir> --worker-id <worker_id> --request-id UUID
uv run paper_reader_batch local-prepare renew <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --request-id UUID
uv run paper_reader_batch local-prepare finish <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --result <result.json> --request-id UUID
uv run paper_reader_batch local-prepare release <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --acknowledge-no-side-effects --request-id UUID
uv run paper_reader_batch local-prepare run <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --paper-reader-root <paper_reader_root> --request-id UUID
```

Local-prepare claim returns and binds its own claim id, lease token, prepare attempt, worker id and item id. Leases default to 900 seconds. Renew/finish/release/run require all bound identities; finish also matches source absolute path/size/SHA-256. `local-prepare run` invokes only the explicit paper_reader V2 grouped `run init-local` and `run prepare` commands through `--paper-reader-root`, validates both command-result envelopes, and records the exact evidence attempt; it does not import paper_reader, dispatch an LLM, search Zotero, or generate a summary/candidate. A failed prepare is re-attempted only by a new claim/run request and new attempt. Recovery by glob, filename stem or mtime is forbidden.

## Result Ingestion

Each `paper_reader_batch.worker-result.v2` is strict, content-addressed and bound to manifest item, claim, attempt, lease token and exact `$paper_reader` V2 artifact identities. A succeeded result must reference a sealed review package whose fully resolved rendered note passed the Chinese-first gate; candidate presence alone is insufficient. Finish rejects stale or mismatched results before journal mutation.

Workers record artifact paths, not final report conclusions. During result
ingestion, `paper_reader_batch` derives `thirty_second_takeaway`,
`takeaway_source_type`, `takeaway_source_path`, and `takeaway_source_sha256`
from the rendered single-paper note and `summary.json`.

## Resume

Use grouped read/recovery commands after interruption:

```bash
uv run paper_reader_batch run status <batch_run_dir>
uv run paper_reader_batch run validate <batch_run_dir>
uv run paper_reader_batch run recover <batch_run_dir> --request-id UUID
```

Recovery reconstructs state only from the validated manifest and event journal. It never imports orphan results by directory scan, stem or mtime and never treats a V1/unversioned file as recoverable state.

For a started write lease expiry, `run recover` holds `.run.lock`, identifies the exact `write_attempt_id` whose `write.started` lease expired, and appends the unique `write.lease_expired_uncertain` event. The expired lease token is neither required nor accepted. The same recover request id replays idempotently; the attempt becomes uncertain, never queued; it never returns queued and cannot begin again. `write mark-uncertain` accepts only an unexpired exact claim/token/write-attempt identity for active error reporting.

## Zotero Write Stage

The default write policy is `zotero_write`; explicit dry-run uses `prepare_only`. PDF items never enter this lane. After eligible Zotero-backed items have immutable single-paper candidates, claim exactly one write with a default 120 seconds lease:

```bash
uv run paper_reader_batch write claim <batch_run_dir> --writer-id <writer_id> --request-id UUID
uv run paper_reader_batch write preview <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>
uv run paper_reader_batch write renew <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --request-id UUID
```

Write claim returns and binds exactly one candidate plus writer id, `claim_id`, `lease_token`, `write_attempt_id` and expiry. The write preview shows only the immutable candidate: its target, fixed title/tags, `note.md`, `note.html` and hashes; no authorization exists yet. After preview, obtain the user's explicit real-write intent. Only then may the external agent run `$paper_reader zotero authorize <candidate> --external-claim-id <claim_id> --write-attempt-id <write_attempt_id>`. The authorization binds the external claim id, candidate digest, and `write_attempt_id`; it does not bind lease_token. Pass that authorization to batch begin; it must have at least 30 seconds remaining. Batch write begin independently validates the current claim_id, lease_token, and write_attempt_id plus writer, item, candidate digest and authorization bindings. It then atomically consumes the nonce and commits `write.started` before returning the exact MCP envelope:

Batch authorization requires both --external-claim-id and --write-attempt-id, and both options must appear together. Partial input is rejected before mutation, both identities must match the batch claim/candidate, and batch authorization must not generate `direct_<uuid>` identities.

```bash
uv run paper_reader_batch write begin <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --authorization <authorization.json> --request-id UUID
```

The batch CLI must not call `write_note`. The external agent may send the exact envelope at most once, then uses `$paper_reader` read-only verification. A verified `paper_reader_batch.write-result.v2` is committed with:

```bash
uv run paper_reader_batch write commit <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --result <write-result.json> --request-id UUID
```

Claim release/expiry before `write.started` may return queued work to the queue. After start, an active writer with an unexpired exact identity may report a crash/error as uncertain:

```bash
uv run paper_reader_batch write mark-uncertain <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --reason <reason> --request-id UUID
uv run paper_reader_batch write reconcile <batch_run_dir> <item_id> --readback <readback.json> --request-id UUID
```

An exact parent + title + canonical HTML hash match locates one note but does not verify it. The located note may become written only after full verification passes exact parent, note key, exact title, complete tags, required headings, minimum length, and canonical HTML hash. Zero matches require explicit retry confirmation; many block. Retry requires `--acknowledge-no-match`, a new authorization and a new request id:

```bash
uv run paper_reader_batch write retry <batch_run_dir> <item_id> --acknowledge-no-match --request-id UUID
uv run paper_reader_batch write release <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --request-id UUID
```

All write events and results bind claim_id, lease_token, and write_attempt_id. Journal replay rejects stale, cross-claim or cross-attempt events/results before state mutation.

## Safety

Batch CLI code must not call Zotero MCP `write_note`; only the external agent may send the exact envelope returned after durable `write.started`. Same request-id begin replay returns `replayed=true` and must not be sent again; a new request id cannot create a second start. Pass manifest builders `--write-policy prepare_only` for explicit dry-run. PDF items remain local-output only and are excluded from the write lane.

## Reporting

The batch report is operational, not a literature synthesis. The per-paper
`thirty_second_takeaway` is extracted from that paper's rendered single-paper
note row `30 秒结论`. If the note row is unavailable, fallback to `tldr`, then
`one_sentence_summary`, and record the fallback source.

For a pure local-PDF batch, the machine report includes
`effective_write_policy=local_only` so readers do not mistake the manifest's
default `write_policy=zotero_write` for Zotero write-through on PDF items.

Reducer priority is `corrupt > write_uncertain > running > needs_attention > awaiting_write > ready > succeeded`. Succeeded requires every obligation; failed, blocked or uncertain work never reports completed. Generate the report with `uv run paper_reader_batch run report <batch_run_dir>`.
