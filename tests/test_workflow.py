import json
from pathlib import Path

import fitz

from zotero_paperread import workflow
from zotero_paperread.workflow import prepare_item_bundle


def make_pdf(path: Path, pages: list[str]) -> None:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_select_pdf_attachment_prefers_main_paper_over_appendix() -> None:
    attachments = [
        {
            "key": "APPENDIX",
            "filename": "paper-appendix.pdf",
            "contentType": "application/pdf",
            "path": "/tmp/paper-appendix.pdf",
        },
        {
            "key": "MAINPDF",
            "filename": "paper.pdf",
            "contentType": "application/pdf",
            "path": "/tmp/paper.pdf",
        },
    ]

    selected = workflow.select_pdf_attachment(attachments)

    assert selected is not None
    assert selected["key"] == "MAINPDF"


def test_figure_context_includes_evidence_tier() -> None:
    payload = {
        "arxiv_id": None,
        "candidate_count": 1,
        "pdf_path": "/tmp/paper.pdf",
        "source_attempts": [],
        "warnings": [],
        "selected_figures": [
            {
                "figure_id": "p1-f1",
                "caption": "Figure 1. Overview.",
                "caption_confidence": 0.56,
                "page": 1,
                "source": "embedded-image",
                "image_path": "/tmp/fig.png",
                "priority_score": 1.0,
                "needs_fallback": False,
                "visual_quality": {"status": "ok", "warnings": []},
                "evidence_tier": "caption_text_grounded",
                "evidence_tier_reason": "embedded-image requires text/caption-grounded analysis",
            }
        ],
    }

    context = workflow.build_figure_context_markdown(payload)

    assert "Evidence Tier: caption_text_grounded" in context
    assert "Analysis Boundary: embedded-image requires text/caption-grounded analysis" in context


def test_select_pdf_attachment_prefers_main_paper_when_low_priority_signal_is_in_title_and_path() -> None:
    attachments = [
        {
            "key": "SUPPLEMENT",
            "filename": "paper-assets.pdf",
            "title": "Supporting Information",
            "contentType": "application/pdf",
            "path": "/tmp/library/supporting-information/paper-assets.pdf",
        },
        {
            "key": "MAINPDF",
            "filename": "paper-assets.pdf",
            "title": "Main Article PDF",
            "contentType": "application/pdf",
            "path": "/tmp/library/paper-assets.pdf",
        },
    ]

    selected = workflow.select_pdf_attachment(attachments)

    assert selected is not None
    assert selected["key"] == "MAINPDF"


def test_select_pdf_attachment_falls_back_to_first_valid_pdf_without_signals() -> None:
    attachments = [
        {
            "key": "FIRSTPDF",
            "filename": "scan-part-1.pdf",
            "contentType": "application/pdf",
            "path": "/tmp/scan-part-1.pdf",
        },
        {
            "key": "SECONDPDF",
            "filename": "scan-part-2.pdf",
            "contentType": "application/pdf",
            "path": "/tmp/scan-part-2.pdf",
        },
    ]

    selected = workflow.select_pdf_attachment(attachments)

    assert selected is not None
    assert selected["key"] == "FIRSTPDF"


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
    figures_payload = {
        "arxiv_id": "2401.01234",
        "pdf_path": str(pdf_path),
        "candidate_count": 1,
        "selected_figures": [
            {
                "figure_id": "fig-1",
                "caption": "Figure 1. Workflow overview.",
                "caption_bbox": [0.0, 0.0, 10.0, 10.0],
                "bbox": [0.0, 0.0, 100.0, 120.0],
                "page": 1,
                "area": 12000.0,
                "image_path": str(tmp_path / "bundle" / "fig-1.png"),
                "priority_score": 9.5,
                "source": "pdf-figure",
                "extraction_strategy": "deterministic",
                "extraction_confidence": 0.95,
                "fallback_reason": None,
                "needs_fallback": False,
            }
        ],
        "source_attempts": [{"stage": "resolve", "status": "resolved", "arxiv_id": "2401.01234"}],
        "warnings": ["arxiv_source_download_failed"],
    }

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
        assert requested_pdf_path == pdf_path
        assert output_dir == tmp_path / "bundle" / "figures"
        assert top_k == 4
        assert max_pages == 1
        assert arxiv_id is None
        assert item_details == details
        assert enable_ocr_fallback is False
        return figures_payload

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(workflow, "extract_figures", fake_extract_figures, raising=False)
    try:
        result = prepare_item_bundle(details, tmp_path / "bundle", max_pages=1)
    finally:
        monkeypatch.undo()

    metadata = json.loads(Path(result["metadata_json"]).read_text(encoding="utf-8"))
    extract = json.loads(Path(result["extract_json"]).read_text(encoding="utf-8"))
    context = Path(result["context_md"]).read_text(encoding="utf-8")
    figures = json.loads(Path(result["figures_json"]).read_text(encoding="utf-8"))
    figure_context = Path(result["figure_context_md"]).read_text(encoding="utf-8")

    assert result["has_pdf"] is True
    assert result["arxiv_id"] == "2401.01234"
    assert result["warnings"] == ["truncated_to_1_pages", "arxiv_source_download_failed"]
    assert result["source_attempts"] == [{"stage": "resolve", "status": "resolved", "arxiv_id": "2401.01234"}]
    assert metadata["title"] == details["title"]
    assert metadata["creators"] == "Zian Chen, Tao He"
    assert metadata["pdf_attachment_key"] == "PDFKEY"
    assert extract["warnings"] == ["truncated_to_1_pages"]
    assert figures == figures_payload
    assert "Perspective on CSP and AI." in context
    assert "This paper studies CSP with AI." in context
    assert "Figure 1. Workflow overview." in figure_context
    assert "arxiv_source_download_failed" in figure_context


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
    assert result["figures_json"] is None
    assert result["figure_context_md"] is None
    assert result["arxiv_id"] is None
    assert result["warnings"] == ["missing_pdf_attachment"]
    assert result["source_attempts"] == []
    assert extract["warnings"] == ["missing_pdf_attachment"]
    assert extract["text"] == ""
    assert "Abstract only." in context


def test_prepare_item_bundle_distinguishes_pdf_attachment_without_local_path(tmp_path: Path) -> None:
    details = {
        "key": "PDFNOPATH",
        "title": "PDF Without Local Path",
        "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
        "date": "2025",
        "DOI": "",
        "url": "",
        "zoteroUrl": "zotero://select/library/items/PDFNOPATH",
        "abstractNote": "Abstract only.",
        "attachments": [
            {
                "key": "PDFKEY",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
            }
        ],
    }

    result = prepare_item_bundle(details, tmp_path / "bundle")

    extract = json.loads(Path(result["extract_json"]).read_text(encoding="utf-8"))

    assert result["has_pdf"] is False
    assert "missing_pdf_path_in_item_details" in result["warnings"]
    assert "missing_pdf_attachment" not in result["warnings"]
    assert "missing_pdf_path_in_item_details" in extract["warnings"]
    assert "missing_pdf_attachment" not in extract["warnings"]


def test_prepare_item_bundle_writes_secondary_sources_json(tmp_path: Path) -> None:
    details = {
        "key": "WEB123",
        "title": "Paper With Secondary Web Source",
        "creators": [],
        "date": "2026",
        "DOI": "",
        "url": "https://example.org/paper",
        "zoteroUrl": "zotero://select/library/items/WEB123",
        "abstractNote": "",
        "extra": "https://mp.weixin.qq.com/s/example?scene=334",
        "attachments": [],
    }

    result = prepare_item_bundle(details, tmp_path / "bundle")

    secondary_path = Path(result["secondary_sources_json"])
    secondary = json.loads(secondary_path.read_text(encoding="utf-8"))
    assert secondary_path.name == "secondary_sources.json"
    assert secondary["sources"][0]["url"] == "https://mp.weixin.qq.com/s/example?scene=334"
    assert secondary["sources"][0]["capture_status"] == "pending_capture"


def test_prepare_item_bundle_keeps_base_bundle_when_figure_extraction_fails(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    make_pdf(pdf_path, ["Abstract\nThis paper studies CSP with AI.", "Methods\nExtra page for truncation."])
    details = {
        "key": "ERR123",
        "title": "Figure Extraction Failure Paper",
        "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
        "date": "2025",
        "DOI": "",
        "url": "",
        "zoteroUrl": "zotero://select/library/items/ERR123",
        "abstractNote": "Abstract text.",
        "attachments": [
            {
                "key": "PDFERR",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "path": str(pdf_path),
            }
        ],
    }

    def fake_extract_figures(*args, **kwargs) -> dict:
        raise RuntimeError("boom")

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(workflow, "extract_figures", fake_extract_figures, raising=False)
    try:
        result = prepare_item_bundle(details, tmp_path / "bundle", max_pages=1)
    finally:
        monkeypatch.undo()

    metadata = json.loads(Path(result["metadata_json"]).read_text(encoding="utf-8"))
    extract = json.loads(Path(result["extract_json"]).read_text(encoding="utf-8"))
    context = Path(result["context_md"]).read_text(encoding="utf-8")

    assert result["has_pdf"] is True
    assert result["figures_json"] is None
    assert result["figure_context_md"] is None
    assert result["arxiv_id"] is None
    assert result["warnings"] == [
        "truncated_to_1_pages",
        "figure_extraction_failed",
        "figure_extraction_error:RuntimeError:boom",
    ]
    assert result["source_attempts"] == [
        {
            "stage": "figure_extraction",
            "status": "error",
            "error_type": "RuntimeError",
            "error_message": "boom",
        }
    ]
    assert metadata["title"] == details["title"]
    assert extract["warnings"] == ["truncated_to_1_pages"]
    assert "This paper studies CSP with AI." in context


def test_prepare_item_bundle_updates_existing_run_manifest(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    workdir = tmp_path / "runs" / "2026-04-24" / "manifest-paper"
    workdir.mkdir(parents=True)
    make_pdf(pdf_path, ["Abstract\nThis paper studies CSP with AI.", "Methods\nDiffusion and GAN models."])

    details = {
        "key": "RUN123",
        "title": "Manifest Paper",
        "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
        "date": "2025",
        "DOI": "10.1000/manifest",
        "url": "https://example.org/manifest",
        "zoteroUrl": "zotero://select/library/items/RUN123",
        "abstractNote": "Manifest abstract.",
        "extra": "https://mp.weixin.qq.com/s/manifest",
        "attachments": [
            {
                "key": "PDFRUN",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "path": str(pdf_path),
            }
        ],
    }
    (workdir / "run.json").write_text(
        json.dumps(
            {
                "title": details["title"],
                "slug": "manifest-paper",
                "item_key": details["key"],
                "created_at": "2026-04-24T09:00:00",
                "status": "initialized",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    figures_payload = {
        "arxiv_id": "2401.01234",
        "pdf_path": str(pdf_path),
        "candidate_count": 0,
        "selected_figures": [],
        "source_attempts": [{"stage": "resolve", "status": "resolved", "arxiv_id": "2401.01234"}],
        "warnings": [],
    }

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(workflow, "extract_figures", lambda *args, **kwargs: figures_payload, raising=False)
    try:
        result = prepare_item_bundle(details, workdir, max_pages=1)
    finally:
        monkeypatch.undo()

    manifest = json.loads((workdir / "run.json").read_text(encoding="utf-8"))
    assert manifest["title"] == details["title"]
    assert manifest["slug"] == "manifest-paper"
    assert manifest["item_key"] == details["key"]
    assert manifest["created_at"] == "2026-04-24T09:00:00"
    assert manifest["status"] == "prepared"
    assert manifest["pdf_path"] == str(pdf_path)
    assert manifest["metadata_json"] == result["metadata_json"]
    assert manifest["extract_json"] == result["extract_json"]
    assert manifest["figures_json"] == result["figures_json"]
    assert manifest["figure_context_md"] == result["figure_context_md"]
    assert manifest["arxiv_id"] == "2401.01234"
    assert manifest["warnings"] == ["truncated_to_1_pages"]
    assert manifest["secondary_sources_json"] == result["secondary_sources_json"]
    secondary = json.loads(Path(result["secondary_sources_json"]).read_text(encoding="utf-8"))
    assert secondary["sources"][0]["url"] == "https://mp.weixin.qq.com/s/manifest"


def test_prepare_item_bundle_removes_stale_figure_artifacts_on_rerun_failure(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    workdir = tmp_path / "runs" / "2026-04-24" / "stale-figures-paper"
    workdir.mkdir(parents=True)
    make_pdf(pdf_path, ["Abstract\nThis paper studies CSP with AI.", "Methods\nDiffusion and GAN models."])

    success_details = {
        "key": "STALE1",
        "title": "Stale Figures Paper",
        "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
        "date": "2025",
        "DOI": "10.1000/stale",
        "url": "https://example.org/stale",
        "zoteroUrl": "zotero://select/library/items/STALE1",
        "abstractNote": "First run with figures.",
        "attachments": [
            {
                "key": "PDFSTALE",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "path": str(pdf_path),
            }
        ],
    }
    failure_details = {
        **success_details,
        "attachments": [],
    }
    figures_payload = {
        "arxiv_id": "2401.01234",
        "pdf_path": str(pdf_path),
        "candidate_count": 1,
        "selected_figures": [],
        "source_attempts": [{"stage": "resolve", "status": "resolved", "arxiv_id": "2401.01234"}],
        "warnings": [],
    }

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(workflow, "extract_figures", lambda *args, **kwargs: figures_payload, raising=False)
    try:
        first_result = prepare_item_bundle(success_details, workdir, max_pages=1)
    finally:
        monkeypatch.undo()

    assert Path(first_result["figures_json"]).exists()
    assert Path(first_result["figure_context_md"]).exists()

    second_result = prepare_item_bundle(failure_details, workdir, max_pages=1)

    assert second_result["figures_json"] is None
    assert second_result["figure_context_md"] is None
    assert not (workdir / "figures.json").exists()
    assert not (workdir / "figure_context.md").exists()
