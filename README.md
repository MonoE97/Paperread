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
2. locate the attached PDF path;
3. create and reuse a local `runs/<date>/<paper-slug>/` bundle for that paper;
4. extract PDF text with a local `uv`-managed Python CLI;
5. extract figures and analyze key images when available;
6. generate a Chinese structured paper summary with figure-aware analysis;
7. preview and validate the note;
8. create a Zotero child note only when explicitly requested.

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
```

The skill then performs Zotero lookup, run-directory creation, bundle preparation, figure-aware summary generation, note validation, and creates a Zotero child note only when the user message contains explicit write intent.

Preferred note finalization command:

```text
finalize-note -> preview-note
```

`finalize-note` encapsulates `render-note -> validate-note` in the correct order. If you still call the lower-level commands manually, keep `render-note -> validate-note -> preview-note` strictly sequential and do not parallelize them.

## Local Commands

```bash
uv run zotero-paperread --help
uv run zotero-paperread create-run --title "<title>" --item-key "<item_key>"
uv run zotero-paperread prepare-item /tmp/item-details.json --workdir /tmp/zotero-paperread-run --max-pages 15
uv run zotero-paperread extract-pdf path/to/paper.pdf --output /tmp/extract.json
uv run zotero-paperread extract-figures path/to/paper.pdf --output-dir /tmp/figures --top-k 4 --max-pages 15
uv run zotero-paperread render-note /tmp/metadata.json /tmp/summary.json --output /tmp/note.md
uv run zotero-paperread finalize-note /tmp/metadata.json /tmp/summary.json --output /tmp/note.md
uv run zotero-paperread validate-note /tmp/note.md
uv run zotero-paperread preview-note /tmp/note.md
```

## V2: Key Figure Extraction and Analysis

- Primary path: resolve arXiv ID and extract source images or source-side figure PDFs when possible.
- Secondary path: detect figure captions and crop the rendered region above or below the caption for local-only PDFs.
- Supplement path: extract embedded PDF images only when source and deterministic paths are sparse.
- Fallback path: run OCR only when deterministic extraction is low-confidence and the project-local OCR adapter is available.
- Output: `figures/`, `figures.json`, `figure_context.md`.
- Goal: improve scientific reading quality, not just embed pictures.
- Current note behavior: figure analysis is written into the Zotero note even if inline image embedding is not enabled.

## Safety

- Dry-run is the default workflow.
- Phrases like `写入笔记`, `写回 Zotero`, `创建 note`, and `保存到 Zotero` count as explicit write intent.
- Tests never write to Zotero.
- Zotero writes happen only through `zotero-mcp write_note`.
- Better Notes is optional and not called directly.

## Reference

This project adapts the skill-based paper-analysis ideas from `evil-read-arxiv`, but replaces arXiv/Obsidian assumptions with a Zotero-first workflow.
