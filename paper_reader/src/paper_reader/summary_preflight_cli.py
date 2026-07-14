from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .summary_lint import lint_summary


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lint rendered summary fields before sealing a review package."
    )
    parser.add_argument("summary", help="Path to summary.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    summary_path = Path(args.summary).expanduser().resolve()
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _emit(
            {
                "ok": False,
                "code": "invalid_summary_json",
                "message": str(exc),
                "summary_path": str(summary_path),
                "issues": [],
            }
        )
        return 2

    if not isinstance(payload, dict):
        _emit(
            {
                "ok": False,
                "code": "invalid_summary_json",
                "message": "summary JSON must be an object",
                "summary_path": str(summary_path),
                "issues": [],
            }
        )
        return 2

    issues = lint_summary(payload)
    _emit(
        {
            "ok": not issues,
            "summary_path": str(summary_path),
            "issues": issues,
        }
    )
    return 1 if issues else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
