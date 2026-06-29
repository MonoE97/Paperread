from pathlib import Path

import fitz

from zotero_paperread import workflow
from zotero_paperread.pdf_workflow import allocate_pdf_output_paths, build_pdf_metadata
from zotero_paperread.workflow import prepare_pdf_bundle


def make_pdf(path: Path, pages: list[str]) -> None:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_allocate_pdf_output_paths_uses_pdf_stem_next_to_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "Paper One.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    outputs = allocate_pdf_output_paths(pdf_path)

    assert outputs.analysis_dir == tmp_path / "Paper One_analysis"
    assert outputs.final_note_path == tmp_path / "Paper One_note.md"
    assert outputs.version_suffix == ""


def test_allocate_pdf_output_paths_versions_without_overwriting_existing_outputs(tmp_path: Path) -> None:
    pdf_path = tmp_path / "Paper One.pdf"
    first_analysis = tmp_path / "Paper One_analysis"
    first_note = tmp_path / "Paper One_note.md"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    first_analysis.mkdir()
    first_note.write_text("existing note", encoding="utf-8")

    outputs = allocate_pdf_output_paths(pdf_path)

    assert outputs.analysis_dir == tmp_path / "Paper One_analysis_v2"
    assert outputs.final_note_path == tmp_path / "Paper One_note_v2.md"
    assert outputs.version_suffix == "_v2"
    assert first_analysis.exists()
    assert first_note.read_text(encoding="utf-8") == "existing note"


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


def test_prepare_pdf_bundle_writes_same_core_artifacts_as_zotero_bundle(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "battery-electrolyte.pdf"
    workdir = tmp_path / "battery-electrolyte_analysis"
    make_pdf(pdf_path, ["Abstract\nThis paper studies argyrodite electrolytes.", "Methods\nImpedance tests."])
    figures_payload = {
        "arxiv_id": None,
        "pdf_path": str(pdf_path),
        "candidate_count": 0,
        "selected_figures": [],
        "source_attempts": [{"stage": "direct_pdf", "status": "used"}],
        "warnings": [],
    }
    seen: dict[str, object] = {}

    def fake_extract_figures(
        requested_pdf_path: Path,
        output_dir: Path,
        top_k: int = 4,
        max_pages: int | None = None,
        *,
        arxiv_id: str | None = None,
        item_details: dict | None = None,
        enable_ocr_fallback: bool = False,
    ) -> dict:
        seen.update(
            {
                "pdf_path": requested_pdf_path,
                "output_dir": output_dir,
                "max_pages": max_pages,
                "item_details": item_details,
            }
        )
        return figures_payload

    monkeypatch.setattr(workflow, "extract_figures", fake_extract_figures)

    result = prepare_pdf_bundle(
        pdf_path,
        workdir,
        title="Low-cost electrolyte",
        authors="A. Researcher",
        max_pages=1,
    )

    metadata = result["metadata"]
    assert result["has_pdf"] is True
    assert Path(result["metadata_json"]).exists()
    assert Path(result["extract_json"]).exists()
    assert Path(result["context_md"]).exists()
    assert Path(result["section_context_md"]).exists()
    assert Path(result["figures_json"]).exists()
    assert Path(result["figure_context_md"]).exists()
    assert metadata["source_type"] == "pdf_path"
    assert metadata["title"] == "Low-cost electrolyte"
    assert metadata["creators"] == "A. Researcher"
    assert seen["pdf_path"] == pdf_path
    assert seen["output_dir"] == workdir / "figures"
    assert seen["max_pages"] == 1
    assert seen["item_details"] is None
    context = Path(result["context_md"]).read_text(encoding="utf-8")
    assert "Low-cost electrolyte" in context
    assert "argyrodite electrolytes" in context
