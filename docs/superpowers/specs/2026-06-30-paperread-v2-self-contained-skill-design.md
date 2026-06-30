# Paperread V2 Self-Contained Skill Design

Date: 2026-06-30
Status: design for user review

## Goal

Paperread V2 turns this repository into a skill repository whose only required runtime artifact is `skill/`. A user should be able to copy the repository's `skill/` directory into a Codex or Claude skills location, rename the copied directory to `paperread`, run `uv sync` inside that directory, and use the same Zotero-title and local-PDF workflows without needing any files from the repository root.

This changes the repository from a repo-local Python project with a thin skill wrapper into a publishable skill source. The repository root remains useful for maintainers and readers, but all code, templates, references, tests, fixtures, scripts, dependency metadata, and lockfile required to run Paperread live under `skill/`.

## Confirmed Decisions

- Use the **fully self-contained** model: copying `skill/` is sufficient.
- Keep `uv` as the only supported Python environment manager.
- Keep both public workflows: Zotero title/title-fragment workflow and local PDF path workflow.
- Keep the repository root as a publishing shell, not a runtime dependency.
- Keep the repository source directory named `skill/`; when installing, copy it to a destination named `paperread` so the installed folder matches the skill name.
- Keep the skill name `paperread`.

## External Compatibility Constraints

Codex skill guidance from `skill-creator` requires:

- `SKILL.md` with YAML frontmatter containing at least `name` and `description`.
- A concise `SKILL.md` body, with detailed workflow material split into directly linked `references/`.
- Optional bundled `scripts/`, `references/`, and `assets/` resources.
- Avoiding nonessential in-skill auxiliary docs such as `README.md`, `INSTALLATION_GUIDE.md`, or changelogs.
- Basic validation with `quick_validate.py <path-to-skill-folder>`.

Claude documentation currently describes skills as folders containing `SKILL.md`, with optional bundled resources such as scripts and references. Claude Code personal skills are discovered under `~/.claude/skills/<skill-name>/SKILL.md`, and the skill folder name is user-facing in Claude Code. Therefore the repo source directory can remain `skill/`, but the copied install directory should be named `paperread` for both Codex and Claude.

References:

- [Claude Code Skills](https://docs.anthropic.com/en/docs/claude-code/skills)
- [Claude Agent Skills Overview](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)

## Target Repository Shape

Repository root:

```text
Paperread/
  AGENTS.md
  README.md
  README.zh-CN.md
  .gitignore
  docs/superpowers/specs/
  skill/
```

The root is not a Python project after V2. It should not contain runtime `pyproject.toml`, `uv.lock`, `src/`, `templates/`, or `tests/`.

Self-contained skill source:

```text
skill/
  SKILL.md
  agents/
    openai.yaml
  pyproject.toml
  uv.lock
  src/
    paperread/
      __init__.py
      arxiv_source.py
      cli.py
      figures.py
      gate.py
      local_candidate.py
      local_gate.py
      note.py
      note_hash.py
      note_table_migration.py
      pdf_extract.py
      pdf_workflow.py
      review.py
      runs.py
      secondary_sources.py
      summary_lint.py
      workflow.py
      write_candidate.py
      write_payload.py
      zotero_details.py
      zotero_item_io.py
      zotero_live.py
      zotero_sqlite.py
  templates/
    zotero_note.md.j2
  references/
    pdf-path-workflow.md
    summary-schema.md
    zotero-workflow.md
  scripts/
    capture-secondary-url.mjs
  tests/
    fixtures/
      minimal.pdf
    test_*.py
```

No `README.md` is placed inside `skill/`. Root README files explain installation and repository purpose. `SKILL.md` and `references/` explain agent execution.

## Installation Model

Codex personal install:

```bash
cp -R /path/to/Paperread/skill "${CODEX_HOME:-$HOME/.codex}/skills/paperread"
cd "${CODEX_HOME:-$HOME/.codex}/skills/paperread"
uv sync
uv run paperread --help
```

Claude Code personal install:

```bash
cp -R /path/to/Paperread/skill "$HOME/.claude/skills/paperread"
cd "$HOME/.claude/skills/paperread"
uv sync
uv run paperread --help
```

The source folder is `skill/`; the installed folder is `paperread/`. The installed folder name and `SKILL.md` frontmatter name both use `paperread`.

## Skill Anatomy

`skill/SKILL.md` should stay lean and route work:

- Frontmatter:
  - `name: paperread`
  - `description`: include both supported triggers, namely Zotero title/title-fragment paper analysis and local PDF path paper analysis, plus Chinese structured note output and write gates.
- Body:
  - State that commands run from the skill root, not the repository root.
  - Route local existing `.pdf` paths to `references/pdf-path-workflow.md`.
  - Route all other paper-title inputs to `references/zotero-workflow.md`.
  - State full-PDF extraction default.
  - State evidence-locator boundary: final locators cite `context.md` or `figure_context.md`, never `section_context.md` or secondary context.
  - State Chinese-first rendered prose rule.
  - State write safety boundary: Zotero writes only through MCP `write_note` after explicit user intent.

`skill/agents/openai.yaml` should be generated or updated to match `SKILL.md`:

```yaml
interface:
  display_name: "Paperread"
  short_description: "Evidence-grounded paper reading notes"
  default_prompt: "Use $paperread to analyze this paper and prepare a Chinese structured reading note."
policy:
  allow_implicit_invocation: true
```

Do not add icon paths or brand color unless assets are intentionally added.

## Python Project Layout

Move `pyproject.toml` into `skill/` and make it relative to the skill root.

Key package settings:

- `name = "paperread"`
- `requires-python = ">=3.13"`
- existing dependencies stay unchanged unless tests expose a real need.
- `[project.scripts] paperread = "paperread.cli:app"`
- remove or adjust `readme = "README.md"` because `skill/README.md` should not exist.
- `[tool.pytest.ini_options] testpaths = ["tests"]` and `pythonpath = ["src"]`.

`uv.lock` moves into `skill/` and should be refreshed from `skill/` only if `uv sync --locked` or tests show the lockfile no longer matches the moved project metadata.

The existing template lookup in `src/paperread/note.py` should continue to work if `templates/` moves under `skill/`, because `Path(__file__).resolve().parents[2]` resolves to the skill root after `src/paperread/` moves under `skill/src/paperread/`.

The existing run directory resolver in `src/paperread/cli.py` should also resolve default `runs/` relative to the skill root after the move. This is acceptable for Zotero-title runs. Local PDF path workflows continue writing final local outputs beside the source PDF by design.

## Workflow References

Keep the three current reference files, but update command paths from repo-root assumptions to skill-root assumptions:

- `references/zotero-workflow.md`
- `references/pdf-path-workflow.md`
- `references/summary-schema.md`

Detailed behavior remains unchanged:

- Zotero path: exact search, duplicate normalized-title stop, raw MCP response landing, normalized `item-details.json`, full `prepare-item`, optional secondary capture, agent-authored `summary.json` and `review.json`, deterministic review chain, `prepare-write-candidate`, preview, explicit MCP `write_note`, read-only verification.
- Local PDF path: `prepare-pdf`, agent-authored `summary.json` and `review.json`, deterministic review chain, `prepare-local-note-candidate`, local-output only.
- Secondary context: cross-check only; never cite in `evidence_summary`.
- `section_context.md`: navigation aid only; never canonical evidence.

## Root Documentation

Root `README.md` and `README.zh-CN.md` should shift from clone-and-run V1 wording to skill-install V2 wording:

- Explain that `skill/` is the only installable artifact.
- Show Codex and Claude copy commands that install to a folder named `paperread`.
- Tell users to run `uv sync` and `uv run paperread --help` from the installed skill directory.
- Keep safety boundaries synchronized in English and Chinese.
- Keep public claims minimal: this is a local skill bundle, not a published package or hosted service.

Root `AGENTS.md` should shift from repo-local Python project conventions to maintainer conventions:

- State that runtime code, tests, templates, scripts, lockfile, and references live under `skill/`.
- State that root changes are documentation or repository hygiene only.
- Keep existing red lines: no Zotero non-MCP writes, no direct SQLite writes, no pushes without confirmation, no global dependency installs.
- Update verification commands to run from `skill/`.

## Tests And Validation

The V2 verification set should run from `skill/`:

```bash
cd skill
uv sync
uv run pytest
uv run paperread --help
uv run paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/paperread-extract.json
uv run --with pyyaml python /Users/jwxi/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
```

Because root README files are not present when `skill/` is copied elsewhere, tests inside `skill/tests/` should not require root README files. Current doc-contract tests should be split or rewritten so self-contained skill validation checks only files inside `skill/`.

Minimum test updates:

- Update test path assumptions from repository root to skill root.
- Keep CLI and behavior tests under `skill/tests/`.
- Keep `tests/fixtures/minimal.pdf` under `skill/tests/fixtures/`.
- Update secondary URL script path checks to `scripts/capture-secondary-url.mjs` when running from the skill root.
- Update public doc tests to validate `SKILL.md`, `references/`, `pyproject.toml`, and optional `agents/openai.yaml`; root README synchronization can be checked by a small maintainer-only root check only if it does not become a skill runtime dependency.

## Migration Plan

1. Create a working branch before implementation.
2. Move source files into `skill/`:
   - `src/` -> `skill/src/`
   - `templates/` -> `skill/templates/`
   - `tests/` -> `skill/tests/`
   - `pyproject.toml` -> `skill/pyproject.toml`
   - `uv.lock` -> `skill/uv.lock`
3. Update `skill/pyproject.toml` so it no longer references a missing `README.md`.
4. Update `skill/SKILL.md` to describe the self-contained V2 model and skill-root commands.
5. Add or regenerate `skill/agents/openai.yaml`.
6. Update `skill/references/*.md` to remove repo-root assumptions and use skill-root paths.
7. Update tests for the new skill-root layout.
8. Update root `README.md`, `README.zh-CN.md`, `AGENTS.md`, and `.gitignore`.
9. Run the V2 verification set from `skill/`.
10. Run root-level sanity checks:
    - `git status --short --branch --untracked-files=all`
    - `git diff --check`
    - confirm no required runtime files remain at root.
11. Review the final diff for accidental deletion of safety rules, Zotero boundaries, or evidence-locator constraints.
12. Commit locally only after verification passes and the implementation diff is reviewed.

## Forward-Testing Plan

After implementation passes deterministic checks, forward-test with clean contexts if available:

1. Codex-style local PDF prompt:
   - Install/copy the built `skill/` to a temporary folder named `paperread`.
   - Ask a fresh agent to use `$paperread` on `tests/fixtures/minimal.pdf`.
   - Require dry-run local output only.
2. Zotero-title dry-run prompt:
   - Ask a fresh agent to use `$paperread` to prepare, but not write, a Zotero-title summary.
   - If live Zotero or MCP is unavailable, stop at tool-discovery diagnosis rather than faking success.
3. Claude-compatible filesystem check:
   - Copy `skill/` to a temporary `paperread/` folder shaped like `~/.claude/skills/paperread`.
   - Run `uv sync`, `uv run paperread --help`, and `uv run --with pyyaml python <quick_validate.py> .` from that copied directory.

Do not run forward-tests that write Zotero unless the user explicitly approves live writes.

## Risks And Mitigations

- Risk: tests accidentally depend on root docs after `skill/` is copied.
  - Mitigation: make `skill/tests/` self-contained and validate copied-directory execution.
- Risk: `SKILL.md` becomes too long because it tries to replace README.
  - Mitigation: keep routing and safety rules in `SKILL.md`; keep details in directly linked `references/`.
- Risk: package metadata points to a missing `README.md`.
  - Mitigation: remove the `readme` field or point it to an existing skill-root file.
- Risk: skill validation requires PyYAML that is not installed in system Python.
  - Mitigation: run the validator through `uv run --with pyyaml ...`, not through global Python and not through global dependency installation.
- Risk: Codex and Claude install paths differ.
  - Mitigation: repository source remains `skill/`, install target is always named `paperread`; root README gives separate copy commands.
- Risk: path assumptions break templates or run directories.
  - Mitigation: verify `note.py` and `cli.py` path helpers after moving, then run note-rendering and CLI tests.
- Risk: Zotero write safety regresses during documentation edits.
  - Mitigation: preserve MCP-only `write_note` boundary in `SKILL.md`, Zotero reference, AGENTS, tests, and gate commands.

## Non-Goals

- Do not publish to PyPI, npm, a marketplace, or a hosted service in this migration.
- Do not add global dependency installation.
- Do not change the note schema or note rendering layout except where path moves require it.
- Do not change Zotero write behavior.
- Do not remove the local PDF workflow.
- Do not add a second runtime package outside `skill/`.
- Do not create a README inside `skill/`.

## Self-Review

Marker scan: no unresolved markers or implementation choices remain in this design.

Consistency check: the design keeps the repo source directory named `skill/` while requiring installed copies to be named `paperread`; this resolves the tension between the user's repository constraint and skill folder naming conventions.

Scope check: this is one implementation plan. It is a structural migration plus documentation and test updates, not a feature rewrite.

Ambiguity check: root files are explicitly non-runtime; `skill/` contains all runtime and validation assets. Zotero writes remain explicit-intent only and MCP-only.
