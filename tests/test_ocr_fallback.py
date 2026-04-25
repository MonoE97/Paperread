from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from zotero_paperread.figures import extract_figures


def make_low_confidence_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=360, height=260)
    page.draw_rect(fitz.Rect(60, 80, 220, 98), color=(0, 0, 0), fill=(0.7, 0.8, 0.9))
    page.insert_text(
        (60, 130),
        "Figure 11. Thin strip candidate needs fallback.",
        fontsize=12,
    )
    doc.save(path)
    doc.close()


def test_extract_figures_warns_when_ocr_fallback_is_needed_but_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
