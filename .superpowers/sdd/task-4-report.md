# Task 4 report: immutable Zotero candidate lifecycle

## Status

Implemented the Zotero-backed V2 lifecycle on `codex/paper-reader-v2-breaking` without performing any real Zotero write:

- `run init-zotero` accepts one strict saved discovery envelope, preserves the exact raw bytes, normalizes the selected item and search inventory, binds the exact expected parent key/fingerprint and readable primary PDF identity, blocks duplicate normalized titles, and atomically reserves versioned run directories.
- `run prepare` reuses the immutable evidence pipeline while revalidating the nested PDF and normalized Zotero source. Network figure-source fallback is enabled only for Zotero-backed evidence and remains resource bounded.
- `candidate build` dispatches by V2 source type. Zotero candidates capture fresh read-only parent/children snapshots, compute the first free same-day note title, render exact versioned Markdown/HTML, bind all source/evidence/review/live artifacts, and publish through atomic no-replace plus a projected 512 MiB gate.
- `zotero authorize` rejects local candidates before provider access, re-hashes the full candidate tree, refreshes parent/children under the global parent lock followed by the run lock, enforces the parent fingerprint and exact title availability, and emits one exact MCP create envelope. The plaintext 256-bit token is returned only in the command result; only its SHA-256 is persisted. TTL defaults to and is capped at 300 seconds.
- An unexpired authorization blocks every other candidate in the same run with the same parent and exact note title, not merely the same candidate digest. Expiry permits a fresh authorization only after another live read-only preflight.
- `zotero verify` accepts expired authorizations, reads one exact note, and performs full parent/key/type/H1 title/complete tag set/required headings/forbidden headings/minimum length/exact canonical length/canonical HTML hash validation. Passed and failed terminal records both preserve exact authorization, note, and check snapshots.
- `zotero reconcile` is read-only and terminal per authorization: zero exact parent+title+canonical-hash matches is `not_found` with explicit retry confirmation required; many is `ambiguous`; one triggers a fresh `get_note` and the same pure full validator before producing an embedded immutable `PaperReaderVerification` artifact. It never trusts the children summary alone and never retries or writes.
- The default provider uses only the Zotero local read-only API. No lifecycle module contains or calls `write_note`; the external agent remains the sole writer and must use the exact authorization envelope at most once.

## Formal review hardening

- Note readback verification now requires both the top-level `key` and `data.key` to exist explicitly and equal the requested portable note key; neither side may fall back to the other.
- Historical and expired authorizations no longer depend on the run's mutable latest `target`. Verification and reconciliation bind the authorization to `run_id`, the immutable Zotero source parent identity/fingerprint, the authorization candidate snapshot, and the exact run-bound candidate digest/ref.
- Terminal main artifacts now use the binding topology exactly: `authorizations/<authorization_id>.json`, `verifications/<authorization_id>/<note_key>.json`, and `reconciliations/<authorization_id>.json`. Same-stem immutable sidecars hold snapshots and a recovery record; the main file is published last with atomic no-replace as the commit marker.
- Retry after a durable main artifact plus failed `run.json` bind validates and binds that exact artifact. Verification and reconciliation replay it; authorization binds it and returns `authorization_recovered_token_unavailable` because the plaintext token is intentionally unrecoverable. No retry overwrites, deletes, or fabricates another terminal.
- Zotero initialization now compares selected-item membership across exact key, normalized title, normalized DOI, and nonnegative version. Strict Identifier, non-finite JSON, Pydantic contract, and canonical serialization checks all finish before any `runs/<day>` or staging directory is created.
- Batch `external_claim_id` and `write_attempt_id` are strictly validated before candidate/provider access, parent locking, or run mutation.

## Safety and recovery coverage

- Strict discovery-envelope membership, expected-key mismatch, selected-item inventory mismatch, duplicate key, normalized-title ambiguity, raw MCP selected-item normalization, unavailable PDF, source/source-snapshot drift, and parent version/DOI drift.
- Concurrent Zotero run allocation, concurrent candidate creation without lost run bindings, same-title cross-candidate active-authorization blocking, concurrent authorization, concurrent verification, and concurrent reconciliation.
- Candidate suffix races, occupied-title authorization races, authorization expiry, fresh nonce/token generation, partial external identities, candidate/authorization tampering, wrong parent/title/key/tags/headings/content/hash/length, and exact 0/1/many reconciliation.
- Projected 512 MiB gates and injected publication/run-binding faults for initialization, candidate, authorization, verification, and reconciliation. Deterministic terminal retry converges on the exact durable main artifact; authorization reports the unrecoverable plaintext-token boundary explicitly.
- All operational CLI tests assert one strict `paper_reader.command-result.v2` JSON object. Fixture providers are injected; tests never access the live Zotero port and never perform a write.

## Verification evidence

- `uv sync --locked`: passed.
- Worktree full suite after formal review fixes: `538 passed, 5 warnings in 48.00s`; exit 0.
- Focused Task 4 Zotero suite after formal review fixes: `89 passed, 5 warnings in 2.63s`; exit 0. Each of the six formal findings was observed RED before its minimal implementation and then GREEN.
- Exact reconciliation/verification matrix: 8 explicit node IDs passed for zero/many/one/full-validation failure/expired authorization/idempotent replay.
- `uv run python scripts/export-v2-schemas.py --output-dir <tmp>` followed by a recursive diff against `references/schemas`: clean after the two portable-note-key schema pattern updates.
- Root and all `run`/`review`/`candidate`/`local`/`zotero`/`maintenance` grouped help: exit 0. `zotero authorize --help` exposes no parent/title/content/tag overrides.
- `uv run python scripts/validate-skill.py .`: `Skill bundle is valid.`
- Minimal-PDF maintenance smoke emitted one successful V2 command-result JSON.
- Active-source scan for `write_note(`: no matches. `git diff --check`: passed.
- Clean skill copy outside the repository, excluding existing `.venv` and caches: `uv sync --locked`; full suite `514 passed, 1 skipped, 5 warnings in 50.47s`; grouped help, validator, minimal-PDF smoke, and four-stage injected read-only Zotero provider smoke all passed. The expected skip is the source-repository-only root `AGENTS.md` contract test.

## Scope notes

- README release claims, package version, lock metadata, batch runtime, V1 source deletion, push, merge, publication, and real Zotero mutation were intentionally left untouched.
- The six confirmed formal review findings are resolved; no additional reviewer findings or minors were reported. Final handoff remains with the controlling agent after this follow-up commit.
