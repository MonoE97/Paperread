# Paper Reader 2.0

**English** | [简体中文](README.zh-CN.md)

Paper Reader `2.0.0` is a breaking, self-contained skill repository for Codex or Claude. It turns a Zotero paper title, a local PDF path, or a batch of papers into evidence-grounded Chinese reading notes through deterministic grouped CLI tooling plus agent-written summaries.

The repository root is only a maintenance shell; it is not the runtime Python project. Install and run one or both skill sources:

- `paper_reader/` installs as `paper_reader` for single-paper deep reading.
- `paper_reader_batch/` installs as `paper_reader_batch` for batch scheduling and lightweight reporting.

Use a clean install: export only the selected skill source's tracked files into a staging directory, validate that release bundle, then move it to a new destination skill folder. Do not recursively copy a working source directory: it may contain ignored `.venv`, cache, or `runs/` state. Do not overlay a V1 installation. V1, unversioned, and unknown run artifacts remain untouched historical files; V2 never discovers or migrates them and rejects an explicitly supplied one with `unsupported_run_schema`.

The CLI prepares immutable artifacts, validates gates, renders notes, and records batch state; the agent still reads the extracted context and writes strict `paper_reader.summary.v2` and `paper_reader.review.v2` inputs.

Do not put a `README.md` inside `paper_reader/` or `paper_reader_batch/`; skills should expose `SKILL.md`, directly linked `references/`, bundled scripts, code, tests, templates, dependency metadata, and fixtures.

## Install

The Paper Reader 2.0 runtime currently supports macOS and Linux; on Windows, use WSL. The tracked-file installation helper below requires a POSIX shell.

Install `uv` before staging the skill. Use the official installer or a package manager; common options are:

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

Set the repository root once, then use this tracked-file staging helper. It validates the staging tree before `uv sync` creates installation-local runtime state:

```bash
set -eu
repo="/path/to/Paperread"

install_tracked_skill() {
  source_name="$1"
  install_dir="$2"
  command_name="$3"
  test ! -e "$install_dir" || { echo "target exists: $install_dir"; return 1; }
  install_parent="$(dirname "$install_dir")"
  mkdir -p "$install_parent"
  stage_dir="$(mktemp -d "$install_parent/.${source_name}.install.XXXXXX")"
  git -C "$repo" archive --format=tar "HEAD:${source_name}" | tar -xf - -C "$stage_dir"
  (
    cd "$stage_dir"
    uv run --no-project --python 3.13 python scripts/validate-skill.py . --release-bundle
  )
  mv "$stage_dir" "$install_dir"
  (
    cd "$install_dir"
    uv sync --locked
    uv run "$command_name" --version
    uv run "$command_name" --help
  )
}
```

The source must be a Git checkout and `HEAD:<source_name>` must exist. This intentionally installs files from the committed `HEAD` tree only; uncommitted working-tree and index changes are not included. Create the intended release commit before installing it.

Codex personal skills:

```bash
install_tracked_skill paper_reader \
  "${CODEX_HOME:-$HOME/.codex}/skills/paper_reader" paper_reader
install_tracked_skill paper_reader_batch \
  "${CODEX_HOME:-$HOME/.codex}/skills/paper_reader_batch" paper_reader_batch
```

Claude Code personal skills:

```bash
install_tracked_skill paper_reader \
  "$HOME/.claude/skills/paper_reader" paper_reader
install_tracked_skill paper_reader_batch \
  "$HOME/.claude/skills/paper_reader_batch" paper_reader_batch
```

If the target `paper_reader/` or `paper_reader_batch/` directory already exists, stop before installing. Paper Reader 2.0 requires a clean install into a new directory. An old installation may remain elsewhere as read-only history. A failed staging validation leaves the hidden staging directory in place for inspection; it is never promoted to the target.

The first `uv sync --locked` initializes each installed skill's local environment from its own lockfile. Re-run it after installing a newly exported revision.

## Zotero MCP Setup

Zotero-backed workflows require Zotero Desktop plus the Zotero MCP plugin before an agent can search your library or call `write_note`. Install and enable Zotero MCP from [cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp#readme):

1. Download the latest `zotero-mcp-plugin-*.xpi` from the repository releases.
2. In Zotero, install the `.xpi` with `Tools -> Add-ons`, then restart Zotero.
3. Open `Preferences -> Zotero MCP Plugin`, enable the integrated server, and generate the client configuration.
4. Use the generated Streamable HTTP MCP configuration, or configure the local endpoint directly as `http://127.0.0.1:23120/mcp`.

The plugin includes the MCP server; no separate Zotero MCP server process is required. paper_reader treats Zotero local API and SQLite as read-only and writes notes only through Zotero MCP `write_note`.

## Skill Usage

### Use `paper_reader`

Use `paper_reader` for one paper at a time. The CLI is deterministic tooling, not a standalone summarizer: it prepares immutable extraction artifacts and gates note readiness, while the agent writes strict summary and review inputs after reading the generated context files.

- Zotero title or title fragment: ask the agent to use `$paper_reader` with the paper title. The agent searches Zotero through Zotero MCP, initializes a V2 run, seals a review package, previews an immutable candidate, creates a 300-second authorization only after explicit write intent, lets the external agent call MCP `write_note` at most once, and verifies or reconciles read-only.
- Local PDF path: give an absolute or relative `.pdf` path. V2 reserves `<pdf_stem>_analysis/` and `<pdf_stem>_note.md`, prepares immutable evidence, seals a review package, builds a local candidate, and publishes atomically without replacement. This workflow never searches Zotero for matching items and never writes Zotero.
- Local directory path: use `paper_reader_batch` with the local PDF folder workflow. Existing local paths are not Zotero title fragments.

Useful installed-skill commands:

```bash
uv run paper_reader --help
uv run paper_reader route "/abs/path/to/paper.pdf"
uv run paper_reader run init-local "/abs/path/to/paper.pdf"
uv run paper_reader run prepare <run_dir>
uv run paper_reader review validate <run_dir>
uv run paper_reader review seal <run_dir>
uv run paper_reader candidate build <run_dir>
uv run paper_reader local publish <candidate.json>
uv run paper_reader zotero authorize <candidate.json>
uv run paper_reader zotero verify <authorization.json> --note-key <note_key>
uv run paper_reader zotero reconcile <authorization.json>
```

### Use `paper_reader_batch`

Use `paper_reader_batch` when the request contains multiple papers. Install both `paper_reader` and `paper_reader_batch`; the batch skill schedules work, while `$paper_reader` still owns each single-paper read.

Typical grouped batch CLI shape (every mutation includes a fresh UUID request id):

```bash
PAPER_READER_ROOT="/path/to/paper_reader"
PAPER_READER_BATCH_ROOT="/path/to/paper_reader_batch"

(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch manifest from-zotero-titles titles.txt --batch-title "my batch" --output manifest.json --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch run init --manifest manifest.json --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch run validate <batch_run_dir>)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch worker claim <batch_run_dir> --worker-id <worker_id> --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch worker prompt <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch worker finish <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --result <result.json> --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch write claim <batch_run_dir> --writer-id <writer_id> --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch write preview <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>)
(cd "$PAPER_READER_ROOT" && uv run paper_reader zotero authorize <candidate.json> --external-claim-id <claim_id> --write-attempt-id <write_attempt_id>)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch write begin <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --authorization <authorization.json> --request-id UUID)
(cd "$PAPER_READER_ROOT" && uv run paper_reader zotero verify <authorization.json> --note-key <note_key>)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch write commit <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --result <write-result.json> --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch run report <batch_run_dir>)
```

`write begin` first commits `write.started`, then returns the only MCP envelope the external agent may send. After that one MCP `write_note` create call, build the strict write result from read-only single-paper verification and commit it. For an unexpired started claim whose outcome becomes unknown, the active writer records uncertainty with its exact live claim identity: `(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch write mark-uncertain <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --reason <reason> --request-id UUID)`. An expired started claim must not reuse its lease token or send the MCP request again; its recovery path is read-only: `(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch run recover <batch_run_dir> --request-id UUID --paper-reader-root "$PAPER_READER_ROOT")`.

Repeat `worker claim -> prompt -> finish` until there are no eligible items. `worker prompt` is a read-only handoff to an outer agent or subagent; the batch CLI does not read papers or dispatch an LLM by itself.

For local PDF batches when outer-agent parallelism is unavailable:

```bash
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch local-prepare claim <batch_run_dir> --worker-id <worker_id> --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch local-prepare run <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --paper-reader-root "$PAPER_READER_ROOT" --request-id UUID)
```

For Zotero-backed items, the default write policy is `zotero_write`. Pass `--write-policy prepare_only` to manifest builders for dry-run. PDF batch items remain local-output only and skip Zotero lookup or duplicate checks.

## Workflows

paper_reader supports two inputs:

- **Zotero title or title fragment**: use Zotero MCP to locate the paper, preserve the complete discovery inventory, prepare immutable evidence, seal review, and build exact Markdown/HTML candidate artifacts. Only the external agent can send an unexpired authorization's exact MCP create envelope, once, after explicit write intent.
- **Local PDF path**: bind the normalized absolute path, size, SHA-256, device, and inode; prepare immutable evidence; seal review; build a local candidate; and atomically publish the final Markdown note beside the PDF without writing Zotero.

Local PDF path and directory path inputs skip Zotero lookup and duplicate checks. Existing local paths are not Zotero title fragments; directory paths belong to `paper_reader_batch manifest from-pdf-folder`, which is non-recursive unless `--recursive` is explicit.

Both workflows use full-PDF extraction by default. Final `evidence_summary` locators must use one of these canonical forms: `context.md page <N>`, `context.md page <N> section <Section Name>`, `context.md page <N> section <Section Name> table_candidate <N>`, or `figure_context.md <figure_id>`. Bare `context.md` / `figure_context.md`, prose locators such as `page 3 method section`, `section_context.md`, and secondary context paths are invalid. `section_context.md` is only a navigation aid. In the current grouped runtime, output from `scripts/capture-secondary-url.mjs` is unbound diagnostic material only: because no immutable secondary-capture ingestion command exists, it must not participate in review or candidate construction and cannot be cited in `evidence_summary`.

paper_reader_batch supports four batch inputs: Zotero collection inventories, multiple Zotero titles, local PDF folders, and multiple PDF paths. It normalizes them into a strict manifest and uses an append-only hash-chain journal as authority; `state.json` is only a reconstructable snapshot. Worker and local-prepare leases default to 900 seconds, and the serial write claim defaults to 120 seconds. Zotero-backed items use `zotero_write` by default, while PDF items never enter the write queue. After durable `write.started`, a crash is uncertain and is never resent: `run recover --paper-reader-root ...` delegates read-only single-paper reconciliation, then records `written`, `retry_confirmation_required`, or `blocked`. Pass `--write-policy prepare_only` for dry-run. A pure local-PDF report uses `effective_write_policy=local_only`; each per-paper result is extracted from the single-paper note's `30 秒结论` row, falling back to `tldr` then `one_sentence_summary` without resummarizing.

## Output Locations

- A single V2 run owns `run.json`, `source/`, `evidence/<evidence_id>/`, `reviews/<review_id>/`, `candidates/<candidate_id>/`, `authorizations/<authorization_id>.json`, `verifications/<authorization_id>/<note_key>.json`, and `reconciliations/<authorization_id>.json`.
- Local PDF initialization reserves `<pdf_stem>_analysis/` and a fixed `<pdf_stem>_note.md`; occupied names allocate `_v2`, `_v3`, and later versions without modifying old output. Publication never overwrites or silently changes target.
- A batch V2 run owns `manifest.json`, `events/<20-digit-sequence>.json`, `state.json`, `results/{worker,local-prepare,write,reconcile}/`, `batch-report.json`, `batch-report.md`, and `.run.lock`. Reports are regenerated solely by journal replay.

## Runtime Requirements

- Install and run CLI: `uv` plus Python `>=3.13` available to `uv`; use `uv python install 3.13` if no compatible interpreter is present.
- Local PDF workflow: no Zotero requirement and no Zotero duplicate check. Figure extraction may try an arXiv source download when an arXiv ID is detected in metadata or the PDF filename; this request uses a bounded network timeout and falls back to PDF-only extraction on failure.
- Zotero title workflow: Zotero Desktop plus Zotero MCP tools or the local MCP endpoint.
- Secondary web context capture: Node.js and a reachable CDP helper when this optional path is used.
- Batch workflow: installed `paper_reader` plus installed `paper_reader_batch`; deterministic child delegation requires and validates an explicit `--paper-reader-root`.

## Verification

From the installed or source `paper_reader/` directory:

```bash
uv sync --locked
uv run pytest
uv run paper_reader --version
uv run paper_reader --help
uv run paper_reader maintenance extract-pdf tests/fixtures/minimal.pdf
uv run python scripts/validate-skill.py .
```

Maintainers should also build a tracked-file staging directory outside the repository as shown above, run `uv run --no-project --python 3.13 python scripts/validate-skill.py . --release-bundle` before `uv sync`, and then run the same verification set there before treating `paper_reader/` as self-contained.

From the installed or source `paper_reader_batch/` directory:

```bash
uv sync --locked
uv run pytest
uv run paper_reader_batch --version
uv run paper_reader_batch --help
uv run python scripts/validate-skill.py .
```

The local-prepare integration tests exercise a real, separately staged `paper_reader`. If that root is not the repository sibling, bind it explicitly rather than relying on discovery:

```bash
PAPER_READER_TEST_ROOT="/path/to/separately-staged/paper_reader" uv run pytest
```

Maintainers should also build a tracked-file staging directory outside the repository as shown above, run `uv run --no-project --python 3.13 python scripts/validate-skill.py . --release-bundle` before `uv sync`, and then run the same verification set there before treating `paper_reader_batch/` as self-contained.

## Safety Boundaries

- V1, unversioned, and unknown run/manifest/result files are historical-only and fail read-only with `unsupported_run_schema`; there is no compatibility command, migration, dual loader, or fallback discovery.
- Preview and pass the sealed-review and immutable-candidate gates before every Zotero authorization; use `--write-policy prepare_only` for dry-run batch runs.
- Zotero writes are allowed only through the external agent's Zotero MCP `write_note` call, only after explicit user intent, and only with the exact unexpired authorization envelope.
- Zotero local API and SQLite are read-only.
- Local PDF path workflow is local-output only; it must not search Zotero, run duplicate checks, refresh live notes, create an authorization, or write Zotero.
- Zotero-backed batch workflow defaults to `zotero_write`, but the batch CLI never calls MCP `write_note`; it durably coordinates one external attempt and verifies or reconciles afterward. PDF batch items remain local-only.
- Rendered note prose should be Chinese-first while preserving paper titles, names, formulas, method names, units, evidence locators, and tag keys.
