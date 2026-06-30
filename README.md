# Paperread

**English** | [简体中文](README.zh-CN.md)

Paperread is a self-contained skill bundle for Codex or Claude. The installable artifact is the repository's `skill/` directory only. Copy it to a destination folder named `paperread`, run commands from that installed skill root, and keep the repository root for maintenance documentation.

Do not put a `README.md` inside `skill/`; skills should expose `SKILL.md`, directly linked `references/`, bundled scripts, code, tests, templates, dependency metadata, and fixtures.

## Install

Install `uv` before copying the skill. Use the official installer or a package manager; common options are:

```bash
# Option A: standalone installer
curl -LsSf https://astral.sh/uv/install.sh | sh

# Option B: Homebrew
brew install uv

uv --version
```

See the official `uv` installation guide for Windows and other package managers: <https://docs.astral.sh/uv/getting-started/installation/>.

If `uv sync --locked` cannot find Python `>=3.13`, install a managed interpreter and retry:

```bash
uv python install 3.13
```

Codex personal skill:

```bash
install_dir="${CODEX_HOME:-$HOME/.codex}/skills/paperread"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/Paperread/skill "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paperread --help
```

Claude Code personal skill:

```bash
install_dir="$HOME/.claude/skills/paperread"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/Paperread/skill "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paperread --help
```

If the target `paperread/` directory already exists, stop before copying. Replacing an installed skill is a deliberate user-approved operation; blind `cp -R` can create an undiscoverable nested layout.

The first `uv sync --locked` initializes the installed skill's local environment from `skill/uv.lock`. Re-run it after updating the copied skill directory.

## Workflows

Paperread supports two inputs:

- **Zotero title or title fragment**: use Zotero MCP to locate the paper, prepare deterministic evidence artifacts, render `note.md` and `note.html`, and create a new Zotero child note only after explicit write intent.
- **Local PDF path**: run the same extraction, summary, review, lint, and render gates on a local PDF, then write local outputs beside the PDF without writing Zotero.

Both workflows use full-PDF extraction by default. Final `evidence_summary` locators must use one of these canonical forms: `context.md page <N>`, `context.md page <N> section <Section Name>`, `context.md page <N> section <Section Name> table_candidate <N>`, or `figure_context.md <figure_id>`. Bare `context.md` / `figure_context.md`, prose locators such as `page 3 method section`, `section_context.md`, and secondary context paths are invalid. `section_context.md` is only a navigation aid. Secondary web context captured through `scripts/capture-secondary-url.mjs` is cross-check material only and must not cite secondary context in `evidence_summary`.

## Output Locations

- Zotero title workflow writes local run artifacts under `<skill_root>/runs/YYYY-MM-DD/<title-slug>/`. The write-candidate step adds `note.md`, `note.html`, `gate-report.json`, and `write-payload.json` there before any Zotero write.
- Local PDF path workflow writes beside the PDF: `<pdf_stem>_analysis/` for analysis artifacts and `<pdf_stem>_note.md` for the final Markdown note. Existing outputs are preserved with `_v2`, `_v3`, and later suffixes.

## Runtime Requirements

- Install and run CLI: `uv` plus Python `>=3.13` available to `uv`; use `uv python install 3.13` if no compatible interpreter is present.
- Local PDF workflow: no Zotero requirement.
- Zotero title workflow: Zotero Desktop plus Zotero MCP tools or the local MCP endpoint.
- Secondary web context capture: Node.js and a reachable CDP helper when this optional path is used.

## Verification

From the installed or source `skill/` directory:

```bash
uv sync --locked
uv run pytest
uv run paperread --help
uv run paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/paperread-extract.json
uv run python scripts/validate-skill.py .
```

Maintainers should also validate a copied directory outside the repository before treating `skill/` as self-contained.

## Safety Boundaries

- Default to dry-run and preview before writing.
- Zotero writes are allowed only through Zotero MCP `write_note` and only after explicit user intent.
- Zotero local API and SQLite are read-only.
- Local PDF path workflow is local-output only; it must not write Zotero, call `refresh-live-notes`, or create `write-payload.json`.
- Rendered note prose should be Chinese-first while preserving paper titles, names, formulas, method names, units, evidence locators, and tag keys.
