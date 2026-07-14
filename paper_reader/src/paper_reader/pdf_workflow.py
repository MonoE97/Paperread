from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz

def _clean_override(value: str | None) -> str:
    return str(value).strip() if value is not None else ""


def validate_pdf_readable(pdf_path: Path) -> None:
    """Raise ValueError if a PDF cannot be opened before creating output artifacts."""
    resolved = Path(pdf_path).expanduser()
    try:
        doc = fitz.open(resolved)
    except Exception as exc:
        raise ValueError(f"PDF unreadable: {resolved}: {exc}") from exc
    try:
        _page_count = doc.page_count
    finally:
        doc.close()


def build_pdf_metadata(
    pdf_path: Path,
    *,
    title: str | None = None,
    authors: str | None = None,
    date: str | None = None,
    doi: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """Build note-renderer metadata for a direct local PDF path."""
    resolved = Path(pdf_path).expanduser()
    title_text = _clean_override(title) or resolved.stem
    return {
        "key": "",
        "title": title_text,
        "creators": _clean_override(authors),
        "date": _clean_override(date),
        "DOI": _clean_override(doi),
        "url": _clean_override(url),
        "zoteroUrl": "",
        "abstractNote": "",
        "pdf_path": str(resolved),
        "pdf_attachment_key": "",
        "pdf_filename": resolved.name,
        "source_type": "pdf_path",
    }
