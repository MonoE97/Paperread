from pathlib import Path

import fitz
import pytest

from paper_reader.pdf_extract import extract_pdf


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


def test_extract_pdf_skips_numeric_table_signals_without_recognized_section(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(
        pdf_path,
        ["Table 1 Baseline RMSE 0.25 MAE 0.13 R2 0.91 speedup 10x."],
    )

    result = extract_pdf(pdf_path)

    assert result["sections"] == []
    assert result["table_candidates"] == []


def test_extract_pdf_page_records_warn_for_empty_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["", "Methods\nEnough text for extraction."])

    result = extract_pdf(pdf_path)

    assert result["pages"][0]["page"] == 1
    assert "empty_page_text" in result["pages"][0]["warnings"]
    assert result["pages"][0]["char_count"] == 0


def test_extract_pdf_aborts_during_page_iteration_when_text_budget_is_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.pdf_extract as pdf_extract

    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["first page has enough text", "second page", "third page"])
    real_open = pdf_extract.fitz.open
    loaded_pages: list[int] = []

    class CountingDocument:
        def __init__(self, path: Path) -> None:
            self._document = real_open(path)
            self.page_count = self._document.page_count

        def load_page(self, index: int):
            loaded_pages.append(index)
            return self._document.load_page(index)

        def close(self) -> None:
            self._document.close()

    monkeypatch.setattr(pdf_extract.fitz, "open", lambda path: CountingDocument(path))

    with pytest.raises(pdf_extract.ExtractedTextLimitError) as exc_info:
        extract_pdf(pdf_path, max_chars=10)

    assert exc_info.value.max_chars == 10
    assert exc_info.value.actual_chars > 10
    assert loaded_pages == [0]
