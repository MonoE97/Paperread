from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

from paper_reader.zotero_discovery import DiscoveryError, discover_exact_title_http


Discover = Callable[..., dict[str, Any]]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build one read-only Zotero title/title-fragment discovery bundle on stdout.",
    )
    parser.add_argument("--title", required=True, help="Zotero paper title or title fragment")
    parser.add_argument("--mcp-endpoint", default="http://127.0.0.1:23120/mcp")
    parser.add_argument("--local-api-base", default="http://127.0.0.1:23119")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    discover: Discover = discover_exact_title_http,
) -> int:
    args = _parser().parse_args(argv)
    try:
        bundle = discover(
            args.title,
            mcp_endpoint=args.mcp_endpoint,
            local_api_base=args.local_api_base,
            timeout_seconds=args.timeout_seconds,
        )
    except DiscoveryError as exc:
        json.dump(
            {"code": exc.code, "message": str(exc), "ok": False},
            sys.stderr,
            ensure_ascii=False,
            sort_keys=True,
        )
        sys.stderr.write("\n")
        return 1
    json.dump(bundle, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


__all__ = ["main"]
