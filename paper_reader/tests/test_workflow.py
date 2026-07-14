from __future__ import annotations

from paper_reader import workflow


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


def test_select_pdf_attachment_prefers_main_paper_when_signal_is_in_title_and_path() -> None:
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


def test_build_metadata_and_context_helpers_are_pure_rendering_helpers() -> None:
    details = {
        "key": "PARENT1",
        "title": "A Useful Paper",
        "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
        "date": "2026",
        "DOI": "10.1000/example",
        "url": "https://example.test/paper",
        "zoteroUrl": "zotero://select/library/items/PARENT1",
        "abstractNote": "An abstract.",
        "attachments": [],
    }

    metadata = workflow.build_metadata(details)
    context = workflow.build_context_markdown(
        metadata,
        {"warnings": [], "text": "Extracted evidence."},
    )

    assert metadata["creators"] == "Ada Lovelace"
    assert "An abstract." in context
    assert "Extracted evidence." in context


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


def test_workflow_module_does_not_expose_v1_bundle_mutators() -> None:
    assert not hasattr(workflow, "_prepare_bundle_from_metadata")
    assert not hasattr(workflow, "prepare_item_bundle")
    assert not hasattr(workflow, "prepare_pdf_bundle")
