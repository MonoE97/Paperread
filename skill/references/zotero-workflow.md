# Zotero Workflow

Use this when the user provides a Zotero title or title fragment. Run commands from the skill root.

## Tool Discovery

Load Zotero MCP tools before the workflow: `search_library`, `get_item_details`, `get_content`, `write_note`, and optional `annotations`.

If native MCP tools are not injected, use the local Zotero MCP endpoint `http://127.0.0.1:23120/mcp` as an HTTP JSON-RPC fallback. The fallback still calls Zotero MCP methods such as `zotero-mcp write_note`; it is not a Zotero local API write path. If localhost requests hit a proxy, clear `ALL_PROXY`, `HTTP_PROXY`, and `HTTPS_PROXY`, then set `NO_PROXY=127.0.0.1,localhost` and `no_proxy=127.0.0.1,localhost`.

## Steps

1. Search exact title first. If duplicate entries have the same normalized title, stop before create-run and ask the user to de-duplicate in Zotero.
2. Create the run directory with `uv run paperread create-run --title "<title>" --item-key "<item_key>"`.
3. Save the raw `get_item_details(mode="complete")` response as `<run_dir>/mcp-response.json`.
4. Normalize item details:

```bash
uv run paperread save-item-details <run_dir>/mcp-response.json --output <run_dir>/item-details.json --raw-output <run_dir>/item-details.raw.json
```

5. Prepare the bundle:

```bash
uv run paperread prepare-item <run_dir>/item-details.json --workdir <run_dir>
```

6. If `secondary_sources.json` lists Extra/web URLs, capture each source for cross-check only:

```bash
mkdir -p <run_dir>/secondary_contexts
node scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

Captured files use `source_status: secondary_context` when usable. Unavailable captures use `source_status: secondary_context_unavailable`, including warnings such as `navigation_timeout`. Secondary context must not cite secondary context in `evidence_summary`; it is only for cross-checking and background.

7. Read `context.md`, `section_context.md`, and `figure_context.md` if available. `section_context.md` is not a canonical evidence source. Final locators must cite `context.md page 3 section Methods`, `context.md page 6 section Results table_candidate 1`, or `figure_context.md fig_p4_1`.
8. Write `summary.json` and `review.json`.
9. Run the deterministic review chain:

```bash
uv run paperread validate-summary-json <run_dir>/summary.json
uv run paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run paperread lint-summary <run_dir>/summary.json
uv run paperread validate-trusted-summary <run_dir>/summary.json
```

10. Prepare a Zotero write candidate only when Zotero output is explicitly requested:

```bash
uv run paperread prepare-write-candidate <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD
```

11. Preview the target Zotero title, `note.md`, and `note.html`.
12. After explicit write intent, call only `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`.
13. Verify with `verify-zotero-note` using expected parent, title, headings, tags, and content hash from `write-payload.json`.

## Boundaries

- Zotero writes must use Zotero MCP `write_note`.
- Zotero local API and SQLite are read-only in this project.
- Do not update existing Zotero notes; create a new versioned child note instead.
- Do not use the PDF local gate as proof of Zotero write readiness.
- Do not cite `section_context.md` or secondary context as canonical evidence.
