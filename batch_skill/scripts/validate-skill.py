#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
    "src/paperread_batch/__init__.py",
    "src/paperread_batch/io.py",
    "src/paperread_batch/manifest.py",
    "src/paperread_batch/runs.py",
    "src/paperread_batch/state.py",
    "src/paperread_batch/takeaway.py",
    "src/paperread_batch/report.py",
    "src/paperread_batch/local_prepare.py",
    "src/paperread_batch/worker_contract.py",
    "src/paperread_batch/cli.py",
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
            if metadata.get("name") != "paperread-batch":
                errors.append("frontmatter name must be paperread-batch")
            if not metadata.get("description"):
                errors.append("frontmatter description must be non-empty")

    for path in root.rglob("*"):
        if any(part in {".venv", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        if path.is_file() and path.name in FORBIDDEN_DOC_NAMES:
            errors.append(f"forbidden in-skill doc file: {path.relative_to(root)}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the portable Paperread Batch skill bundle.")
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
