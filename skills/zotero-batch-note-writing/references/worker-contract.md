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

A successful worker should leave these files for coordinator gates:

- `item-details.json`
- `context.md`
- `figure_context.md`
- `summary.json`
- `review.json`
- `note.md`

If any required input is missing, the worker should stop with a short `blocked_reason` and put detailed diagnostics in `error_detail`.

## Secondary Material Boundary

WeChat, news, blog, press-release, and other webpage links are secondary cross-check material only. Workers may capture them for background or consistency checks, but `evidence_summary` must cite only primary paper artifacts such as `context.md` and `figure_context.md`.
