from __future__ import annotations

import re
from dataclasses import dataclass

SECONDARY_EVIDENCE_PREFIXES = (
    "secondary_context",
    "secondary_context.md",
    "secondary_contexts/",
    "secondary_sources.json",
    "wechat-context",
)
CANONICAL_CONTEXT_LOCATOR = re.compile(
    r"^context\.md page (?P<page>\d+)"
    r"(?: section (?P<section>[A-Za-z0-9][A-Za-z0-9 /&().,+:_-]*?)"
    r"(?: table_candidate (?P<table_candidate>\d+))?)?$"
)
CANONICAL_FIGURE_LOCATOR = re.compile(
    r"^figure_context\.md (?P<figure_id>[A-Za-z0-9_.:-]+)$"
)


@dataclass(frozen=True, slots=True)
class TrustedEvidenceLocator:
    source: str
    page: int | None = None
    section: str | None = None
    table_candidate: int | None = None
    figure_id: str | None = None


def parse_trusted_locator(locator: str) -> TrustedEvidenceLocator | None:
    context_match = CANONICAL_CONTEXT_LOCATOR.fullmatch(locator)
    if context_match is not None:
        return TrustedEvidenceLocator(
            source="context",
            page=int(context_match.group("page")),
            section=context_match.group("section"),
            table_candidate=(
                int(context_match.group("table_candidate"))
                if context_match.group("table_candidate") is not None
                else None
            ),
        )
    figure_match = CANONICAL_FIGURE_LOCATOR.fullmatch(locator)
    if figure_match is not None:
        return TrustedEvidenceLocator(
            source="figure_context",
            figure_id=figure_match.group("figure_id"),
        )
    return None


def is_canonical_trusted_locator(locator: str) -> bool:
    """Return whether a locator cites an allowed primary paper artifact."""
    return parse_trusted_locator(locator) is not None


__all__ = [
    "SECONDARY_EVIDENCE_PREFIXES",
    "TrustedEvidenceLocator",
    "is_canonical_trusted_locator",
    "parse_trusted_locator",
]
