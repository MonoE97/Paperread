---
name: paper_reader_batch
description: Use when the user asks to analyze multiple papers from Zotero collections, Zotero titles, PDF folders, or PDF paths under the Paper Reader Batch 2.0 journal-and-lease contract, dispatching each item to $paper_reader while keeping PDF items local-only.
---

# paper_reader_batch

paper_reader_batch orchestrates multiple paper reads. This file defines the Paper Reader Batch 2.0 target contract and grouped CLI. It is binding for staged implementation and does not claim every grouped command is already present before the 2.0 runtime and release tasks finish. It does not perform deep
single-paper analysis itself. Each paper must be dispatched to `$paper_reader`,
which remains the owner of extraction, evidence rules, summary schema, note
rendering, immutable candidates/authorizations, and Zotero verification.

## Setup

Run setup commands from the installed `paper_reader_batch` skill root:

```bash
uv --version
uv sync --locked
uv run paper_reader_batch --help
```

`$paper_reader` must also be installed and available. Batch validation checks the
batch manifest, run directory, and configured `paper_reader` skill root before
dispatch.

For Zotero-backed batch items, Zotero Desktop and `zotero-mcp-plugin` must be
installed and enabled before dispatch. Use the plugin's Streamable HTTP endpoint
from Zotero preferences, normally `http://127.0.0.1:23120/mcp`.

## Typical Use

- Zotero collection or multiple Zotero titles: use `$paper_reader_batch` to build a strict V2 manifest, initialize the append-only journal, claim leased work, dispatch each item to `$paper_reader`, and process the recoverable serial write lane through immutable single-paper authorizations.
- Local PDF folder or multiple PDF paths: use `$paper_reader_batch` to dispatch
  each PDF to `$paper_reader` local PDF workflow and generate a batch report; PDF
  items remain local-output only and skip Zotero lookup or duplicate checks.

## Paper Reader Batch 2.0 Grouped CLI

The public grouped CLI is:

```text
uv run paper_reader_batch manifest
uv run paper_reader_batch run init
uv run paper_reader_batch run validate
uv run paper_reader_batch run status
uv run paper_reader_batch run recover
uv run paper_reader_batch run report
uv run paper_reader_batch worker claim
uv run paper_reader_batch worker prompt
uv run paper_reader_batch worker renew
uv run paper_reader_batch worker finish
uv run paper_reader_batch worker release
uv run paper_reader_batch worker retry
uv run paper_reader_batch local-prepare claim
uv run paper_reader_batch local-prepare renew
uv run paper_reader_batch local-prepare finish
uv run paper_reader_batch local-prepare release
uv run paper_reader_batch local-prepare run
uv run paper_reader_batch write claim
uv run paper_reader_batch write preview
uv run paper_reader_batch write renew
uv run paper_reader_batch write release
uv run paper_reader_batch write begin
uv run paper_reader_batch write commit
uv run paper_reader_batch write mark-uncertain
uv run paper_reader_batch write reconcile
uv run paper_reader_batch write retry
```

All state mutation requires `--request-id UUID`. Operational commands emit exactly one `paper_reader_batch.command-result.v2` JSON object on stdout and diagnostics on stderr. V1/unversioned artifacts are historical-only and fail before lock or mutation with `unsupported_run_schema`; there are no aliases, migrations, schema guessing or hidden fallbacks.

## Routing

Use `references/batch-workflow.md` for all batch workflows:

- Zotero collection.
- Multiple Zotero titles or title fragments.
- Local PDF folder.
- Multiple local PDF paths.

Use `references/parallel-dispatch.md` for concurrency, worker/local preparation leases, fallback pre-extraction, and recoverable serial write rules. Use `references/worker-result-contract.md` for strict V2 result and reconciliation schemas.

PDF folder and PDF path items are local-only: do not run Zotero lookup, duplicate checks, or Zotero write-through for them. Manifest builders store these items as `pdf_path` with `expected_output=local_note`.
An existing directory path passed through `$paper_reader` should be routed here
instead of being treated as a Zotero title fragment.

Default Codex concurrency is 3. When outer-agent parallelism is unavailable,
claim local-prepare leases as the fallback pre-extraction path for local PDF items, then use `local-prepare run --paper-reader-root <root>` for deterministic V2 init/prepare only and continue deep reading from the exact prepared attempt. `worker prompt` is read-only and never dispatches an LLM. Worker and local-prepare leases default to 900 seconds; stale lease tokens, changed source identity and same-PDF concurrent work are rejected. Release requires explicit `--acknowledge-no-side-effects` and is forbidden after external artifacts or a child-start marker exists. On expiry, `run recover` requeues only a proven-unstarted reservation; a started or unverifiable local attempt is renewed in place and can never allocate attempt 2.

The append-only hash-chain at `events/<20-digit-seq>.json` is source of truth; `state.json` is only a reconstructable snapshot. `.run.lock`, manifest hash binding and request-id idempotency protect mutation. Journal gaps or hash failures return `journal_corrupt` and must not mutate.

Every successful worker result must bind a sealed `$paper_reader` review package whose fully resolved rendered note passed the Chinese-first gate; batch must not accept a candidate as a substitute for that proof.

The default write policy is `zotero_write`; pass `--write-policy prepare_only` only for explicit dry-run. PDF items remain local-output only and a pure local PDF report uses `effective_write_policy=local_only`. The write sequence is fixed: claim exactly one candidate and its `claim_id` / `lease_token` / `write_attempt_id` -> preview that candidate while no authorization exists -> obtain the user's explicit real-write intent -> let the external agent call `$paper_reader zotero authorize` with the external claim id and `write_attempt_id` -> pass the resulting immutable authorization to batch `write begin`, which independently validates the current claim/lease/write-attempt identity. Authorization binds the external claim id, candidate digest and `write_attempt_id`, not the renewable lease token. Begin needs at least 30 seconds of authorization lifetime, commits `write.started` before returning the exact envelope, and a started crash becomes uncertain, never queued. The batch CLI must not call Zotero MCP `write_note`; only the external agent may send the envelope and the lane must verify or reconcile before progress. An exact parent + title + canonical HTML hash match locates one note but does not verify it. The located note becomes written only after full verification passes exact parent, note key, exact title, complete tags, required headings, minimum length, and canonical HTML hash. Per-paper report entries come from the single note's `30 秒结论`, with fallback to `tldr` then `one_sentence_summary`, preserving `takeaway_source_sha256`.

Batch authorization requires both --external-claim-id and --write-attempt-id, and both options must appear together. Partial input is rejected before mutation and batch authorization must not generate `direct_<uuid>` identities; direct identity generation belongs only to direct single-paper authorize.

For a started write lease expiry, `run recover` holds `.run.lock`, identifies the exact `write_attempt_id`, and appends the unique `write.lease_expired_uncertain` event. The expired lease token is neither required nor accepted. The same recover request id replays idempotently; the attempt becomes uncertain, never returns queued and cannot begin again. `write mark-uncertain` accepts only an unexpired exact claim/token/write-attempt identity for active error reporting.
