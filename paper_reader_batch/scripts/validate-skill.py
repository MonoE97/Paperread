#!/usr/bin/env python3
from __future__ import annotations

import argparse
import tomllib
from pathlib import Path


ALLOWED_FRONTMATTER_KEYS = {"name", "description"}
FORBIDDEN_DOC_NAMES = {
    "README.md",
    "INSTALLATION_GUIDE.md",
    "QUICK_REFERENCE.md",
    "CHANGELOG.md",
}
REQUIRED_PATHS = [
    "SKILL.md",
    "agents/openai.yaml",
    "pyproject.toml",
    "uv.lock",
    "src/paper_reader_batch/__init__.py",
    "src/paper_reader_batch/v2_cli.py",
    "src/paper_reader_batch/v2_artifacts.py",
    "src/paper_reader_batch/v2_contracts.py",
    "src/paper_reader_batch/v2_errors.py",
    "src/paper_reader_batch/v2_journal.py",
    "src/paper_reader_batch/v2_json.py",
    "src/paper_reader_batch/v2_manifest.py",
    "src/paper_reader_batch/v2_receipts.py",
    "src/paper_reader_batch/v2_reducer.py",
    "src/paper_reader_batch/v2_run.py",
    "src/paper_reader_batch/v2_worker.py",
    "src/paper_reader_batch/v2_local_prepare.py",
    "src/paper_reader_batch/v2_write.py",
    "src/paper_reader_batch/v2_report.py",
    "references/schemas/paper_reader_batch.manifest.v2.schema.json",
    "references/schemas/paper_reader_batch.state.v2.schema.json",
    "references/schemas/paper_reader_batch.event.v2.schema.json",
    "references/schemas/paper_reader_batch.worker-result.v2.schema.json",
    "references/schemas/paper_reader_batch.local-prepare-result.v2.schema.json",
    "references/schemas/paper_reader_batch.write-result.v2.schema.json",
    "references/schemas/paper_reader_batch.reconciliation.v2.schema.json",
    "references/schemas/paper_reader_batch.report.v2.schema.json",
    "references/schemas/paper_reader_batch.command-result.v2.schema.json",
    "references/batch-workflow.md",
    "references/parallel-dispatch.md",
    "references/worker-result-contract.md",
    "scripts/validate-skill.py",
]


def parse_frontmatter(skill_md: Path) -> dict[str, str]:
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")

    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("SKILL.md frontmatter must end with ---")

    metadata: dict[str, str] = {}
    for raw_line in text[4:end].splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"frontmatter line is not key/value: {raw_line!r}")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in metadata:
            raise ValueError(f"invalid or duplicate frontmatter key: {key!r}")
        metadata[key] = value
    return metadata


def _read_toml(path: Path, label: str, errors: list[str]) -> dict | None:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"{label} is not readable TOML: {exc}")
        return None


def _validate_release_metadata(root: Path, errors: list[str]) -> None:
    pyproject_path = root / "pyproject.toml"
    if pyproject_path.exists() and (pyproject := _read_toml(pyproject_path, "pyproject.toml", errors)):
        project = pyproject.get("project")
        if not isinstance(project, dict):
            errors.append("pyproject.toml must contain [project]")
        else:
            if project.get("name") != "paper_reader_batch":
                errors.append("pyproject project.name must be paper_reader_batch")
            if project.get("version") != "2.0.0":
                errors.append("pyproject project.version must be 2.0.0")
            scripts = project.get("scripts")
            if not isinstance(scripts, dict) or scripts.get("paper_reader_batch") != "paper_reader_batch.v2_cli:app":
                errors.append(
                    "pyproject paper_reader_batch entrypoint must be paper_reader_batch.v2_cli:app"
                )

    lock_path = root / "uv.lock"
    if lock_path.exists() and (lock := _read_toml(lock_path, "uv.lock", errors)):
        packages = lock.get("package")
        matches = (
            [item for item in packages if isinstance(item, dict) and item.get("name") == "paper-reader-batch"]
            if isinstance(packages, list)
            else []
        )
        if len(matches) != 1:
            errors.append("uv.lock must contain exactly one paper-reader-batch package")
        else:
            package = matches[0]
            if package.get("version") != "2.0.0":
                errors.append("uv.lock paper-reader-batch package version must be 2.0.0")
            if package.get("source") != {"editable": "."}:
                errors.append("uv.lock paper-reader-batch package must be the editable skill root")


def validate_skill(skill_root: Path) -> list[str]:
    errors: list[str] = []
    root = skill_root.resolve()

    for relative_path in REQUIRED_PATHS:
        if not (root / relative_path).exists():
            errors.append(f"missing required path: {relative_path}")

    skill_md = root / "SKILL.md"
    if skill_md.exists():
        try:
            metadata = parse_frontmatter(skill_md)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            extra_keys = set(metadata) - ALLOWED_FRONTMATTER_KEYS
            if extra_keys:
                errors.append(f"unsupported frontmatter keys: {', '.join(sorted(extra_keys))}")
            if metadata.get("name") != "paper_reader_batch":
                errors.append("frontmatter name must be paper_reader_batch")
            if not metadata.get("description"):
                errors.append("frontmatter description must be non-empty")

    init_py = root / "src/paper_reader_batch/__init__.py"
    if init_py.exists() and '__version__ = "2.0.0"' not in init_py.read_text(encoding="utf-8"):
        errors.append("paper_reader_batch package version must be 2.0.0")

    _validate_release_metadata(root, errors)

    for path in root.rglob("*"):
        if any(part in {".venv", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        if path.is_file() and path.name in FORBIDDEN_DOC_NAMES:
            errors.append(f"forbidden in-skill doc file: {path.relative_to(root)}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the portable paper_reader_batch skill bundle.")
    parser.add_argument("skill_root", nargs="?", default=".", help="Path to the batch skill root")
    args = parser.parse_args()

    errors = validate_skill(Path(args.skill_root))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print("Batch skill bundle is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
