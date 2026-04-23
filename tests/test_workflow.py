import json
from pathlib import Path

import fitz

from zotero_paperread.workflow import prepare_item_bundle


def make_pdf(path: Path, pages: list[str]) -> None:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_prepare_item_bundle_writes_metadata_extract_and_context(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["Abstract\nThis paper studies CSP with AI.", "Methods\nDiffusion and GAN models."])
    details = {
        "key": "ABC123",
        "title": "Crystal Structure Prediction Meets Artificial Intelligence",
        "creators": [
            {"firstName": "Zian", "lastName": "Chen"},
            {"firstName": "Tao", "lastName": "He"},
        ],
        "date": "2025-03-13",
        "DOI": "10.1000/example",
        "url": "https://example.org/paper",
        "zoteroUrl": "zotero://select/library/items/ABC123",
        "abstractNote": "Perspective on CSP and AI.",
        "attachments": [
            {
                "key": "PDFKEY",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "path": str(pdf_path),
            }
        ],
    }

    result = prepare_item_bundle(details, tmp_path / "bundle", max_pages=1)

    metadata = json.loads(Path(result["metadata_json"]).read_text(encoding="utf-8"))
    extract = json.loads(Path(result["extract_json"]).read_text(encoding="utf-8"))
    context = Path(result["context_md"]).read_text(encoding="utf-8")

    assert result["has_pdf"] is True
    assert metadata["title"] == details["title"]
    assert metadata["creators"] == "Zian Chen, Tao He"
    assert metadata["pdf_attachment_key"] == "PDFKEY"
    assert extract["warnings"] == ["truncated_to_1_pages"]
    assert "Perspective on CSP and AI." in context
    assert "This paper studies CSP with AI." in context


def test_prepare_item_bundle_handles_missing_pdf(tmp_path: Path) -> None:
    details = {
        "key": "NOPDF1",
        "title": "No PDF Paper",
        "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
        "date": "2025",
        "DOI": "",
        "url": "",
        "zoteroUrl": "zotero://select/library/items/NOPDF1",
        "abstractNote": "Abstract only.",
        "attachments": [],
    }

    result = prepare_item_bundle(details, tmp_path / "bundle")

    extract = json.loads(Path(result["extract_json"]).read_text(encoding="utf-8"))
    context = Path(result["context_md"]).read_text(encoding="utf-8")

    assert result["has_pdf"] is False
    assert extract["warnings"] == ["missing_pdf_attachment"]
    assert extract["text"] == ""
    assert "Abstract only." in context
