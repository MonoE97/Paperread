# Zotero Paper Figures V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a V2 figure workflow that opportunistically uses arXiv source assets when available, otherwise extracts figure candidates from Zotero-linked PDFs, selects the most important figures for scientific understanding, folds figure analysis into the generated Zotero paper note, and operationalizes the workflow with a skill-first one-click entry plus project-local run directories.

**Architecture:** Keep the V1 split, but lock the implementation stack. `zotero_paperread` now uses a source-first figure pipeline implemented on top of PyMuPDF only: when a Zotero item can be mapped to an arXiv ID, try arXiv source assets first, then render source-side figure PDFs with PyMuPDF, then run deterministic PDF caption-and-geometry extraction with PyMuPDF, and only then gated OCR fallback plus embedded-image supplementation when coverage or confidence is still insufficient. The Codex skill stays responsible for Zotero lookup and scientific interpretation, while the Python CLI remains deterministic and Zotero-free. Add a small run-management layer so skill-driven summaries land under `runs/<date>/<paper-slug>/` instead of ad hoc `/tmp` bundles. Do not make Zotero inline-image compatibility a blocker for V2; optimize for figure selection quality and figure analysis depth first.

**Tech Stack:** Python 3.13, `uv`, Typer, Rich, PyMuPDF 1.27.2.2 (verified in both the main repo and the active worktree), Jinja2, pytest, `tarfile`, `tempfile`, `urllib.request`, Codex skill orchestration, Zotero MCP, optional project-local OCR fallback via `uv add` (no system OCR binaries assumed).

---

## Stack Decision Lock

- PyMuPDF is already installed and importable in this project environment; no installation work is needed before V2 implementation.
- V2 does **not** introduce Docling, Marker, `pdfplumber`, `pypdf`, or other secondary PDF parsers into the extraction path.
- Text extraction for summary context and figure caption reasoning stays on the existing PyMuPDF path so the project keeps one parser, one fixture strategy, and one debugging surface.
- Any OCR addition remains an optional adapter behind the existing PyMuPDF-based extraction flow; OCR is not allowed to become a second primary document parser.

## Plan Review

- The V1 plan solved text extraction, note rendering, and Zotero write gating, but it had no explicit figure pipeline.
- The naive next step would be "extract all embedded images", but that is the wrong default for research papers because many key figures are vector graphics, multi-panel layouts, or page content that is not stored as a standalone raster image.
- `evil-read-arxiv` confirms a better ordering: **source-first when arXiv assets exist, PDF reasoning only when they do not**.
- V2 should therefore use a **multi-source figure pipeline**:
  - resolve an arXiv ID from Zotero metadata or attachment hints when possible
  - download the arXiv source package safely and collect source images from common figure directories
  - render source-side figure PDFs to PNGs before touching the paper PDF
  - for local-only PDFs or missing source assets, run deterministic caption-anchored crop extraction
  - use OCR only for low-confidence or missing-caption cases
  - use raw embedded-image extraction only as a late supplement, not as the main path
- Keep inline-image-in-Zotero as a later compatibility spike. It is useful, but it is not the main research-value path.

## Execution Reality Update

- Task 1 was underestimated. Real execution showed that caption extraction is still brittle around prose references like `Figure 1 shows ...`, wrapped uppercase scientific continuations like `SEM images ...`, and separator-line artifacts that can steal the crop anchor.
- The deterministic extractor is still the right primary path because it preserves page geometry and works well on born-digital PDFs, but it is no longer sufficient as the only plan assumption.
- `evil-read-arxiv` clarified what is actually worth borrowing: not its PDF fallback, but its **source-first acquisition order**, source/PDF provenance tagging, safe tar extraction, and source-side figure PDF rendering.
- The tool-choice question is closed for V2: keep PyMuPDF as the only core PDF engine because it is already installed, already used by the repository, and best aligned with the current `uv` + CLI + pytest workflow.
- OCR is therefore promoted from "not in V2" to a **gated fallback path**. It should only run when the deterministic path yields low-confidence crops, missing captions, or obviously malformed regions.
- Current machine state matters: `tesseract`, `ocrmypdf`, `pdftoppm`, and `mutool` are not installed locally. Any OCR addition must be project-local via `uv add`; do not assume system tools exist.

## Plan Review Update

- The figure-extraction core is no longer the only missing piece. Real usage showed that V2 also needs an operational layer: a canonical one-click skill entry, stable run directories inside the repository, and a repeatable place to inspect generated artifacts.
- `summarize-zotero-title` must be treated as a **Codex skill invocation**, not a Python CLI subcommand. Zotero lookup remains in Codex because `zotero-mcp` is already the verified boundary for live library access.
- A plain `runs.py` helper is not sufficient on its own. The skill needs a deterministic tool-facing wrapper, so add a small CLI helper such as `create-run` that returns the chosen run directory and writes a `run.json` manifest.
- `runs/<date>/<paper-slug>/` must be collision-safe. Repeated same-day runs for the same paper should produce `paper-slug-2`, `paper-slug-3`, and so on instead of overwriting the old bundle.
- Cleanup scope must stay explicit: delete only the legacy `/tmp` test bundles and generated notes we created during V2 testing. Do not delete project-local `runs/` and do not touch Zotero child notes.

## Current Status Snapshot

- V2 code path is now merged locally into `main` at commit `6fdb863`.
- The source-first figure pipeline is implemented: arXiv source download, source-side figure PDF rendering, deterministic PDF extraction, cross-source deduplication, and warning propagation are all in the repository.
- The operational layer is also implemented: `create-run`, project-local `runs/<date>/<paper-slug>/`, `run.json`, `extract-figures`, and `finalize-note` are available.
- The skill layer now supports both:
  - dry-run: `summarize-zotero-title "<paper title>"`
  - analyze and write: `summarize-zotero-title "<paper title>" and write to zotero`
- Real Zotero validations have already been exercised on both local-PDF and arXiv-backed papers, including successful child-note writes for figure-aware summaries.
- The latest local verification status before this plan sync was:
  - `uv run pytest` -> `69 passed`
  - `uv run zotero-paperread --help` -> passed
  - `uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json` -> passed
- This plan should now be read as a combined design-and-execution record: completed sections document the implemented architecture, while any remaining unchecked steps are future hardening or packaging work rather than the original V2 bring-up.

## Out of Scope

- Do not add Better Notes API integration.
- Do not modify Zotero storage or SQLite.
- Do not make arXiv source availability a hard requirement; many Zotero items will only have a local PDF.
- Do not make OCR the default extraction path.
- Do not introduce Docling, Marker, the Codex PDF skill workflow, or the Anthropic PDF skill workflow into V2 implementation.
- Do not add OpenCV, layout-detection models, or other heavyweight CV stacks in V2.0.
- Do not add or depend on system-level OCR binaries; if OCR is added, it must live inside the project environment managed by `uv`.
- Do not make note rendering depend on images being physically embedded in Zotero notes.
- Do not implement `summarize-zotero-title` as a Python CLI subcommand; the one-click entry stays at the skill layer.

## File Structure

- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/arxiv_source.py`: arXiv ID resolution, safe source-package download/extraction, source figure discovery, source-side figure PDF rendering via PyMuPDF.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/figures.py`: deterministic caption detection, crop generation, embedded-image supplementation, candidate scoring, confidence gating, bundle serialization, figure context markdown, source-result merging, all on top of PyMuPDF.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/ocr.py`: optional OCR adapter layer used only when fallback criteria are met.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/runs.py`: project-local run directory allocation, slug generation, collision handling, and `run.json` manifest writing.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/workflow.py`: call figure extraction from `prepare_item_bundle()`, emit `figures.json` and `figure_context.md`.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`: add `extract-figures`, add a deterministic `create-run` helper command, and extend `prepare-item` output contract.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/note.py`: extend summary schema and required note sections for figure analysis.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/templates/zotero_note.md.j2`: render key figure overview and per-figure analysis blocks.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`: add figure extraction and figure-aware summary requirements.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`: document the V2 figure workflow and CLI entry points.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/references/evil-read-arxiv-adaptation.md`: record what was borrowed from `extract-paper-images` and what was intentionally changed.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_arxiv_source.py`: unit tests for arXiv ID resolution, safe extraction, and source figure discovery.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_figures.py`: unit tests for figure extraction and ranking.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_ocr_fallback.py`: unit tests for confidence gating and OCR fallback behavior.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_runs.py`: unit tests for slugging, run-directory collision handling, and manifest writing.
- Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_figures.py`: CLI tests for `extract-figures`.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_prepare_item.py`: add CLI coverage for `create-run` and project-local run directories.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_workflow.py`: bundle tests for `figures.json` and `figure_context.md`.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_note.py`: note schema tests for the new figure sections.
- Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_end_to_end_dry_run.py`: end-to-end dry-run test covering a figure-aware summary payload.
- Do not create Docling- or Marker-specific adapter files in V2.0.

---

### Task 1: Build the Source-First Figure Acquisition Core

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/arxiv_source.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/figures.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_arxiv_source.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_figures.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_ocr_fallback.py`
- Create if fallback is adopted: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/ocr.py`

- [ ] **Step 1: Lock in the real remaining failures and new source-first behavior with tests**

Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_arxiv_source.py` with tests that cover:
- resolving arXiv IDs from Zotero `url`, `archiveLocation`, `extra`, and attachment filename hints
- safely extracting `tar.gz` source packages without path traversal or symlink surprises
- collecting source figures from `pics/`, `figures/`, `fig/`, `images/`, `img/`, plus root-level image files
- rendering source-side figure PDFs to PNGs with provenance `source = "pdf-figure"`

```python
from pathlib import Path

from zotero_paperread.arxiv_source import (
    collect_source_figures,
    render_source_figure_pdfs,
    resolve_arxiv_id,
)


def test_resolve_arxiv_id_prefers_metadata_then_attachment_hints() -> None:
    details = {
        "url": "https://arxiv.org/abs/2402.12345",
        "archiveLocation": "",
        "extra": "",
        "attachments": [{"filename": "2402.12345v2-paper.pdf"}],
    }

    assert resolve_arxiv_id(details) == "2402.12345"
```

Update `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_figures.py` so Task 1 is also judged against the blockers that actually appeared during execution:
- prose references like `Figure 1 shows the pipeline ...` must not be parsed as captions
- wrapped scientific captions that continue with uppercase or acronym-heavy lines like `SEM images ...` or `XRD patterns ...` must stay attached
- thin separator lines above a caption must not win over the real figure region
- low-confidence geometry must be surfaced as fallback metadata instead of silently emitting a bad crop
- embedded-image supplementation must never outrank good source images or deterministic crops just because the raster payload is large

Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_ocr_fallback.py` with a stubbed adapter test so OCR can be added without binding Task 1 to a real OCR runtime:

```python
from pathlib import Path

from zotero_paperread.figures import extract_figures


def test_detect_captions_ignores_body_text_figure_references(tmp_path: Path) -> None:
    pdf_path = tmp_path / "body-reference.pdf"
    output_dir = tmp_path / "figures"
    make_body_reference_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir=output_dir, top_k=2)

    assert payload["candidate_count"] == 0


def test_detect_captions_continues_wrapped_caption_with_uppercase_line(tmp_path: Path) -> None:
    pdf_path = tmp_path / "wrapped-uppercase.pdf"
    output_dir = tmp_path / "figures"
    make_uppercase_continuation_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir=output_dir, top_k=1)

    assert "SEM images" in payload["selected_figures"][0]["caption"]
```

```python
from pathlib import Path

from zotero_paperread.figures import extract_figures


def test_extract_figures_warns_when_ocr_fallback_is_needed_but_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pdf_path = tmp_path / "low-confidence.pdf"
    output_dir = tmp_path / "figures"
    make_low_confidence_pdf(pdf_path)
    monkeypatch.setattr("zotero_paperread.figures.ocr_fallback_available", lambda: False)

    payload = extract_figures(
        pdf_path,
        output_dir=output_dir,
        top_k=1,
        enable_ocr_fallback=True,
    )

    assert "ocr_fallback_unavailable" in payload["warnings"]
    assert payload["selected_figures"][0]["extraction_strategy"] == "deterministic"
    assert payload["selected_figures"][0]["needs_fallback"] is True
```

- [ ] **Step 2: Run the extraction tests to verify the blockers are real**

Run:

```bash
uv run pytest tests/test_arxiv_source.py tests/test_figures.py tests/test_ocr_fallback.py -v
```

Expected: FAIL on the new blocker-focused tests before implementation changes.

- [ ] **Step 3: Implement the arXiv source layer before touching the PDF**

Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/arxiv_source.py` with these responsibilities:

```python
from pathlib import Path
from typing import Any


def resolve_arxiv_id(details: dict[str, Any], pdf_path: Path | None = None) -> str | None:
    ...


def download_arxiv_source(arxiv_id: str, workdir: Path) -> Path | None:
    ...


def collect_source_figures(source_root: Path, output_dir: Path) -> list[dict[str, Any]]:
    ...


def render_source_figure_pdfs(source_figures: list[dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
    ...
```

Implementation requirements:
- use `urllib.request` or another project-local path, not shelling out to `curl`
- extract tarballs safely: reject absolute paths, `..` traversal, and symlinks
- preserve provenance with `source = "arxiv-source"` or `source = "pdf-figure"`
- render source-side figure PDFs with PyMuPDF so the whole project stays on one rendering engine
- do not fail the workflow if arXiv download is unavailable; return structured warnings instead

- [ ] **Step 4: Harden the deterministic PDF path instead of adding more heuristics blindly**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/figures.py` so the deterministic extractor explicitly tracks confidence and failure mode:

```python
def extract_figures(
    pdf_path: Path,
    output_dir: Path,
    top_k: int = 4,
    max_pages: int | None = None,
    *,
    arxiv_id: str | None = None,
    item_details: dict[str, Any] | None = None,
    enable_ocr_fallback: bool = False,
) -> dict[str, Any]:
    ...


class FigureCandidate(TypedDict):
    figure_id: str
    kind: Literal["figure"]
    caption: str
    caption_bbox: list[float]
    bbox: list[float]
    page: int
    area: float
    image_path: str
    priority_score: float
    source: Literal["arxiv-source", "pdf-figure", "deterministic-pdf", "embedded-image", "ocr-fallback"]
    extraction_strategy: Literal["deterministic", "ocr_fallback"]
    extraction_confidence: float
    fallback_reason: str
    needs_fallback: bool
```

Implementation requirements:
- merge source-derived figures, deterministic crops, and optional embedded-image supplements into one ranking pool
- rank source-derived figures above PDF-only supplements when scientific-signal heuristics are otherwise similar
- tighten caption-start detection so only caption-like label-first patterns match; prose like `Figure 1 shows ...` must be rejected
- allow wrapped continuation lines that start with uppercase scientific terms or acronyms when a caption is already open
- reject separator-line artifacts and other degenerate owned regions before rasterization
- keep raw `page.get_images(full=True)` extraction as a late supplement for born-digital raster figures, filtered by minimum width/height/bytes
- keep figure-region crops and text extraction on PyMuPDF `Page` / `TextPage` data instead of introducing a second parser
- compute `extraction_confidence` and `fallback_reason` so downstream code can tell whether a candidate is trustworthy

- [ ] **Step 5: Add OCR as a gated fallback, not a replacement path**

If the deterministic path produces `needs_fallback = True`, add `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/ocr.py` behind a soft import boundary. The first fallback target is project-local OCR only, for example via `uv add rapidocr-onnxruntime`; do not assume `tesseract` or other system binaries exist.

Use this interface:

```python
from pathlib import Path


def ocr_fallback_available() -> bool:
    ...


def run_ocr_on_page_clip(pdf_path: Path, page_number: int, bbox: list[float]) -> dict[str, str]:
    return {
        "caption_hint": "",
        "text": "",
        "engine": "",
    }
```

Fallback gate:
- run OCR only when `enable_ocr_fallback=True`
- run OCR only when there is no usable caption, the crop is obviously malformed, or `extraction_confidence` is below threshold
- if OCR is unavailable, emit a warning and keep the deterministic candidate plus fallback metadata; do not crash

- [ ] **Step 6: Re-run focused tests, then the full suite**

Run:

```bash
uv run pytest tests/test_arxiv_source.py tests/test_figures.py tests/test_ocr_fallback.py -v
uv run pytest
```

Expected:
- focused extraction tests pass
- full suite stays green

- [ ] **Step 7: Commit the hardened acquisition core**

Run:

```bash
git add src/zotero_paperread/arxiv_source.py src/zotero_paperread/figures.py src/zotero_paperread/ocr.py tests/test_arxiv_source.py tests/test_figures.py tests/test_ocr_fallback.py
git commit -m "feat: add source-first figure acquisition"
```

If OCR is not yet added in this step, omit `src/zotero_paperread/ocr.py` from the commit and use:

```bash
git commit -m "feat: add source-first deterministic figure acquisition"
```

---

### Task 2: Wire Figure Extraction into CLI and Bundle Preparation

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/workflow.py`
- Read: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/arxiv_source.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_workflow.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_figures.py`

Bundle contract additions for V2.0:
- top-level `arxiv_id`
- top-level `source_attempts`
- `selected_figures[*].extraction_strategy`
- `selected_figures[*].extraction_confidence`
- `selected_figures[*].fallback_reason`
- `selected_figures[*].source`
- top-level `warnings`

- [ ] **Step 1: Write the failing bundle and CLI tests**

Use `apply_patch` to update `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_workflow.py` and create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_figures.py` with this content:

```python
# tests/test_cli_figures.py
import json
from pathlib import Path

import fitz
from typer.testing import CliRunner

import zotero_paperread.cli as cli_module
from zotero_paperread.cli import app


def make_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(72, 80, 300, 240), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
    page.insert_text((72, 280), "Figure 1. Framework overview.", fontsize=12)
    doc.save(path)
    doc.close()


def test_extract_figures_cli_writes_selected_figures(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "paper.pdf"
    output_dir = tmp_path / "figures"
    make_pdf(pdf_path)
    fake_payload = {
        "arxiv_id": "2402.12345",
        "source_attempts": [{"kind": "arxiv-source", "status": "ok"}],
        "candidate_count": 1,
        "selected_figures": [],
        "warnings": [],
    }
    monkeypatch.setattr(cli_module, "extract_figures", lambda *args, **kwargs: fake_payload)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["extract-figures", str(pdf_path), "--output-dir", str(output_dir), "--top-k", "2", "--arxiv-id", "2402.12345"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source_attempts"][0]["kind"] == "arxiv-source"
    assert payload["candidate_count"] >= 1
```

```python
# tests/test_workflow.py additions
def test_prepare_item_bundle_writes_figures_and_figure_context(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["Figure body", "Figure 1. Architecture overview."])
    monkeypatch.setattr(
        "zotero_paperread.workflow.extract_figures",
        lambda *args, **kwargs: {
            "arxiv_id": "2402.12345",
            "source_attempts": [{"kind": "arxiv-source", "status": "ok"}],
            "candidate_count": 1,
            "selected_figures": [],
            "warnings": [],
        },
    )
    details = {
        "key": "FIG123",
        "title": "Figure Heavy Paper",
        "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
        "date": "2026",
        "DOI": "",
        "url": "https://arxiv.org/abs/2402.12345",
        "zoteroUrl": "zotero://select/library/items/FIG123",
        "abstractNote": "Has figures.",
        "attachments": [
            {
                "key": "PDF1",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "path": str(pdf_path),
            }
        ],
    }

    result = prepare_item_bundle(details, tmp_path / "bundle", max_pages=5)

    assert Path(result["figures_json"]).exists()
    assert Path(result["figure_context_md"]).exists()
```

Unit-test rule: these tests must not touch the network. Stub the arXiv download/discovery layer or monkeypatch `extract_figures`/`download_arxiv_source` so CI stays deterministic.

- [ ] **Step 2: Run the bundle and CLI tests to verify they fail**

Run:

```bash
uv run pytest tests/test_cli_figures.py tests/test_workflow.py -v
```

Expected: FAIL because `extract-figures` does not yet accept `--arxiv-id`, `prepare_item_bundle()` does not yet include source attempts, and the figure payload does not yet expose provenance fields.

- [ ] **Step 3: Implement CLI wiring and bundle outputs**

Use `apply_patch` to modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py` and `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/workflow.py` with these changes:

```python
# cli.py
from zotero_paperread.figures import extract_figures


@app.command("extract-figures")
def extract_figures_command(
    pdf_path: Path,
    output_dir: Path = typer.Option(..., "--output-dir", help="Directory for extracted figure images."),
    top_k: int = typer.Option(4, "--top-k", min=1, help="Keep at most this many key figures."),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, help="Inspect at most this many pages."),
    arxiv_id: str | None = typer.Option(None, "--arxiv-id", help="Optional arXiv ID for source-first figure collection."),
) -> None:
    """Extract and rank figure candidates from a PDF."""
    payload = extract_figures(
        pdf_path,
        output_dir=output_dir,
        top_k=top_k,
        max_pages=max_pages,
        arxiv_id=arxiv_id,
    )
    typer.echo(json.dumps(payload, ensure_ascii=False))
```

```python
# workflow.py
from zotero_paperread.figures import build_figure_context_markdown, extract_figures


def prepare_item_bundle(
    details: dict[str, Any],
    workdir: Path,
    max_pages: int | None = None,
    figure_top_k: int = 4,
) -> dict[str, Any]:
    ...
    figures_dir = bundle_dir / "figures"
    figures_json_path = bundle_dir / "figures.json"
    figure_context_path = bundle_dir / "figure_context.md"
    if pdf_path:
        figures = extract_figures(
            Path(pdf_path),
            output_dir=figures_dir,
            top_k=figure_top_k,
            max_pages=max_pages,
            item_details=details,
        )
    else:
        figures = {
            "arxiv_id": "",
            "source_attempts": [],
            "pdf_path": "",
            "candidate_count": 0,
            "selected_figures": [],
            "warnings": ["missing_pdf_attachment"],
        }
    figures_json_path.write_text(json.dumps(figures, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    figure_context_path.write_text(build_figure_context_markdown(figures), encoding="utf-8")
    return {
        "metadata_json": str(metadata_path),
        "extract_json": str(extract_path),
        "context_md": str(context_path),
        "figures_json": str(figures_json_path),
        "figure_context_md": str(figure_context_path),
        "arxiv_id": figures.get("arxiv_id", ""),
        "has_pdf": bool(pdf_path),
    }
```

- [ ] **Step 4: Run the bundle and CLI tests again**

Run:

```bash
uv run pytest tests/test_cli_figures.py tests/test_workflow.py -v
```

Expected: PASS with the CLI emitting JSON and the bundle containing `figures.json` and `figure_context.md`.

- [ ] **Step 5: Commit the bundle integration**

Run:

```bash
git add src/zotero_paperread/cli.py src/zotero_paperread/workflow.py tests/test_cli_figures.py tests/test_workflow.py
git commit -m "feat: add figure bundle outputs"
```

Expected: one new commit wiring figure extraction into the existing bundle flow.

---

### Task 3: Extend Note Rendering for Figure-Aware Analysis

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/note.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/templates/zotero_note.md.j2`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_note.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_end_to_end_dry_run.py`

- [ ] **Step 1: Write the failing note tests**

Use `apply_patch` to update `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_note.py` and `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_end_to_end_dry_run.py` with these additions:

```python
# tests/test_note.py additions
SUMMARY_WITH_FIGURES = {
    **SUMMARY,
    "figure_overview": "论文的关键证据主要集中在框架图和定量对比图。",
    "key_figures": [
        {
            "figure_id": "fig_p1_1",
            "caption": "Figure 1. Overall pipeline.",
            "page": 1,
            "priority_score": 5.2,
            "why_it_matters": "这张图定义了整篇论文的方法对象和信息流。",
            "analysis": "图 1 展示了从输入结构到扩散采样再到性质打分的主链路。",
        }
    ],
}


def test_render_note_contains_figure_sections() -> None:
    note = render_note(METADATA, SUMMARY_WITH_FIGURES, generated_date="2026-04-23")

    assert "## 关键图片总览" in note
    assert "### fig_p1_1" in note
    assert "Figure 1. Overall pipeline." in note


def test_validate_note_requires_figure_overview_section() -> None:
    note = render_note(METADATA, SUMMARY_WITH_FIGURES, generated_date="2026-04-23")
    errors = validate_note(note.replace("## 关键图片总览", "## 图片"))
    assert "missing_section: 关键图片总览" in errors
```

```python
# tests/test_end_to_end_dry_run.py summary payload change
"figure_overview": "关键图集中在框架图和结果对比图。",
"key_figures": [
    {
        "figure_id": "fig_p1_1",
        "caption": "Figure 1. Framework overview.",
        "page": 1,
        "priority_score": 4.8,
        "why_it_matters": "它定义了本文方法的核心链路。",
        "analysis": "该图解释了模型模块之间的依赖关系和训练信息流。",
    }
],
```

- [ ] **Step 2: Run the note tests to verify they fail**

Run:

```bash
uv run pytest tests/test_note.py tests/test_end_to_end_dry_run.py -v
```

Expected: FAIL because `render_note()` and the template do not yet know about `figure_overview` or `key_figures`.

- [ ] **Step 3: Extend note schema, validation, and template**

Use `apply_patch` to modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/note.py` and `/Users/jwxi/Desktop/AIflow/Zotero_paperread/templates/zotero_note.md.j2`:

```python
# note.py
REQUIRED_SECTIONS = [
    "元数据",
    "核心结论",
    "摘要翻译",
    "关键要点",
    "研究问题",
    "方法拆解",
    "关键图片总览",
    "实验与证据",
    "主要贡献",
    "局限与风险",
    "AI+物理/材料启发",
    "后续关键词",
    "抽取告警",
]

...
    context = {
        ...
        "figure_overview": summary.get("figure_overview", ""),
        "key_figures": summary.get("key_figures", []),
    }
```

```jinja2
## 关键图片总览

{{ figure_overview }}

{% for item in key_figures -%}
### {{ item.figure_id }}

- **Caption**: {{ item.caption }}
- **Page**: {{ item.page }}
- **Priority Score**: {{ item.priority_score }}
- **Why It Matters**: {{ item.why_it_matters }}

{{ item.analysis }}

{% endfor %}
```

- [ ] **Step 4: Run the note tests again**

Run:

```bash
uv run pytest tests/test_note.py tests/test_end_to_end_dry_run.py -v
```

Expected: PASS with rendered notes containing figure-aware sections.

- [ ] **Step 5: Commit the note schema update**

Run:

```bash
git add src/zotero_paperread/note.py templates/zotero_note.md.j2 tests/test_note.py tests/test_end_to_end_dry_run.py
git commit -m "feat: render figure-aware notes"
```

Expected: one new commit covering note schema and template changes.

---

### Task 4: Update the Skill Contract and Project Docs

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/references/evil-read-arxiv-adaptation.md`

- [ ] **Step 1: Update the skill contract to require figure-aware summaries**

Use `apply_patch` to update `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md` so that:

```markdown
- `prepare-item` now generates:
  - `metadata.json`
  - `extract.json`
  - `context.md`
  - `figures.json`
  - `figure_context.md`
```

and the required summary schema becomes:

```json
{
  "one_sentence_summary": "",
  "abstract_translation": "",
  "key_points": [],
  "research_question": "",
  "method": "",
  "figure_overview": "",
  "key_figures": [
    {
      "figure_id": "",
      "caption": "",
      "page": 0,
      "priority_score": 0,
      "why_it_matters": "",
      "analysis": ""
    }
  ],
  "experiments": "",
  "contributions": [],
  "limitations": [],
  "ai4s_relevance": "",
  "follow_up_keywords": [],
  "quality_score": "",
  "extraction_warnings": []
}
```

- [ ] **Step 2: Update top-level documentation**

Use `apply_patch` to update `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md` with a new V2 section:

```markdown
## V2: Key Figure Extraction and Analysis

- Primary path: resolve arXiv ID and extract source images or source-side figure PDFs when possible
- Secondary path: detect figure captions and crop the rendered region above the caption for local-only PDFs
- Supplement path: extract embedded PDF images only when source and deterministic paths are sparse
- Fallback path: run OCR only when deterministic extraction is low-confidence and the project-local OCR adapter is available
- Output: `figures/`, `figures.json`, `figure_context.md`
- Goal: improve scientific reading quality, not just embed pictures
- Current note behavior: figure analysis is written into the Zotero note even if inline image embedding is not enabled
```

Then update `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/references/evil-read-arxiv-adaptation.md` with:

```markdown
## V2 Figure Strategy

- Reuse from `extract-paper-images`: source-first acquisition order, safe source extraction, source/PDF provenance tags, and source-side figure PDF rendering
- Reject from `extract-paper-images`: raw embedded-image extraction as the primary PDF strategy
- New project choice: caption-anchored page crops remain necessary for local Zotero PDFs because many items are not resolvable to arXiv source
- New execution lesson: opportunistic arXiv source plus deterministic PDF extraction plus OCR fallback is materially stronger than a PDF-only pipeline
```

- [ ] **Step 3: Verify the CLI help and docs mention the new workflow**

Run:

```bash
uv run zotero-paperread --help
rg -n "extract-figures|figure_context|key_figures|关键图片总览|arxiv-source|source_attempts" README.md skills/zotero-paper-summary/SKILL.md docs/references/evil-read-arxiv-adaptation.md
```

Expected:
- `--help` lists `extract-figures`
- ripgrep shows the new figure and provenance terms in the updated docs

- [ ] **Step 4: Commit the skill and docs update**

Run:

```bash
git add skills/zotero-paper-summary/SKILL.md README.md docs/references/evil-read-arxiv-adaptation.md
git commit -m "docs: document figure-aware summary workflow"
```

Expected: one docs-focused commit.

---

### Task 5: Add Project-Local Run Management and a Deterministic CLI Wrapper

**Files:**
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/runs.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/workflow.py`
- Create: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_runs.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_prepare_item.py`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_workflow.py`

- [ ] **Step 1: Write the failing run-management tests**

Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_runs.py` with:

```python
from datetime import date
from pathlib import Path

from zotero_paperread.runs import allocate_run_dir, slugify_title, write_run_manifest


def test_slugify_title_normalizes_unicode_punctuation() -> None:
    assert slugify_title("Deep‐Learning Assisted Polarization Holograms") == "deep-learning-assisted-polarization-holograms"


def test_allocate_run_dir_uses_date_slug_and_collision_suffix(tmp_path: Path) -> None:
    base_dir = tmp_path / "runs"
    first = allocate_run_dir(base_dir, "CrystalGRW: Geodesic Random Walks", today=date(2026, 4, 24))
    first.mkdir(parents=True)
    second = allocate_run_dir(base_dir, "CrystalGRW: Geodesic Random Walks", today=date(2026, 4, 24))

    assert first == base_dir / "2026-04-24" / "crystalgrw-geodesic-random-walks"
    assert second == base_dir / "2026-04-24" / "crystalgrw-geodesic-random-walks-2"


def test_write_run_manifest_records_core_metadata(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "2026-04-24" / "example-paper"
    run_dir.mkdir(parents=True)
    manifest = write_run_manifest(
        run_dir,
        {
            "title": "Example Paper",
            "item_key": "ABC123",
            "status": "initialized",
        },
    )

    assert manifest.exists()
    assert '"item_key": "ABC123"' in manifest.read_text(encoding="utf-8")
```

Update `/Users/jwxi/Desktop/AIflow/Zotero_paperread/tests/test_cli_prepare_item.py` with:

```python
def test_create_run_command_emits_project_local_run_dir(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "create-run",
            "--title",
            "Deep‐Learning Assisted Polarization Holograms",
            "--base-dir",
            str(tmp_path / "runs"),
            "--today",
            "2026-04-24",
            "--item-key",
            "8HCYMEEB",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run_dir"].endswith("runs/2026-04-24/deep-learning-assisted-polarization-holograms")
    assert Path(payload["manifest_path"]).exists()
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
uv run pytest tests/test_runs.py tests/test_cli_prepare_item.py -v
```

Expected: FAIL because `runs.py` and `create-run` do not exist yet.

- [ ] **Step 3: Implement run allocation and manifest writing**

Create `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/runs.py` with:

```python
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any


def slugify_title(title: str) -> str:
    ...


def allocate_run_dir(base_dir: Path, title: str, today: date | None = None) -> Path:
    ...


def write_run_manifest(run_dir: Path, payload: dict[str, Any]) -> Path:
    ...
```

Implementation requirements:
- normalize Unicode punctuation and whitespace before slugging
- create slugs that are stable and lowercase ASCII
- allocate `runs/<YYYY-MM-DD>/<paper-slug>/`, then `-2`, `-3`, and so on if the path already exists
- write `run.json` with `title`, `slug`, `item_key`, `created_at`, `status`, and any extra payload fields
- do not reach into Zotero or note rendering from this module

- [ ] **Step 4: Add a deterministic CLI wrapper for the skill**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/cli.py` to add:

```python
@app.command("create-run")
def create_run_command(
    title: str = typer.Option(..., "--title", help="Paper title used for slugging."),
    item_key: str = typer.Option("", "--item-key", help="Optional Zotero item key for the manifest."),
    base_dir: Path = typer.Option(Path("runs"), "--base-dir", help="Project-local runs directory."),
    today: str | None = typer.Option(None, "--today", help="Override date for deterministic tests."),
) -> None:
    """Allocate a project-local run directory and write run.json."""
    ...
```

Implementation requirements:
- default `base_dir` must resolve inside the repository as `runs/`
- emit JSON with `run_dir`, `manifest_path`, `slug`, and `date`
- write `run.json` immediately so later steps have a durable manifest anchor
- keep this command deterministic and Zotero-free; do not add title lookup here

- [ ] **Step 5: Thread the run metadata into bundle preparation**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/src/zotero_paperread/workflow.py` so that `prepare_item_bundle()` can update `run.json` when called inside a run directory:

```python
def prepare_item_bundle(details: dict[str, Any], workdir: Path, max_pages: int | None = None) -> dict[str, Any]:
    ...
    manifest_path = bundle_dir / "run.json"
    if manifest_path.exists():
        ...
```

Implementation requirements:
- if `run.json` exists, update it with `pdf_path`, `metadata_json`, `extract_json`, `figures_json`, `figure_context_md`, `arxiv_id`, `warnings`, and `status`
- do not require `run.json`; plain bundle directories must still work
- keep `prepare-item` backwards compatible for tests that pass an arbitrary `--workdir`

- [ ] **Step 6: Re-run tests and commit the run-management layer**

Run:

```bash
uv run pytest tests/test_runs.py tests/test_cli_prepare_item.py tests/test_workflow.py -v
```

Expected: PASS with `create-run` returning a stable project-local path and `prepare-item` preserving backward compatibility.

Commit:

```bash
git add src/zotero_paperread/runs.py src/zotero_paperread/cli.py src/zotero_paperread/workflow.py tests/test_runs.py tests/test_cli_prepare_item.py tests/test_workflow.py
git commit -m "feat: add project-local run management"
```

---

### Task 6: Document the Skill-First One-Click Entry and Clean Legacy Test Artifacts

**Files:**
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md`
- Modify: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md`

- [ ] **Step 1: Update the skill contract to define the canonical one-click entry**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/skills/zotero-paper-summary/SKILL.md` so the recommended invocation is explicitly:

```text
summarize-zotero-title "<paper title>"
```

and the workflow becomes:

```markdown
1. Use Zotero MCP to locate the single target item.
2. Run `uv run zotero-paperread create-run --title "<title>" --item-key "<item_key>"`.
3. Save `item-details.json` inside the returned run directory.
4. Run `uv run zotero-paperread prepare-item ... --workdir <run_dir>`.
5. Write `summary.json`, `note.md`, and previews into the same run directory.
6. Only write to Zotero when the user explicitly requests it.
```

- [ ] **Step 2: Update README for reusable runs and retention policy**

Modify `/Users/jwxi/Desktop/AIflow/Zotero_paperread/README.md` with:

```markdown
## Reusable One-Click Flow

- Canonical Codex skill entry: `summarize-zotero-title "<paper title>"`
- Project-local artifacts live under `runs/<date>/<paper-slug>/`
- Typical run contents:
  - `run.json`
  - `item-details.json`
  - `metadata.json`
  - `extract.json`
  - `context.md`
  - `figures.json`
  - `figure_context.md`
  - `summary.json`
  - `note.md`
  - `figures/`
- These files are intermediate and audit artifacts. Keep them while reviewing a run; delete old runs manually when they are no longer useful.
```

- [ ] **Step 3: Verify docs and CLI help mention the new entrypoints**

Run:

```bash
uv run zotero-paperread --help
rg -n "create-run|summarize-zotero-title|runs/<date>|run.json" README.md skills/zotero-paper-summary/SKILL.md
```

Expected:
- `--help` lists `create-run`
- docs mention the skill-first one-click flow and project-local run layout

- [ ] **Step 4: Remove the known legacy `/tmp` test artifacts**

Run:

```bash
rm -rf /tmp/zotero-paperread-v2-test.FEVday
rm -rf /tmp/zotero-paperread-arxiv-test.HYeouc
rm -f /tmp/zotero-paperread-extract.json
```

Then verify:

```bash
ls -1d /tmp/zotero-paperread-v2-test.FEVday /tmp/zotero-paperread-arxiv-test.HYeouc 2>/dev/null
test ! -e /tmp/zotero-paperread-extract.json
```

Expected: no output from `ls`; the old `/tmp` test artifacts are gone. Do not delete anything under `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/` and do not delete Zotero child notes.

- [ ] **Step 5: Commit the docs and cleanup update**

Run:

```bash
git add skills/zotero-paper-summary/SKILL.md README.md
git commit -m "docs: define one-click skill workflow"
```

Expected: one docs-focused commit. The `/tmp` cleanup is intentionally not part of the commit because it is outside the repository.

---

### Task 7: Verification and Acceptance Gate

**Files:**
- Read: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/AGENTS.md`
- Read: `/Users/jwxi/Desktop/AIflow/Zotero_paperread/docs/superpowers/plans/2026-04-23-zotero-paper-figures-v2.md`

- [ ] **Step 1: Run the full automated test suite**

Run:

```bash
uv run pytest
```

Expected: PASS across the existing V1 tests plus the new figure tests.

- [ ] **Step 2: Verify required CLI entry points**

Run:

```bash
uv run zotero-paperread --help
uv run zotero-paperread version
```

Expected:
- `--help` shows `extract-figures`, `prepare-item`, and `create-run`
- `version` prints the package version without traceback

- [ ] **Step 3: Verify the bundle contract end-to-end without writing Zotero**

Run:

```bash
uv run pytest tests/test_arxiv_source.py tests/test_cli_figures.py tests/test_workflow.py tests/test_note.py tests/test_end_to_end_dry_run.py -v
```

Expected: PASS with evidence that:
- source-first figure acquisition works when arXiv metadata is present
- figure extraction works for local-only PDFs
- `prepare-item` writes `figures.json`
- note rendering includes figure sections
- dry-run still does not touch Zotero

- [ ] **Step 4: Manual acceptance check with real Zotero items, dry-run only**

Run this only after the user provides or confirms target paper titles. Use:
- one Zotero item with a resolvable arXiv ID
- one Zotero item with only a local PDF
- one harder PDF if available

```bash
uv run zotero-paperread create-run --title "Example Paper" --item-key EXAMPLE123
cp /path/to/item-details.json /Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-04-24/example-paper/item-details.json
uv run zotero-paperread prepare-item /Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-04-24/example-paper/item-details.json --workdir /Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-04-24/example-paper --max-pages 15
```

Expected:
- `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-04-24/example-paper/figures.json` exists
- `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-04-24/example-paper/figure_context.md` exists
- `/Users/jwxi/Desktop/AIflow/Zotero_paperread/runs/2026-04-24/example-paper/run.json` exists
- arXiv-backed items show `source_attempts` containing `arxiv-source`
- clean papers produce at least one high-signal figure without fallback warnings
- harder papers either produce usable figures or surface explicit fallback warnings without crashing

- [ ] **Step 5: Commit any final verification-only adjustments**

Run:

```bash
git status --short
```

Expected: no unexpected modified files. If verification exposed a real defect, fix it in the smallest possible follow-up commit with a message matching the defect, such as:

```bash
git commit -am "fix: correct figure caption ranking"
```

Do not push. Remote publication remains user-gated.
