# PDF Path Workflow

Use this when the user provides a local PDF path.

## Setup

Run from the skill root:

```bash
uv --version
uv sync --locked
uv run paperread --help
```

If `uv sync --locked` cannot find Python `>=3.13`, run `uv python install 3.13` from the skill root and retry. If `uv` is not installed, stop and ask the user to install `uv` first; do not use `pip`, `conda`, or system Python as a replacement.

## Steps

1. Prepare local artifacts beside the PDF:

```bash
uv run paperread prepare-pdf "/path/to/paper.pdf"
```

The first run writes `<pdf_stem>_analysis/` and targets `<pdf_stem>_note.md`. Repeated runs use `_v2`, `_v3`, and so on without overwriting old notes or analysis directories.

2. Read the generated `context.md`, `section_context.md`, and `figure_context.md` if available.

3. Write `summary.json` and `review.json` in the analysis directory. Use `section_context.md` only as navigation. It is not a canonical evidence source. Evidence locators must cite `context.md` or `figure_context.md`.

4. Run the deterministic review chain:

```bash
uv run paperread validate-summary-json <analysis_dir>/summary.json
uv run paperread apply-review <analysis_dir>/summary.json <analysis_dir>/review.json
uv run paperread lint-summary <analysis_dir>/summary.json
uv run paperread validate-trusted-summary <analysis_dir>/summary.json
```

5. Prepare the local note:

```bash
uv run paperread prepare-local-note-candidate <analysis_dir> --generated-date YYYY-MM-DD
```

This writes `note.md`, `note.html`, previews, `note-tags.json`, `local-gate-report.json`, and the final Markdown note beside the PDF.

## Hard Boundaries

- The PDF path workflow must not write Zotero.
- The PDF path workflow must not call refresh-live-notes.
- The PDF path workflow must not create write-payload.json.
- The PDF path workflow must not treat `section_context.md` as a canonical evidence source.
- The PDF path workflow is local-output only; if the user later wants Zotero write-through, rerun through the Zotero title workflow.
