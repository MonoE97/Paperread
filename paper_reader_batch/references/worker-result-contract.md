# Worker Result Contract — Paper Reader Batch 2.2 Runtime Contract

Worker, local-prepare, write and reconciliation results are durable handoff artifacts between the external agent, `$paper_reader`, and `paper_reader_batch`. This released runtime contract requires strict Pydantic v2 models with `extra=forbid`, canonical JSON digests, absolute source/artifact paths and exact V2 identities. Results point to artifacts created by `$paper_reader`; batch never synthesizes single-paper conclusions.

Active schemas are only:

- `paper_reader_batch.manifest.v2`
- `paper_reader_batch.state.v2`
- `paper_reader_batch.event.v2`
- `paper_reader_batch.worker-result.v2`
- `paper_reader_batch.local-prepare-result.v2`
- `paper_reader_batch.write-result.v2`
- `paper_reader_batch.reconciliation.v2`
- `paper_reader_batch.report.v2`
- `paper_reader_batch.command-result.v2`

V1/unversioned/unknown files are historical-only. Loaders reject them read-only with `unsupported_run_schema` before `.run.lock`, output allocation or journal mutation; no alias, migration, dual loader, schema guessing or fallback is permitted.

## Zotero Candidate Success

`paper_reader_batch.worker-result.v2` for a Zotero item binds manifest/item id, claim id, exact worker attempt, lease token, result status, content digest and the referenced `paper_reader.run.v2`, `paper_reader.review-package.v2` and `paper_reader.candidate.v2` identities. The sealed review package must prove that the fully resolved rendered note passed the Chinese-first gate. Candidate path/digest, source parent fingerprint, fixed note title and all referenced artifact hashes must agree. Markdown metacharacters in that title are literal-escaped in the candidate Markdown H1 and rendered with the exact single-skill renderer version; the visible HTML H1 must equal the raw candidate/authorization/readback title character for character. A worker result never contains mutable write authority and the batch CLI must not call Zotero MCP `write_note`.

Zotero workers delegate the current secondary-plan policy, `secondary_cross_checks`, and finding-anchor assessment to `$paper_reader`. Batch validates only the sealed artifact identities, hash closure and rendered-note proof; it does not copy, parse, reinterpret, or validate the single-paper finding-anchor schema, and local PDF workers never enable that path.

For `zotero_title`, `inventory_sha256` is exactly `SHA-256(canonical_json_bytes(raw_discovery.search_results))`. The raw selected record must normalize to the candidate source inventory, selected key/title/DOI/version and attachment identity. A manifest `inventory_sha256=null` may be filled once by the successful result; a non-null manifest value must match exactly. Duplicate normalized-title parents remain blocked.

## Local PDF Success

`paper_reader_batch.worker-result.v2` for a local PDF binds the exact resolved source path/size/SHA-256, claim id, exact worker attempt, lease token, `paper_reader.run.v2`, sealed review package, immutable local candidate and no-replace publication result. The same Chinese-first resolved-render proof is mandatory. Local PDF results are local-output only and must not contain a Zotero candidate, authorization, write-lane claim or note key.

## Failure

Failed/blocked `paper_reader_batch.worker-result.v2` still binds manifest/item, exact attempt and lease token and supplies a structured error code plus safe message. Finish records it once in the hash-chain; retry is a separate explicit request id and creates a new attempt. A stale token or attempt is rejected rather than recorded as failure.

## Local Prepare Fallback Result

`paper_reader_batch.local-prepare-result.v2` binds manifest/item, exact local-prepare attempt and lease token, source absolute path/size/SHA-256, the stable device/inode identity of the returned run directory, returned `paper_reader.run.v2`, evidence id/digest and the explicit `--paper-reader-root` identity. Every `prepared` result must carry the stable directory identity before worker prompt/finish can mutate state; missing required V2 fields are rejected during journal/report replay as well as new mutation. No compatibility reader or field fallback exists. `prepared` means the V2 run validates and the referenced evidence is complete. Recovery by glob, filename stem, mtime, stdout parsing or a historical manifest is forbidden.

## Verified Zotero Write Result

`paper_reader_batch.write-result.v2` binds item, writer id, claim id, lease token, `write_attempt_id`, `write.started` event, candidate digest, authorization digest/nonce, external claim id, exact note/parent keys, canonical HTML hash and `paper_reader.verification.v2`. Authorization binds external claim id + candidate digest + `write_attempt_id`, never the renewable lease token; batch begin independently validates the current claim/lease/write-attempt identity. Commit accepts only passed verification whose parent, note key, exact title, complete tags, required headings, minimum length and canonical HTML hash match the immutable authorization.

If the external outcome is unknown, record uncertain instead of fabricating a result. `paper_reader_batch.reconciliation.v2` binds the same claim id, `write_attempt_id`, candidate/authorization and read-only search evidence. An exact parent + title + canonical HTML hash match locates one note but does not verify it. The located note may become written only after full verification passes exact parent, note key, exact title, complete tags, required headings, minimum length, and canonical HTML hash. Zero matches require explicit no-match acknowledgement plus new authorization/request id; many remain blocked. Expired authorization is valid evidence for verification/reconciliation but never write authority.

## Command Result

Every operational invocation prints exactly one `paper_reader_batch.command-result.v2` JSON object on stdout. It includes command identity, request id when applicable, replay status, result or structured error; diagnostics go to stderr. A replayed `write begin` envelope must never be sent again.
