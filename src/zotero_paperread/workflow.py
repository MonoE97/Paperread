from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zotero_paperread.pdf_extract import extract_pdf


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


def prepare_item_bundle(details: dict[str, Any], workdir: Path, max_pages: int | None = None) -> dict[str, Any]:
    """Prepare metadata, extraction, and context files from raw Zotero item details."""
    bundle_dir = Path(workdir).expanduser().resolve()
    bundle_dir.mkdir(parents=True, exist_ok=True)

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

    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    extract_path.write_text(json.dumps(extract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    context_path.write_text(build_context_markdown(metadata, extract), encoding="utf-8")

    return {
        "metadata_json": str(metadata_path),
        "extract_json": str(extract_path),
        "context_md": str(context_path),
        "has_pdf": bool(pdf_path),
    }
