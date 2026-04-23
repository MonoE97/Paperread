# Zotero Paperread

Zotero-first literature summary workflow for Codex.

## What It Does

Given a Zotero paper title, Codex can:

1. find the Zotero item through `zotero-mcp`;
2. locate the attached PDF path;
3. extract PDF text with a local `uv`-managed Python CLI;
4. generate a Chinese structured paper summary;
5. preview and validate the note;
6. create a Zotero child note only when explicitly requested.

## Local Commands

```bash
uv run zotero-paperread --help
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
