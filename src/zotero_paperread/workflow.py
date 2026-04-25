from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zotero_paperread.figures import extract_figures
from zotero_paperread.pdf_extract import extract_pdf
from zotero_paperread.runs import write_run_manifest


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


def select_pdf_attachment(attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the first local PDF attachment from Zotero item details."""
    for attachment in attachments:
        if attachment.get("contentType") == "application/pdf" and attachment.get("path"):
            return attachment
    return None


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
                        f"- Page: {figure.get('page', '')}",
                        f"- Source: {figure.get('source', '')}",
                        f"- Image Path: {figure.get('image_path', '')}",
                        f"- Priority Score: {figure.get('priority_score', '')}",
                        f"- Needs Fallback: {figure.get('needs_fallback', False)}",
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


def prepare_item_bundle(details: dict[str, Any], workdir: Path, max_pages: int | None = None) -> dict[str, Any]:
    """Prepare metadata, extraction, and context files from raw Zotero item details."""
    bundle_dir = Path(workdir).expanduser().resolve()
    bundle_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = bundle_dir / "run.json"

    metadata = build_metadata(details)
    pdf_path = metadata["pdf_path"]
    if pdf_path:
        extract = extract_pdf(Path(pdf_path), max_pages=max_pages)
    else:
        extract = {
            "pdf_path": "",
            "page_count": 0,
            "extracted_pages": 0,
            "text": "",
            "warnings": ["missing_pdf_attachment"],
        }

    metadata_path = bundle_dir / "metadata.json"
    extract_path = bundle_dir / "extract.json"
    context_path = bundle_dir / "context.md"
    figures_path: Path | None = None
    figure_context_path: Path | None = None
    figures_payload: dict[str, Any] | None = None
    source_attempts: list[dict[str, Any]] = []

    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    extract_path.write_text(json.dumps(extract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    context_path.write_text(build_context_markdown(metadata, extract), encoding="utf-8")

    if pdf_path:
        try:
            figures_payload = extract_figures(
                Path(pdf_path),
                output_dir=bundle_dir / "figures",
                max_pages=max_pages,
                item_details=details,
            )
        except Exception:
            source_attempts = [{"stage": "figure_extraction", "status": "error"}]
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
    warnings = merge_warnings(list(extract.get("warnings", [])), figure_warnings)
    result = {
        "metadata_json": str(metadata_path),
        "extract_json": str(extract_path),
        "context_md": str(context_path),
        "figures_json": str(figures_path) if figures_path else None,
        "figure_context_md": str(figure_context_path) if figure_context_path else None,
        "arxiv_id": figures_payload.get("arxiv_id") if figures_payload else None,
        "warnings": warnings,
        "source_attempts": source_attempts,
        "has_pdf": bool(pdf_path),
    }

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "pdf_path": pdf_path,
                "metadata_json": result["metadata_json"],
                "extract_json": result["extract_json"],
                "figures_json": result["figures_json"],
                "figure_context_md": result["figure_context_md"],
                "arxiv_id": result["arxiv_id"],
                "warnings": result["warnings"],
                "status": "prepared",
            }
        )
        write_run_manifest(bundle_dir, manifest)

    return result
