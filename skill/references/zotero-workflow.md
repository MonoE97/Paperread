# Zotero Workflow

Use this when the user provides a Zotero title or title fragment. Run commands from the skill root. Local PDF path and directory path inputs skip Zotero lookup and duplicate checks. Existing local paths are not Zotero title fragments.

## Setup

Run from the skill root:

```bash
uv --version
uv sync --locked
uv run paperread --help
```

If `uv sync --locked` cannot find Python `>=3.13`, run `uv python install 3.13` from the skill root and retry. If `uv` is not installed, stop and ask the user to install `uv` first; do not use `pip`, `conda`, or system Python as a replacement.

## Output Location

By default, `create-run` writes local artifacts under `<skill_root>/runs/YYYY-MM-DD/<title-slug>/`. Keep `mcp-response.json`, `item-details.json`, `metadata.json`, `context.md`, `section_context.md`, optional figure artifacts, `summary.json`, and `review.json` in that run directory. `prepare-write-candidate` adds `note.md`, `note.html`, previews, `note-tags.json`, `gate-report.json`, and `write-payload.json` in the same directory.

## Tool Discovery

Before using the Zotero workflow, install and enable Zotero MCP from
https://github.com/cookjohn/zotero-mcp#readme. Download the
`zotero-mcp-plugin-*.xpi`, install it in Zotero with `Tools -> Add-ons`, restart
Zotero, then enable the integrated server in `Preferences -> Zotero MCP Plugin`.

Load Zotero MCP tools before the workflow: `search_library`, `get_item_details`, `get_content`, `write_note`, and optional `annotations`.

If native MCP tools are not injected, use the local Zotero MCP endpoint `http://127.0.0.1:23120/mcp` as an HTTP JSON-RPC fallback. The fallback still calls Zotero MCP methods such as `zotero-mcp write_note`; it is not a Zotero local API write path. If localhost requests hit a proxy, clear `ALL_PROXY`, `HTTP_PROXY`, and `HTTPS_PROXY`, then set `NO_PROXY=127.0.0.1,localhost` and `no_proxy=127.0.0.1,localhost`.

## Steps

1. Before searching Zotero, confirm the input is non-path text. Existing `.pdf` paths must use the local PDF path workflow, and existing directory paths must be delegated to `$paperread-batch`; neither path type should trigger Zotero lookup or duplicate checks.
2. Search exact title first. If duplicate entries have the same normalized title, stop before create-run and ask the user to de-duplicate in Zotero.
3. Create the run directory with `uv run paperread create-run --title "<title>" --item-key "<item_key>"`.
4. Save the raw `get_item_details(mode="complete")` response as `<run_dir>/mcp-response.json`.
5. Normalize item details:

```bash
uv run paperread save-item-details <run_dir>/mcp-response.json --output <run_dir>/item-details.json --raw-output <run_dir>/item-details.raw.json
```

6. Prepare the bundle:

```bash
uv run paperread prepare-item <run_dir>/item-details.json --workdir <run_dir>
```

7. If `secondary_sources.json` lists Extra/web URLs, capture each source for cross-check only:

```bash
mkdir -p <run_dir>/secondary_contexts
node scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

Captured files use `source_status: secondary_context` when usable. Unavailable captures use `source_status: secondary_context_unavailable`, including warnings such as `navigation_timeout`. Secondary context must not cite secondary context in `evidence_summary`; it is only for cross-checking and background.

8. Read `context.md`, `section_context.md`, and `figure_context.md` if available. `section_context.md` is not a canonical evidence source. Final locators must use canonical forms such as `context.md page 3 section Methods`, `context.md page 6 section Results table_candidate 1`, or `figure_context.md fig_p4_1`. Bare `context.md` / `figure_context.md`, prose locators such as `page 3 method section`, `section_context.md`, and secondary context paths are invalid.
9. Write `summary.json` and `review.json`.
10. Run the deterministic review chain:

```bash
uv run paperread validate-summary-json <run_dir>/summary.json
uv run paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run paperread lint-summary <run_dir>/summary.json
uv run paperread validate-trusted-summary <run_dir>/summary.json
```

11. Prepare a Zotero write candidate only when Zotero output is explicitly requested:

```bash
uv run paperread prepare-write-candidate <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD
```

12. Preview the target Zotero title, `note.md`, and `note.html`.
13. After explicit write intent, call only `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`.
14. Verify with `verify-zotero-note` using expected parent, title, headings, tags, and content hash from `write-payload.json`.

## Boundaries

- Zotero writes must use Zotero MCP `write_note`.
- Zotero lookup and duplicate blocking apply only to non-path title/title-fragment inputs.
- Zotero local API and SQLite are read-only in this project.
- Do not update existing Zotero notes; create a new versioned child note instead.
- Do not use the PDF local gate as proof of Zotero write readiness.
- Do not cite `section_context.md` or secondary context as canonical evidence.
