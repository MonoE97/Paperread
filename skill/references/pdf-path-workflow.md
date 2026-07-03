# PDF Path Workflow

Use this when the user provides a local PDF path. Local PDF path and directory path inputs skip Zotero lookup and duplicate checks, including same-title or same-DOI checks. Existing local paths are not Zotero title fragments.

## Setup

Run from the skill root:

```bash
uv --version
uv sync --locked
uv run paperread --help
```

If `uv sync --locked` cannot find Python `>=3.13`, run `uv python install 3.13` from the skill root and retry. If `uv` is not installed, stop and ask the user to install `uv` first; do not use `pip`, `conda`, or system Python as a replacement.

## Output Location

`prepare-pdf` writes beside the PDF. The first run creates `<pdf_stem>_analysis/` for analysis artifacts and targets `<pdf_stem>_note.md` as the final Markdown note. Repeated runs preserve existing outputs with `_v2`, `_v3`, and later suffixes.

## Network Boundary

The local PDF path workflow does not require Zotero and does not inspect whether Zotero already contains a matching title, DOI, or attachment. Figure extraction may still try an arXiv source download when an arXiv ID is detected in metadata or the PDF filename. The download uses a bounded network timeout and degrades to PDF-only extraction if arXiv source is unavailable.

If the user provides an existing local directory path instead of one PDF, delegate to `$paperread-batch` and its local PDF folder workflow. Do not reinterpret the directory name as a Zotero title fragment.

## Steps

1. Prepare local artifacts beside the PDF:

```bash
uv run paperread prepare-pdf "/path/to/paper.pdf"
```

The first run writes `<pdf_stem>_analysis/` and targets `<pdf_stem>_note.md`. Repeated runs use `_v2`, `_v3`, and so on without overwriting old notes or analysis directories.

2. Read the generated `context.md`, `section_context.md`, and `figure_context.md` if available.

3. Write `summary.json` and `review.json` in the analysis directory. Use `section_context.md` only as navigation. It is not a canonical evidence source. Evidence locators must use canonical forms: `context.md page <N>`, `context.md page <N> section <Section Name>`, `context.md page <N> section <Section Name> table_candidate <N>`, or `figure_context.md <figure_id>`. Bare `context.md` / `figure_context.md`, prose locators such as `page 3 method section`, `section_context.md`, and secondary context paths are invalid.

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
- The PDF path workflow must not search Zotero or run duplicate checks.
- The PDF path workflow must not call refresh-live-notes.
- The PDF path workflow must not create write-payload.json.
- The PDF path workflow must not treat `section_context.md` as a canonical evidence source.
- The PDF path workflow is local-output only; if the user later wants Zotero write-through, rerun through the Zotero title workflow.
