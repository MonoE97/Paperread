from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

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


def allocate_run_dir(base_dir: Path, title: str, today: date | None = None) -> Path:
    """Allocate a dated run directory path with deterministic collision suffixes."""
    run_date = today or date.today()
    dated_dir = Path(base_dir) / run_date.isoformat()
    slug = slugify_title(title)
    candidate = dated_dir / slug
    suffix = 2
    while candidate.exists():
        candidate = dated_dir / f"{slug}-{suffix}"
        suffix += 1
    return candidate


def write_run_manifest(run_dir: Path, payload: dict[str, Any]) -> Path:
    """Write run.json with the required core metadata and any extra payload fields."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest_payload = dict(payload)
    core_payload = {
        "title": str(manifest_payload.pop("title", "")),
        "slug": str(manifest_payload.pop("slug", run_dir.name)),
        "item_key": str(manifest_payload.pop("item_key", "")),
        "created_at": str(manifest_payload.pop("created_at", datetime.now().isoformat(timespec="seconds"))),
        "status": str(manifest_payload.pop("status", "initialized")),
    }
    manifest = {**core_payload, **manifest_payload}

    manifest_path = run_dir / "run.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path
