from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz


def extract_pdf(pdf_path: Path, max_pages: int | None = None) -> dict[str, Any]:
    """Extract text and lightweight metadata from a PDF."""
    resolved = Path(pdf_path).expanduser()
    if not resolved.exists():
        raise FileNotFoundError(f"PDF not found: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"PDF path is not a file: {resolved}")

    warnings: list[str] = []
    doc = fitz.open(resolved)
    try:
        page_count = doc.page_count
        limit = page_count if max_pages is None else min(max_pages, page_count)
        if max_pages is not None and max_pages < page_count:
            warnings.append(f"truncated_to_{max_pages}_pages")

        page_texts: list[str] = []
        for index in range(limit):
            text = doc.load_page(index).get_text("text").strip()
            if text:
                page_texts.append(f"\n\n<!-- page:{index + 1} -->\n{text}")

        combined = "".join(page_texts).strip()
        if not combined:
            warnings.append("no_extractable_text")

        return {
            "pdf_path": str(resolved),
            "page_count": page_count,
            "extracted_pages": limit,
            "text": combined,
            "warnings": warnings,
        }
    finally:
        doc.close()
