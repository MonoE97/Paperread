from __future__ import annotations

import re
from typing import Any


HTTP_URL_RE = re.compile(r"https?://[^\s<>()\"']+")
TRAILING_URL_PUNCTUATION = ".,;:!?)，。；：！？）】》"
USAGE_BOUNDARY = "cross-check only; must not be cited in evidence_summary"
NON_ACTIONABLE_WARNING_CODES = {"sqlite_immutable_snapshot_used", "sqlite_ro_retry_after_locked"}


def _clean_url(url: str) -> str:
    return url.rstrip(TRAILING_URL_PUNCTUATION)


def extract_http_urls(text: str) -> list[str]:
    """Extract stable, de-duplicated HTTP(S) URLs from free-form text."""
    urls: list[str] = []
    seen: set[str] = set()
    for match in HTTP_URL_RE.finditer(str(text or "")):
        url = _clean_url(match.group(0))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _extra_provenance(details: dict[str, Any]) -> str:
    paperread = details.get("_paperread")
    if not isinstance(paperread, dict):
        return "mcp_payload"
    enrichment = paperread.get("enrichment")
    if not isinstance(enrichment, dict):
        return "mcp_payload"
    extra = enrichment.get("extra")
    if not isinstance(extra, dict):
        return "mcp_payload"
    return str(extra.get("source", "mcp_payload"))


def _paperread_warnings(details: dict[str, Any]) -> list[str]:
    paperread = details.get("_paperread")
    if not isinstance(paperread, dict):
        return []
    warnings = paperread.get("warnings")
    if not isinstance(warnings, list):
        return []
    cleaned: list[str] = []
    for item in warnings:
        warning = str(item).strip()
        if not warning or warning in NON_ACTIONABLE_WARNING_CODES:
            continue
        cleaned.append(warning)
    return cleaned


def build_secondary_sources(details: dict[str, Any]) -> dict[str, Any]:
    """Build the run artifact describing secondary web sources from Zotero Extra."""
    extra = str(details.get("extra", "")).strip()
    warnings = _paperread_warnings(details)
    if not extra:
        warnings = warnings + ["missing_extra_field"]

    urls = extract_http_urls(extra)
    if extra and not urls:
        warnings = warnings + ["extra_contains_no_http_url"]

    sources = [
        {
            "source_id": f"secondary-{index:03d}",
            "url": url,
            "source_field": "extra",
            "source_provenance": _extra_provenance(details),
            "capture_status": "pending_capture",
        }
        for index, url in enumerate(urls, start=1)
    ]
    return {
        "item_key": str(details.get("key", "")),
        "title": str(details.get("title", "")),
        "usage_boundary": USAGE_BOUNDARY,
        "sources": sources,
        "warnings": warnings,
    }
