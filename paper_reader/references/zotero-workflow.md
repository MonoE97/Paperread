# Zotero Workflow — Paper Reader 2.0 Target Contract

Use this when the user provides a Zotero title or title fragment. This is the binding grouped-CLI target contract for staged Paper Reader 2.0 implementation; it does not claim runtime completion before the 2.0 implementation and release tasks finish. Run commands from the skill root. Local PDF path and directory path inputs skip Zotero lookup and duplicate checks. Existing local paths are not Zotero title fragments.

## Setup

Run from the skill root:

```bash
uv --version
uv sync --locked
uv run paper_reader --help
```

If `uv sync --locked` cannot find Python `>=3.13`, run `uv python install 3.13` from the skill root and retry. If `uv` is not installed, stop and ask the user to install `uv` first; do not use `pip`, `conda`, or system Python as a replacement.

## Output Location

By default, `uv run paper_reader run init-zotero` allocates a V2 run under `<skill_root>/runs/YYYY-MM-DD/<title-slug>/`. The run owns immutable raw and normalized source snapshots, `evidence/<evidence_id>/`, the sealed review package, immutable candidates, immutable authorizations, verification and reconciliation records. Candidate artifacts include `note.md` and `note.html`, but neither file is authority to write without a matching unexpired `paper_reader.write-authorization.v2`.

## Tool Discovery

Before using the Zotero workflow, install and enable Zotero MCP from
https://github.com/cookjohn/zotero-mcp#readme. Download the
`zotero-mcp-plugin-*.xpi`, install it in Zotero with `Tools -> Add-ons`, restart
Zotero, then enable the integrated server in `Preferences -> Zotero MCP Plugin`.

Load Zotero MCP tools before the workflow: `search_library`, `get_item_details`, `get_content`, `write_note`, and optional `annotations`.

If native MCP tools are not injected, use the local Zotero MCP endpoint `http://127.0.0.1:23120/mcp` as an HTTP JSON-RPC fallback. The fallback still calls Zotero MCP methods such as `zotero-mcp write_note`; it is not a Zotero local API write path. If localhost requests hit a proxy, clear `ALL_PROXY`, `HTTP_PROXY`, and `HTTPS_PROXY`, then set `NO_PROXY=127.0.0.1,localhost` and `no_proxy=127.0.0.1,localhost`.

## Steps

1. Run `uv run paper_reader route` before Zotero search. Existing `.pdf` paths must use the local PDF path workflow, existing directory paths must be delegated to `$paper_reader_batch`, and missing path-like input must fail as `unsupported_local_path`; none may trigger Zotero lookup or duplicate checks.
2. Use `search_library` for exact-title resolution. Save the exact `search_library response` together with the selected item details from `get_item_details(mode="complete")` as one unmodified raw discovery bundle. The bundle must preserve every search candidate needed to prove whether multiple entries have the same normalized title and identify the exact selected item key. If duplicates exist, stop before run allocation/lock/mutation and ask the user to de-duplicate in Zotero.
3. Initialize from the saved raw discovery bundle and exact expected item key:

```bash
uv run paper_reader run init-zotero --raw-mcp-response <raw-discovery.json> --expected-item-key <item_key>
```

The command must bind raw and normalized source snapshots. Item-key mismatch or ambiguous normalized-title matches are blockers.

4. Prepare immutable full-PDF evidence by default:

```bash
uv run paper_reader run prepare <run_dir>
uv run paper_reader run validate <run_dir>
```

5. If `secondary_sources.json` lists Extra/web URLs, capture each source for cross-check only:

```bash
mkdir -p <run_dir>/secondary_contexts
node scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

Captured files use `source_status: secondary_context` when usable. Unavailable captures use `source_status: secondary_context_unavailable`, including warnings such as `navigation_timeout`. Secondary context must not cite secondary context in `evidence_summary`; it is only for cross-checking and background.

6. Read `context.md`, `section_context.md`, and `figure_context.md` if available. `section_context.md` is not a canonical evidence source. Final locators must use canonical forms such as `context.md page 3 section Methods`, `context.md page 6 section Results table_candidate 1`, or `figure_context.md fig_p4_1`. Bare `context.md` / `figure_context.md`, prose locators, `section_context.md`, and secondary context paths are invalid.
7. Create `paper_reader.summary.v2` and `paper_reader.review.v2`, then validate and seal an immutable `paper_reader.review-package.v2`:

```bash
uv run paper_reader review validate <run_dir>
uv run paper_reader review seal <run_dir>
```

Failed review, changed summary hash, unresolved locator or rendered English prose blocks sealing and candidate creation.

8. Refresh the read-only parent/children snapshot and build `paper_reader.candidate.v2`:

```bash
uv run paper_reader candidate build <run_dir>
```

The immutable candidate binds run/source/evidence/review identity, exact parent fingerprint, exact versioned title, tags, `note.md`, `note.html`, canonical HTML hash, file sizes and artifact hashes. Preview the target item, fixed title, tags, `note.md` and `note.html` before asking for write intent.

9. Only after explicit real-write intent, create `paper_reader.write-authorization.v2`:

```bash
uv run paper_reader zotero authorize <candidate> --external-claim-id <claim_id> --write-attempt-id <write_attempt_id>
```

Authorization accepts no parent/title/content/tag overrides. It re-hashes all artifacts, refreshes the read-only parent/children snapshot, verifies title availability and parent fingerprint, takes a local parent lease, then binds the exact HTML, canonical HTML hash/length, tags, candidate digest, parent snapshot, external claim id, `write_attempt_id`, random nonce/token and TTL. Authorization does not bind lease_token: a batch lease may be renewed independently, while batch `write begin` validates its current claim and lease. TTL defaults to and may not exceed 300 seconds.

10. The external agent is the only writer. It may send the authorization's exact MCP envelope at most once: `zotero-mcp write_note(action="create", parentKey=<authorization parentKey>, content=<exact authorization HTML>, tags=<authorization tags>)`. Neither the CLI nor batch runtime may call `write_note`.
11. Verify immediately:

```bash
uv run paper_reader zotero verify <authorization> --note-key <created_note_key>
```

Verification checks exact parent, note key, title, complete tags, required headings, minimum length and canonical HTML hash.

12. If the write outcome is uncertain, never resend automatically. Reconcile read-only:

```bash
uv run paper_reader zotero reconcile <authorization>
```

An exact parent + title + canonical HTML hash match locates one note but does not verify it. For one located note, run `zotero verify` against that exact note key and readback; only after full verification passes exact parent, note key, exact title, complete tags, required headings, minimum length, and canonical HTML hash may the note become verified. Zero matches -> `not_found` and retry requires explicit confirmation; many -> ambiguous/blocked. Expired authorization remains evidence for verify/reconcile, not permission to write.

## Boundaries

- Zotero writes must use Zotero MCP `write_note`.
- Zotero lookup and duplicate blocking apply only to non-path title/title-fragment inputs.
- Zotero local API and SQLite are read-only in this project.
- Do not update existing Zotero notes; create a new versioned child note instead.
- An immutable candidate is not write authority; only its exact current immutable authorization can authorize one external MCP create call.
- Do not use local publish readiness as proof of Zotero write readiness.
- Do not cite `section_context.md` or secondary context as canonical evidence.
- V1/unversioned/unknown artifacts are historical-only and must fail before lock, network or mutation with `unsupported_run_schema`; no alias, migration or fallback is permitted.
