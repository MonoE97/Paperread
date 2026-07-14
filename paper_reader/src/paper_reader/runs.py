from __future__ import annotations

import re
import unicodedata

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
_GREEK_TRANSLITERATION = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "ε": "epsilon",
    "ζ": "zeta",
    "η": "eta",
    "θ": "theta",
    "ι": "iota",
    "κ": "kappa",
    "λ": "lambda",
    "μ": "mu",
    "ν": "nu",
    "ξ": "xi",
    "ο": "omicron",
    "π": "pi",
    "ρ": "rho",
    "σ": "sigma",
    "ς": "sigma",
    "τ": "tau",
    "υ": "upsilon",
    "φ": "phi",
    "χ": "chi",
    "ψ": "psi",
    "ω": "omega",
}


def _slug_fragment(char: str) -> str:
    if char.isspace():
        return "-"

    folded = char.casefold()
    if folded in _GREEK_TRANSLITERATION:
        return _GREEK_TRANSLITERATION[folded]

    if char.isascii():
        if char.isalnum():
            return char.lower()
        return "-"

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
    """Return a stable lowercase ASCII slug for a paper title."""
    normalized = unicodedata.normalize("NFKC", title).translate(_DASH_TRANSLATION)
    raw_slug = "".join(_slug_fragment(char) for char in normalized)
    slug = _SLUG_PATTERN.sub("-", raw_slug).strip("-")
    return slug or "untitled"
