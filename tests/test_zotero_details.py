from zotero_paperread.zotero_details import (
    codex_summary_titles_from_details,
    next_version_suffix_from_details,
    primary_pdf_path_from_details,
)


def test_codex_summary_titles_from_html_notes() -> None:
    details = {
        "notes": [
            "<h1>[Codex Summary] Paper A - 2026-04-26</h1><p>body</p>",
            "<h1>Manual note</h1>",
            "<h2>[Codex Summary] Paper A - 2026-04-26 (v2)</h2>",
        ]
    }

    assert codex_summary_titles_from_details(details) == [
        "[Codex Summary] Paper A - 2026-04-26",
        "[Codex Summary] Paper A - 2026-04-26 (v2)",
    ]


def test_codex_summary_titles_from_markdown_notes() -> None:
    details = {"notes": ["# [Codex Summary] Paper B - 2026-04-26\n\nBody", "No heading"]}

    assert codex_summary_titles_from_details(details) == ["[Codex Summary] Paper B - 2026-04-26"]


def test_next_version_suffix_from_details() -> None:
    details = {
        "notes": [
            "<h1>[Codex Summary] Paper A - 2026-04-26</h1>",
            "<h1>[Codex Summary] Paper A - 2026-04-26 (v2)</h1>",
        ]
    }

    assert next_version_suffix_from_details(details, paper_title="Paper A", generated_date="2026-04-26") == " (v3)"


def test_primary_pdf_path_from_details() -> None:
    details = {
        "attachments": [
            {
                "key": "SUPP",
                "filename": "supporting-information.pdf",
                "title": "Supporting Information",
                "contentType": "application/pdf",
                "path": "/tmp/supporting-information.pdf",
            },
            {"key": "MAIN", "filename": "paper.pdf", "title": "PDF", "contentType": "application/pdf", "path": "/tmp/paper.pdf"},
        ]
    }

    assert primary_pdf_path_from_details(details) == "/tmp/paper.pdf"
