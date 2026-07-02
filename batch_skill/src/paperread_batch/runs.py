from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path

_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u2043": "-",
        "\u00ad": "-",
    }
)
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _slug_fragment(char: str) -> str:
    if char.isspace():
        return "-"
    if char.isascii():
        return char.lower() if char.isalnum() else "-"

    normalized_ascii = (
        unicodedata.normalize("NFKD", char)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    if normalized_ascii:
        return normalized_ascii

    category = unicodedata.category(char)
    if category.startswith(("L", "N")):
        return f"u{ord(char):04x}-"
    if category.startswith("M"):
        return ""
    return "-"


def slugify_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).translate(_DASH_TRANSLATION)
    raw_slug = "".join(_slug_fragment(char) for char in normalized)
    slug = _SLUG_PATTERN.sub("-", raw_slug).strip("-")
    return slug or "untitled"


def allocate_batch_run_dir(base_dir: Path, batch_title: str, *, run_date: date | None = None) -> Path:
    dated_dir = Path(base_dir) / (run_date or date.today()).isoformat()
    slug = slugify_title(batch_title)
    candidate = dated_dir / slug
    suffix = 2
    while candidate.exists():
        candidate = dated_dir / f"{slug}-{suffix}"
        suffix += 1
    return candidate
