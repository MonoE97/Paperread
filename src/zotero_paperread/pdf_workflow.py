from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PDFOutputPaths:
    analysis_dir: Path
    final_note_path: Path
    version_suffix: str


def allocate_pdf_output_paths(pdf_path: Path) -> PDFOutputPaths:
    """Return non-overwriting analysis and final-note paths next to a PDF."""
    resolved = Path(pdf_path).expanduser()
    parent = resolved.parent
    stem = resolved.stem
    version = 1
    while True:
        suffix = "" if version == 1 else f"_v{version}"
        analysis_dir = parent / f"{stem}_analysis{suffix}"
        final_note_path = parent / f"{stem}_note{suffix}.md"
        if not analysis_dir.exists() and not final_note_path.exists():
            return PDFOutputPaths(
                analysis_dir=analysis_dir,
                final_note_path=final_note_path,
                version_suffix=suffix,
            )
        version += 1


def _clean_override(value: str | None) -> str:
    return str(value).strip() if value is not None else ""


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
