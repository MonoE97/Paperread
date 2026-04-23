from pathlib import Path

import fitz
import pytest

from zotero_paperread.pdf_extract import extract_pdf


def make_pdf(path: Path, pages: list[str]) -> None:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_extract_pdf_returns_text_and_page_count(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["Abstract\nThis is page one.", "Methods\nThis is page two."])

    result = extract_pdf(pdf_path)

    assert result["pdf_path"] == str(pdf_path)
    assert result["page_count"] == 2
    assert "Abstract" in result["text"]
    assert "Methods" in result["text"]
    assert result["warnings"] == []


def test_extract_pdf_respects_max_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["first page", "second page"])

    result = extract_pdf(pdf_path, max_pages=1)

    assert result["page_count"] == 2
    assert "first page" in result["text"]
    assert "second page" not in result["text"]
    assert "truncated_to_1_pages" in result["warnings"]


def test_extract_pdf_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="PDF not found"):
        extract_pdf(tmp_path / "missing.pdf")
