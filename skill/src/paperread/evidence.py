from __future__ import annotations

import re

SECONDARY_EVIDENCE_PREFIXES = (
    "secondary_context",
    "secondary_context.md",
    "secondary_contexts/",
    "secondary_sources.json",
    "wechat-context",
)
CANONICAL_CONTEXT_LOCATOR = re.compile(
    r"^context\.md page \d+(?: section [A-Za-z0-9][A-Za-z0-9 /&().,+:_-]*)?(?: table_candidate \d+)?$"
)
CANONICAL_FIGURE_LOCATOR = re.compile(r"^figure_context\.md [A-Za-z0-9_.:-]+$")


def is_canonical_trusted_locator(locator: str) -> bool:
    """Return whether a locator cites an allowed primary paper artifact."""
    return bool(CANONICAL_CONTEXT_LOCATOR.match(locator) or CANONICAL_FIGURE_LOCATOR.match(locator))
