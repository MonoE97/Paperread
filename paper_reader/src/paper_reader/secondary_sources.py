from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit

from paper_reader.storage import canonical_json_bytes


HTTP_URL_RE = re.compile(
    r"<(?P<delimited>https?://[^\s<>]+)>|(?P<plain>https?://[^\s<>\"'，。；！？、]+)",
    re.IGNORECASE,
)
AMBIGUOUS_NUMERIC_HOST_RE = re.compile(
    r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*$",
    re.IGNORECASE,
)
USAGE_BOUNDARY = "cross-check only; must not be cited in evidence_summary"
NON_ACTIONABLE_WARNING_CODES = {"sqlite_immutable_snapshot_used", "sqlite_ro_retry_after_locked"}
SECONDARY_PLAN_FORMAT = "paper_reader.secondary-plan.v2-internal"
SECONDARY_FINDING_ANCHOR_POLICY = "codepoint_sha256_v1"
SECONDARY_PLAN_MAX_BYTES = 2 * 1024 * 1024
MAX_SECONDARY_URLS = 8
MAX_SECONDARY_PLAN_SOURCES = 256
MAX_SECONDARY_URL_LENGTH = 4096
MAX_SECONDARY_WARNINGS = 256
MAX_SECONDARY_WARNING_BYTES = 4096

NON_PUBLIC_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
NON_PUBLIC_IPV6_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:2::/48",
        "2001:db8::/32",
        "2002::/16",
        "3ffe::/16",
        "3fff::/20",
        "5f00::/16",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)
GLOBAL_UNICAST_IPV6_NETWORK = ipaddress.ip_network("2000::/3")


PLAIN_URL_TRAILING_PUNCTUATION = ".,;!，。；！？、"
PLAIN_URL_WRAPPER_PAIRS = {
    ")": "(",
    "]": "[",
    "}": "{",
    "）": "（",
    "】": "【",
    "》": "《",
}


def _clean_plain_url(url: str) -> str:
    """Remove prose delimiters without changing balanced URL syntax."""
    cleaned = url.rstrip(PLAIN_URL_TRAILING_PUNCTUATION)
    while cleaned:
        closing = cleaned[-1]
        opening = PLAIN_URL_WRAPPER_PAIRS.get(closing)
        if opening is None or cleaned.count(closing) <= cleaned.count(opening):
            break
        cleaned = cleaned[:-1].rstrip(PLAIN_URL_TRAILING_PUNCTUATION)
    return cleaned


def extract_http_urls(text: str) -> list[str]:
    """Extract stable, de-duplicated HTTP(S) URLs from free-form text."""
    if not isinstance(text, str):
        raise ValueError("extra must be a string or missing")
    urls: list[str] = []
    seen: set[str] = set()
    for match in HTTP_URL_RE.finditer(text):
        delimited = match.group("delimited")
        url = delimited if delimited is not None else _clean_plain_url(match.group("plain"))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _extra_provenance(details: dict[str, Any]) -> str:
    paper_reader = details.get("_paper_reader")
    if not isinstance(paper_reader, dict):
        return "mcp_payload"
    enrichment = paper_reader.get("enrichment")
    if not isinstance(enrichment, dict):
        return "mcp_payload"
    extra = enrichment.get("extra")
    if not isinstance(extra, dict):
        return "mcp_payload"
    return str(extra.get("source", "mcp_payload"))


def _extra_text(details: dict[str, Any]) -> str:
    if "extra" not in details:
        return ""
    value = details["extra"]
    if not isinstance(value, str):
        raise ValueError("extra must be a string or missing")
    return value.strip()


def _comparison_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not host:
        return None
    default_port = (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    )
    netloc = host.casefold() if port is None or default_port else f"{host.casefold()}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, ""))


def is_unsafe_secondary_url(url: str) -> bool:
    if len(url) > MAX_SECONDARY_URL_LENGTH:
        return True
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        return True
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        return True
    normalized_host = host.rstrip(".").casefold()
    if normalized_host == "localhost" or normalized_host.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        return AMBIGUOUS_NUMERIC_HOST_RE.fullmatch(normalized_host) is not None
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in NON_PUBLIC_IPV4_NETWORKS)
    return (
        address not in GLOBAL_UNICAST_IPV6_NETWORK
        or any(address in network for network in NON_PUBLIC_IPV6_NETWORKS)
    )


def _is_primary_source_url(url: str, *, doi: str, publisher_url: str) -> bool:
    comparison = _comparison_url(url)
    publisher_comparison = _comparison_url(publisher_url) if publisher_url else None
    if comparison is not None and publisher_comparison is not None and comparison == publisher_comparison:
        return True
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").rstrip(".").casefold()
    if host not in {"doi.org", "dx.doi.org"}:
        return False
    return unquote(parsed.path).strip("/").casefold() == doi.strip().casefold()


def build_secondary_source_plan(
    details: dict[str, Any],
    *,
    source_snapshot_sha256: str,
    finding_anchor_policy: Literal["codepoint_sha256_v1"] | None,
) -> dict[str, Any]:
    """Build the immutable, source-snapshot-bound Zotero secondary URL plan."""
    if not re.fullmatch(r"[0-9a-f]{64}", source_snapshot_sha256):
        raise ValueError("source_snapshot_sha256 must be a lowercase SHA-256 digest")
    if finding_anchor_policy not in {None, SECONDARY_FINDING_ANCHOR_POLICY}:
        raise ValueError(
            "finding_anchor_policy must be None or codepoint_sha256_v1"
        )
    extra = _extra_text(details)
    urls = extract_http_urls(extra)
    if len(urls) > MAX_SECONDARY_PLAN_SOURCES:
        raise ValueError("secondary source plan accepts at most 256 URLs")
    doi = details.get("DOI", "")
    publisher_url = details.get("url", "")
    if not isinstance(doi, str) or not isinstance(publisher_url, str):
        raise ValueError("DOI and url must be strings when present")

    eligible_count = 0
    sources: list[dict[str, Any]] = []
    for index, url in enumerate(urls, start=1):
        rejection_reason: str | None = None
        if is_unsafe_secondary_url(url):
            rejection_reason = "unsafe_url"
        elif _is_primary_source_url(url, doi=doi, publisher_url=publisher_url):
            rejection_reason = "primary_source"
        elif eligible_count >= MAX_SECONDARY_URLS:
            rejection_reason = "source_limit"
        else:
            eligible_count += 1
        sources.append(
            {
                "source_id": f"secondary-{index:03d}",
                "url": url,
                "source_field": "extra",
                "source_provenance": _extra_provenance(details),
                "eligibility": "eligible" if rejection_reason is None else "rejected",
                "rejection_reason": rejection_reason,
            }
        )

    plan: dict[str, Any] = {
        "format": SECONDARY_PLAN_FORMAT,
        "item_key": details.get("key", ""),
        "source_snapshot_sha256": source_snapshot_sha256,
        "usage_boundary": USAGE_BOUNDARY,
        "eligible_source_count": eligible_count,
        "sources": sources,
        "warnings": _paper_reader_warnings(
            details,
            strict=finding_anchor_policy is not None,
        ),
    }
    if finding_anchor_policy is not None:
        plan["finding_anchor_policy"] = finding_anchor_policy
    if (
        finding_anchor_policy is not None
        and len(canonical_json_bytes(plan)) > SECONDARY_PLAN_MAX_BYTES
    ):
        raise ValueError(
            "secondary source plan exceeds the 2 MiB strict capture limit"
        )
    return plan


def _paper_reader_warnings(
    details: dict[str, Any],
    *,
    strict: bool = False,
) -> list[str]:
    paper_reader = details.get("_paper_reader")
    if not isinstance(paper_reader, dict):
        return []
    if "warnings" not in paper_reader:
        return []
    warnings = paper_reader.get("warnings")
    if not isinstance(warnings, list):
        if not strict:
            return []
        raise ValueError("secondary source warnings must be an array of strings")
    if strict and len(warnings) > MAX_SECONDARY_WARNINGS:
        raise ValueError(
            f"secondary source warnings accept at most {MAX_SECONDARY_WARNINGS} entries"
        )
    cleaned: list[str] = []
    for item in warnings:
        if strict and not isinstance(item, str):
            raise ValueError("secondary source warnings must contain only strings")
        warning_source = item if isinstance(item, str) else str(item)
        if (
            strict
            and len(warning_source.encode("utf-8")) > MAX_SECONDARY_WARNING_BYTES
        ):
            raise ValueError(
                "secondary source warning exceeds 4096 UTF-8 bytes"
            )
        warning = warning_source.strip()
        if not warning or warning in NON_ACTIONABLE_WARNING_CODES:
            continue
        cleaned.append(warning)
    return cleaned


def build_secondary_sources(details: dict[str, Any]) -> dict[str, Any]:
    """Build the run artifact describing secondary web sources from Zotero Extra."""
    extra = _extra_text(details)
    warnings = _paper_reader_warnings(details)
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
