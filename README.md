# Zotero Paperread

Zotero-first literature summary workflow for Codex.

## What It Does

Given a Zotero paper title, Codex can:

1. find the Zotero item through `zotero-mcp`;
2. locate the attached PDF path;
3. prepare a local summarization bundle from raw Zotero item details;
4. extract PDF text with a local `uv`-managed Python CLI;
5. generate a Chinese structured paper summary;
6. preview and validate the note;
7. create a Zotero child note only when explicitly requested.

## Codex Workflow

The intended top-level entry is the repo-local Codex skill:

- `skills/zotero-paper-summary/SKILL.md`

In Codex, the user should be able to say:

```text
用 zotero-paper-summary 总结我 Zotero 里标题为 "Crystal Structure Prediction Meets Artificial Intelligence" 的文献，并先预览笔记。
```

The skill then performs Zotero lookup, bundle preparation, summary generation, note validation, and optional Zotero note creation.

## Local Commands

```bash
uv run zotero-paperread --help
uv run zotero-paperread prepare-item /tmp/item-details.json --workdir /tmp/zotero-paperread-run --max-pages 15
uv run zotero-paperread extract-pdf path/to/paper.pdf --output /tmp/extract.json
uv run zotero-paperread render-note /tmp/metadata.json /tmp/summary.json --output /tmp/note.md
uv run zotero-paperread validate-note /tmp/note.md
uv run zotero-paperread preview-note /tmp/note.md
```

## Safety

- Dry-run is the default workflow.
- Tests never write to Zotero.
- Zotero writes happen only through `zotero-mcp write_note`.
- Better Notes is optional and not called directly.

## Reference

This project adapts the skill-based paper-analysis ideas from `evil-read-arxiv`, but replaces arXiv/Obsidian assumptions with a Zotero-first workflow.
