# Zotero Paperread

Zotero-first literature summary workflow for Codex.

## Run Directory

Each invocation creates a project-local run directory under:

```text
runs/<date>/<paper-slug>/
```

`create-run` initializes that directory and writes `run.json` as the run manifest. Typical contents are:

- `run.json`
- `mcp-response.json`
- `item-details.json`
- `item-details.raw.json`
- `metadata.json`
- `extract.json`
- `context.md`
- `secondary_sources.json`
- `secondary_contexts/`
- `figures.json`
- `figure_context.md`
- `summary.json`
- `review.json`
- `note.md`
- `note.html`
- `gate-report.json`
- `write-payload.json`
- `figures/`

These are intermediate and audit artifacts. Keep them while reviewing a run. Delete old runs manually when they are no longer useful.

## What It Does

Given a Zotero paper title, Codex can:

1. find the Zotero item through `zotero-mcp`;
2. locate the attached PDF path, preferring the main paper over appendices or supporting-information PDFs;
3. create and reuse a local `runs/<date>/<paper-slug>/` bundle for that paper;
4. normalize item details and recover missing Zotero `Extra` / `其他` through a read-only SQLite fallback when needed;
5. extract PDF text with a local `uv`-managed Python CLI;
6. extract figures, backfill nearby captions for embedded images, and analyze key images when available;
7. capture Extra/web links as secondary context for cross-checking only;
8. generate a Chinese structured paper summary with figure-aware analysis;
9. render a small set of normalized English key labels at the end of the note;
10. validate summary JSON, render auditable Markdown plus Zotero-ready HTML, and preview the note;
11. create a Zotero child note only when explicitly requested.

The normal Zotero path is native `zotero-mcp` tool access. This repository intentionally does not implement an HTTP JSON-RPC fallback client for Zotero writes; if a Codex session lacks native Zotero MCP tools, treat it as a session/tool-injection issue and start a fresh session or verify the MCP registration.

## Codex Workflow

The intended top-level entry is the repo-local Codex skill:

- `skills/zotero-paper-summary/SKILL.md`

In Codex, the user should be able to say:

```text
summarize-zotero-title "Crystal Structure Prediction Meets Artificial Intelligence"
```

If the user wants the note written back immediately, the intended write-through forms are:

```text
summarize-zotero-title "Crystal Structure Prediction Meets Artificial Intelligence" and write to zotero
请帮我分析这篇文献并写入笔记：Crystal Structure Prediction Meets Artificial Intelligence
请对 Zotero 中的 Crystal Structure Prediction Meets Artificial Intelligence 文章进行分析并输出笔记
```

The skill then performs Zotero lookup, run-directory creation, bundle preparation, figure-aware summary generation, note validation, and creates a Zotero child note only when the user message contains explicit write intent.

In this project/user-specific convention, `输出笔记` also means Zotero write-through intent, not just printing Markdown. It still requires all write-through gates in the Trusted Notes section, and the write must be performed only through `zotero-mcp write_note`.

### Duplicate Zotero Entries

If a title search finds duplicate Zotero entries with the same normalized title, the workflow stops before `create-run`. The agent must not choose among duplicate items; in short, do not choose among duplicate items because writing to the wrong parent item is harder to recover from than asking the user to de-duplicate first.

The user-facing message should be direct: duplicate Zotero entries exist; please de-duplicate in Zotero first, then rerun the workflow. If a broad `contains` search finds several different titles, the workflow asks for a more exact title or item key instead.

For a dry-run note render, preferred note finalization command:

```text
finalize-note --html-output <run_dir>/note.html -> preview-note note.md -> preview-note note.html
```

`finalize-note` encapsulates `render-note -> validate-note` in the correct order. For Zotero writes, pass `--html-output <run_dir>/note.html` and send that HTML file to `write_note`; Zotero notes are HTML internally, and Markdown table syntax is not reliable at the write boundary. If you still call the lower-level commands manually, keep `render-note -> validate-note -> render-note-html -> preview-note` strictly sequential and do not parallelize them.

Same-day regenerated notes should not overwrite earlier notes. Use a date-only title for the first note and pass `--version-suffix " (v2)"`, `--version-suffix " (v3)"`, etc. for later notes on the same date.

## MCP Tool Discovery

Codex App may lazy-load MCP tool schemas. Before running a Zotero note workflow, use `tool_search` to load the full Zotero tool set with a targeted tool search for `search_library`, `get_item_details`, `get_content`, `write_note`, and `annotations`. `annotations` tools are optional enhancements; the required core tools are `search_library`, `get_item_details`, `get_content`, and `write_note`.

Known MCP behavior: `get_item_details` is available in `cookjohn/zotero-mcp` 1.4.7. If Codex does not show it initially, run a targeted tool search before assuming the MCP server lacks the tool.

## Local Commands

```bash
uv run zotero-paperread --help
uv run zotero-paperread create-run --title "<title>" --item-key "<item_key>"
uv run zotero-paperread save-item-details runs/<date>/<paper-slug>/mcp-response.json --output runs/<date>/<paper-slug>/item-details.json --raw-output runs/<date>/<paper-slug>/item-details.raw.json
uv run zotero-paperread prepare-item runs/<date>/<paper-slug>/item-details.json --workdir runs/<date>/<paper-slug>
uv run zotero-paperread extract-pdf path/to/paper.pdf --output runs/<date>/<paper-slug>/extract.json
uv run zotero-paperread extract-figures path/to/paper.pdf --output-dir runs/<date>/<paper-slug>/figures --top-k 4
uv run zotero-paperread validate-summary-json runs/<date>/<paper-slug>/summary.json
uv run zotero-paperread apply-review runs/<date>/<paper-slug>/summary.json runs/<date>/<paper-slug>/review.json
uv run zotero-paperread lint-summary runs/<date>/<paper-slug>/summary.json
uv run zotero-paperread validate-trusted-summary runs/<date>/<paper-slug>/summary.json
uv run zotero-paperread next-version-suffix runs/<date>/<paper-slug>/item-details.json --paper-title "<title>" --generated-date "<YYYY-MM-DD>"
uv run zotero-paperread render-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md
uv run zotero-paperread finalize-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md
uv run zotero-paperread finalize-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md --html-output runs/<date>/<paper-slug>/note.html
uv run zotero-paperread finalize-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md --html-output runs/<date>/<paper-slug>/note.html --version-suffix " (v2)"
uv run zotero-paperread render-note-html runs/<date>/<paper-slug>/note.md --output runs/<date>/<paper-slug>/note.html
uv run zotero-paperread gate-run runs/<date>/<paper-slug> --paper-title "<title>" --generated-date "<YYYY-MM-DD>" --output runs/<date>/<paper-slug>/gate-report.json
uv run zotero-paperread prepare-write-payload runs/<date>/<paper-slug>/gate-report.json --output runs/<date>/<paper-slug>/write-payload.json
uv run zotero-paperread note-tags runs/<date>/<paper-slug>/summary.json
uv run zotero-paperread validate-note runs/<date>/<paper-slug>/note.md
uv run zotero-paperread preview-note runs/<date>/<paper-slug>/note.md
uv run zotero-paperread preview-note runs/<date>/<paper-slug>/note.html
```

The recommended dry-run manual sequence is:

```bash
uv run zotero-paperread create-run --title "<title>" --item-key "<item_key>"
uv run zotero-paperread prepare-item runs/<date>/<paper-slug>/item-details.json --workdir runs/<date>/<paper-slug>
# Codex then reads context.md / figure_context.md and writes summary.json.
uv run zotero-paperread validate-summary-json runs/<date>/<paper-slug>/summary.json
uv run zotero-paperread finalize-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md --html-output runs/<date>/<paper-slug>/note.html
uv run zotero-paperread preview-note runs/<date>/<paper-slug>/note.md
uv run zotero-paperread preview-note runs/<date>/<paper-slug>/note.html
```

`create-run` prints a JSON payload containing `run_dir`, `manifest_path`, `slug`, and `date`. Use the returned `run_dir` instead of guessing the final slug when there may already be a same-day run for the same title.

By default, `prepare-item`, `extract-pdf`, and `extract-figures` process the full PDF. Use `--max-pages <N>` only for explicit debugging or deliberately shortened dry runs.

`validate-summary-json` only verifies that the file is readable UTF-8 JSON with an object at the top level. It is not a semantic schema validator. `render-note` and `finalize-note` use the same friendly JSON error path, so malformed or missing JSON fails before any partial note is written.

## Secondary Context

When the user provides a WeChat article, press release, blog, or other webpage as supplemental context, capture it as secondary context instead of mixing it into PDF evidence:

```bash
node skills/zotero-paper-summary/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_context.md
```

`save-item-details` also smooths Zotero `Extra` / `其他` handling. If the MCP `get_item_details` response omits `extra`, the command uses a read-only SQLite fallback against `~/Zotero/zotero.sqlite`. Successful immutable SQLite Extra reads are recorded as provenance diagnostics under `_paperread.enrichment.extra.diagnostics`; they are not treated as extraction warnings because the Extra value was recovered. Actual missing or unreadable Extra fallback remains a warning. Disable this fallback with `--no-sqlite-extra-fallback`.

`prepare-item` reads normalized `item-details.json`, extracts `http://` / `https://` URLs from `extra`, and writes `<run_dir>/secondary_sources.json`. When `sources` is non-empty, capture each URL with:

```bash
mkdir -p <run_dir>/secondary_contexts
node skills/zotero-paper-summary/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

Captured secondary contexts are `cross-check only; must not be cited in evidence_summary`. Trusted evidence remains limited to `context.md` and `figure_context.md`.

The capture script waits up to `60000` ms for browser navigation and non-empty page text. Use `--timeout-ms <ms>` and `--poll-ms <ms>` only for debugging or tests. By default, transient CDP request failures are retried with `--request-retries 2` and `--request-retry-ms 500`. A successful capture contains `source_status: secondary_context` and can be used for cross-checking, background, and follow-up questions, but `evidence_summary` must not cite secondary context. If a transient request recovers, the output keeps `source_status: secondary_context` and records a `capture_warning`. If the page never leaves `about:blank` or never yields text before timeout, the file contains `source_status: secondary_context_unavailable` and `capture_warning: navigation_timeout`. Persistent CDP failures write secondary_context_unavailable with a `capture_warning` instead of a raw stack trace. Do not treat unavailable files as usable secondary material. Trusted evidence remains limited to `context.md` and `figure_context.md`.

## Trusted Notes

The workflow asks Codex to classify paper type, assign trust status, attach compact evidence pointers, and run a second-pass note quality review before Zotero write-through. If review finds fixable omissions, Codex may perform one bounded improvement pass by re-reading only the current run directory artifacts.

After generating `review.json`, the final write-through gate order is:

```bash
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread lint-summary <run_dir>/summary.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
PAPER_TITLE="<paper title>"
GENERATED_DATE="<YYYY-MM-DD>"
VERSION_SUFFIX="$(uv run zotero-paperread next-version-suffix <run_dir>/item-details.json --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE")"
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --generated-date "$GENERATED_DATE" --version-suffix "$VERSION_SUFFIX" --output <run_dir>/note.md --html-output <run_dir>/note.html
NOTE_TAGS_JSON="$(uv run zotero-paperread note-tags <run_dir>/summary.json)"
uv run zotero-paperread preview-note <run_dir>/note.md
uv run zotero-paperread preview-note <run_dir>/note.html
uv run zotero-paperread gate-run <run_dir> --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE" --output <run_dir>/gate-report.json
uv run zotero-paperread prepare-write-payload <run_dir>/gate-report.json --output <run_dir>/write-payload.json
```

Write to Zotero only if all of these are true:

```text
review_status is passed or passed_with_caveats
review.json needs_improvement is false
summary.json improvement_status is neither needed nor blocked after apply-review
validate-trusted-summary passes
same-day version suffix has been computed from current item-details.json
note tags have been computed from current summary.json
preview-note has been shown for note.md and note.html
target Zotero item title has been shown
```

Actual Zotero write-through still requires explicit write intent and uses only `zotero-mcp write_note`. Use `content=<contents of note.html>` for writes so Markdown tables are already converted to Zotero-renderable HTML.

`prepare-write-payload does not write to Zotero`. It records `parentKey`, tags, `note_html_path`, `contentLength`, and readback checks. The actual write remains an explicit `zotero-mcp write_note` action performed by the agent after the gate report is `write_ready`.

The rendered note is a layered learning note. It opens with `## 0. 速读卡片` so the first screen shows paper type, research object, core problem, core method, core result, trust status, main risk, reading decision, and relevance to AI4S / battery / materials research. The main body then follows problem, method, results, figures, contributions, limits, transferable workflows, concept cards, and follow-up keywords. Metadata, extraction warnings, review issues, trust rationale, evidence chains, and improvement notes are kept in rear sections (`## 9` through `## 12`) so provenance remains available without interrupting the reading flow.

New learning-note fields such as `method_modules`, `key_results_table`, `concept_cards`, `workflow_lessons`, and `reading_decision` are optional. Old `summary.json` files still render through safe fallbacks: `method_overview` falls back to `method`, `core_result_short` falls back to `one_sentence_summary`, and `transferable_insight` falls back to `ai4s_relevance`.

## Historical Note Table Migration

Historical `[Codex Summary]` notes created before `note.html` support may contain Markdown table syntax that Zotero displays as plain text. Do not rerun paper summarization for this. Treat it as a content-format migration.

Safe migration order:

1. Discover candidate notes through Zotero MCP.
2. Save raw note content under `runs/migrations/<date>-zotero-note-table-html/raw/`.
3. Classify each raw note with `uv run zotero-paperread classify-note-tables <raw-note-file>`.
4. Convert only dry-run local files with `uv run zotero-paperread convert-note-tables <raw-note-file> --output <converted-file> --report <report-json>`.
5. Review `manifest.json`, converted previews, and `report.md`.
6. Stop for explicit user confirmation.
7. After confirmation, update one Zotero note at a time with `write_note(action="update", noteKey=<note_key>, content=<converted_html>)`.
8. Verify each update by reading the note back through Zotero MCP.

Do not pass tags during update. This migration changes note content only.

## V2: Key Figure Extraction and Analysis

- Primary path: resolve arXiv ID and extract source images or source-side figure PDFs when possible.
- Secondary path: detect figure captions and crop the rendered region above or below the caption for local-only PDFs.
- Supplement path: extract embedded PDF images only when source and deterministic paths are sparse, then backfill exactly one nearby unclaimed caption when the spatial match is unambiguous.
- Fallback path: run OCR only when deterministic extraction is low-confidence and the project-local OCR adapter is available.
- Output: `figures/`, `figures.json`, `figure_context.md`.
- `figure_context.md` includes `Caption Confidence`, source attempts, warnings, priority score, and fallback metadata.
- Figure ranking includes generic scientific-plot signals such as capacitance, charge density/response, concentration distributions, PMFs, ions, cations, and anions.
- If figure extraction crashes, `prepare-item` keeps the text bundle, clears stale optional figure artifacts, and surfaces `figure_extraction_failed` plus a compact `figure_extraction_error:<type>:<message>` warning.
- Goal: improve scientific reading quality, not just embed pictures.
- Current note behavior: figure analysis is written into the Zotero note even if inline image embedding is not enabled.
- Current label behavior: the note no longer renders a separate `## 本文标签` section. It ends with a single `Tags:` line containing `codex-summary`, `paper-summary`, and at most four inferred English key labels. The same label set is available through `uv run zotero-paperread note-tags <summary.json>` and should be passed to Zotero `write_note(..., tags=...)` so Zotero note metadata tags match the rendered note.

## Safety

- Dry-run is the default workflow.
- Phrases like `输出笔记`, `写入笔记`, `写回 Zotero`, `创建 note`, and `保存到 Zotero` count as explicit write intent. `输出笔记` is a project/user-specific convention for Zotero write-through and still requires all Trusted Notes write-through gates plus `zotero-mcp write_note` as the only write path.
- If a Zotero item already has a Codex summary note, the skill stops by default and reports the existing note. It only continues when the user explicitly asks to continue or regenerate.
- Same-day regeneration creates a new title version such as `[Codex Summary] <paper title> - YYYY-MM-DD (v2)` instead of overwriting or reusing the first title.
- Tests never write to Zotero.
- Zotero writes happen only through `zotero-mcp write_note`.
- Better Notes is optional and not called directly.

## Reference

This project adapts the skill-based paper-analysis ideas from `evil-read-arxiv`, but replaces arXiv/Obsidian assumptions with a Zotero-first workflow.

For batch note writing and historical note-content migrations, see
`docs/references/zotero-batch-write-runbook.md`.
