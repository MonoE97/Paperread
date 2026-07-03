# Paperread

**English** | [简体中文](README.zh-CN.md)

Paperread is a self-contained skill repository for Codex or Claude. It contains two installable skill sources:

- `skill/` installs as `paperread` for single-paper deep reading.
- `batch_skill/` installs as `paperread-batch` for batch scheduling and lightweight reporting.

Copy each source directory to its destination skill folder, run commands from that installed skill root, and keep the repository root for maintenance documentation.

Do not put a `README.md` inside `skill/` or `batch_skill/`; skills should expose `SKILL.md`, directly linked `references/`, bundled scripts, code, tests, templates, dependency metadata, and fixtures.

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

Codex personal single-paper skill:

```bash
install_dir="${CODEX_HOME:-$HOME/.codex}/skills/paperread"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/Paperread/skill "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paperread --help
```

Codex personal batch skill:

```bash
install_dir="${CODEX_HOME:-$HOME/.codex}/skills/paperread-batch"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/Paperread/batch_skill "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paperread-batch --help
```

Claude Code personal single-paper skill:

```bash
install_dir="$HOME/.claude/skills/paperread"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/Paperread/skill "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paperread --help
```

Claude Code personal batch skill:

```bash
install_dir="$HOME/.claude/skills/paperread-batch"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/Paperread/batch_skill "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paperread-batch --help
```

If the target `paperread/` or `paperread-batch/` directory already exists, stop before copying. Replacing an installed skill is a deliberate user-approved operation; blind `cp -R` can create an undiscoverable nested layout.

The first `uv sync --locked` initializes each installed skill's local environment from its own lockfile. Re-run it after updating a copied skill directory.

## Zotero MCP Setup

Zotero-backed workflows require Zotero Desktop plus the Zotero MCP plugin before an agent can search your library or call `write_note`. Install and enable Zotero MCP from [cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp#readme):

1. Download the latest `zotero-mcp-plugin-*.xpi` from the repository releases.
2. In Zotero, install the `.xpi` with `Tools -> Add-ons`, then restart Zotero.
3. Open `Preferences -> Zotero MCP Plugin`, enable the integrated server, and generate the client configuration.
4. Use the generated Streamable HTTP MCP configuration, or configure the local endpoint directly as `http://127.0.0.1:23120/mcp`.

The plugin includes the MCP server; no separate Zotero MCP server process is required. Paperread treats Zotero local API and SQLite as read-only and writes notes only through Zotero MCP `write_note`.

## Skill Usage

### Use `paperread`

Use `paperread` for one paper at a time.

- Zotero title or title fragment: ask the agent to use `$paperread` with the paper title. The agent searches Zotero through Zotero MCP, prepares evidence artifacts, renders `note.md` and `note.html`, previews the candidate, writes only through MCP `write_note` after explicit write intent, and verifies the created note.
- Local PDF path: give an absolute or relative `.pdf` path. The skill writes `<pdf_stem>_analysis/` and `<pdf_stem>_note.md` beside the PDF, never searches Zotero for matching items, and never writes Zotero.
- Local directory path: use `paperread-batch` with the local PDF folder workflow. Existing local paths are not Zotero title fragments.

Useful installed-skill commands:

```bash
uv run paperread --help
uv run paperread prepare-pdf "/abs/path/to/paper.pdf"
```

### Use `paperread-batch`

Use `paperread-batch` when the request contains multiple papers. Install both `paperread` and `paperread-batch`; the batch skill schedules work, while `$paperread` still owns each single-paper read.

Typical batch CLI shape:

```bash
uv run paperread-batch manifest from-zotero-titles titles.txt --batch-title "my batch" --output manifest.json
uv run paperread-batch init --manifest manifest.json
uv run paperread-batch next <batch_run_dir> --limit 3
uv run paperread-batch next-write <batch_run_dir> --limit 1
uv run paperread-batch record-write <batch_run_dir> <item_id> --result write-result.json
uv run paperread-batch report <batch_run_dir>
```

For Zotero-backed items, the default write policy is `zotero_write`. Pass `--write-policy prepare_only` to manifest builders for dry-run. PDF batch items remain local-output only and skip Zotero lookup or duplicate checks.

## Workflows

Paperread supports two inputs:

- **Zotero title or title fragment**: use Zotero MCP to locate the paper, prepare deterministic evidence artifacts, render `note.md` and `note.html`, and create a new Zotero child note only after explicit write intent.
- **Local PDF path**: run the same extraction, summary, review, lint, and render gates on a local PDF, then write local outputs beside the PDF without writing Zotero or checking whether Zotero already has the same paper.

Local PDF path and directory path inputs skip Zotero lookup and duplicate checks. Existing local paths are not Zotero title fragments; directory paths belong to `paperread-batch manifest from-pdf-folder`, which is non-recursive unless `--recursive` is explicit.

Both workflows use full-PDF extraction by default. Final `evidence_summary` locators must use one of these canonical forms: `context.md page <N>`, `context.md page <N> section <Section Name>`, `context.md page <N> section <Section Name> table_candidate <N>`, or `figure_context.md <figure_id>`. Bare `context.md` / `figure_context.md`, prose locators such as `page 3 method section`, `section_context.md`, and secondary context paths are invalid. `section_context.md` is only a navigation aid. Secondary web context captured through `scripts/capture-secondary-url.mjs` is cross-check material only and must not cite secondary context in `evidence_summary`.

Paperread Batch supports four batch inputs: Zotero collection inventories, multiple Zotero titles, local PDF folders, and multiple PDF paths. It normalizes them into a manifest, dispatches each item to `$paperread`, and uses `zotero_write` by default for Zotero-backed items: prepared candidates are listed with `next-write`, written by the outer agent through Zotero MCP, verified read-only, then recorded with `record-write`. PDF folder/path items are `pdf_path` items with `expected_output=local_note`; they do not run Zotero lookup, duplicate checks, `next-write`, or write-through. Pass `--write-policy prepare_only` for dry-run. Default Codex concurrency is 3. The per-paper 30-second result is extracted from each single-paper note's `30 秒结论` row; batch does not summarize papers again. Claude-compatible fallback is sequential.

## Output Locations

- Zotero title workflow writes local run artifacts under `<skill_root>/runs/YYYY-MM-DD/<title-slug>/`. The write-candidate step adds `note.md`, `note.html`, `gate-report.json`, and `write-payload.json` there before any Zotero write.
- Local PDF path workflow writes beside the PDF: `<pdf_stem>_analysis/` for analysis artifacts and `<pdf_stem>_note.md` for the final Markdown note. Existing outputs are preserved with `_v2`, `_v3`, and later suffixes.
- Batch workflow writes batch run artifacts under `<paperread-batch_skill_root>/runs/YYYY-MM-DD/<batch-slug>/`, including `manifest.json`, `state.json`, `items/*.json`, `items/*.write.json`, `batch-report.json`, and `batch-report.md`. Single-paper artifacts remain owned by `paperread`; batch stores indexes, local-only paths, Zotero note keys, and verify report paths.

## Runtime Requirements

- Install and run CLI: `uv` plus Python `>=3.13` available to `uv`; use `uv python install 3.13` if no compatible interpreter is present.
- Local PDF workflow: no Zotero requirement and no Zotero duplicate check. Figure extraction may try an arXiv source download when an arXiv ID is detected in metadata or the PDF filename; this request uses a bounded network timeout and falls back to PDF-only extraction on failure.
- Zotero title workflow: Zotero Desktop plus Zotero MCP tools or the local MCP endpoint.
- Secondary web context capture: Node.js and a reachable CDP helper when this optional path is used.
- Batch workflow: installed `paperread` plus installed `paperread-batch`; batch validation checks the configured `paperread` root before dispatch.

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

From the installed or source `batch_skill/` directory:

```bash
uv sync --locked
uv run pytest
uv run paperread-batch --help
uv run python scripts/validate-skill.py .
```

Maintainers should also validate a copied directory outside the repository before treating `batch_skill/` as self-contained.

## Safety Boundaries

- Preview and pass the single-paper write gate before every Zotero write; use `--write-policy prepare_only` for dry-run batch runs.
- Zotero writes are allowed only through Zotero MCP `write_note` and only after explicit user intent.
- Zotero local API and SQLite are read-only.
- Local PDF path workflow is local-output only; it must not search Zotero, run duplicate checks, write Zotero, call `refresh-live-notes`, or create `write-payload.json`.
- Zotero-backed batch workflow default is `zotero_write`: the batch CLI emits pending writes and records verification, while the outer agent calls Zotero MCP `write_note`. PDF batch items remain local-only. Pass `--write-policy prepare_only` for dry-run.
- Rendered note prose should be Chinese-first while preserving paper titles, names, formulas, method names, units, evidence locators, and tag keys.
