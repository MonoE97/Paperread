from pathlib import Path

from paper_reader import pdf_workflow
from paper_reader.pdf_workflow import build_pdf_metadata


def test_build_pdf_metadata_uses_filename_stem_by_default(tmp_path: Path) -> None:
    pdf_path = tmp_path / "battery-electrolyte.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    metadata = build_pdf_metadata(pdf_path)

    assert metadata["key"] == ""
    assert metadata["title"] == "battery-electrolyte"
    assert metadata["creators"] == ""
    assert metadata["date"] == ""
    assert metadata["DOI"] == ""
    assert metadata["url"] == ""
    assert metadata["zoteroUrl"] == ""
    assert metadata["pdf_path"] == str(pdf_path)
    assert metadata["pdf_filename"] == "battery-electrolyte.pdf"
    assert metadata["source_type"] == "pdf_path"


def test_build_pdf_metadata_prefers_explicit_overrides(tmp_path: Path) -> None:
    pdf_path = tmp_path / "raw-name.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    metadata = build_pdf_metadata(
        pdf_path,
        title="Explicit Paper Title",
        authors="Ada Lovelace, Grace Hopper",
        date="2026",
        doi="10.1000/example",
        url="https://example.org/paper",
    )

    assert metadata["title"] == "Explicit Paper Title"
    assert metadata["creators"] == "Ada Lovelace, Grace Hopper"
    assert metadata["date"] == "2026"
    assert metadata["DOI"] == "10.1000/example"
    assert metadata["url"] == "https://example.org/paper"


def test_pdf_workflow_does_not_expose_v1_output_allocator() -> None:
    assert not hasattr(pdf_workflow, "PDFOutputPaths")
    assert not hasattr(pdf_workflow, "allocate_pdf_output_paths")
