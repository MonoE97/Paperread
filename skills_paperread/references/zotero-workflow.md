# Zotero Workflow

Use this when the user provides a Zotero title or title fragment.

## Steps

1. Use Zotero MCP tool discovery for `search_library`, `get_item_details`, and `write_note`.
2. Search exact title first. If duplicate normalized titles exist, stop and ask the user to de-duplicate in Zotero.
3. Save raw MCP item details, then normalize:

```bash
uv run zotero-paperread save-item-details <run_dir>/mcp-response.json --output <run_dir>/item-details.json --raw-output <run_dir>/item-details.raw.json
```

4. Prepare the bundle:

```bash
uv run zotero-paperread prepare-item <run_dir>/item-details.json --workdir <run_dir>
```

5. Read `context.md`, `section_context.md`, and `figure_context.md` if available. Write `summary.json` and `review.json`.

6. Run the deterministic review chain:

```bash
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread lint-summary <run_dir>/summary.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
```

7. Prepare a Zotero write candidate only when Zotero output is requested:

```bash
uv run zotero-paperread prepare-write-candidate <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD
```

8. After explicit write intent, call only `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`.

9. Verify with `verify-zotero-note` using the content hash and checks from `write-payload.json`.

## Boundaries

- Zotero writes must use Zotero MCP `write_note`.
- Zotero local API and SQLite are read-only in this project.
- Do not use the PDF local gate as proof of Zotero write readiness.
