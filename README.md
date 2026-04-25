# Zotero Paperread

Zotero-first literature summary workflow for Codex.

## Run Directory

Each invocation creates a project-local run directory under:

```text
runs/<date>/<paper-slug>/
```

`create-run` initializes that directory and writes `run.json` as the run manifest. Typical contents are:

- `run.json`
- `item-details.json`
- `metadata.json`
- `extract.json`
- `context.md`
- `figures.json`
- `figure_context.md`
- `summary.json`
- `note.md`
- `figures/`

These are intermediate and audit artifacts. Keep them while reviewing a run. Delete old runs manually when they are no longer useful.

## What It Does

Given a Zotero paper title, Codex can:

1. find the Zotero item through `zotero-mcp`;
2. locate the attached PDF path, preferring the main paper over appendices or supporting-information PDFs;
3. create and reuse a local `runs/<date>/<paper-slug>/` bundle for that paper;
4. extract PDF text with a local `uv`-managed Python CLI;
5. extract figures, backfill nearby captions for embedded images, and analyze key images when available;
6. generate a Chinese structured paper summary with figure-aware analysis;
7. render a small set of normalized English key labels at the end of the note;
8. validate summary JSON, render Markdown that survives Zotero list conversion, and preview the note;
9. create a Zotero child note only when explicitly requested.

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

In this project/user-specific convention, `输出笔记` also means Zotero write-through intent, not just printing Markdown. It still requires the existing write gates: note preview shown, target Zotero item title shown, and the write performed only through `zotero-mcp write_note`.

Preferred note finalization command:

```text
finalize-note -> preview-note
```

`finalize-note` encapsulates `render-note -> validate-note` in the correct order. If you still call the lower-level commands manually, keep `render-note -> validate-note -> preview-note` strictly sequential and do not parallelize them.

Same-day regenerated notes should not overwrite earlier notes. Use a date-only title for the first note and pass `--version-suffix " (v2)"`, `--version-suffix " (v3)"`, etc. for later notes on the same date.

## MCP Tool Discovery

Codex App may lazy-load MCP tool schemas. Before running a Zotero note workflow, use `tool_search` to load the full Zotero tool set with a targeted tool search for `search_library`, `get_item_details`, `get_content`, `write_note`, and `annotations`. `annotations` tools are optional enhancements; the required core tools are `search_library`, `get_item_details`, `get_content`, and `write_note`. If `get_item_details` is not initially visible, treat that as a tool discovery issue, not as missing Zotero metadata.

## Local Commands

```bash
uv run zotero-paperread --help
uv run zotero-paperread create-run --title "<title>" --item-key "<item_key>"
uv run zotero-paperread prepare-item runs/<date>/<paper-slug>/item-details.json --workdir runs/<date>/<paper-slug> --max-pages 15
uv run zotero-paperread extract-pdf path/to/paper.pdf --output runs/<date>/<paper-slug>/extract.json
uv run zotero-paperread extract-figures path/to/paper.pdf --output-dir runs/<date>/<paper-slug>/figures --top-k 4 --max-pages 15
uv run zotero-paperread validate-summary-json runs/<date>/<paper-slug>/summary.json
uv run zotero-paperread render-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md
uv run zotero-paperread finalize-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md
uv run zotero-paperread finalize-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md --version-suffix " (v2)"
uv run zotero-paperread validate-note runs/<date>/<paper-slug>/note.md
uv run zotero-paperread preview-note runs/<date>/<paper-slug>/note.md
```

The recommended manual sequence is:

```bash
uv run zotero-paperread create-run --title "<title>" --item-key "<item_key>"
uv run zotero-paperread prepare-item runs/<date>/<paper-slug>/item-details.json --workdir runs/<date>/<paper-slug> --max-pages 15
uv run zotero-paperread validate-summary-json runs/<date>/<paper-slug>/summary.json
uv run zotero-paperread finalize-note runs/<date>/<paper-slug>/metadata.json runs/<date>/<paper-slug>/summary.json --output runs/<date>/<paper-slug>/note.md
uv run zotero-paperread preview-note runs/<date>/<paper-slug>/note.md
```

`create-run` prints a JSON payload containing `run_dir`, `manifest_path`, `slug`, and `date`. Use the returned `run_dir` instead of guessing the final slug when there may already be a same-day run for the same title.

`validate-summary-json` only verifies that the file is readable UTF-8 JSON with an object at the top level. It is not a semantic schema validator. `render-note` and `finalize-note` use the same friendly JSON error path, so malformed or missing JSON fails before any partial note is written.

## Trusted Notes

The workflow asks Codex to classify paper type, assign trust status, attach compact evidence pointers, and run a second-pass note quality review before Zotero write-through. If review finds fixable omissions, Codex may perform one bounded improvement pass by re-reading only the current run directory artifacts.

After generating `review.json`, merge the review gate fields into `summary.json` and validate write-readiness before finalizing the write-through note:

```bash
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
PAPER_TITLE="<paper title>"
GENERATED_DATE="<YYYY-MM-DD>"
VERSION_SUFFIX="$(uv run zotero-paperread next-version-suffix <run_dir>/item-details.json --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE")"
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --generated-date "$GENERATED_DATE" --version-suffix "$VERSION_SUFFIX" --output <run_dir>/note.md
```

For Zotero write-through, `validate-trusted-summary` must pass, `preview-note` must be shown, and the target Zotero item title must be shown before calling `zotero-mcp write_note`.

The rendered note includes `## 可信度与证据` with `paper_type`, `trust_status`, `review_status`, `improvement_status`, evidence pointers, review issues, and any improvement notes. This section is meant to make each note useful as a long-term knowledge-base entry without hiding extraction uncertainty.

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
- Current label behavior: the note ends with `## 本文标签`, containing `codex-summary`, `paper-summary`, and at most four inferred English key labels.

## Safety

- Dry-run is the default workflow.
- Phrases like `输出笔记`, `写入笔记`, `写回 Zotero`, `创建 note`, and `保存到 Zotero` count as explicit write intent. `输出笔记` is a project/user-specific convention for Zotero write-through and still requires note preview, target Zotero item title display, and `zotero-mcp write_note` as the only write path.
- If a Zotero item already has a Codex summary note, the skill stops by default and reports the existing note. It only continues when the user explicitly asks to continue or regenerate.
- Same-day regeneration creates a new title version such as `[Codex Summary] <paper title> - YYYY-MM-DD (v2)` instead of overwriting or reusing the first title.
- Tests never write to Zotero.
- Zotero writes happen only through `zotero-mcp write_note`.
- Better Notes is optional and not called directly.

## Reference

This project adapts the skill-based paper-analysis ideas from `evil-read-arxiv`, but replaces arXiv/Obsidian assumptions with a Zotero-first workflow.
