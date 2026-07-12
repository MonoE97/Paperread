# PDF Path Workflow — Paper Reader 2.0 Runtime Contract

Use this when the user provides a local PDF path. This is the released grouped-CLI runtime contract for Paper Reader 2.0. Local PDF path and directory path inputs skip Zotero lookup and duplicate checks, including same-title or same-DOI checks. Existing local paths are not Zotero title fragments.

## Setup

Run from the skill root:

```bash
uv --version
uv sync --locked
uv run paper_reader --help
```

If `uv sync --locked` cannot find Python `>=3.13`, run `uv python install 3.13` from the skill root and retry. If `uv` is not installed, stop and ask the user to install `uv` first; do not use `pip`, `conda`, or system Python as a replacement.

## Output Location

`uv run paper_reader run init-local` resolves and fingerprints the PDF exactly once, then atomically reserves `<pdf_stem>_analysis/` beside it and fixes `<pdf_stem>_note.md` as the candidate publication target. Repeated runs preserve every existing V1/V2 directory and note by reserving `_v2`, `_v3`, and later suffixes. Initialization does not summarize or publish the note.

## Network Boundary

The local PDF path workflow does not require Zotero and does not inspect whether Zotero already contains a matching title, DOI, or attachment. Figure extraction may still try an arXiv source download when an arXiv ID is detected in metadata or the PDF filename. The download uses a bounded network timeout and degrades to PDF-only extraction if arXiv source is unavailable.

If the user provides an existing local directory path instead of one PDF, delegate to `$paper_reader_batch` and its local PDF folder workflow. Do not reinterpret the directory name as a Zotero title fragment.

## Steps

1. Confirm path-first routing and initialize the local run:

```bash
uv run paper_reader route "/path/to/paper.pdf"
uv run paper_reader run init-local "/path/to/paper.pdf"
```

The source contract binds resolved absolute path, size, SHA-256 and inode identity. Output must not alias the source through a relative path, symlink or hardlink. The first run reserves `<pdf_stem>_analysis/` and `<pdf_stem>_note.md`; later runs use `_v2`, `_v3` and so on without overwriting old notes or analysis directories.

2. Prepare immutable full-PDF evidence by default:

```bash
uv run paper_reader run prepare <run_dir>
uv run paper_reader run status <run_dir>
uv run paper_reader run validate <run_dir>
```

Evidence is published under immutable `evidence/<evidence_id>/` with canonical hashes. Operational stdout is exactly one `paper_reader.command-result.v2` JSON object; diagnostics belong on stderr.

3. Read `context.md`, `section_context.md`, and `figure_context.md` if available.

4. The agent creates `paper_reader.summary.v2` and `paper_reader.review.v2`. Use `section_context.md` only as navigation; it is not a canonical evidence source. Locators must resolve through `evidence.json` membership and use `context.md page <N>`, `context.md page <N> section <Section Name>`, `context.md page <N> section <Section Name> table_candidate <N>`, or `figure_context.md <figure_id>`. Bare names, prose locators, `section_context.md` and secondary context paths are blockers.

5. Validate and seal review:

```bash
uv run paper_reader review validate <run_dir>
uv run paper_reader review seal <run_dir>
```

Sealing emits immutable `paper_reader.review-package.v2`. Failed review, changed summary hash, invalid locators or Chinese-first lint failures after fallback resolution block sealing.

6. Build and preview the immutable candidate:

```bash
uv run paper_reader candidate build <run_dir>
```

The candidate binds source/evidence/review identities, all artifact hashes/sizes and the fixed final Markdown target. Changing any input or target requires rebuilding the candidate.

7. Publish locally:

```bash
uv run paper_reader local publish <candidate>
```

Publication re-hashes all inputs and source identity, then uses same-filesystem atomic no-replace. If the fixed target is occupied, stop and rebuild the candidate against a newly reserved target; never overwrite or silently choose a different path.

## Hard Boundaries

- The PDF path workflow must not write Zotero.
- The PDF path workflow must not search Zotero or run duplicate checks.
- The PDF path workflow must not refresh Zotero live notes.
- The PDF path workflow must not create a Zotero authorization or enter a batch write lane.
- The PDF path workflow must not treat `section_context.md` as a canonical evidence source.
- The PDF path workflow is local-output only. A later Zotero request is a new Zotero-title run; the local candidate cannot be converted or migrated.
- V1/unversioned/unknown artifacts are immutable historical-only inputs and must fail before locks or mutation with `unsupported_run_schema`.
