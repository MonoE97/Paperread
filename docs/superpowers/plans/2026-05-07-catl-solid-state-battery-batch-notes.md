# CATL Solid-State Battery Batch Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Summarize Zotero items under the `CATL/固态电池` collection that do not already have Codex summary notes, incorporate WeChat links as secondary cross-check material, preview all write-ready notes, then write Zotero child notes only after explicit preview approval.

**Architecture:** Use a two-phase batch workflow: first freeze the live Zotero candidate set into `manifest.json`, then generate per-paper local run bundles with bounded parallel workers while the coordinator owns all gates. Workers may create only local artifacts; the coordinator performs central validation, produces `write-preview.md`, pauses for approval, then serially calls Zotero MCP `write_note` and verifies each note by readback. WeChat, news, and blog links discovered from Zotero `Extra` are captured as secondary context only and must never be cited in `evidence_summary`.

**Tech Stack:** Zotero MCP (`search_collections`, `get_collection_details`, `get_collection_items`, `get_subcollections`, `get_item_details`, `write_note`), Python 3 via `uv`, existing `zotero-paperread` CLI, Node.js CDP capture script, local run artifacts under `runs/`, optional Codex subagents for bounded parallel local analysis.

---

## Review And Optimization Decisions

1. The earlier high-level plan is structurally correct, but the target collection must be resolved live. Do not reuse historical CATL collection keys unless the current Zotero MCP audit confirms them.
2. The batch must freeze candidates before worker dispatch. Rebuilding the collection mid-run makes resume and audit unreliable.
3. Duplicate normalized titles in the target collection are a per-duplicate-group block. Do not choose a parent item for the user.
4. Missing or unreadable primary PDF text is blocked by default. A metadata-only note is too weak for this request unless the user explicitly authorizes metadata-only summaries later.
5. WeChat links should be first-class secondary artifacts, but they remain cross-check only. Primary evidence remains `context.md` and `figure_context.md`.
6. Parallelism is useful only for local analysis. Persistent Zotero writes must be serial and owned by the coordinator.
7. The user's initial request contains write intent, but this batch still stops at `write-preview.md` before any persistent write. The separate write confirmation is required because the preview contains the actual targets, note titles, and blocked/skipped decisions.
8. Existing Codex summary detection should use multiple markers: note title prefix `[Codex Summary]`, tag `codex-summary`, or note body footer `Tags: codex-summary, paper-summary`.

## File And Artifact Structure

- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/superpowers/plans/2026-05-07-catl-solid-state-battery-batch-notes.md`
- Create during execution: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/catl-solid-state-battery-batch-<HHMMSS>/manifest.json`
- Create during execution: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/catl-solid-state-battery-batch-<HHMMSS>/write-preview.md`
- Create during execution: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/catl-solid-state-battery-batch-<HHMMSS>/report.md`
- Create during execution for each queued paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/mcp-response.json`
- Create during execution for each queued paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/item-details.raw.json`
- Create during execution for each queued paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/item-details.json`
- Create during execution for each queued paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/context.md`
- Create during execution for each queued paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/figure_context.md`
- Create during execution for each queued paper with secondary links: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/secondary_contexts/secondary-001.md`
- Create during execution for each queued paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/summary.json`
- Create during execution for each queued paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/review.json`
- Create during execution for each write-ready paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/note.md`
- Create during execution for each write-ready paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/note.html`
- Create during execution for each write-ready paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/gate-report.json`
- Create during execution for each write-ready paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/write-payload.json`

## Manifest Schema

Use this compact schema for each `items[]` entry. Keep raw errors out of the preview; store concise `blocked_reason` and use `error_detail` only when needed for audit.

```json
{
  "batch_id": "catl-solid-state-battery-batch-HHMMSS",
  "target_collection_path": "CATL/固态电池",
  "generated_date": "2026-05-07",
  "state": "frozen",
  "items": [
    {
      "item_key": "ABC123",
      "title": "Paper title",
      "normalized_title": "paper title",
      "source_collection_key": "COLLKEY",
      "source_collection_path": "CATL/固态电池",
      "status": "queued",
      "run_dir": "runs/2026-05-07/paper-title-abc123",
      "existing_summary_notes": [],
      "secondary_sources": [],
      "primary_pdf_status": "available",
      "trust_status": "",
      "review_status": "",
      "note_title": "",
      "note_tags": [],
      "write_payload": "",
      "written_note_key": "",
      "blocked_reason": "",
      "error_detail": ""
    }
  ],
  "counts": {
    "discovered": 0,
    "queued": 0,
    "skipped_existing_summary": 0,
    "skipped_invalid_item": 0,
    "blocked": 0,
    "write_ready": 0,
    "verified": 0,
    "failed": 0
  }
}
```

## Status Model

Use these per-item states:

```text
discovered
skipped_existing_summary
skipped_invalid_item
blocked_duplicate_normalized_title
queued
prepared
summarized
reviewed
gated
previewed
write_ready
written
verified
blocked
failed
```

Terminal states before write are `skipped_existing_summary`, `skipped_invalid_item`, `blocked_duplicate_normalized_title`, `blocked`, and `failed`. Terminal states after write are `verified` and `failed`.

---

### Task 0: Baseline Safety And Tool Discovery

**Files:**
- No file changes during this task.

- [ ] **Step 1: Confirm branch and dirty state**

Run:

```bash
git branch --show-current
git status --short
```

Expected: branch is visible and unrelated dirty files, if any, are left untouched.

- [ ] **Step 2: Load Zotero MCP tools**

Use `tool_search` with:

```text
zotero mcp search_collections get_collection_details get_collection_items get_subcollections get_item_details write_note
```

Expected: tools needed for collection discovery, item readback, and note writing are callable. If native tools are not exposed, verify MCP configuration before continuing; do not fabricate Zotero state from memory.

- [ ] **Step 3: Verify local CLI surface**

Run:

```bash
uv run zotero-paperread --help
uv run zotero-paperread save-item-details --help
uv run zotero-paperread prepare-item --help
uv run zotero-paperread gate-run --help
uv run zotero-paperread prepare-write-payload --help
```

Expected: all commands exit 0 and expose the options required by this plan.

---

### Task 1: Resolve Target Collection And Freeze Candidates

**Files:**
- Create during execution: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/catl-solid-state-battery-batch-<HHMMSS>/manifest.json`

- [ ] **Step 1: Resolve `CATL/固态电池` live**

Use Zotero MCP:

```text
search_collections(q="CATL")
get_subcollections(collectionKey=<CATL key>)
get_collection_details(collectionKey=<candidate key>)
```

Expected: exactly one collection path resolves to `CATL/固态电池`. If multiple candidates match, report the collection keys and stop before manifest freeze.

- [ ] **Step 2: Create the batch directory**

Run:

```bash
BATCH_ID="catl-solid-state-battery-batch-$(date +%H%M%S)"
BATCH_DIR="/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/${BATCH_ID}"
mkdir -p "$BATCH_DIR"
printf '%s\n' "$BATCH_DIR"
```

Expected: prints the new batch directory path.

- [ ] **Step 3: Enumerate target items recursively**

Use Zotero MCP:

```text
get_collection_items(collectionKey=<solid_state_battery_collection_key>, mode="minimal")
get_subcollections(collectionKey=<solid_state_battery_collection_key>)
get_collection_items(collectionKey=<subcollection_key>, mode="minimal")
```

Expected: every paper item in `CATL/固态电池` and its subcollections is listed once with `item_key`, `title`, and source collection path. Non-paper placeholders with empty titles are marked `skipped_invalid_item`.

- [ ] **Step 4: Detect duplicate normalized titles**

Normalize titles by lowercasing, trimming, collapsing whitespace, and normalizing dash variants. If two or more Zotero items in the target set share the same normalized title, mark every item in that duplicate group as `blocked_duplicate_normalized_title`.

Expected: no duplicate group is sent to workers.

- [ ] **Step 5: Detect existing Codex summary notes**

For each non-duplicate paper item, use:

```text
get_item_details(itemKey=<item_key>, mode="complete")
```

Mark `skipped_existing_summary` if any child note matches one of:

```text
title starts with "[Codex Summary]"
metadata tags include "codex-summary"
body includes "Tags: codex-summary, paper-summary"
```

Expected: only items without existing Codex summaries remain `queued`.

- [ ] **Step 6: Write frozen manifest**

Write `manifest.json` under `$BATCH_DIR` with the schema above.

Expected: `manifest.json` contains a complete frozen candidate set and counts for discovered, queued, skipped, and blocked items. Do not rebuild this list during worker execution.

---

### Task 2: Create Per-Paper Run Bundles And Normalize Item Details

**Files:**
- Create during execution for each queued paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/mcp-response.json`
- Create during execution for each queued paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/item-details.raw.json`
- Create during execution for each queued paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/item-details.json`

- [ ] **Step 1: Allocate run directories through the CLI**

For each `queued` item, run:

```bash
uv run zotero-paperread create-run --title "$PAPER_TITLE" --item-key "$ITEM_KEY"
```

Expected: the command prints a project-local `run_dir`. Store that exact path in `manifest.json`; do not hand-roll paper slugs.

- [ ] **Step 2: Save raw MCP details**

For each `queued` item, call:

```text
get_item_details(itemKey=<item_key>, mode="complete")
```

Save the raw response to:

```text
<run_dir>/mcp-response.json
```

Expected: every queued item has a raw MCP audit file before local preparation.

- [ ] **Step 3: Normalize item details**

Run:

```bash
uv run zotero-paperread save-item-details "$RUN_DIR/mcp-response.json" --output "$RUN_DIR/item-details.json" --raw-output "$RUN_DIR/item-details.raw.json"
```

Expected: `item-details.json` is the only input used by later local commands. If Zotero `Extra` is recovered from read-only SQLite, successful immutable fallback diagnostics appear under `_paperread.enrichment.extra.diagnostics`, not as normal workflow warnings.

- [ ] **Step 4: Preflight primary PDF availability**

Read `item-details.json` and inspect local PDF attachment paths.

Expected:

```text
available PDF path -> keep status queued
missing PDF path -> status blocked, blocked_reason primary_pdf_missing
PDF path exists but extraction later yields no usable text -> status blocked, blocked_reason insufficient_primary_context
```

Do not create metadata-only summary notes in this batch unless the user explicitly authorizes that weaker mode.

---

### Task 3: Prepare Primary Context And Capture Secondary Links

**Files:**
- Create during execution for each prepared paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/context.md`
- Create during execution for each prepared paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/figure_context.md`
- Create during execution for each prepared paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/secondary_sources.json`
- Create during execution for each paper with links: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/secondary_contexts/secondary-001.md`

- [ ] **Step 1: Prepare full primary bundle**

Run for each item that passed PDF preflight:

```bash
uv run zotero-paperread prepare-item "$RUN_DIR/item-details.json" --workdir "$RUN_DIR"
```

Expected: `metadata.json`, `extract.json`, `context.md`, `figures.json`, `figure_context.md`, and `secondary_sources.json` exist. Do not pass `--max-pages` for this production batch.

- [ ] **Step 2: Block unusable primary context**

Inspect `extract.json` and `context.md`.

Expected:

```text
usable full-paper text -> status prepared
missing_pdf_attachment or empty extracted text -> status blocked, blocked_reason insufficient_primary_context
```

- [ ] **Step 3: Capture WeChat and other secondary URLs**

If `secondary_sources.json.sources[]` contains URLs, run for each URL:

```bash
mkdir -p "$RUN_DIR/secondary_contexts"
node skills/zotero-paper-summary/scripts/capture-secondary-url.mjs "$URL" --output "$RUN_DIR/secondary_contexts/secondary-001.md" --request-retries 2 --request-retry-ms 500
```

Use `secondary-002.md`, `secondary-003.md`, and so on for additional URLs.

Expected: successful files include `source_status: secondary_context`. Persistent WeChat/CDP failures create `source_status: secondary_context_unavailable` and do not block the paper.

- [ ] **Step 4: Record secondary-source state in manifest**

For each captured source, update the item's manifest entry with:

```json
{
  "source_id": "secondary-001",
  "url": "https://mp.weixin.qq.com/s/example",
  "status": "secondary_context",
  "path": "runs/2026-05-07/<paper-slug>/secondary_contexts/secondary-001.md"
}
```

Expected: preview later shows whether each paper had WeChat/secondary material and whether capture succeeded.

---

### Task 4: Generate Summaries With Bounded Parallel Workers

**Files:**
- Create during execution for each prepared paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/summary.json`
- Create during execution for each prepared paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/review.json`

- [ ] **Step 1: Set worker concurrency**

Use a concurrency cap of 3 workers.

Expected: at most 3 active workers at a time; completed workers are closed before new workers are dispatched.

- [ ] **Step 2: Dispatch one local-analysis task per paper**

Each worker receives exactly one `run_dir` and this instruction:

```text
You are analyzing one Zotero paper run directory.

Allowed reads:
- <run_dir>/metadata.json
- <run_dir>/extract.json
- <run_dir>/context.md
- <run_dir>/figures.json
- <run_dir>/figure_context.md
- <run_dir>/secondary_sources.json
- <run_dir>/secondary_contexts/*.md if present and source_status is secondary_context

Allowed writes:
- <run_dir>/summary.json
- <run_dir>/review.json

Forbidden:
- Do not call Zotero MCP write_note.
- Do not mutate Zotero collections, metadata, tags, Better Notes, or SQLite.
- Do not edit source code, docs, or another paper's run directory.
- Do not cite secondary_contexts in evidence_summary.

Output requirements:
- summary.json must follow the schema in skills/zotero-paper-summary/SKILL.md.
- evidence_summary locators must cite only context.md or figure_context.md.
- WeChat and other secondary material may be used only for cross-check, background, conflict detection, and follow-up questions.
- review.json must include review_status, review_issues, trust_status_recommendation, needs_improvement, and improvement_requests.
- If primary evidence is insufficient, set conservative trust fields and explain the blocker in review.json.
```

Expected: each worker returns the changed file paths and a compact status.

- [ ] **Step 3: Run local validation inside each run directory**

For each worker output, run:

```bash
uv run zotero-paperread validate-summary-json "$RUN_DIR/summary.json"
uv run zotero-paperread finalize-note "$RUN_DIR/metadata.json" "$RUN_DIR/summary.json" --output "$RUN_DIR/note.md" --html-output "$RUN_DIR/note.html"
uv run zotero-paperread preview-note "$RUN_DIR/note.md"
uv run zotero-paperread preview-note "$RUN_DIR/note.html"
```

Expected: valid initial note artifacts exist for coordinator review. These previews are local dry-run artifacts, not write approval.

---

### Task 5: Central Review Gate And Write Payload Preparation

**Files:**
- Create during execution for each write-ready paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/gate-report.json`
- Create during execution for each write-ready paper: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/<paper-slug>/write-payload.json`

- [ ] **Step 1: Re-run deterministic gate sequence centrally**

For each paper with `summary.json` and `review.json`, run:

```bash
GENERATED_DATE="2026-05-07"
uv run zotero-paperread validate-summary-json "$RUN_DIR/summary.json"
uv run zotero-paperread apply-review "$RUN_DIR/summary.json" "$RUN_DIR/review.json"
uv run zotero-paperread lint-summary "$RUN_DIR/summary.json"
uv run zotero-paperread validate-trusted-summary "$RUN_DIR/summary.json"
VERSION_SUFFIX="$(uv run zotero-paperread next-version-suffix "$RUN_DIR/item-details.json" --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE")"
uv run zotero-paperread finalize-note "$RUN_DIR/metadata.json" "$RUN_DIR/summary.json" --generated-date "$GENERATED_DATE" --version-suffix "$VERSION_SUFFIX" --output "$RUN_DIR/note.md" --html-output "$RUN_DIR/note.html"
uv run zotero-paperread note-tags "$RUN_DIR/summary.json"
uv run zotero-paperread preview-note "$RUN_DIR/note.md"
uv run zotero-paperread preview-note "$RUN_DIR/note.html"
uv run zotero-paperread gate-run "$RUN_DIR" --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE" --output "$RUN_DIR/gate-report.json"
uv run zotero-paperread prepare-write-payload "$RUN_DIR/gate-report.json" --output "$RUN_DIR/write-payload.json"
```

Expected: only items whose `gate-report.json` state is `write_ready` proceed to preview. Any item with `needs_improvement`, blocked `improvement_status`, invalid evidence locators, missing note HTML, or failed trusted summary validation becomes `blocked` or `failed`.

- [ ] **Step 2: Ensure secondary material is not primary evidence**

Inspect each `summary.json`:

```text
evidence_summary[].evidence[].locator
```

Expected: locators mention only `context.md` or `figure_context.md`. Locators mentioning `secondary_context`, `secondary_contexts`, WeChat URLs, news pages, or blogs fail the gate.

- [ ] **Step 3: Update manifest gate fields**

For each gated item, store:

```text
trust_status
review_status
reading_decision
note_title
note_tags
write_payload path
gate_report path
status write_ready or blocked
blocked_reason when not write_ready
```

Expected: manifest supports restart from the first non-terminal item.

---

### Task 6: Build Preview And Stop For User Approval

**Files:**
- Create during execution: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/catl-solid-state-battery-batch-<HHMMSS>/write-preview.md`

- [ ] **Step 1: Generate compact preview table**

Create `write-preview.md` with columns:

```text
item_key
Zotero title
status
note title
note tags
trust_status
review_status
reading_decision
secondary sources
run_dir
blocked_reason
```

Expected: every discovered item appears in the table, including skipped and blocked items.

- [ ] **Step 2: Present preview and target titles**

Show the user:

```text
BATCH_DIR
manifest.json path
write-preview.md path
counts by status
all write_ready target item titles
all blocked/skipped reasons
```

Expected: no Zotero `write_note` call has happened. Stop here and wait for explicit write approval such as "确认，开始写入".

---

### Task 7: Serial Zotero Write-Through After Approval

**Files:**
- Update during execution: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/catl-solid-state-battery-batch-<HHMMSS>/manifest.json`
- Create or update during execution: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/catl-solid-state-battery-batch-<HHMMSS>/report.md`

- [ ] **Step 1: Re-check current Zotero state before first write**

For each `write_ready` item, call:

```text
get_item_details(itemKey=<item_key>, mode="complete")
```

Expected: no new equivalent Codex summary note appeared since preview. If one appeared, mark the item `skipped_existing_summary_after_preview`.

- [ ] **Step 2: Write one note at a time**

For each still-write-ready item:

```text
read <run_dir>/write-payload.json
read <run_dir>/note.html
write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)
```

Expected: Zotero returns a created note key. Store it as `written_note_key` and set status `written`.

- [ ] **Step 3: Verify each write by readback**

Immediately after each `write_note`, call:

```text
get_item_details(itemKey=<item_key>, mode="complete")
```

Expected: the created note key appears under the target item and its title starts with `[Codex Summary]`. Set status `verified`.

- [ ] **Step 4: Continue safely on per-item failure**

If one item fails to write or verify, mark only that item `failed` with concise `blocked_reason` or `error_detail`, then continue with the next item.

Expected: one failed item does not corrupt the rest of the batch.

---

### Task 8: Final Report And Verification

**Files:**
- Update during execution: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/catl-solid-state-battery-batch-<HHMMSS>/manifest.json`
- Create during execution: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-05-07/catl-solid-state-battery-batch-<HHMMSS>/report.md`

- [ ] **Step 1: Write final report**

Create `report.md` with:

```text
batch id
target collection path
manifest path
write-preview path
counts by final status
verified note keys
skipped existing-summary items
blocked duplicate-title groups
blocked primary-content items
secondary-source capture summary
failed write/readback items
resume instructions
```

Expected: final report is enough to audit what was written and what was intentionally skipped.

- [ ] **Step 2: Run project verification commands**

Run:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
git diff --check
```

Expected: commands exit 0. If tests fail due unrelated existing state, report the exact failing command and first relevant failure.

- [ ] **Step 3: Final user summary**

Report:

```text
verified note count
skipped count
blocked count
failed count
report.md path
manifest.json path
write-preview.md path
```

Expected: the user can inspect all durable artifacts without relying on chat history.

## Resume Rules

Resume only from `manifest.json`.

If stopped before preview, continue from the first item not in a terminal pre-write state. If stopped after preview but before write approval, re-check current Zotero item details and regenerate the preview before writing. If stopped during writes, resume from the first item that is not `verified`, but re-check for newly created Codex summary notes before calling `write_note`.

## Self-Review

- Spec coverage: The plan covers target collection discovery, no-summary filtering, WeChat secondary capture, bounded parallel analysis, central gate, preview pause, serial write, readback verification, and final report.
- Placeholder scan: Dynamic batch and run paths are expressed as executable shell variables or CLI-created run directories; workers are instructed to use actual `run_dir` paths from the manifest.
- Boundary check: The plan forbids Zotero writes by workers, forbids direct SQLite mutation, preserves WeChat as secondary context only, and requires explicit user approval after `write-preview.md`.
