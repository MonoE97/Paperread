# Public Repo Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `Zotero_paperread` into a public clone-and-run tool repo with one canonical repo-local skill bundle at `skills_paperread/`.

**Architecture:** Keep the Python package, tests, template, and lockfile as the executable tool. Consolidate the public agent workflow into `skills_paperread/`, move the one still-needed helper script there, rewrite README/AGENTS as the public and agent entry points, then remove historical `skills/` and `docs/` only after explicit deletion confirmation.

**Tech Stack:** Python package managed by `uv`, pytest, Node.js helper script for secondary context capture, Markdown skill/reference docs, project-local `zotero-paperread` CLI.

---

## First-Principles Review Gate

Run this review after each task before committing:

```text
Does each changed or retained public file directly support install/run, workflow understanding, agent safety boundaries, rendering/validation, testing, or environment reproducibility?
Does the change reduce duplicate public entry points?
Does the change avoid exposing paper PDFs, extracted text, Zotero metadata, generated notes, local run artifacts, or internal planning history?
Does the change preserve the two supported public workflows: Zotero title and local PDF path?
```

If any answer is no, revise the task before moving on.

## File Structure

Keep as public surface:

- `README.md`: external user setup, usage, outputs, privacy, verification.
- `AGENTS.md`: agent rules and write boundaries.
- `.gitignore`: public privacy and local-output boundary.
- `src/zotero_paperread/`: deterministic CLI/tooling code; no behavior rewrite planned.
- `templates/zotero_note.md.j2`: note rendering template; no behavior rewrite planned.
- `tests/`: shipped behavior and public-boundary tests.
- `tests/fixtures/minimal.pdf`: minimal extraction fixture.
- `pyproject.toml` and `uv.lock`: reproducible Python environment.
- `skills_paperread/`: only public workflow bundle.
- `skills_paperread/scripts/capture-secondary-url.mjs`: secondary context capture helper.
- `skills_paperread/references/zotero-workflow.md`: Zotero title workflow.
- `skills_paperread/references/pdf-path-workflow.md`: PDF path workflow.
- `skills_paperread/references/summary-schema.md`: summary/review authoring contract.

Remove after explicit deletion confirmation:

- `skills/`: old duplicate skill entry points.
- `docs/`: internal historical specs/plans/runbooks, including this implementation plan.
- `tests/test_zotero_batch_skill_scripts.py`: tests only the old batch skill, which public v1 does not ship.
- `tests/fixtures/batch_manifest_valid.json`
- `tests/fixtures/batch_manifest_invalid_duplicate.json`
- `tests/fixtures/batch_manifest_for_preview.json`
- `tests/fixtures/batch_write_report_escaped_title.json`

Do not create `.agents/skills/paperread` or `.claude/skills/paperread`.

### Task 1: Add Public-Boundary Tests First

**Files:**
- Modify: `tests/test_default_workflow_docs.py`
- Modify: `tests/test_capture_secondary_url.py`

- [ ] **Step 1: Update secondary capture test path to the intended public path**

Change this line in `tests/test_capture_secondary_url.py`:

```python
SCRIPT = Path("skills/zotero-paper-summary/scripts/capture-secondary-url.mjs")
```

to:

```python
SCRIPT = Path("skills_paperread/scripts/capture-secondary-url.mjs")
```

- [ ] **Step 2: Replace legacy docs tests with public-boundary tests**

Replace `tests/test_default_workflow_docs.py` with:

```python
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
README = PROJECT_ROOT / "README.md"
AGENTS = PROJECT_ROOT / "AGENTS.md"
PAPERREAD_SKILL = PROJECT_ROOT / "skills_paperread" / "SKILL.md"
ZOTERO_REFERENCE = PROJECT_ROOT / "skills_paperread" / "references" / "zotero-workflow.md"
PDF_REFERENCE = PROJECT_ROOT / "skills_paperread" / "references" / "pdf-path-workflow.md"
SUMMARY_REFERENCE = PROJECT_ROOT / "skills_paperread" / "references" / "summary-schema.md"
CAPTURE_SCRIPT = PROJECT_ROOT / "skills_paperread" / "scripts" / "capture-secondary-url.mjs"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_public_docs_use_single_repo_local_skill_entry() -> None:
    combined = "\n".join(read(path) for path in [README, AGENTS, PAPERREAD_SKILL])

    assert "skills_paperread/" in combined
    assert "repo-local" in combined
    assert "uv sync" in combined
    assert "from the repo root" in combined
    assert "not a standalone global skill installation" in read(PAPERREAD_SKILL)
    assert "skills/zotero-paper-summary" not in combined
    assert "skills/zotero-batch-note-writing" not in combined
    assert ".agents/skills/paperread" not in combined
    assert ".claude/skills/paperread" not in combined


def test_public_docs_describe_supported_workflows_and_outputs() -> None:
    combined = "\n".join(read(path) for path in [README, AGENTS, PAPERREAD_SKILL])

    for phrase in [
        "Zotero title",
        "local PDF path",
        "prepare-pdf",
        "<pdf_stem>_analysis",
        "<pdf_stem>_note.md",
        "prepare-write-candidate",
        "prepare-local-note-candidate",
    ]:
        assert phrase in combined


def test_zotero_reference_keeps_single_paper_write_safety_contract() -> None:
    text = read(ZOTERO_REFERENCE)

    for phrase in [
        "search_library",
        "get_item_details",
        "write_note",
        "same normalized title",
        "stop before create-run",
        "save-item-details",
        "prepare-item",
        "section_context.md",
        "not a canonical evidence source",
        "prepare-write-candidate",
        'write_note(action="create"',
        "verify-zotero-note",
        "HTTP JSON-RPC fallback",
        "http://127.0.0.1:23120/mcp",
        "NO_PROXY",
        "Zotero local API and SQLite are read-only",
    ]:
        assert phrase in text

    assert 'write_note(action="update"' not in text


def test_secondary_context_contract_uses_public_script_path() -> None:
    readme = read(README)
    zotero = read(ZOTERO_REFERENCE)

    for text in (readme, zotero):
        assert "skills_paperread/scripts/capture-secondary-url.mjs" in text
        assert "secondary_sources.json" in text
        assert "secondary_contexts" in text
        assert "source_status: secondary_context" in text
        assert "secondary_context_unavailable" in text
        assert "navigation_timeout" in text
        assert "must not cite secondary context" in text
        assert "--request-retries" in text


def test_pdf_path_reference_forbids_zotero_write_path() -> None:
    text = read(PDF_REFERENCE)

    for phrase in [
        "prepare-pdf",
        "prepare-local-note-candidate",
        "must not write Zotero",
        "must not call refresh-live-notes",
        "must not create write-payload.json",
        "context.md",
        "figure_context.md",
        "not a canonical evidence source",
    ]:
        assert phrase in text


def test_summary_reference_documents_rendered_chinese_fields() -> None:
    text = read(SUMMARY_REFERENCE)

    for phrase in [
        "paper_type",
        "trust_status",
        "review_status",
        "one_sentence_summary",
        "abstract_translation",
        "research_question",
        "method_modules",
        "workflow_steps",
        "technical_details",
        "key_figures",
        "author_stated_limitations",
        "inferred_limits",
        "applicability_limits",
        "evidence_summary",
        "context.md",
        "figure_context.md",
        "Chinese-first",
    ]:
        assert phrase in text


def test_gitignore_documents_private_outputs_and_local_state() -> None:
    text = read(PROJECT_ROOT / ".gitignore")

    for phrase in [
        ".venv/",
        ".superpowers/",
        ".worktrees/",
        "runs/",
        "papers/",
        "*_analysis/",
        "*_analysis_v[0-9]*/",
        "*_note.md",
        "*_note_v[0-9]*.md",
        "*.extract.json",
        "*.summary.json",
        "*.note.md",
    ]:
        assert phrase in text


def test_capture_secondary_script_is_in_public_skill_bundle() -> None:
    assert CAPTURE_SCRIPT.exists()
    text = read(CAPTURE_SCRIPT)
    assert "secondary_context" in text
    assert "secondary_context_unavailable" in text
    assert "request-retries" in text
```

- [ ] **Step 3: Run targeted tests and verify expected failures**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py tests/test_capture_secondary_url.py -q
```

Expected now: failures because `skills_paperread/scripts/capture-secondary-url.mjs` does not exist yet and the public docs still contain old references or missing contract text.

- [ ] **Step 4: First-principles review**

Confirm the failing tests describe the public boundary rather than preserving old `skills/` or `docs/` behavior.

### Task 2: Migrate The Secondary Capture Script Without Deleting Old Skills

**Files:**
- Create: `skills_paperread/scripts/capture-secondary-url.mjs`
- Modify: `tests/test_capture_secondary_url.py`
- Test: `tests/test_capture_secondary_url.py`

- [ ] **Step 1: Create the public scripts directory and copy the helper**

Run:

```bash
mkdir -p skills_paperread/scripts
cp skills/zotero-paper-summary/scripts/capture-secondary-url.mjs skills_paperread/scripts/capture-secondary-url.mjs
```

Do not remove `skills/zotero-paper-summary/scripts/capture-secondary-url.mjs` in this task. Deletion waits for the explicit deletion-confirmation task.

- [ ] **Step 2: Run the secondary capture tests**

Run:

```bash
uv run pytest tests/test_capture_secondary_url.py -q
```

Expected: all tests in `tests/test_capture_secondary_url.py` pass against `skills_paperread/scripts/capture-secondary-url.mjs`.

- [ ] **Step 3: First-principles review**

Confirm the helper script now lives under the only public skill bundle and remains tested by behavior, not by old path existence.

- [ ] **Step 4: Commit**

```bash
git add skills_paperread/scripts/capture-secondary-url.mjs tests/test_capture_secondary_url.py
git commit -m "chore: move secondary capture helper to paperread skill"
```

### Task 3: Expand `skills_paperread` Into The Canonical Workflow Bundle

**Files:**
- Modify: `skills_paperread/SKILL.md`
- Modify: `skills_paperread/references/zotero-workflow.md`
- Modify: `skills_paperread/references/pdf-path-workflow.md`
- Modify: `skills_paperread/references/summary-schema.md`
- Test: `tests/test_default_workflow_docs.py`

- [ ] **Step 1: Update `skills_paperread/SKILL.md`**

Ensure `skills_paperread/SKILL.md` contains this routing and boundary text:

```markdown
# Paperread

This is the repo-local v1 paper reading skill bundle for this repository. It is not a standalone global skill installation. Use it after cloning this repo, installing `uv`, running `uv sync`, and executing commands from the repo root.

Do not copy this directory by itself and expect the workflow to run. The skill depends on the repository's Python package, templates, lockfile, and `zotero-paperread` CLI.

## Entry Routing

- If the user input is a local PDF path and the path exists with suffix `.pdf`, use `references/pdf-path-workflow.md`.
- Otherwise treat the input as a Zotero title or title fragment and use `references/zotero-workflow.md`.
- For both modes, use full-PDF extraction by default. Pass `--max-pages` only when the user explicitly asks for debugging or a shortened preview.
- For both modes, the agent writes `summary.json` and `review.json` after reading `context.md`, `section_context.md`, and `figure_context.md` when available.
```

Keep the existing YAML front matter with `name: paperread`.

- [ ] **Step 2: Replace `skills_paperread/references/zotero-workflow.md` with the public Zotero workflow**

Use this structure and required text:

```markdown
# Zotero Workflow

Use this when the user provides a Zotero title or title fragment.

## Tool Discovery

Load Zotero MCP tools before the workflow: `search_library`, `get_item_details`, `get_content`, `write_note`, and optional `annotations`.

If native MCP tools are not injected, use the local Zotero MCP endpoint `http://127.0.0.1:23120/mcp` as an HTTP JSON-RPC fallback. The fallback still calls Zotero MCP methods such as `zotero-mcp write_note`; it is not a Zotero local API write path. If localhost requests hit a proxy, clear `ALL_PROXY`, `HTTP_PROXY`, and `HTTPS_PROXY`, then set `NO_PROXY=127.0.0.1,localhost` and `no_proxy=127.0.0.1,localhost`.

## Steps

1. Search exact title first. If duplicate entries have the same normalized title, stop before create-run and ask the user to de-duplicate in Zotero.
2. Create the run directory with `uv run zotero-paperread create-run --title "<title>" --item-key "<item_key>"`.
3. Save the raw `get_item_details(mode="complete")` response as `<run_dir>/mcp-response.json`.
4. Normalize item details:

```bash
uv run zotero-paperread save-item-details <run_dir>/mcp-response.json --output <run_dir>/item-details.json --raw-output <run_dir>/item-details.raw.json
```

5. Prepare the bundle:

```bash
uv run zotero-paperread prepare-item <run_dir>/item-details.json --workdir <run_dir>
```

6. If `secondary_sources.json` lists Extra/web URLs, capture each source for cross-check only:

```bash
mkdir -p <run_dir>/secondary_contexts
node skills_paperread/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

Captured files use `source_status: secondary_context` when usable. Unavailable captures use `source_status: secondary_context_unavailable`, including warnings such as `navigation_timeout`. Secondary context must not cite secondary context in `evidence_summary`; it is only for cross-checking and background.

7. Read `context.md`, `section_context.md`, and `figure_context.md` if available. `section_context.md` is not a canonical evidence source. Final locators must cite `context.md page 3 section Methods`, `context.md page 6 section Results table_candidate 1`, or `figure_context.md fig_p4_1`.
8. Write `summary.json` and `review.json`.
9. Run the deterministic review chain:

```bash
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread lint-summary <run_dir>/summary.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
```

10. Prepare a Zotero write candidate only when Zotero output is explicitly requested:

```bash
uv run zotero-paperread prepare-write-candidate <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD
```

11. Preview the target Zotero title, `note.md`, and `note.html`.
12. After explicit write intent, call only `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`.
13. Verify with `verify-zotero-note` using expected parent, title, headings, tags, and content hash from `write-payload.json`.

## Boundaries

- Zotero writes must use Zotero MCP `write_note`.
- Zotero local API and SQLite are read-only in this project.
- Do not call `write_note(action="update")`.
- Do not use the PDF local gate as proof of Zotero write readiness.
- Do not cite `section_context.md` or secondary context as canonical evidence.
```

- [ ] **Step 3: Update `skills_paperread/references/pdf-path-workflow.md`**

Keep its current command chain and add these exact boundary phrases if missing:

```markdown
- The PDF path workflow must not write Zotero.
- The PDF path workflow must not call refresh-live-notes.
- The PDF path workflow must not create write-payload.json.
- The PDF path workflow must not treat `section_context.md` as a canonical evidence source.
```

- [ ] **Step 4: Update `skills_paperread/references/summary-schema.md`**

Ensure the `summary.json` section lists these rendered Chinese-first fields:

```markdown
- `method_modules`
- `workflow_steps`
- `technical_details`
- `key_figures`
- `author_stated_limitations`
- `inferred_limits`
- `applicability_limits`
```

Add this rule:

```markdown
Rendered note prose is Chinese-first. Paper titles, author names, institution names, formulas, method names, abbreviations, units, evidence locators, code-like keys, and tag keys may remain in English.
```

- [ ] **Step 5: Run targeted public-boundary tests**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: remaining failures should be from README, AGENTS, or `.gitignore`, not from `skills_paperread/`.

- [ ] **Step 6: Validate the skill bundle**

Run:

```bash
uv run --with pyyaml python /Users/jwxi/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/jwxi/Desktop/AIflow/Zotero_paperread/skills_paperread
```

Expected output contains:

```text
Skill is valid!
```

- [ ] **Step 7: First-principles review**

Confirm `skills_paperread/` alone now tells an agent how to route Zotero title input and PDF path input without referring to old `skills/`.

- [ ] **Step 8: Commit**

```bash
git add skills_paperread tests/test_default_workflow_docs.py
git commit -m "docs: consolidate paperread skill workflow"
```

### Task 4: Rewrite README, AGENTS, And `.gitignore` For Public Release

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `.gitignore`
- Test: `tests/test_default_workflow_docs.py`

- [ ] **Step 1: Rewrite README around the public user path**

Ensure `README.md` has these top-level sections in this order:

```markdown
# Zotero Paperread

## What It Does
## Public V1 Setup
## Use As A Repo-Local Skill
## Zotero Title Workflow
## Local PDF Path Workflow
## Privacy And Local Outputs
## Verification
## Safety Boundaries
```

Include this exact setup command block:

```bash
uv sync
uv run zotero-paperread --help
```

Include this exact repo-local skill wording:

```markdown
The only public workflow bundle is `skills_paperread/`. Public v1 is repo-local: clone this repository, run `uv sync`, and execute commands from the repo root. Do not copy `skills_paperread/` by itself and expect the workflow to run, because it depends on this repository's Python package, templates, lockfile, and CLI.
```

Include the secondary capture command with the new path:

```bash
node skills_paperread/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

Include the verification block:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

- [ ] **Step 2: Rewrite AGENTS directory conventions**

Ensure `AGENTS.md` lists only these public top-level conventions:

```markdown
- `src/zotero_paperread/`: Python package code for deterministic CLI/tooling logic.
- `tests/`: pytest tests; never perform real Zotero writes.
- `templates/`: Jinja2 note template.
- `skills_paperread/`: the only public repo-local paperread workflow bundle.
- `README.md`: public user entry point.
- `AGENTS.md`: agent behavior and safety rules.
```

Remove references to `skills/`, `docs/references/`, `docs/superpowers/plans/`, and `docs/superpowers/specs/` as active public directories.

- [ ] **Step 3: Replace `.gitignore` with grouped public-boundary rules**

Use this content:

```gitignore
# macOS
.DS_Store

# Python caches, packaging, and coverage
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.coverage
htmlcov/
dist/
build/

# uv and virtual environments
.venv/

# Agent/session local state
.worktrees/
.superpowers/
*.log
tmp/
.tmp/

# Paper, Zotero, and generated note outputs
runs/
papers/
*_analysis/
*_analysis_v[0-9]*/
*_note.md
*_note_v[0-9]*.md
*.pdf.txt
*.extract.json
*.summary.json
*.note.md
```

- [ ] **Step 4: Run public documentation tests**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: pass.

- [ ] **Step 5: First-principles review**

Check README as a new external user: can they understand setup, choose Zotero vs PDF path, know where outputs go, and know what is private?

- [ ] **Step 6: Commit**

```bash
git add README.md AGENTS.md .gitignore tests/test_default_workflow_docs.py
git commit -m "docs: simplify public paperread entrypoints"
```

### Task 5: Request Deletion Confirmation, Then Remove Legacy Public Surface

**Files:**
- Delete after explicit confirmation: `skills/`
- Delete after explicit confirmation: `docs/`
- Delete after explicit confirmation: `tests/test_zotero_batch_skill_scripts.py`
- Delete after explicit confirmation: batch-only fixture JSON files in `tests/fixtures/`
- Test: full suite

- [ ] **Step 1: Stop and ask for explicit deletion confirmation**

Ask the user exactly:

```text
下一步会删除 `skills/`、`docs/`、`tests/test_zotero_batch_skill_scripts.py` 以及 batch-only fixture JSON。请明确回复“确认删除”，我再执行删除。
```

Do not continue this task until the user explicitly replies `确认删除` or another unambiguous deletion approval.

- [ ] **Step 2: Remove legacy directories and batch-only tests**

After confirmation, run:

```bash
rm -rf skills docs
rm -f tests/test_zotero_batch_skill_scripts.py
rm -f tests/fixtures/batch_manifest_valid.json
rm -f tests/fixtures/batch_manifest_invalid_duplicate.json
rm -f tests/fixtures/batch_manifest_for_preview.json
rm -f tests/fixtures/batch_write_report_escaped_title.json
```

- [ ] **Step 3: Verify no public docs point to old paths**

Run:

```bash
rg -n "skills/zotero|zotero-batch-note-writing|docs/superpowers|docs/references|batch_manifest|batch_write_report" README.md AGENTS.md tests skills_paperread pyproject.toml
```

Expected: no output.

- [ ] **Step 4: Verify remaining tracked files**

Run:

```bash
git ls-files | rg "^(skills/|docs/|tests/test_zotero_batch_skill_scripts.py|tests/fixtures/batch_)"
```

Expected: no output after deletion is staged.

- [ ] **Step 5: Run tests**

Run:

```bash
uv run pytest
```

Expected: pass.

- [ ] **Step 6: First-principles review**

Confirm every remaining top-level directory directly supports install/run, workflow understanding, safety rules, testing, rendering, or reproducible environment setup.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chore: remove legacy private docs and skills"
```

### Task 6: Full Verification And Adversarial Review

**Files:**
- Read: `README.md`
- Read: `AGENTS.md`
- Read: `skills_paperread/SKILL.md`
- Read: `skills_paperread/references/*.md`
- Read: `.gitignore`

- [ ] **Step 1: Run the full verification set**

Run:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
uv run --with pyyaml python /Users/jwxi/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/jwxi/Desktop/AIflow/Zotero_paperread/skills_paperread
git diff --check origin/main...HEAD
```

Expected:

```text
pytest passes
zotero-paperread --help exits 0
extract-pdf exits 0 and writes /tmp/zotero-paperread-extract.json
Skill is valid!
git diff --check has no output
```

- [ ] **Step 2: Search for stale legacy references**

Run:

```bash
rg -n "skills/zotero|zotero-paper-summary|zotero-batch-note-writing|docs/superpowers|docs/references|write_note\\(action=\"update\"|\\.agents/skills/paperread|\\.claude/skills/paperread" .
```

Expected: no output except possible `.git` internals if the command is accidentally run without ripgrep's default ignore behavior. With normal `rg`, `.git` is ignored.

- [ ] **Step 3: Search for unignored local-output examples**

Run:

```bash
git status --short --untracked-files=all
```

Expected: no untracked local run artifacts, PDF analysis directories, PDF notes, cache directories, or batch fixture leftovers.

- [ ] **Step 4: Read public entrypoints like an external user**

Confirm:

```text
README explains clone/uv setup, the single skill entry, both workflows, outputs, privacy, and verification.
AGENTS forbids Zotero writes outside zotero-mcp write_note and says PDF path workflow is local-output only.
skills_paperread routes Zotero titles and PDF paths without referencing old skills or docs.
```

- [ ] **Step 5: Commit final fixes if Step 1-4 reveal issues**

If any verification or review issue required edits, commit them:

```bash
git add -A
git commit -m "fix: close public repo cleanup review gaps"
```

If no issues required edits, do not create an empty commit.

## Completion Criteria

The cleanup is complete only when all are true:

- `skills_paperread/` is the only skill bundle in the repo.
- `docs/` and legacy `skills/` are gone after explicit deletion approval.
- README/AGENTS do not point users to old directories.
- Public v1 does not claim batch workflow support.
- `.gitignore` covers caches, agent state, runs, papers, PDF analysis directories, and generated notes.
- Full verification passes.
- The final response says whether anything was not run or not completed.
