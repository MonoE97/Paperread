# Paperread

**English** | [简体中文](README.zh-CN.md)

Paperread is a self-contained skill bundle for Codex or Claude. The installable artifact is the repository's `skill/` directory only. Copy it to a destination folder named `paperread`, run commands from that installed skill root, and keep the repository root for maintenance documentation.

Do not put a `README.md` inside `skill/`; skills should expose `SKILL.md`, directly linked `references/`, bundled scripts, code, tests, templates, dependency metadata, and fixtures.

## Install

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

## Workflows

Paperread supports two inputs:

- **Zotero title or title fragment**: use Zotero MCP to locate the paper, prepare deterministic evidence artifacts, render `note.md` and `note.html`, and create a new Zotero child note only after explicit write intent.
- **Local PDF path**: run the same extraction, summary, review, lint, and render gates on a local PDF, then write local outputs beside the PDF without writing Zotero.

Both workflows use full-PDF extraction by default. Final `evidence_summary` locators must cite `context.md` or `figure_context.md`; `section_context.md` is only a navigation aid. Secondary web context captured through `scripts/capture-secondary-url.mjs` is cross-check material only and must not cite secondary context in `evidence_summary`.

## Runtime Requirements

- Install and run CLI: `uv` plus Python `>=3.13` available to `uv`.
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
