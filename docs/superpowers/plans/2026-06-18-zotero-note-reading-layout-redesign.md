# Zotero Note Reading Layout Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render Zotero paper-reading notes as a concise 0-7 reading layout, keep audit fields in JSON/gates, sync the skill instructions, and regenerate the Polyanion example note with the new logic.

**Architecture:** This is a render-layer redesign. The summary schema and gate pipeline remain intact; `templates/zotero_note.md.j2` and `src/zotero_paperread/note.py` decide which fields are shown in the final Markdown/HTML note. Tests prove the new headings, the absence of audit-only sections, and continued evidence validation.

**Tech Stack:** Python, Jinja2, markdown-it-py, Typer CLI, pytest, uv, Zotero MCP for final note writes.

---

## File Structure

- Modify `.gitignore`
  - Add `.superpowers/` under local outputs/previews so visual companion artifacts do not pollute `git status`.
- Modify `templates/zotero_note.md.j2`
  - Replace rendered sections 0-11 with the approved 0-7 reading-thread layout.
  - Stop rendering metadata, evidence appendix, and improvement notes as dedicated note sections.
- Modify `src/zotero_paperread/note.py`
  - Update `REQUIRED_SECTIONS`.
  - Keep existing cleaning and gate-facing fields available in render context, even if some are not rendered.
- Modify `tests/test_note.py`
  - Update section-order tests and render assertions.
  - Add negative assertions for removed audit sections.
  - Keep evidence/lint-related tests focused on gate-visible data, not rendered appendices.
- Modify `tests/test_cli_note.py`
  - Update CLI render expectations to the 0-7 layout.
- Modify `skills/zotero-paper-summary/SKILL.md`
  - Update note-writing instructions so future analyses generate content for the new reading layout and treat metadata/evidence appendix/review notes as audit-only.
- Optionally modify `skills/zotero-batch-note-writing/SKILL.md` if it names the old 9/10/11 rendered sections.
- Modify `README.md`
  - Keep the current rendered note structure documentation aligned with the 0-7 reading-thread layout.
- Use existing run artifacts:
  - `runs/2026-06-18/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/summary.json`
  - same directory `item-details.json`, `review.json`, `note.md`, `note.html`, `gate-report.json`, `write-payload.json`

## Task 1: Ignore Visual Companion Runtime State

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Add the ignore-rule test by command**

Run before editing:

```bash
git check-ignore -q .superpowers/brainstorm/example || echo "not ignored"
```

Expected before implementation:

```text
not ignored
```

- [ ] **Step 2: Add `.superpowers/` to `.gitignore`**

Add this line under `# Local outputs and previews`:

```gitignore
.superpowers/
```

- [ ] **Step 3: Verify ignore rule**

Run:

```bash
git check-ignore .superpowers/brainstorm/example
git status --short --branch --untracked-files=all
```

Expected:

```text
.superpowers/brainstorm/example
```

`git status` should no longer show `.superpowers/brainstorm/...` files.

## Task 2: Write Failing Renderer Tests for the New Layout

**Files:**
- Modify: `tests/test_note.py`

- [ ] **Step 1: Add tests for section structure and removed audit sections**

Add or update tests to express this behavior:

```python
def test_render_note_uses_reading_thread_sections_without_audit_appendices() -> None:
    note = render_note(METADATA, {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS}, generated_date="2026-06-18")

    expected_sections = [
        "## 0. 阅读结论",
        "## 1. 论文主张",
        "## 2. 方法与设计",
        "## 3. 结果可信度",
        "## 4. 图表导读",
        "## 5. 边界与机会",
        "## 6. 我能怎么用",
        "## 7. 术语与检索",
    ]
    for section in expected_sections:
        assert section in note

    assert "## 9. 元数据" not in note
    assert "## 10. 证据链附录" not in note
    assert "## 11. 补充优化记录" not in note
    assert note.index("## 0. 阅读结论") < note.index("## 1. 论文主张")
    assert note.index("## 3. 结果可信度") < note.index("## 4. 图表导读")
    assert note.index("## 7. 术语与检索") < note.index("---\n\nTags: codex-summary, paper-summary")
```

Update existing tests that refer to old sections instead of keeping both old and new names.

- [ ] **Step 2: Run the targeted renderer tests and verify RED**

Run:

```bash
uv run pytest tests/test_note.py::test_render_note_uses_reading_thread_sections_without_audit_appendices -q
```

Expected before implementation: FAIL because the current template still renders old section names and audit appendices.

## Task 3: Implement the New Template and Section Validation

**Files:**
- Modify: `templates/zotero_note.md.j2`
- Modify: `src/zotero_paperread/note.py`

- [ ] **Step 1: Update `REQUIRED_SECTIONS`**

Replace the current section list with:

```python
REQUIRED_SECTIONS = [
    "0. 阅读结论",
    "1. 论文主张",
    "2. 方法与设计",
    "3. 结果可信度",
    "4. 图表导读",
    "5. 边界与机会",
    "6. 我能怎么用",
    "7. 术语与检索",
]
```

- [ ] **Step 2: Rewrite `templates/zotero_note.md.j2` headings and grouping**

Use this structure:

```markdown
## 0. 阅读结论
...
## 1. 论文主张
...
## 2. 方法与设计
...
## 3. 结果可信度
...
## 4. 图表导读
...
## 5. 边界与机会
...
## 6. 我能怎么用
...
## 7. 术语与检索
...
---

Tags: {{ note_labels | join(', ') }}
```

Keep field rendering from the old template, but move it into the approved sections:

- `recommended_sections` and `recommended_figures` stay in `0. 阅读结论`.
- `background_problem`, `existing_gap`, `paper_entry_point`, `one_sentence_summary`, and `abstract_translation` move under `1. 论文主张`.
- `method_overview`, `method_modules`, `workflow_steps`, and `technical_details` move under `2. 方法与设计`.
- `key_results_table`, `baseline_or_comparison`, `result_evidence_notes`, and `evidence_quality_summary` move under `3. 结果可信度`.
- `figure_overview` and `key_figures` stay under `4. 图表导读`.
- `author_stated_limitations`, `inferred_limits`, `applicability_limits`, and `potential_gaps` move under `5. 边界与机会`.
- `transferable_insight`, `workflow_lessons`, and `follow_up_questions` move under `6. 我能怎么用`.
- `concept_cards`, `follow_up_keywords`, and `note_labels` render under `7. 术语与检索` plus trailing Tags.

Do not render a dedicated metadata table, evidence appendix, or improvement notes.

- [ ] **Step 3: Run targeted renderer test and verify GREEN**

Run:

```bash
uv run pytest tests/test_note.py::test_render_note_uses_reading_thread_sections_without_audit_appendices -q
```

Expected: PASS.

## Task 4: Update Existing Note and CLI Tests

**Files:**
- Modify: `tests/test_note.py`
- Modify: `tests/test_cli_note.py`

- [ ] **Step 1: Find old section assertions**

Run:

```bash
rg -n "9\\. 元数据|10\\. 证据链附录|11\\. 补充优化记录|0\\. 速读决策|1\\. 论文核心|2\\. 方法怎么做|3\\. 结果是否站得住|5\\. 局限、适用边界" tests/test_note.py tests/test_cli_note.py
```

- [ ] **Step 2: Update assertions to the new outline**

Replace old expected section names with:

```python
[
    "## 0. 阅读结论",
    "## 1. 论文主张",
    "## 2. 方法与设计",
    "## 3. 结果可信度",
    "## 4. 图表导读",
    "## 5. 边界与机会",
    "## 6. 我能怎么用",
    "## 7. 术语与检索",
]
```

For tests that previously sliced the evidence appendix, change them to assert that evidence inputs are still accepted by render/gate helpers, or move the evidence-specific assertion to `tests/test_summary_lint.py` / existing gate tests if the behavior is not about rendering.

- [ ] **Step 3: Run note and CLI tests**

Run:

```bash
uv run pytest tests/test_note.py tests/test_cli_note.py -q
```

Expected: PASS.

## Task 5: Verify Gate Behavior Still Uses Evidence Summary

**Files:**
- Modify only if tests reveal drift:
  - `tests/test_summary_lint.py`
  - `tests/test_gate.py`
  - `src/zotero_paperread/summary_lint.py`
  - `src/zotero_paperread/note.py`

- [ ] **Step 1: Run existing gate/lint tests**

Run:

```bash
uv run pytest tests/test_summary_lint.py tests/test_gate.py -q
```

Expected: PASS.

- [ ] **Step 2: If failures show render expectations only, fix tests**

Do not weaken any evidence locator validation. The expected behavior remains:

```text
secondary contexts cannot be cited in evidence_summary
trusted evidence locators must cite context.md or figure_context.md
write_ready still requires trusted summary status
```

## Task 6: Sync Skill Instructions

**Files:**
- Modify: `skills/zotero-paper-summary/SKILL.md`
- Modify: `README.md`
- Inspect and modify if necessary: `skills/zotero-batch-note-writing/SKILL.md`

- [ ] **Step 1: Find old layout references**

Run:

```bash
rg -n "0\\. 速读决策|1\\. 论文核心|2\\. 方法怎么做|3\\. 结果是否站得住|5\\. 局限|8\\. 后续检索|9\\. 元数据|10\\. 证据链附录|11\\. 补充优化记录|evidence_summary" skills
```

- [ ] **Step 2: Update skill note-layout guidance**

Document the rendered note sections as:

```markdown
0. 阅读结论
1. 论文主张
2. 方法与设计
3. 结果可信度
4. 图表导读
5. 边界与机会
6. 我能怎么用
7. 术语与检索
```

Also state:

```markdown
metadata、evidence_summary、review_status、improvement_status、improvement_notes are audit-only for the rendered Zotero note. Keep them in JSON artifacts and gate inputs, but do not render them as dedicated note sections.
```

Update the README rendered-note paragraph with the same 0-7 section list and audit-only rule.

- [ ] **Step 3: Verify no stale rendered section guidance remains**

Run:

```bash
rg -n "9\\. 元数据|10\\. 证据链附录|11\\. 补充优化记录|0\\. 速读决策|1\\. 论文核心|2\\. 方法怎么做|3\\. 结果是否站得住" skills templates src tests
```

Expected: no stale rendered-layout references except historical docs/tests intentionally updated or comments explaining the migration.

## Task 7: Regenerate the Polyanion Note Locally With the New Layout

**Files/artifacts:**
- Modify generated ignored files under:
  - `runs/2026-06-18/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries/note.md`
  - same directory `note.html`, previews, gate report, write payload

- [ ] **Step 1: Apply review fields, then regenerate note and HTML from existing summary**

Use the existing run directory:

```bash
RUN_DIR="runs/2026-06-18/polyanion-stabilized-amorphous-halide-electrolytes-with-low-lithium-content-for-all-solid-state-lithium-batteries"
uv run zotero-paperread apply-review "$RUN_DIR/summary.json" "$RUN_DIR/review.json"
uv run zotero-paperread finalize-note \
  "$RUN_DIR/item-details.json" \
  "$RUN_DIR/summary.json" \
  --output "$RUN_DIR/note.md" \
  --html-output "$RUN_DIR/note.html"
```

Expected: command exits 0 and writes note files.

- [ ] **Step 2: Verify the local note layout**

Run:

```bash
rg -n "^## " "$RUN_DIR/note.md"
rg -n "## 9\\. 元数据|## 10\\. 证据链附录|## 11\\. 补充优化记录" "$RUN_DIR/note.md"; test $? -eq 1
```

Expected: only sections `0. 阅读结论` through `7. 术语与检索`; removed audit sections are absent.

- [ ] **Step 3: Run final local write gate**

Run the existing write-gate sequence for the regenerated note:

```bash
uv run zotero-paperread lint-summary "$RUN_DIR/summary.json"
uv run zotero-paperread validate-trusted-summary "$RUN_DIR/summary.json"
uv run zotero-paperread gate-run \
  "$RUN_DIR" \
  --paper-title "Polyanion-stabilized amorphous halide electrolytes with low lithium content for all-solid-state lithium batteries" \
  --generated-date "2026-06-18" \
  --output "$RUN_DIR/gate-report.json"
uv run zotero-paperread prepare-write-payload \
  "$RUN_DIR/gate-report.json" \
  --output "$RUN_DIR/write-payload.json"
```

Expected: `gate-report.json` status is `write_ready`; payload `contentLength` matches the regenerated HTML.

## Task 8: Full Verification

**Files:** no intended edits unless verification reveals failures.

- [ ] **Step 1: Run project tests**

Run:

```bash
uv run pytest
```

Expected: all tests pass.

- [ ] **Step 2: Run CLI smoke check**

Run:

```bash
uv run zotero-paperread --help
```

Expected: exits 0.

- [ ] **Step 3: Run PDF extraction smoke check**

Run:

```bash
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

Expected: exits 0 and writes extraction JSON.

- [ ] **Step 4: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

## Task 9: Code Review and Fix Pass

**Files:** inspect all changed files.

- [ ] **Step 1: Review diffs**

Run:

```bash
git diff -- .gitignore templates/zotero_note.md.j2 src/zotero_paperread/note.py tests/test_note.py tests/test_cli_note.py skills/zotero-paper-summary/SKILL.md skills/zotero-batch-note-writing/SKILL.md
```

Check for:

- stale section names
- weakened evidence gates
- accidental schema deletion
- duplicate rendering
- overbroad changes outside the requested layout redesign

- [ ] **Step 2: Fix findings**

If review finds issues, add or update tests first, then fix implementation, then rerun the relevant targeted tests.

## Task 10: Commit Implementation

**Files:** stage only intentional tracked changes.

- [ ] **Step 1: Confirm status**

Run:

```bash
git status --short --branch --untracked-files=all
```

Expected:

- tracked changes for implementation files
- ignored `.superpowers/` should not appear
- pre-existing untracked spec preview files may still appear unless intentionally handled separately

- [ ] **Step 2: Stage and commit**

Run:

```bash
git add .gitignore templates/zotero_note.md.j2 src/zotero_paperread/note.py tests/test_note.py tests/test_cli_note.py skills/zotero-paper-summary/SKILL.md
git add skills/zotero-batch-note-writing/SKILL.md || true
git commit -m "refactor: streamline zotero note reading layout"
```

Expected: commit succeeds.

## Task 11: Write the Updated Polyanion Note to Zotero

**Files/artifacts:**
- Use `RUN_DIR/note.html`
- Zotero parent item key: `CABS9KGA`
- Existing newly written note key from the previous run: `6UMI3FZ8`

- [ ] **Step 1: Confirm target and payload**

Run:

```bash
jq '{status, parentKey, trust_status, review_status}' "$RUN_DIR/gate-report.json"
jq '{action, parentKey, contentLength, tagCount: (.tags | length)}' "$RUN_DIR/write-payload.json"
```

Expected:

```json
{"status":"write_ready","parentKey":"CABS9KGA",...}
```

- [ ] **Step 2: Update the existing same-day Codex note**

Use Zotero MCP `write_note` with:

```json
{
  "action": "update",
  "noteKey": "6UMI3FZ8",
  "content": "<contents of RUN_DIR/note.html>"
}
```

This updates only the just-created 2026-06-18 note and does not overwrite the older 2026-05-06 note.

- [ ] **Step 3: Read back note details**

Use Zotero MCP `get_item_details` for `6UMI3FZ8`.

Expected:

- note exists
- tags are preserved
- title still starts with `[Codex Summary] Polyanion-stabilized amorphous halide electrolytes...`

## Self-Review

- Spec coverage: every spec requirement maps to a task.
- No placeholders: all commands, file paths, and expected outcomes are explicit.
- Type consistency: no new function signatures are introduced beyond updating constants/template behavior.
- TDD coverage: renderer behavior changes start with failing tests before production template/code edits.
- Safety: Zotero write occurs only after local gates and targets the same explicit same-day note key.
