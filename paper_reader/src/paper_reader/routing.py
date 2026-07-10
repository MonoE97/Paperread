from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


RouteKind = Literal["local_pdf", "local_pdf_directory", "zotero_title"]


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: RouteKind
    input: str
    resolved_path: str | None = None
    query: str | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {"route": self.route, "input": self.input}
        if self.resolved_path is not None:
            payload["resolved_path"] = self.resolved_path
        if self.query is not None:
            payload["query"] = self.query
        return payload


class RoutingError(ValueError):
    def __init__(self, code: str, message: str, *, raw_input: str) -> None:
        super().__init__(message)
        self.code = code
        self.raw_input = raw_input


def _looks_like_path(raw_input: str) -> bool:
    return (
        raw_input.lower().endswith(".pdf")
        or raw_input.startswith(("/", "~", "./", "../"))
        or "/" in raw_input
        or "\\" in raw_input
        or re.match(r"^[A-Za-z]:[\\/]", raw_input) is not None
    )


def route_input(raw_input: str) -> RouteDecision:
    if not raw_input.strip():
        raise RoutingError("invalid_input", "route input must not be empty", raw_input=raw_input)

    try:
        candidate = Path(raw_input).expanduser()
    except RuntimeError:
        candidate = Path(raw_input)
    path_exists = candidate.exists() or os.path.lexists(candidate)
    if path_exists:
        if candidate.is_dir():
            return RouteDecision(
                route="local_pdf_directory",
                input=raw_input,
                resolved_path=str(candidate.resolve(strict=True)),
            )
        if candidate.is_file() and candidate.suffix.lower() == ".pdf":
            return RouteDecision(
                route="local_pdf",
                input=raw_input,
                resolved_path=str(candidate.resolve(strict=True)),
            )
        raise RoutingError(
            "unsupported_local_path",
            f"existing local path is neither a PDF nor a directory: {raw_input}",
            raw_input=raw_input,
        )

    if _looks_like_path(raw_input):
        raise RoutingError(
            "unsupported_local_path",
            f"local path does not exist or is unsupported: {raw_input}",
            raw_input=raw_input,
        )

    query = raw_input.strip()
    return RouteDecision(route="zotero_title", input=raw_input, query=query)


__all__ = ["RouteDecision", "RouteKind", "RoutingError", "route_input"]
