from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


class RunSizeLimitError(ValueError):
    def __init__(self, *, actual_bytes: int, max_bytes: int) -> None:
        super().__init__(f"projected run size {actual_bytes} exceeds {max_bytes} bytes")
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def projected_run_size(
    run_dir: Path,
    *,
    staging_dir: Path | None = None,
    replacements: Mapping[Path, bytes] | None = None,
) -> int:
    root = Path(run_dir).resolve(strict=True)
    staging = Path(staging_dir).resolve(strict=True) if staging_dir is not None else None
    replacement_bytes = {
        Path(path).resolve(strict=False): content
        for path, content in (replacements or {}).items()
    }
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        if staging is not None and _inside(resolved, staging):
            continue
        if resolved in replacement_bytes:
            continue
        total += path.stat().st_size
    if staging is not None:
        total += sum(
            path.stat().st_size
            for path in staging.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    total += sum(len(content) for content in replacement_bytes.values())
    return total


def enforce_projected_run_size(
    run_dir: Path,
    *,
    max_bytes: int,
    staging_dir: Path | None = None,
    replacements: Mapping[Path, bytes] | None = None,
) -> int:
    actual = projected_run_size(
        run_dir,
        staging_dir=staging_dir,
        replacements=replacements,
    )
    if actual > max_bytes:
        raise RunSizeLimitError(actual_bytes=actual, max_bytes=max_bytes)
    return actual


__all__ = ["RunSizeLimitError", "enforce_projected_run_size", "projected_run_size"]
