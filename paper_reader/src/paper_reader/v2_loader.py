from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from paper_reader.contracts import PaperReaderRun
from paper_reader.storage import canonical_json_sha256


@dataclass(frozen=True, slots=True)
class LoadedRun:
    run: PaperReaderRun
    manifest_path: Path
    manifest_sha256: str
    canonical_digest: str


class RunLoadError(ValueError):
    def __init__(self, code: str, message: str, *, manifest_path: Path) -> None:
        super().__init__(message)
        self.code = code
        self.manifest_path = manifest_path


def run_manifest_path(run_path: Path | str) -> Path:
    path = Path(run_path).expanduser()
    if path.is_dir() or (not path.exists() and path.suffix.lower() != ".json"):
        return path / "run.json"
    return path


def load_v2_run(run_path: Path | str) -> LoadedRun:
    manifest_path = run_manifest_path(run_path)
    try:
        raw_bytes = manifest_path.read_bytes()
    except FileNotFoundError as exc:
        raise RunLoadError(
            "run_manifest_missing",
            f"run manifest not found: {manifest_path}",
            manifest_path=manifest_path,
        ) from exc
    except OSError as exc:
        raise RunLoadError(
            "run_manifest_unreadable",
            f"run manifest is unreadable: {manifest_path}: {exc}",
            manifest_path=manifest_path,
        ) from exc

    try:
        payload = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunLoadError(
            "invalid_run_json",
            f"run manifest is not valid UTF-8 JSON: {manifest_path}",
            manifest_path=manifest_path,
        ) from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != "paper_reader.run.v2":
        found = payload.get("schema_version") if isinstance(payload, dict) else None
        label = repr(found) if found is not None else "unversioned"
        raise RunLoadError(
            "unsupported_run_schema",
            f"unsupported run schema {label}: {manifest_path}",
            manifest_path=manifest_path,
        )

    try:
        # Validate the original JSON bytes so strict tuple fields still accept
        # their only JSON representation (arrays) without enabling Python-side coercion.
        run = PaperReaderRun.model_validate_json(raw_bytes)
    except ValidationError as exc:
        raise RunLoadError(
            "invalid_run_schema",
            f"paper_reader.run.v2 validation failed: {manifest_path}: {exc.error_count()} error(s)",
            manifest_path=manifest_path,
        ) from exc

    return LoadedRun(
        run=run,
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        canonical_digest=canonical_json_sha256(run),
    )


__all__ = ["LoadedRun", "RunLoadError", "load_v2_run", "run_manifest_path"]
