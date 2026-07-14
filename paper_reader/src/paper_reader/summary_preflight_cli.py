from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from .contracts import PaperReaderSummary
from .raw_schema import require_raw_schema_version
from .resource_policy import V2_RESOURCE_POLICY
from .summary_lint import lint_summary
from .v2_loader import RunLoadError


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lint rendered summary fields before sealing a review package."
    )
    parser.add_argument("summary", help="Path to summary.json")
    return parser


def _error(
    *,
    code: str,
    message: str,
    summary_path: Path,
) -> int:
    _emit(
        {
            "ok": False,
            "code": code,
            "message": message,
            "summary_path": str(summary_path),
            "issues": [],
        }
    )
    return 2


def _read_bounded_summary(path: Path, *, max_bytes: int) -> bytes:
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("summary JSON must be a regular file")
    if metadata.st_size > max_bytes:
        raise OverflowError(f"summary JSON exceeds the {max_bytes}-byte resource limit")
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise OverflowError(f"summary JSON exceeds the {max_bytes}-byte resource limit")
    return raw


def main(
    argv: Sequence[str] | None = None,
    *,
    max_summary_bytes: int = V2_RESOURCE_POLICY.structured_artifact_max_bytes,
) -> int:
    if type(max_summary_bytes) is not int or max_summary_bytes < 1:
        raise ValueError("max_summary_bytes must be a positive integer")
    args = _parser().parse_args(argv)
    summary_path = Path(args.summary).expanduser().resolve()
    try:
        raw = _read_bounded_summary(summary_path, max_bytes=max_summary_bytes)
    except OverflowError as exc:
        return _error(
            code="resource_limit",
            message=str(exc),
            summary_path=summary_path,
        )
    except OSError as exc:
        return _error(
            code="invalid_summary_json",
            message=str(exc),
            summary_path=summary_path,
        )

    try:
        json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _error(
            code="invalid_summary_json",
            message=str(exc),
            summary_path=summary_path,
        )

    try:
        require_raw_schema_version(
            raw,
            expected="paper_reader.summary.v2",
            artifact_path=summary_path,
        )
    except RunLoadError as exc:
        return _error(
            code=exc.code,
            message=str(exc),
            summary_path=summary_path,
        )

    try:
        summary = PaperReaderSummary.model_validate_json(raw)
    except ValidationError as exc:
        return _error(
            code="invalid_summary_schema",
            message=f"strict paper_reader.summary.v2 validation failed: {exc}",
            summary_path=summary_path,
        )

    issues = lint_summary(summary.model_dump(mode="json"))
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
