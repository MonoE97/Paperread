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
- `section_context.md`
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
5. extract PDF text, page records, section records, and conservative table/value candidates with a local `uv`-managed Python CLI;
6. extract figures, backfill nearby captions for embedded images, and analyze key images when available;
7. capture Extra/web links as secondary context for cross-checking only;
8. generate a Chinese structured paper summary with figure-aware analysis and the compact 0-5 reading-card layout;
9. validate summary JSON, apply review, lint and trusted-summary gates, then render auditable Markdown plus Zotero-ready HTML;
10. prepare a versioned write candidate that refreshes live child-note titles, computes the same-day suffix, previews both `note.md` and `note.html`, and writes `gate-report.json` plus `write-payload.json`;
11. create a new Zotero child note only when explicitly requested, using `zotero-mcp write_note(action="create", ...)`;
12. verify the written note through read-only Zotero local API readback.

The normal Zotero path is native `zotero-mcp` tool access. This repository intentionally does not implement a built-in HTTP JSON-RPC fallback client, but Codex agents may use the local Zotero MCP endpoint `http://127.0.0.1:23120/mcp` as a session-level HTTP JSON-RPC fallback when native MCP tools are not injected. The fallback must still call Zotero MCP methods such as `zotero-mcp write_note`; it must not use Zotero local API, SQLite, or any other write path.

If localhost requests hit a proxy, clear `ALL_PROXY` / `HTTP_PROXY` / `HTTPS_PROXY` and set `NO_PROXY=127.0.0.1,localhost` plus `no_proxy=127.0.0.1,localhost` before retrying.

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

## Current Stable Single-Paper Flow

Use this flow for new paper notes:

1. Search Zotero through MCP and stop on duplicate normalized titles.
2. Save the raw `get_item_details(mode="complete")` response, then normalize it with `save-item-details`.
3. Run `prepare-item` with the full PDF by default.
4. Draft `summary.json` from `context.md`, `figure_context.md`, and `section_context.md`; use `section_context.md` only for navigation.
5. Produce `review.json`, apply the review, lint and validate trusted summary fields.
6. Run `prepare-write-candidate <run_dir> --paper-title "<title>" --generated-date YYYY-MM-DD`, which runs `refresh-live-notes -> next-version-suffix -> finalize-note --html-output -> note-tags -> preview-note -> gate-run -> prepare-write-payload`.
7. Show the target Zotero title plus `note.md` and `note.html` previews.
8. After explicit write intent, call only `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`.
9. Run `verify-zotero-note` with the payload readback checks.

Rendered reading-note prose is Chinese-first. Proper nouns, paper titles, author names, institution names, formulas, material/model/method names, abbreviations, units, evidence locators, code-like keys, and Zotero tag keys may stay in English. Free-text explanations that appear in `note.md` / `note.html`, including `method_modules`, `workflow_steps`, `technical_details`, figure analysis, figure rationale, captions used as fallback when figure analysis is missing, and limitation fields, should not be written as English prose. `lint-summary` reports `rendered_note_field_english_prose`, and `gate-run` blocks write-through until those fields are rewritten in Chinese.

`prepare-write-candidate` is the normal write-preparation entry point. It uses read-only live note refresh, computes the next version suffix, regenerates `note.md` and `note.html`, writes `preview-note-md.txt` and `preview-note-html.txt`, writes `note-tags.json`, runs `gate-run`, and writes `write-payload.json` only when the gate is `write_ready`.

`write-payload.json` must live at `<run_dir>/write-payload.json`. The CLI rejects attempts to write the payload over `gate-report.json`, over `note.html`, to a non-`write-payload.json` filename, or outside the gate report's run directory. If a rerun is blocked, stale `write-payload.json` is removed so an old payload cannot be mistaken for a fresh write candidate.

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
uv run zotero-paperread refresh-live-notes runs/<date>/<paper-slug>/item-details.json --output runs/<date>/<paper-slug>/item-details.json
uv run zotero-paperread next-version-suffix runs/<date>/<paper-slug>/item-details.json --paper-title "<title>" --generated-date "<YYYY-MM-DD>"
uv run zotero-paperread render-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md
uv run zotero-paperread finalize-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md
uv run zotero-paperread finalize-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md --html-output runs/<date>/<paper-slug>/note.html
uv run zotero-paperread finalize-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md --html-output runs/<date>/<paper-slug>/note.html --version-suffix " (v2)"
uv run zotero-paperread render-note-html runs/<date>/<paper-slug>/note.md --output runs/<date>/<paper-slug>/note.html
uv run zotero-paperread gate-run runs/<date>/<paper-slug> --paper-title "<title>" --generated-date "<YYYY-MM-DD>" --output runs/<date>/<paper-slug>/gate-report.json
uv run zotero-paperread prepare-write-payload runs/<date>/<paper-slug>/gate-report.json --output runs/<date>/<paper-slug>/write-payload.json
uv run zotero-paperread prepare-write-candidate runs/<date>/<paper-slug> --paper-title "<title>" --generated-date "<YYYY-MM-DD>"
uv run zotero-paperread verify-zotero-note <note_key> --expected-parent <item_key>
uv run zotero-paperread note-tags runs/<date>/<paper-slug>/summary.json
uv run zotero-paperread validate-note runs/<date>/<paper-slug>/note.md
uv run zotero-paperread preview-note runs/<date>/<paper-slug>/note.md
uv run zotero-paperread preview-note runs/<date>/<paper-slug>/note.html
```

The recommended dry-run manual sequence is:

```bash
uv run zotero-paperread create-run --title "<title>" --item-key "<item_key>"
uv run zotero-paperread prepare-item runs/<date>/<paper-slug>/item-details.json --workdir runs/<date>/<paper-slug>
# Codex then reads context.md / section_context.md / figure_context.md and writes summary.json.
uv run zotero-paperread validate-summary-json runs/<date>/<paper-slug>/summary.json
uv run zotero-paperread finalize-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md --html-output runs/<date>/<paper-slug>/note.html
uv run zotero-paperread preview-note runs/<date>/<paper-slug>/note.md
uv run zotero-paperread preview-note runs/<date>/<paper-slug>/note.html
```

`create-run` prints a JSON payload containing `run_dir`, `manifest_path`, `slug`, and `date`. Use the returned `run_dir` instead of guessing the final slug when there may already be a same-day run for the same title.

By default, `prepare-item`, `extract-pdf`, and `extract-figures` process the full PDF. Use `--max-pages <N>` only for explicit debugging or deliberately shortened dry runs.

`prepare-item` writes `section_context.md` when structured extraction data is available. Codex should read it as a navigation aid for sections and table/value candidates, but it is not a canonical evidence source. Final `evidence_summary` locators must still cite canonical sources such as `context.md page 3 section Methods`, `context.md page 6 section Results table_candidate 1`, or `figure_context.md fig_p4_1`.

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

After generating `review.json`, the recommended final write-through preparation for a single-paper summary is:

```bash
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread lint-summary <run_dir>/summary.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
PAPER_TITLE="<paper title>"
GENERATED_DATE="<YYYY-MM-DD>"
uv run zotero-paperread prepare-write-candidate <run_dir> --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE"
```

The lower-level debug chain inside `prepare-write-candidate` is:

```bash
uv run zotero-paperread refresh-live-notes <run_dir>/item-details.json --output <run_dir>/item-details.json
VERSION_SUFFIX="$(uv run zotero-paperread next-version-suffix <run_dir>/item-details.json --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE")"
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --generated-date "$GENERATED_DATE" --version-suffix "$VERSION_SUFFIX" --output <run_dir>/note.md --html-output <run_dir>/note.html
uv run zotero-paperread note-tags <run_dir>/summary.json
uv run zotero-paperread preview-note <run_dir>/note.md --output <run_dir>/preview-note-md.txt
uv run zotero-paperread preview-note <run_dir>/note.html --output <run_dir>/preview-note-html.txt
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

`prepare-write-payload does not write to Zotero`. It records `parentKey`, tags, `note_html_path`, `contentLength`, `contentSha256`, and readback checks. The actual write remains an explicit `zotero-mcp write_note` action performed by the agent after the gate report is `write_ready`.

`contentSha256` is computed with the same terminal-newline normalization used by `verify-zotero-note`, because Zotero readback may trim or normalize trailing newlines. Treat the hash as the canonical readback check; do not recompute it with ad hoc shell commands.

For single-paper summaries, single-paper summary writes always create a new versioned Zotero child note. Do not update an existing `[Codex Summary]` note for normal paper summaries. The recommended daily command is `prepare-write-candidate <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD`; it runs read-only `refresh-live-notes`, computes the next suffix, regenerates `note.md` and `note.html`, writes previews, runs `gate-run`, and writes `write-payload.json`. The lower-level debug chain is `refresh-live-notes -> next-version-suffix -> finalize-note --html-output -> note-tags -> preview-note note.md/note.html -> gate-run -> prepare-write-payload`. The actual persistent write remains `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`.

Zotero local API is read-only in this project. It may be used by `refresh-live-notes` and `verify-zotero-note`, but it must not be used for PUT, PATCH, POST, DELETE, SQLite mutation, or any persistent write.

After `write_note(action="create", ...)` returns the new note key, verify readback with the exact expected title from `write-payload.json`:

```bash
uv run zotero-paperread verify-zotero-note <note_key> \
  --expected-parent <payload parentKey> \
  --expected-title "<payload noteTitle>" \
  --required-heading "0. 阅读结论" \
  --required-heading "1. 速读信息" \
  --required-heading "2. 论文主张" \
  --required-heading "3. 方法与设计" \
  --required-heading "4. 图表导读" \
  --required-heading "5. 边界与机会" \
  --forbidden-heading "3. 结果可信度" \
  --forbidden-heading "6. 我能怎么用" \
  --forbidden-heading "7. 术语与检索" \
  --forbidden-heading "9. 元数据" \
  --forbidden-heading "10. 证据链附录" \
  --forbidden-heading "11. 补充优化记录" \
  --expected-tag codex-summary \
  --expected-tag paper-summary \
  --expected-content-sha256 <payload required_readback_checks.contentSha256> \
  --min-content-length <payload required_readback_checks.contentLengthAtLeast>
```

For historical note migration, `write_note(action="update", ...)` is still allowed after explicit confirmation because the task is a content-format migration, not a new paper summary. If a migration update times out and readback still shows old content, stop and report the failed update readback; do not create a duplicate migration note unless the user explicitly asks for that separate recovery action.

The rendered note is a compact 0-5 reading card. It opens with `## 0. 阅读结论`, a table containing the 30-second conclusion, main risk, and reading decision. `## 1. 速读信息` is a second table containing paper type, research object, core problem, core method, and core result. The body then follows `## 2. 论文主张`, `## 3. 方法与设计`, `## 4. 图表导读`, and `## 5. 边界与机会`. Internal enum values such as `research_article`, `strongly_recommended`, and `ok` render as Chinese display labels rather than raw keys. Trust status, quality score, key result tables, baseline/comparison notes, result evidence notes, concept cards, follow-up keywords, metadata, evidence chains, review status, and improvement notes remain in JSON artifacts and gate reports instead of being rendered as Zotero note sections.

New learning-note fields such as `method_modules`, `key_results_table`, `concept_cards`, `workflow_lessons`, `reading_decision`, `recommended_sections`, `recommended_figures`, `baseline_or_comparison`, `result_evidence_notes`, `author_stated_limitations`, `inferred_limits`, `potential_gaps`, and `evidence_quality_summary` are optional. Old `summary.json` files still render through safe fallbacks: `method_overview` falls back to `method`, `core_result_short` falls back to `one_sentence_summary`, and `applicability_limits` can carry reusable opportunity/boundary statements. Author-stated limitations, LLM-inferred limits, and potential gaps should be kept separate in JSON so inferred reading judgments are not presented as paper-authored claims.

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
- Current label behavior: the note no longer renders a separate `## 本文标签` section. It ends with a single `Tags:` line containing `codex-summary`, `paper-summary`, and at most four inferred English key labels. These are machine-readable tag keys and are exempt from the Chinese-prose rule. The same label set is available through `uv run zotero-paperread note-tags <summary.json>` and should be passed to Zotero `write_note(..., tags=...)` so Zotero note metadata tags match the rendered note.

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
