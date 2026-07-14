from __future__ import annotations

import re
import threading
from functools import wraps
from typing import Callable, ParamSpec, TypeVar, cast

import fitz


P = ParamSpec("P")
R = TypeVar("R")
_TOOLS_LOCK = threading.RLock()
_REPEAT_LINE_RE = re.compile(r"^\.\.\. repeated \d+ times\.\.\.$")
_DISPLAY_PREFIX_RE = re.compile(r"^MuPDF (?:error|warning):\s*", flags=re.IGNORECASE)


def _normalized_warnings(raw: str) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for raw_line in raw.splitlines():
        line = _DISPLAY_PREFIX_RE.sub("", raw_line.strip())
        if not line or _REPEAT_LINE_RE.fullmatch(line):
            continue
        warning = f"mupdf:{line}"
        if warning not in seen:
            seen.add(warning)
            warnings.append(warning)
    return warnings


def _append_warnings(result: R, warnings: list[str]) -> R:
    if not warnings or not isinstance(result, dict):
        return result
    existing = result.get("warnings")
    if not isinstance(existing, list):
        return result
    merged = list(existing)
    for warning in warnings:
        if warning not in merged:
            merged.append(warning)
    copied = dict(result)
    copied["warnings"] = merged
    return cast(R, copied)


def record_mupdf_diagnostics(function: Callable[P, R]) -> Callable[P, R]:
    """Capture MuPDF's global diagnostic buffer and attach unique warnings to dict results."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with _TOOLS_LOCK:
            tools = fitz.TOOLS
            display_errors = tools.mupdf_display_errors()
            display_warnings = tools.mupdf_display_warnings()
            tools.reset_mupdf_warnings()
            tools.mupdf_display_errors(False)
            tools.mupdf_display_warnings(False)
            captured = ""
            try:
                result = function(*args, **kwargs)
            finally:
                try:
                    captured = tools.mupdf_warnings(reset=1)
                finally:
                    tools.mupdf_display_errors(display_errors)
                    tools.mupdf_display_warnings(display_warnings)
            return _append_warnings(result, _normalized_warnings(captured))

    return wrapped


__all__ = ["record_mupdf_diagnostics"]
