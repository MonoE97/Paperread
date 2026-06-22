# Worker Contract

Workers are local artifact producers. They receive one frozen manifest item or a small bounded batch and write only inside assigned run directories.

## Allowed Actions

- Read the assigned manifest item and normalized `item-details.json`.
- Run local `zotero-paperread` CLI steps needed to create paper artifacts.
- Produce or update files inside the assigned `run_dir`.
- Report concise status, `blocked_reason`, and paths back to the coordinator.

## Forbidden Actions

- Do not call `write_note`.
- Do not call Zotero collection mutation tools.
- Do not edit Zotero SQLite or Zotero storage metadata.
- Do not change Better Notes settings or templates.
- Do not rebuild the manifest candidate set.
- Do not edit project docs, scripts, tests, or unrelated run directories.

## Successful Output Paths

A successful worker should leave these inputs for coordinator gates:

- `item-details.json`
- `context.md`
- `section_context.md`
- `figure_context.md`
- `summary.json`
- `review.json`

The coordinator owns the write-candidate step. It refreshes live notes, computes
the version suffix, regenerates `note.md` and `note.html`, writes
`note-tags.json`, runs `gate-run`, and creates `<run_dir>/write-payload.json`.
Workers may render a dry-run `note.md` for review, but they must not treat it as
the final write candidate.

If any required input is missing, the worker should stop with a short `blocked_reason` and put detailed diagnostics in `error_detail`.

## Secondary Material Boundary

`section_context.md` is a navigation aid for sections and table/value candidates. Workers may use it to draft source-grounded summaries, but it is not a canonical evidence source. Final `evidence_summary` locators must cite `context.md` or `figure_context.md`, for example `context.md page 3 section Methods`, `context.md page 6 section Results table_candidate 1`, or `figure_context.md fig_p4_1`.

WeChat, news, blog, press-release, and other webpage links are secondary cross-check material only. Workers may capture them for background or consistency checks, but `evidence_summary` must cite only primary paper artifacts such as `context.md` and `figure_context.md`.
