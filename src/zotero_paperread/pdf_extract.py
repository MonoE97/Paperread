from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import fitz

SECTION_KIND_BY_HEADING = {
    "abstract": "abstract",
    "introduction": "introduction",
    "background": "background",
    "methods": "methods",
    "method": "methods",
    "materials and methods": "methods",
    "experimental": "experimental",
    "experimental section": "experimental",
    "computational": "computational",
    "computational details": "computational",
    "dft calculations": "computational",
    "results": "results",
    "results and discussion": "results",
    "discussion": "discussion",
    "conclusion": "conclusion",
    "conclusions": "conclusion",
    "limitations": "limitations",
    "references": "references",
    "acknowledgements": "acknowledgements",
    "acknowledgments": "acknowledgements",
    "electrochemical performance": "results",
    "ionic conductivity": "results",
    "characterization": "results",
}

TABLE_VALUE_SIGNALS = (
    "accuracy",
    "mae",
    "rmse",
    "r2",
    "speedup",
    "baseline",
    "ablation",
    "conductivity",
    "ionic conductivity",
    "activation energy",
    "diffusion barrier",
    "capacity",
    "cycle life",
    "rate performance",
    "energy density",
    "voltage",
    "bandgap",
    "formation energy",
    "ehull",
)

NUMERIC_VALUE_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:\s*(?:%|x|eV|mS|cm-1|cm²|cm2))?")
NUMBERED_HEADING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVX]+\.?)\s+", flags=re.IGNORECASE)


def _normalize_heading_line(line: str) -> str:
    text = NUMBERED_HEADING_RE.sub("", line.strip())
    text = re.sub(r"[:.\s]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def _heading_title(line: str) -> str:
    title = NUMBERED_HEADING_RE.sub("", line.strip())
    title = re.sub(r"[:.\s]+$", "", title)
    return re.sub(r"\s+", " ", title)


def _classify_heading(line: str) -> tuple[str, str, str] | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 120:
        return None
    normalized = _normalize_heading_line(stripped)
    kind = SECTION_KIND_BY_HEADING.get(normalized)
    if kind is None:
        return None
    return kind, _heading_title(stripped), "high"


def _page_warnings(text: str) -> list[str]:
    if not text:
        return ["empty_page_text"]
    if len(text) < 40:
        return ["short_page_text"]
    return []


def _build_sections(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for page in pages:
        page_number = int(page["page"])
        for line_index, line in enumerate(str(page.get("text", "")).splitlines()):
            classified = _classify_heading(line)
            if classified is None:
                continue
            kind, title, confidence = classified
            markers.append(
                {
                    "kind": kind,
                    "title": title,
                    "page": page_number,
                    "line_index": line_index,
                    "confidence": confidence,
                }
            )

    sections: list[dict[str, Any]] = []
    for marker_index, marker in enumerate(markers):
        next_marker = markers[marker_index + 1] if marker_index + 1 < len(markers) else None
        section_lines: list[str] = []
        end_page = int(marker["page"])
        for page in pages:
            page_number = int(page["page"])
            if page_number < marker["page"]:
                continue
            if next_marker is not None and page_number > next_marker["page"]:
                break

            lines = str(page.get("text", "")).splitlines()
            start = marker["line_index"] + 1 if page_number == marker["page"] else 0
            end = (
                next_marker["line_index"]
                if next_marker is not None and page_number == next_marker["page"]
                else len(lines)
            )
            if start < end:
                section_lines.extend(lines[start:end])
            end_page = page_number
            if next_marker is not None and page_number == next_marker["page"]:
                break

        section_text = "\n".join(line.strip() for line in section_lines if line.strip()).strip()
        sections.append(
            {
                "kind": marker["kind"],
                "title": marker["title"],
                "start_page": marker["page"],
                "end_page": end_page,
                "text": section_text,
                "confidence": marker["confidence"],
                "locator": f"context.md page {marker['page']} section {marker['title']}",
            }
        )
    return sections


def _signals_in_text(text: str) -> list[str]:
    lowered = text.lower()
    signals: list[str] = []
    for signal in TABLE_VALUE_SIGNALS:
        if signal in lowered and signal not in signals:
            signals.append(signal)
    return signals


def _section_for_page(sections: list[dict[str, Any]], page_number: int) -> str:
    candidates = [
        section
        for section in sections
        if int(section.get("start_page", 0)) <= page_number <= int(section.get("end_page", 0))
    ]
    if not candidates:
        return "Unknown"
    return str(candidates[-1].get("title", "")).strip() or "Unknown"


def _candidate_confidence(text: str, signals: list[str]) -> str:
    numeric_count = len(NUMERIC_VALUE_RE.findall(text))
    table_like = bool(re.search(r"\b(?:table|tab\.)\b", text, flags=re.IGNORECASE))
    if table_like and len(signals) >= 2 and numeric_count >= 2:
        return "high"
    if signals and numeric_count:
        return "medium"
    return "low"


def _build_table_candidates(pages: list[dict[str, Any]], sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for page in pages:
        page_number = int(page["page"])
        text = str(page.get("text", "")).strip()
        if not text or not NUMERIC_VALUE_RE.search(text):
            continue
        signals = _signals_in_text(text)
        if not signals:
            continue
        section_title = _section_for_page(sections, page_number)
        candidate_index = len(candidates) + 1
        candidates.append(
            {
                "page": page_number,
                "section": section_title,
                "text": text,
                "signals": signals,
                "confidence": _candidate_confidence(text, signals),
                "locator": f"context.md page {page_number} section {section_title} table_candidate {candidate_index}",
            }
        )
    return candidates


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
        pages: list[dict[str, Any]] = []
        for index in range(limit):
            text = doc.load_page(index).get_text("text").strip()
            pages.append(
                {
                    "page": index + 1,
                    "text": text,
                    "char_count": len(text),
                    "warnings": _page_warnings(text),
                }
            )
            if text:
                page_texts.append(f"\n\n<!-- page:{index + 1} -->\n{text}")

        combined = "".join(page_texts).strip()
        if not combined:
            warnings.append("no_extractable_text")
        sections = _build_sections(pages)
        table_candidates = _build_table_candidates(pages, sections)

        return {
            "pdf_path": str(resolved),
            "page_count": page_count,
            "extracted_pages": limit,
            "text": combined,
            "warnings": warnings,
            "pages": pages,
            "sections": sections,
            "table_candidates": table_candidates,
        }
    finally:
        doc.close()
