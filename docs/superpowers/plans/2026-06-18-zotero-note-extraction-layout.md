# Zotero Note Extraction Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Implementation Status:** Completed and merged to `main` by 2026-06-23. Current behavior includes section-aware extraction, conservative table/value candidates, `section_context.md`, canonical evidence locator linting, and the later 0-7 reading-thread rendered note layout. Evidence/review appendices are retained in JSON artifacts and gates rather than rendered as dedicated Zotero note sections.

**Goal:** Implement the approved Zotero note extraction and layout design: additive section/table-aware PDF extraction, `section_context.md`, canonical evidence locator linting, and the two-layer Zotero child note layout.

**Architecture:** Keep PDF structure detection deterministic and conservative inside `src/zotero_paperread/pdf_extract.py`; keep artifact wiring inside `src/zotero_paperread/workflow.py`; keep semantic judgement in the Codex-generated `summary.json` and review pass. Rendering remains permissive for old summaries, while lint and write gates continue to reject untrusted evidence sources before Zotero writes.

**Tech Stack:** Python 3.13 via `uv`, PyMuPDF (`fitz`), Jinja2 with `StrictUndefined`, Typer CLI, pytest, Markdown-it for Zotero-ready HTML.

---

## Constraints

- Do not write to Zotero during implementation or verification.
- Do not change Zotero write behavior; `note.html` remains the write payload.
- Do not introduce ResearchWiki directories such as `wiki/`, `synthesis/`, or `memory/`.
- Do not introduce new runtime dependencies.
- Use `uv run` for all commands.
- Keep all new `summary.json` fields optional.
- Trusted evidence locators must remain limited to `context.md ...` and `figure_context.md ...`.
- Preserve the existing Chinese HTML/Markdown spec preview files if they are untracked in the parent checkout.

## File Structure

- Modify `src/zotero_paperread/pdf_extract.py`
  - Adds page records, conservative section records, and table/value candidates to `extract.json`.
- Modify `src/zotero_paperread/workflow.py`
  - Writes `section_context.md`, returns `section_context_md`, and records it in `run.json` when a manifest exists.
- Modify `src/zotero_paperread/note.py`
  - Cleans optional new summary fields and updates required note section names.
- Modify `templates/zotero_note.md.j2`
  - Renders the two-layer note layout.
- Modify `src/zotero_paperread/summary_lint.py`
  - Enforces canonical trusted locator forms and structured limitation source types.
- Modify tests:
  - `tests/test_pdf_extract.py`
  - `tests/test_workflow.py`
  - `tests/test_cli_prepare_item.py`
  - `tests/test_note.py`
  - `tests/test_cli_note.py`
  - `tests/test_summary_lint.py`
- Modify docs:
  - `README.md`
  - `skills/zotero-paper-summary/SKILL.md`

---

### Task 0: Prepare Isolated Implementation Workspace

**Files:**
- Already modified in parent checkout: `.gitignore`
- Already created in parent checkout: `docs/superpowers/plans/2026-06-18-zotero-note-extraction-layout.md`

- [ ] **Step 1: Verify parent checkout state**

Run:

```bash
git status --short --branch --untracked-files=all
```

Expected:

```text
## main...origin/main [ahead <N>]
 M .gitignore
?? docs/superpowers/plans/2026-06-18-zotero-note-extraction-layout.md
?? docs/superpowers/specs/2026-06-18-zotero-note-extraction-layout-design.zh.html
?? docs/superpowers/specs/2026-06-18-zotero-note-extraction-layout-design.zh.md
```

Only `.gitignore` and this plan are setup changes for this implementation. The Chinese spec preview files must remain untracked unless the user separately asks to commit them.

- [ ] **Step 2: Verify setup diff has no whitespace errors**

Run:

```bash
git diff --check -- .gitignore docs/superpowers/plans/2026-06-18-zotero-note-extraction-layout.md
```

Expected:

```text
```

- [ ] **Step 3: Commit setup changes locally**

Run:

```bash
git add .gitignore docs/superpowers/plans/2026-06-18-zotero-note-extraction-layout.md
git commit -m "docs: plan zotero extraction note layout"
```

Expected:

```text
[main <sha>] docs: plan zotero extraction note layout
```

- [ ] **Step 4: Create an isolated worktree**

Run:

```bash
git check-ignore -q .worktrees
git worktree add .worktrees/zotero-note-extraction-layout -b codex/zotero-note-extraction-layout
```

Expected:

```text
Preparing worktree (new branch 'codex/zotero-note-extraction-layout')
HEAD is now at <sha> docs: plan zotero extraction note layout
```

- [ ] **Step 5: Verify baseline in the worktree**

Run:

```bash
cd .worktrees/zotero-note-extraction-layout
uv run pytest tests/test_pdf_extract.py tests/test_workflow.py tests/test_note.py tests/test_summary_lint.py -q
uv run zotero-paperread --help
```

Expected: all selected tests pass, and the CLI prints the Typer help without writing Zotero.

---

### Task 1: Add Structured PDF Extraction

**Files:**
- Modify `tests/test_pdf_extract.py`
- Modify `src/zotero_paperread/pdf_extract.py`

- [ ] **Step 1: Write failing tests for page, section, and table candidates**

Append these tests to `tests/test_pdf_extract.py`:

```python
def test_extract_pdf_returns_page_records_and_sections(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(
        pdf_path,
        [
            "Abstract\nThis paper reports ionic conductivity of a solid electrolyte.\n"
            "1 Introduction\nBattery interfaces need better models.",
            "2 Methods\nWe trained a model with DFT calculations.\n"
            "Computational details\nThe cutoff was tested.",
            "3 Results and discussion\nTable 1 Conductivity 1.2 mS cm-1 baseline 0.5 mS cm-1.\n"
            "Activation energy was 0.21 eV.",
        ],
    )

    result = extract_pdf(pdf_path)

    assert [page["page"] for page in result["pages"]] == [1, 2, 3]
    assert result["pages"][0]["char_count"] > 0
    assert result["pages"][0]["warnings"] == []
    assert any(section["kind"] == "abstract" and section["start_page"] == 1 for section in result["sections"])
    assert any(section["kind"] == "methods" and section["start_page"] == 2 for section in result["sections"])
    assert any(section["kind"] == "computational" and section["start_page"] == 2 for section in result["sections"])
    assert any(section["kind"] == "results" and section["start_page"] == 3 for section in result["sections"])
    assert all(section["locator"].startswith("context.md page ") for section in result["sections"])
```

```python
def test_extract_pdf_emits_conservative_table_value_candidates(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(
        pdf_path,
        [
            "Abstract\nA paper.",
            "Results\nTable 2 Baseline RMSE 0.25 MAE 0.13 R2 0.91 speedup 10x.",
        ],
    )

    result = extract_pdf(pdf_path)

    assert result["table_candidates"]
    candidate = result["table_candidates"][0]
    assert candidate["page"] == 2
    assert candidate["section"] == "Results"
    assert candidate["confidence"] in {"high", "medium"}
    assert "baseline" in candidate["signals"]
    assert "rmse" in candidate["signals"]
    assert candidate["locator"] == "context.md page 2 section Results table_candidate 1"
```

```python
def test_extract_pdf_page_records_warn_for_empty_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["", "Methods\nEnough text for extraction."])

    result = extract_pdf(pdf_path)

    assert result["pages"][0]["page"] == 1
    assert "empty_page_text" in result["pages"][0]["warnings"]
    assert result["pages"][0]["char_count"] == 0
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_pdf_extract.py -q
```

Expected: the new tests fail because `pages`, `sections`, `table_candidates`, and `locator` fields do not exist yet.

- [ ] **Step 3: Implement minimal structured extraction**

In `src/zotero_paperread/pdf_extract.py`:

1. Keep existing top-level keys: `pdf_path`, `page_count`, `extracted_pages`, `text`, `warnings`.
2. Add helper constants and functions:

```python
SECTION_KIND_BY_HEADING = {
    "abstract": "abstract",
    "introduction": "introduction",
    "background": "background",
    "methods": "methods",
    "materials and methods": "methods",
    "experimental": "experimental",
    "experimental section": "experimental",
    "computational details": "computational",
    "dft calculations": "computational",
    "results": "results",
    "results and discussion": "results",
    "discussion": "discussion",
    "conclusion": "conclusion",
    "conclusions": "conclusion",
    "limitations": "limitations",
    "references": "references",
    "acknowledgements": "acknowledgements",
    "acknowledgments": "acknowledgements",
    "electrochemical performance": "results",
    "ionic conductivity": "results",
    "characterization": "results",
}
```

```python
TABLE_VALUE_SIGNALS = (
    "accuracy",
    "mae",
    "rmse",
    "r2",
    "speedup",
    "baseline",
    "ablation",
    "conductivity",
    "ionic conductivity",
    "activation energy",
    "diffusion barrier",
    "capacity",
    "cycle life",
    "rate performance",
    "energy density",
    "voltage",
    "bandgap",
    "formation energy",
    "ehull",
)
```

3. Add conservative heading normalization:

```python
def normalize_heading_line(line: str) -> str:
    text = re.sub(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVX]+\.?)\s+", "", line.strip(), flags=re.IGNORECASE)
    text = re.sub(r"[:.\s]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.lower()
```

4. Build page records in the existing page loop and keep combined text unchanged:

```python
page_record = {
    "page": index + 1,
    "text": text,
    "char_count": len(text),
    "warnings": page_warnings,
}
```

5. Build sections by scanning standalone lines, aggregating text from each heading until the next heading, and emitting:

```python
{
    "kind": kind,
    "title": title,
    "start_page": start_page,
    "end_page": end_page,
    "text": section_text,
    "confidence": confidence,
    "locator": f"context.md page {start_page} section {title}",
}
```

6. Build table candidates by scanning section/page text blocks that contain a numeric token and at least one signal, emitting:

```python
{
    "page": page_number,
    "section": section_title,
    "text": candidate_text,
    "signals": signals,
    "confidence": confidence,
    "locator": f"context.md page {page_number} section {section_title} table_candidate {index}",
}
```

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_pdf_extract.py -q
```

Expected: all `tests/test_pdf_extract.py` tests pass.

---

### Task 2: Write `section_context.md` From Prepared Bundles

**Files:**
- Modify `tests/test_workflow.py`
- Modify `tests/test_cli_prepare_item.py`
- Modify `src/zotero_paperread/workflow.py`

- [ ] **Step 1: Write failing workflow tests**

Add this assertion block to `test_prepare_item_bundle_writes_metadata_extract_and_context` after `context` is read:

```python
section_context = Path(result["section_context_md"]).read_text(encoding="utf-8")
assert Path(result["section_context_md"]).exists()
assert "# Section Context" in section_context
assert "## Extraction Summary" in section_context
assert "Section Count:" in section_context
assert "Table Candidate Count:" in section_context
assert "Locator: context.md page" in section_context
assert "## Table / Value Candidates" in section_context
```

Add this assertion to `test_prepare_item_bundle_updates_existing_run_manifest`:

```python
assert manifest["section_context_md"] == result["section_context_md"]
```

Add this assertion to `tests/test_cli_prepare_item.py::test_prepare_item_command_outputs_bundle_paths`:

```python
assert Path(payload["section_context_md"]).exists()
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_workflow.py tests/test_cli_prepare_item.py -q
```

Expected: failures mention missing `section_context_md`.

- [ ] **Step 3: Implement section context builder and manifest wiring**

In `src/zotero_paperread/workflow.py`, add:

```python
def build_section_context_markdown(metadata: dict[str, Any], extract: dict[str, Any]) -> str:
    pages = extract.get("pages", []) if isinstance(extract.get("pages"), list) else []
    sections = extract.get("sections", []) if isinstance(extract.get("sections"), list) else []
    table_candidates = (
        extract.get("table_candidates", []) if isinstance(extract.get("table_candidates"), list) else []
    )
    section_blocks = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_blocks.append(
            "\n".join(
                [
                    f"### {section.get('title', 'Unknown')}",
                    f"- Kind: {section.get('kind', 'unknown')}",
                    f"- Pages: {section.get('start_page', '')}-{section.get('end_page', '')}",
                    f"- Confidence: {section.get('confidence', 'unknown')}",
                    f"- Locator: {section.get('locator', '')}",
                    "",
                    str(section.get("text", "")).strip() or "_No section text available._",
                ]
            )
        )
    candidate_blocks = []
    for index, candidate in enumerate(table_candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        signals = candidate.get("signals", [])
        signal_text = ", ".join(str(signal) for signal in signals) if isinstance(signals, list) else ""
        candidate_blocks.append(
            "\n".join(
                [
                    f"### Candidate {index}",
                    f"- Locator: {candidate.get('locator', '')}",
                    f"- Confidence: {candidate.get('confidence', 'unknown')}",
                    f"- Signals: {signal_text}",
                    "",
                    str(candidate.get("text", "")).strip() or "_No candidate text available._",
                ]
            )
        )
    sections_body = "\n\n".join(section_blocks) if section_blocks else "_No sections detected._"
    candidates_body = "\n\n".join(candidate_blocks) if candidate_blocks else "_No table/value candidates detected._"
    return (
        "# Section Context\n\n"
        "## Extraction Summary\n\n"
        f"- PDF Path: {extract.get('pdf_path', '')}\n"
        f"- Title: {metadata.get('title', '')}\n"
        f"- Page Count: {extract.get('page_count', 0)}\n"
        f"- Extracted Pages: {extract.get('extracted_pages', 0)}\n"
        f"- Page Record Count: {len(pages)}\n"
        f"- Section Count: {len(section_blocks)}\n"
        f"- Table Candidate Count: {len(candidate_blocks)}\n\n"
        "## Sections\n\n"
        f"{sections_body}\n\n"
        "## Table / Value Candidates\n\n"
        f"{candidates_body}\n"
    )
```

Wire `section_context_path = bundle_dir / "section_context.md"` in `prepare_item_bundle`, write it after `context.md`, return `"section_context_md": str(section_context_path)`, and include it in the manifest update.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_workflow.py tests/test_cli_prepare_item.py -q
```

Expected: all selected tests pass.

---

### Task 3: Render The Two-Layer Note Layout

**Files:**
- Modify `tests/test_note.py`
- Modify `tests/test_cli_note.py`
- Modify `src/zotero_paperread/note.py`
- Modify `templates/zotero_note.md.j2`

- [ ] **Step 1: Write failing note-rendering tests**

In `tests/test_note.py`, update required section expectations to:

```python
expected_sections = [
    "## 0. 速读决策",
    "## 1. 论文核心",
    "## 2. 方法怎么做",
    "## 3. 结果是否站得住",
    "## 4. 图表导读",
    "## 5. 局限、适用边界与潜在 gap",
    "## 6. 可迁移启发",
    "## 7. 术语与概念卡片",
    "## 8. 后续检索关键词",
    "## 9. 元数据",
    "## 10. 证据链附录",
    "## 11. 补充优化记录",
]
```

Add a test that renders new optional fields:

```python
def test_render_note_renders_recommendations_result_evidence_and_gap_fields() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        **LEARNING_FIELDS,
        "recommended_sections": [
            {
                "section": "Methods",
                "locator": "context.md page 2 section Methods",
                "reason": "Best source for model design.",
            }
        ],
        "recommended_figures": [
            {
                "figure_id": "fig_p1_1",
                "locator": "figure_context.md fig_p1_1",
                "reason": "Shows the overall workflow.",
            }
        ],
        "baseline_or_comparison": [
            {
                "target": "DFT baseline",
                "result": "Lower MAE on formation energy prediction.",
                "locator": "context.md page 3 section Results table_candidate 1",
            }
        ],
        "result_evidence_notes": [
            {
                "result": "Conductivity improved.",
                "evidence": "Reported with numeric comparison.",
                "locator": "context.md page 3 section Results table_candidate 1",
                "confidence": "medium",
            }
        ],
        "author_stated_limitations": [
            {
                "text": "The authors evaluate one material family.",
                "locator": "context.md page 8 section Discussion",
                "source_type": "author_stated",
            }
        ],
        "inferred_limits": [
            {
                "text": "Transfer to sulfide solid electrolytes is not established.",
                "basis": "The experiments cover oxide examples only.",
                "locator": "context.md page 6 section Results",
                "source_type": "inferred",
            }
        ],
        "potential_gaps": [
            {
                "text": "Reactive battery interfaces remain open.",
                "basis": "The paper validates non-reactive examples.",
                "locator": "context.md page 7 section Results",
                "uncertainty": "AI inference",
            }
        ],
        "evidence_quality_summary": "Full text and figure context are available; table candidates are medium-confidence.",
    }

    rendered = render_note(METADATA, summary, generated_date="2026-06-18")

    assert "## 0. 速读决策" in rendered
    assert "### 推荐先读章节" in rendered
    assert "Methods: Best source for model design. (context.md page 2 section Methods)" in rendered
    assert "fig_p1_1: Shows the overall workflow. (figure_context.md fig_p1_1)" in rendered
    assert "## 3. 结果是否站得住" in rendered
    assert "DFT baseline" in rendered
    assert "Conductivity improved." in rendered
    assert "Full text and figure context are available" in rendered
    assert "### 作者明示局限" in rendered
    assert "The authors evaluate one material family. (context.md page 8 section Discussion)" in rendered
    assert "### Codex 推断限制" in rendered
    assert "Transfer to sulfide solid electrolytes is not established." in rendered
    assert "basis: The experiments cover oxide examples only." in rendered
    assert "### 潜在 gap / 后续问题" in rendered
    assert "Reactive battery interfaces remain open." in rendered
    assert "uncertainty: AI inference" in rendered
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_note.py tests/test_cli_note.py -q
```

Expected: failures mention old section names and missing rendered new fields.

- [ ] **Step 3: Implement note cleaners and section names**

In `src/zotero_paperread/note.py`:

1. Replace `REQUIRED_SECTIONS` with the new section names from Step 1.
2. Add cleaning helpers:

```python
def clean_recommendations(value: Any, *, limit: int = 5) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for item in safe_list(value)[:limit]:
        if not isinstance(item, dict):
            continue
        label = safe_text(item.get("section") or item.get("figure_id") or item.get("target"), "")
        reason = safe_text(item.get("reason") or item.get("result"), "")
        locator = safe_text(item.get("locator"), "")
        if label != "unknown" and reason != "unknown":
            cleaned.append({"label": label, "reason": reason, "locator": locator})
    return cleaned
```

```python
def clean_result_evidence_notes(value: Any) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for item in safe_list(value)[:8]:
        if not isinstance(item, dict):
            continue
        result = safe_text(item.get("result"), "")
        if result == "unknown":
            continue
        cleaned.append(
            {
                "result": result,
                "evidence": safe_text(item.get("evidence"), ""),
                "locator": safe_text(item.get("locator"), ""),
                "confidence": safe_text(item.get("confidence"), "unknown"),
            }
        )
    return cleaned
```

```python
def clean_limitation_objects(value: Any, *, expected_source_type: str | None = None) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for item in safe_list(value)[:8]:
        if isinstance(item, str):
            text = safe_text(item)
            if text != "unknown":
                cleaned.append({"text": text, "basis": "", "locator": "", "source_type": expected_source_type or ""})
            continue
        if not isinstance(item, dict):
            continue
        text = safe_text(item.get("text"), "")
        if text == "unknown":
            continue
        cleaned.append(
            {
                "text": text,
                "basis": optional_text(item.get("basis")),
                "locator": optional_text(item.get("locator")),
                "source_type": optional_text(item.get("source_type")) or (expected_source_type or ""),
                "uncertainty": optional_text(item.get("uncertainty")),
            }
        )
    return cleaned
```

3. Add these fields to the render context:

```python
"recommended_sections": clean_recommendations(summary.get("recommended_sections", [])),
"recommended_figures": clean_recommendations(summary.get("recommended_figures", [])),
"baseline_or_comparison": clean_recommendations(summary.get("baseline_or_comparison", []), limit=8),
"result_evidence_notes": clean_result_evidence_notes(summary.get("result_evidence_notes", [])),
"author_stated_limitations": clean_limitation_objects(
    summary.get("author_stated_limitations", []), expected_source_type="author_stated"
),
"inferred_limits": clean_limitation_objects(summary.get("inferred_limits", []), expected_source_type="inferred"),
"potential_gaps": clean_limitation_objects(summary.get("potential_gaps", [])),
"evidence_quality_summary": optional_text(summary.get("evidence_quality_summary")),
```

- [ ] **Step 4: Replace the template with the approved two-layer structure**

In `templates/zotero_note.md.j2`, keep the title and trailing `Tags:` behavior, but render top-level sections in this order:

```md
## 0. 速读决策
## 1. 论文核心
## 2. 方法怎么做
## 3. 结果是否站得住
## 4. 图表导读
## 5. 局限、适用边界与潜在 gap
## 6. 可迁移启发
## 7. 术语与概念卡片
## 8. 后续检索关键词
## 9. 元数据
## 10. 证据链附录
## 11. 补充优化记录
```

Use compact bullets in section 0 before any large table:

```md
- **30 秒结论**: {{ tldr or one_sentence_summary }}
- **阅读决策**: {{ reading_decision }}
- **与我的研究关系**: {{ relevance_to_user }}
- **可信状态**: {{ trust_status }}
- **主要风险**: {{ main_risk_short }}
```

Render `recommended_sections`, `recommended_figures`, `baseline_or_comparison`, `result_evidence_notes`, `author_stated_limitations`, `inferred_limits`, `potential_gaps`, and `evidence_quality_summary` only when non-empty; otherwise render `- none` in their subsection.

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_note.py tests/test_cli_note.py -q
```

Expected: all selected note tests pass.

---

### Task 4: Enforce Canonical Evidence Locator Lint Rules

**Files:**
- Modify `tests/test_summary_lint.py`
- Modify `src/zotero_paperread/summary_lint.py`

- [ ] **Step 1: Write failing lint tests**

Append these tests to `tests/test_summary_lint.py`:

```python
def test_lint_summary_flags_section_context_locator() -> None:
    summary = {
        "evidence_summary": [
            {
                "claim": "Claim",
                "evidence": [{"locator": "section_context.md section Methods", "summary": "Not canonical"}],
            }
        ],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "malformed_trusted_evidence_locator" for issue in issues)
```

```python
def test_lint_summary_allows_canonical_context_and_figure_locators() -> None:
    summary = {
        "evidence_summary": [
            {
                "claim": "Claim",
                "evidence": [
                    {"locator": "context.md page 3 section Methods", "summary": "Text"},
                    {"locator": "context.md page 6 section Results table_candidate 1", "summary": "Table hint"},
                    {"locator": "figure_context.md fig_p4_1", "summary": "Figure"},
                ],
            }
        ],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert not any(issue["code"] == "malformed_trusted_evidence_locator" for issue in issues)
```

```python
def test_lint_summary_flags_structured_limitation_source_type_mismatch() -> None:
    summary = {
        "author_stated_limitations": [{"text": "Claimed limit.", "source_type": "inferred"}],
        "inferred_limits": [{"text": "Reader limit.", "source_type": "author_stated"}],
        "evidence_summary": [],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    codes = [issue["code"] for issue in issues]
    assert "author_stated_limitation_source_type_invalid" in codes
    assert "inferred_limit_source_type_invalid" in codes
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_summary_lint.py -q
```

Expected: new tests fail because the lint codes are not implemented.

- [ ] **Step 3: Implement lint rules**

In `src/zotero_paperread/summary_lint.py`, add:

```python
CANONICAL_CONTEXT_LOCATOR = re.compile(
    r"^context\.md page \d+(?: section [A-Za-z0-9][A-Za-z0-9 /&().,+:_-]*)?(?: table_candidate \d+)?$"
)
CANONICAL_FIGURE_LOCATOR = re.compile(r"^figure_context\.md [A-Za-z0-9_.:-]+$")
```

Add helper:

```python
def is_canonical_trusted_locator(locator: str) -> bool:
    return bool(CANONICAL_CONTEXT_LOCATOR.match(locator) or CANONICAL_FIGURE_LOCATOR.match(locator))
```

During `evidence_summary` lint, after secondary-source checks:

```python
if locator and not is_canonical_trusted_locator(locator):
    issues.append(
        {
            "code": "malformed_trusted_evidence_locator",
            "message": f"evidence_summary[{claim_index}].evidence[{evidence_index}] has malformed trusted locator",
        }
    )
```

Add object-form source type checks:

```python
for index, item in enumerate(summary.get("author_stated_limitations", []) or []):
    if isinstance(item, dict) and item.get("source_type") not in {"author_stated", None, ""}:
        issues.append(
            {
                "code": "author_stated_limitation_source_type_invalid",
                "message": f"author_stated_limitations[{index}] source_type must be author_stated",
            }
        )
for index, item in enumerate(summary.get("inferred_limits", []) or []):
    if isinstance(item, dict) and item.get("source_type") not in {"inferred", None, ""}:
        issues.append(
            {
                "code": "inferred_limit_source_type_invalid",
                "message": f"inferred_limits[{index}] source_type must be inferred",
            }
        )
```

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_summary_lint.py -q
```

Expected: all summary lint tests pass.

---

### Task 5: Update README And Zotero Skill Instructions

**Files:**
- Modify `README.md`
- Modify `skills/zotero-paper-summary/SKILL.md`
- Modify `tests/test_default_workflow_docs.py`

- [ ] **Step 1: Write failing documentation tests**

In `tests/test_default_workflow_docs.py`, add assertions that both docs mention:

```python
"section_context.md"
"context.md page 3 section Methods"
"context.md page 6 section Results table_candidate 1"
"author_stated_limitations"
"inferred_limits"
"potential_gaps"
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: at least one assertion fails until docs are updated.

- [ ] **Step 3: Update README**

Update README workflow text so it says:

```md
`prepare-item` writes `section_context.md` when structured extraction data is available. Codex should read it as a navigation aid for sections and table/value candidates, but final `evidence_summary` locators must still cite canonical sources such as `context.md page 3 section Methods`, `context.md page 6 section Results table_candidate 1`, or `figure_context.md fig_p4_1`.
```

Update note-layout docs so it says the rendered note opens with `## 0. 速读决策` and separates `author_stated_limitations`, `inferred_limits`, and `potential_gaps`.

- [ ] **Step 4: Update Zotero paper summary skill**

Update `skills/zotero-paper-summary/SKILL.md` so the bundle artifact list includes `section_context.md`, and the summary JSON example includes optional:

```json
{
  "recommended_sections": [],
  "recommended_figures": [],
  "baseline_or_comparison": [],
  "result_evidence_notes": [],
  "author_stated_limitations": [],
  "inferred_limits": [],
  "potential_gaps": [],
  "evidence_quality_summary": ""
}
```

State explicitly that `section_context.md` is a reading aid, not a canonical evidence source.

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: documentation tests pass.

---

### Task 6: Final Verification And Commit

**Files:**
- All modified implementation, tests, docs.

- [ ] **Step 1: Run focused phase verification**

Run:

```bash
uv run pytest tests/test_pdf_extract.py tests/test_workflow.py -q
uv run pytest tests/test_note.py tests/test_cli_note.py tests/test_summary_lint.py -q
uv run pytest tests/test_default_workflow_docs.py tests/test_cli_prepare_item.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run full project verification**

Run:

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

Expected: full pytest passes, CLI help exits 0, and extraction JSON is written under `/tmp` without Zotero writes.

- [ ] **Step 3: Inspect final diff**

Run:

```bash
git status --short --branch --untracked-files=all
git diff --check
git diff --stat
```

Expected: implementation branch contains only planned files. `.worktrees/` must not appear as tracked content.

- [ ] **Step 4: Commit implementation locally**

Run:

```bash
git add src/zotero_paperread/pdf_extract.py src/zotero_paperread/workflow.py src/zotero_paperread/note.py src/zotero_paperread/summary_lint.py templates/zotero_note.md.j2 tests/test_pdf_extract.py tests/test_workflow.py tests/test_cli_prepare_item.py tests/test_note.py tests/test_cli_note.py tests/test_summary_lint.py tests/test_default_workflow_docs.py README.md skills/zotero-paper-summary/SKILL.md
git commit -m "feat: add structured extraction and two-layer notes"
```

Expected:

```text
[codex/zotero-note-extraction-layout <sha>] feat: add structured extraction and two-layer notes
```

- [ ] **Step 5: Report integration state**

Report:

- Worktree path.
- Branch name.
- Commit SHA.
- Verification commands and results.
- Reminder that no Zotero writes were performed.
