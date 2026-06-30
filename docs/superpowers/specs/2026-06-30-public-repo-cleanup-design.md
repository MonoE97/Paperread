# Public Repo Cleanup Design

## Status

Approved for design by the user on 2026-06-30.

This document defines the public repository boundary for `Zotero_paperread`.
It is a design record, not permission to delete files. Deleting `skills/` or
`docs/` still requires an explicit confirmation immediately before the
implementation step, because project rules treat deletion as a red-line action.

## Goal

Prepare the repository for public release as a complete paper-reading tool
repository with one canonical skill bundle.

The public user should be able to clone the repo, install dependencies with
`uv`, run tests, and use one skill entry to analyze either:

- a Zotero paper title or title fragment; or
- a local PDF path.

The repository should not expose internal planning history, duplicate skill
entry points, local run artifacts, Zotero metadata, generated notes, or paper
PDF outputs.

## First-Principles Criteria

A public file or directory should stay only if it directly supports one of
these jobs:

1. install and run the tool;
2. understand the supported workflows;
3. enforce agent behavior and safety boundaries;
4. render or validate notes;
5. test the shipped behavior; or
6. preserve reproducibility of the Python environment.

Anything that mainly documents historical planning, internal iteration, old
entry points, or local execution state should not be part of the public surface.

## Public Repository Shape

Keep these top-level assets:

- `README.md`
- `AGENTS.md`
- `pyproject.toml`
- `uv.lock`
- `src/`
- `templates/`
- `tests/`
- `skills_paperread/`

Remove these public-facing directories after their necessary content is
migrated:

- `skills/`
- `docs/`

`skills_paperread/` is the only public workflow bundle. It is repo-local for
public v1: users clone the repository, run `uv sync`, and use the skill from the
repo root. It is not presented as a standalone global installation directory.

## Distribution Model

Public v1 is a clone-and-run repository with one bundled skill, not a pure
skills-only package.

The README may explain how a user or agent can point Codex or Claude at
`skills_paperread/`, but it must not imply that copying the skill directory
alone is sufficient. The skill depends on this repository's Python package,
templates, lockfile, and CLI. Any installation guidance must preserve that
dependency by telling users to clone the repo, run `uv sync`, and execute
commands from the repo root.

The implementation should not create `.agents/skills/paperread` or
`.claude/skills/paperread` directories in this repository. Tool-specific global
installation layouts can be documented later if they become a separate release
target.

## Skill Consolidation

The old `skills/` directory currently contains two legacy skill entry points:

- `skills/zotero-paper-summary/`
- `skills/zotero-batch-note-writing/`

Public v1 should not keep both `skills/` and `skills_paperread/`, because two
skill directories create an ambiguous canonical entry for users and agents.

The implementation should migrate only the single-paper material needed for the
public v1 workflows:

- move `skills/zotero-paper-summary/scripts/capture-secondary-url.mjs` to
  `skills_paperread/scripts/capture-secondary-url.mjs`;
- merge the active Zotero title workflow rules into
  `skills_paperread/references/zotero-workflow.md`;
- keep the local PDF path workflow in
  `skills_paperread/references/pdf-path-workflow.md`;
- keep shared summary fields and authoring expectations in
  `skills_paperread/references/summary-schema.md`.

The old batch skill should not be part of public v1. Batch operation increases
the support surface with manifests, worker contracts, failure modes, and extra
scripts. It is a separate future capability, not required for the stated public
release goal of one paperread skill covering Zotero title input and PDF path
input.

## Documentation Boundary

`README.md` should be rewritten as the external user entry point:

- what the tool does;
- what outputs are generated;
- setup with `uv sync`;
- how to use `skills_paperread/`;
- Zotero title workflow;
- PDF path workflow;
- privacy and ignored outputs;
- verification commands.

`AGENTS.md` should remain the agent rulebook:

- project goal;
- public directory conventions;
- Chinese-first note rule;
- Zotero write boundary;
- PDF path local-output-only boundary;
- validation commands;
- no direct Zotero SQLite writes;
- no Better Notes dependency.

`docs/` should not be public v1 surface. Historical plans and specs are useful
for internal audit, but they are not operational guidance for external users.
Any still-useful operational guidance should be compressed into `README.md` or
`skills_paperread/references/` before `docs/` is removed.

## `.gitignore` Boundary

The public `.gitignore` should explain and enforce three classes of private or
local-only files.

Development environment and caches:

- `.DS_Store`
- `__pycache__/`
- `*.py[cod]`
- `*.egg-info/`
- `.pytest_cache/`
- `.ruff_cache/`
- `.mypy_cache/`
- `.coverage`
- `htmlcov/`
- `.venv/`
- `dist/`
- `build/`

Agent and session state:

- `.worktrees/`
- `.superpowers/`
- `*.log`
- `tmp/`
- `.tmp/`

Paper, Zotero, and note outputs:

- `runs/`
- `papers/`
- `*_analysis/`
- `*_analysis_v[0-9]*/`
- `*_note.md`
- `*_note_v[0-9]*.md`
- `*.pdf.txt`
- `*.extract.json`
- `*.summary.json`
- `*.note.md`

The purpose is not just cleanliness. The main public risk is accidentally
committing copyrighted PDFs, extracted paper text, Zotero metadata, generated
notes, review reports, or local run artifacts.

## Test Strategy

Tests should describe the new public boundary, not preserve the legacy one.

Required updates:

- redirect documentation consistency tests from `skills/` to
  `skills_paperread/`;
- update secondary-source script tests to use
  `skills_paperread/scripts/capture-secondary-url.mjs`;
- remove or rewrite tests that exist only to validate the old batch skill;
- add or keep checks that README and AGENTS do not point to
  `skills/zotero-*` as the current public entry;
- keep checks that the PDF path workflow forbids Zotero writes,
  `refresh-live-notes`, and `write-payload.json`.

Required verification after implementation:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
uv run --with pyyaml python /Users/jwxi/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/jwxi/Desktop/AIflow/Zotero_paperread/skills_paperread
git diff --check
```

## Risks And Controls

Risk: deleting `skills/` breaks script references.
Control: migrate `capture-secondary-url.mjs`, update all references, and run
the existing script tests against the new path.

Risk: deleting `docs/` loses live operational guidance.
Control: review references before deletion and move only current, public-useful
instructions into README or `skills_paperread/references/`.

Risk: public v1 accidentally promises batch support.
Control: remove batch workflow from the public entry surface and avoid README
claims about collection-scale operation.

Risk: tests become weaker during cleanup.
Control: replace old-directory assertions with canonical-entry assertions and
privacy/output-boundary assertions.

Risk: `.gitignore` misses local PDF path outputs.
Control: add explicit ignore patterns for `<pdf_stem>_analysis/`,
`<pdf_stem>_analysis_vN/`, `<pdf_stem>_note.md`, and
`<pdf_stem>_note_vN.md`.

Risk: public installation guidance overclaims standalone skill portability.
Control: describe public v1 as a repo-local skill bundle that depends on the
cloned repository and `uv` environment.

## Non-Goals

- No git push.
- No public release, package publishing, or deployment.
- No global dependency installation.
- No rewrite of the core paper extraction or note rendering behavior.
- No batch workflow support in public v1.
- No standalone global Codex or Claude skill packaging in public v1.
- No deletion until the implementation step receives explicit deletion
  confirmation.

## Implementation Readiness

This scope is one implementation plan. The work is a repository publication
cleanup with clear boundaries:

1. migrate necessary skill content;
2. rewrite public README/AGENTS guidance;
3. tighten `.gitignore`;
4. update tests to the new public boundary;
5. request explicit deletion confirmation;
6. remove old public directories;
7. run the full verification set.
