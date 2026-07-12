from __future__ import annotations

import json
from pathlib import Path

from paper_reader.v2_loader import RunLoadError


def require_raw_schema_version(
    raw: bytes,
    *,
    expected: str,
    artifact_path: Path,
) -> None:
    """Reject versioned JSON from outside the one supported V2 contract.

    Malformed JSON remains the responsibility of the strict model loader so its
    existing invalid/tampered error category is preserved. Valid JSON that is
    not an object is unversioned and therefore unsupported.
    """

    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    found = payload.get("schema_version") if isinstance(payload, dict) else None
    if found == expected:
        return
    label = repr(found) if found is not None else "unversioned"
    raise RunLoadError(
        "unsupported_run_schema",
        f"unsupported run schema {label}: {artifact_path}",
        manifest_path=artifact_path,
    )


__all__ = ["require_raw_schema_version"]
