from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paperread.figures import extract_figures
from paperread.pdf_workflow import build_pdf_metadata
from paperread.pdf_extract import extract_pdf
from paperread.runs import write_run_manifest
from paperread.secondary_sources import build_secondary_sources

LOW_PRIORITY_PDF_TERMS = (
    "appendix",
    "appendices",
    "supplement",
    "supplemental",
    "supplementary",
    "supporting information",
    "supporting-information",
)


def format_creators(creators: list[dict[str, Any]]) -> str:
    """Convert Zotero creators into a compact display string."""
    names: list[str] = []
    for creator in creators:
        if creator.get("name"):
            names.append(str(creator["name"]))
            continue
        first = str(creator.get("firstName", "")).strip()
        last = str(creator.get("lastName", "")).strip()
        full = " ".join(part for part in [first, last] if part).strip()
        if full:
            names.append(full)
    return ", ".join(names)


def _attachment_priority_key(index: int, attachment: dict[str, Any]) -> tuple[int, int]:
    """Rank local PDF attachments while preserving stable fallback order."""
    signals = " ".join(
        str(attachment.get(field, "")).strip().lower()
        for field in ("filename", "path", "title")
    )
    penalty = int(any(term in signals for term in LOW_PRIORITY_PDF_TERMS))
    return (penalty, index)


def select_pdf_attachment(attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select a local PDF attachment, preferring the main paper over appendices."""
    candidates = [
        (index, attachment)
        for index, attachment in enumerate(attachments)
        if attachment.get("contentType") == "application/pdf" and attachment.get("path")
    ]
    if not candidates:
        return None
    _, selected = min(candidates, key=lambda item: _attachment_priority_key(item[0], item[1]))
    return selected


def has_pdf_attachment(attachments: list[dict[str, Any]]) -> bool:
    """Return whether item details include any PDF attachment entry."""
    return any(attachment.get("contentType") == "application/pdf" for attachment in attachments)


def build_metadata(details: dict[str, Any]) -> dict[str, Any]:
    """Build normalized metadata for note rendering."""
    attachment = select_pdf_attachment(details.get("attachments", []))
    return {
        "key": details.get("key", ""),
        "title": details.get("title", ""),
        "creators": format_creators(details.get("creators", [])),
        "date": details.get("date", ""),
        "DOI": details.get("DOI", ""),
        "url": details.get("url", ""),
        "zoteroUrl": details.get("zoteroUrl", ""),
        "abstractNote": details.get("abstractNote", ""),
        "pdf_path": attachment.get("path", "") if attachment else "",
        "pdf_attachment_key": attachment.get("key", "") if attachment else "",
        "pdf_filename": attachment.get("filename", "") if attachment else "",
    }


def build_context_markdown(metadata: dict[str, Any], extract: dict[str, Any]) -> str:
    """Build a single markdown context file for Codex summarization."""
    warnings = extract.get("warnings", [])
    warning_lines = "\n".join(f"- {item}" for item in warnings) if warnings else "- none"
    full_text = extract.get("text", "").strip() or "_No extracted PDF text available._"
    return (
        f"# Zotero Paper Context\n\n"
        f"## Metadata\n\n"
        f"- Title: {metadata.get('title', '')}\n"
        f"- Creators: {metadata.get('creators', '')}\n"
        f"- Date: {metadata.get('date', '')}\n"
        f"- DOI: {metadata.get('DOI', '')}\n"
        f"- URL: {metadata.get('url', '')}\n"
        f"- Zotero URL: {metadata.get('zoteroUrl', '')}\n"
        f"- PDF Path: {metadata.get('pdf_path', '')}\n\n"
        f"## Abstract\n\n"
        f"{metadata.get('abstractNote', '') or '_No abstract available._'}\n\n"
        f"## Extraction Warnings\n\n"
        f"{warning_lines}\n\n"
        f"## Full Text\n\n"
        f"{full_text}\n"
    )


def build_section_context_markdown(metadata: dict[str, Any], extract: dict[str, Any]) -> str:
    """Build a section-aware navigation aid while preserving canonical locators."""
    pages = extract.get("pages", []) if isinstance(extract.get("pages"), list) else []
    sections = extract.get("sections", []) if isinstance(extract.get("sections"), list) else []
    table_candidates = extract.get("table_candidates", [])
    table_candidates = table_candidates if isinstance(table_candidates, list) else []

    section_blocks: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_blocks.append(
            "\n".join(
                [
                    f"### {section.get('title', 'Unknown')}",
                    f"- Kind: {section.get('kind', 'unknown')}",
                    f"- Pages: {section.get('start_page', '')}-{section.get('end_page', '')}",
                    f"- Confidence: {section.get('confidence', 'unknown')}",
                    f"- Locator: {section.get('locator', '')}",
                    "",
                    str(section.get("text", "")).strip() or "_No section text available._",
                ]
            )
        )

    candidate_blocks: list[str] = []
    for index, candidate in enumerate(table_candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        signals = candidate.get("signals", [])
        signal_text = ", ".join(str(signal) for signal in signals) if isinstance(signals, list) else ""
        candidate_blocks.append(
            "\n".join(
                [
                    f"### Candidate {index}",
                    f"- Locator: {candidate.get('locator', '')}",
                    f"- Confidence: {candidate.get('confidence', 'unknown')}",
                    f"- Signals: {signal_text}",
                    "",
                    str(candidate.get("text", "")).strip() or "_No candidate text available._",
                ]
            )
        )

    sections_body = "\n\n".join(section_blocks) if section_blocks else "_No sections detected._"
    candidates_body = "\n\n".join(candidate_blocks) if candidate_blocks else "_No table/value candidates detected._"
    return (
        "# Section Context\n\n"
        "## Extraction Summary\n\n"
        f"- PDF Path: {extract.get('pdf_path', '')}\n"
        f"- Title: {metadata.get('title', '')}\n"
        f"- Page Count: {extract.get('page_count', 0)}\n"
        f"- Extracted Pages: {extract.get('extracted_pages', 0)}\n"
        f"- Page Record Count: {len(pages)}\n"
        f"- Section Count: {len(section_blocks)}\n"
        f"- Table Candidate Count: {len(candidate_blocks)}\n\n"
        "## Sections\n\n"
        f"{sections_body}\n\n"
        "## Table / Value Candidates\n\n"
        f"{candidates_body}\n"
    )


def build_figure_context_markdown(figures_payload: dict[str, Any]) -> str:
    """Build a markdown summary of extracted figure candidates."""
    warnings = figures_payload.get("warnings", [])
    warning_lines = "\n".join(f"- {item}" for item in warnings) if warnings else "- none"

    source_attempts = figures_payload.get("source_attempts", [])
    attempt_lines = (
        "\n".join(f"- {json.dumps(item, ensure_ascii=False, sort_keys=True)}" for item in source_attempts)
        if source_attempts
        else "- none"
    )

    selected_figures = figures_payload.get("selected_figures", [])
    if selected_figures:
        figure_sections: list[str] = []
        for figure in selected_figures:
            figure_sections.append(
                "\n".join(
                    [
                        f"### {figure.get('figure_id', 'unknown')}",
                        f"- Caption: {figure.get('caption', '') or '_No caption available._'}",
                        f"- Caption Confidence: {figure.get('caption_confidence', 0.0)}",
                        f"- Page: {figure.get('page', '')}",
                        f"- Source: {figure.get('source', '')}",
                        f"- Image Path: {figure.get('image_path', '')}",
                        f"- Priority Score: {figure.get('priority_score', '')}",
                        f"- Needs Fallback: {figure.get('needs_fallback', False)}",
                        f"- Visual Quality: {json.dumps(figure.get('visual_quality', {}), ensure_ascii=False, sort_keys=True)}",
                        f"- Evidence Tier: {figure.get('evidence_tier', 'unknown')}",
                        f"- Analysis Boundary: {figure.get('evidence_tier_reason', '')}",
                    ]
                )
            )
        figure_body = "\n\n".join(figure_sections)
    else:
        figure_body = "_No figures selected._"

    return (
        f"# Figure Context\n\n"
        f"## Summary\n\n"
        f"- arXiv ID: {figures_payload.get('arxiv_id') or 'none'}\n"
        f"- Candidate Count: {figures_payload.get('candidate_count', 0)}\n"
        f"- PDF Path: {figures_payload.get('pdf_path', '')}\n\n"
        f"## Source Attempts\n\n"
        f"{attempt_lines}\n\n"
        f"## Warnings\n\n"
        f"{warning_lines}\n\n"
        f"## Selected Figures\n\n"
        f"{figure_body}\n"
    )


def merge_warnings(*warning_lists: list[str]) -> list[str]:
    """Merge warning lists while preserving first-seen order."""
    merged: list[str] = []
    seen: set[str] = set()
    for warning_list in warning_lists:
        for warning in warning_list:
            if warning in seen:
                continue
            seen.add(warning)
            merged.append(warning)
    return merged


def clear_optional_figure_artifacts(bundle_dir: Path) -> None:
    """Remove stale figure artifacts when the current run has no figure output."""
    for path in [bundle_dir / "figures.json", bundle_dir / "figure_context.md"]:
        path.unlink(missing_ok=True)


def _build_figure_extraction_error_context(error: Exception) -> tuple[dict[str, str], str]:
    """Capture a compact, deterministic summary for figure extraction failures."""
    error_type = type(error).__name__
    error_message = str(error).strip() or "no_message"
    return (
        {
            "stage": "figure_extraction",
            "status": "error",
            "error_type": error_type,
            "error_message": error_message,
        },
        f"figure_extraction_error:{error_type}:{error_message}",
    )


def _empty_secondary_sources(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_key": "",
        "title": str(metadata.get("title", "")),
        "usage_boundary": "cross-check only; must not be cited in evidence_summary",
        "sources": [],
        "warnings": [],
    }


def _prepare_bundle_from_metadata(
    metadata: dict[str, Any],
    workdir: Path,
    *,
    max_pages: int | None = None,
    item_details: dict[str, Any] | None = None,
    secondary_sources: dict[str, Any] | None = None,
    missing_pdf_warning: str = "missing_pdf_attachment",
) -> dict[str, Any]:
    """Prepare metadata, extraction, and context files from normalized source metadata."""
    bundle_dir = Path(workdir).expanduser().resolve()
    bundle_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = bundle_dir / "run.json"

    pdf_path = metadata["pdf_path"]
    if pdf_path:
        extract = extract_pdf(Path(pdf_path), max_pages=max_pages)
    else:
        extract = {
            "pdf_path": "",
            "page_count": 0,
            "extracted_pages": 0,
            "text": "",
            "warnings": [missing_pdf_warning],
        }

    metadata_path = bundle_dir / "metadata.json"
    extract_path = bundle_dir / "extract.json"
    context_path = bundle_dir / "context.md"
    section_context_path = bundle_dir / "section_context.md"
    secondary_sources_path = bundle_dir / "secondary_sources.json"
    figures_path: Path | None = None
    figure_context_path: Path | None = None
    figures_payload: dict[str, Any] | None = None
    source_attempts: list[dict[str, Any]] = []
    figure_error_warning: str | None = None
    resolved_secondary_sources = secondary_sources or _empty_secondary_sources(metadata)

    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    extract_path.write_text(json.dumps(extract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    context_path.write_text(build_context_markdown(metadata, extract), encoding="utf-8")
    section_context_path.write_text(build_section_context_markdown(metadata, extract), encoding="utf-8")
    secondary_sources_path.write_text(
        json.dumps(resolved_secondary_sources, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if pdf_path:
        try:
            figures_payload = extract_figures(
                Path(pdf_path),
                output_dir=bundle_dir / "figures",
                max_pages=max_pages,
                item_details=item_details,
            )
        except Exception as error:
            error_attempt, figure_error_warning = _build_figure_extraction_error_context(error)
            source_attempts = [error_attempt]
            figures_payload = None
        else:
            source_attempts = list(figures_payload.get("source_attempts", []))
            figures_path = bundle_dir / "figures.json"
            figure_context_path = bundle_dir / "figure_context.md"
            figures_path.write_text(json.dumps(figures_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            figure_context_path.write_text(build_figure_context_markdown(figures_payload), encoding="utf-8")
    if figures_payload is None:
        clear_optional_figure_artifacts(bundle_dir)

    figure_warnings = list(figures_payload.get("warnings", [])) if figures_payload else []
    if pdf_path and figures_payload is None:
        figure_warnings.append("figure_extraction_failed")
    if figure_error_warning:
        figure_warnings.append(figure_error_warning)
    warnings = merge_warnings(list(extract.get("warnings", [])), figure_warnings)
    result = {
        "metadata_json": str(metadata_path),
        "extract_json": str(extract_path),
        "context_md": str(context_path),
        "section_context_md": str(section_context_path),
        "secondary_sources_json": str(secondary_sources_path),
        "figures_json": str(figures_path) if figures_path else None,
        "figure_context_md": str(figure_context_path) if figure_context_path else None,
        "arxiv_id": figures_payload.get("arxiv_id") if figures_payload else None,
        "warnings": warnings,
        "source_attempts": source_attempts,
        "has_pdf": bool(pdf_path),
        "metadata": metadata,
    }

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "pdf_path": pdf_path,
                "metadata_json": result["metadata_json"],
                "extract_json": result["extract_json"],
                "section_context_md": result["section_context_md"],
                "secondary_sources_json": result["secondary_sources_json"],
                "figures_json": result["figures_json"],
                "figure_context_md": result["figure_context_md"],
                "arxiv_id": result["arxiv_id"],
                "warnings": result["warnings"],
                "status": "prepared",
            }
        )
        write_run_manifest(bundle_dir, manifest)

    return result


def prepare_item_bundle(details: dict[str, Any], workdir: Path, max_pages: int | None = None) -> dict[str, Any]:
    """Prepare metadata, extraction, and context files from raw Zotero item details."""
    metadata = build_metadata(details)
    attachments = details.get("attachments", [])
    if not isinstance(attachments, list):
        attachments = []
    missing_pdf_warning = (
        "missing_pdf_path_in_item_details"
        if has_pdf_attachment([attachment for attachment in attachments if isinstance(attachment, dict)])
        else "missing_pdf_attachment"
    )
    return _prepare_bundle_from_metadata(
        metadata,
        workdir,
        max_pages=max_pages,
        item_details=details,
        secondary_sources=build_secondary_sources(details),
        missing_pdf_warning=missing_pdf_warning,
    )


def prepare_pdf_bundle(
    pdf_path: Path,
    workdir: Path,
    *,
    title: str | None = None,
    authors: str | None = None,
    date: str | None = None,
    doi: str | None = None,
    url: str | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Prepare metadata, extraction, and context files from a direct local PDF path."""
    metadata = build_pdf_metadata(
        pdf_path,
        title=title,
        authors=authors,
        date=date,
        doi=doi,
        url=url,
    )
    return _prepare_bundle_from_metadata(
        metadata,
        workdir,
        max_pages=max_pages,
        item_details=None,
        secondary_sources=_empty_secondary_sources(metadata),
    )
