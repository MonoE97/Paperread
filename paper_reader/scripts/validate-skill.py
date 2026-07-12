#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
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
    "src/paper_reader/__init__.py",
    "src/paper_reader/arxiv_source.py",
    "src/paper_reader/candidate_builder.py",
    "src/paper_reader/candidate_integrity.py",
    "src/paper_reader/public_cli.py",
    "src/paper_reader/contracts.py",
    "src/paper_reader/evidence.py",
    "src/paper_reader/evidence_bundle.py",
    "src/paper_reader/evidence_figures.py",
    "src/paper_reader/evidence_manifest.py",
    "src/paper_reader/figures.py",
    "src/paper_reader/storage.py",
    "src/paper_reader/v2_loader.py",
    "src/paper_reader/routing.py",
    "src/paper_reader/local_lifecycle.py",
    "src/paper_reader/local_publish.py",
    "src/paper_reader/note.py",
    "src/paper_reader/note_hash.py",
    "src/paper_reader/pdf_extract.py",
    "src/paper_reader/pdf_workflow.py",
    "src/paper_reader/resource_policy.py",
    "src/paper_reader/review_package.py",
    "src/paper_reader/run_lock.py",
    "src/paper_reader/run_size.py",
    "src/paper_reader/runs.py",
    "src/paper_reader/secondary_sources.py",
    "src/paper_reader/summary_lint.py",
    "src/paper_reader/workflow.py",
    "src/paper_reader/zotero_artifact_paths.py",
    "src/paper_reader/zotero_lifecycle.py",
    "src/paper_reader/zotero_authorization.py",
    "src/paper_reader/zotero_authorization_loader.py",
    "src/paper_reader/zotero_candidate.py",
    "src/paper_reader/zotero_item_io.py",
    "src/paper_reader/zotero_live.py",
    "src/paper_reader/zotero_lock.py",
    "src/paper_reader/zotero_note_validation.py",
    "src/paper_reader/zotero_read.py",
    "src/paper_reader/zotero_sqlite.py",
    "src/paper_reader/zotero_verification.py",
    "src/paper_reader/zotero_reconciliation.py",
    "templates/zotero_note.md.j2",
    "references/zotero-workflow.md",
    "references/pdf-path-workflow.md",
    "references/summary-schema.md",
    "references/schemas/paper_reader.run.v2.schema.json",
    "references/schemas/paper_reader.summary.v2.schema.json",
    "references/schemas/paper_reader.review.v2.schema.json",
    "references/schemas/paper_reader.review-package.v2.schema.json",
    "references/schemas/paper_reader.candidate.v2.schema.json",
    "references/schemas/paper_reader.write-authorization.v2.schema.json",
    "references/schemas/paper_reader.verification.v2.schema.json",
    "references/schemas/paper_reader.reconciliation.v2.schema.json",
    "references/schemas/paper_reader.command-result.v2.schema.json",
    "scripts/capture-secondary-url.mjs",
    "scripts/validate-skill.py",
    "tests/fixtures/minimal.pdf",
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
            if project.get("name") != "paper_reader":
                errors.append("pyproject project.name must be paper_reader")
            if project.get("version") != "2.0.0":
                errors.append("pyproject project.version must be 2.0.0")
            scripts = project.get("scripts")
            if not isinstance(scripts, dict) or scripts.get("paper_reader") != "paper_reader.public_cli:app":
                errors.append("pyproject paper_reader entrypoint must be paper_reader.public_cli:app")

    lock_path = root / "uv.lock"
    if lock_path.exists() and (lock := _read_toml(lock_path, "uv.lock", errors)):
        packages = lock.get("package")
        matches = (
            [item for item in packages if isinstance(item, dict) and item.get("name") == "paper-reader"]
            if isinstance(packages, list)
            else []
        )
        if len(matches) != 1:
            errors.append("uv.lock must contain exactly one paper-reader package")
        else:
            package = matches[0]
            if package.get("version") != "2.0.0":
                errors.append("uv.lock paper-reader package version must be 2.0.0")
            if package.get("source") != {"editable": "."}:
                errors.append("uv.lock paper-reader package must be the editable skill root")


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
            if metadata.get("name") != "paper_reader":
                errors.append("frontmatter name must be paper_reader")
            if not metadata.get("description"):
                errors.append("frontmatter description must be non-empty")

    init_py = root / "src/paper_reader/__init__.py"
    if init_py.exists() and '__version__ = "2.0.0"' not in init_py.read_text(encoding="utf-8"):
        errors.append("paper_reader package version must be 2.0.0")

    _validate_release_metadata(root, errors)

    for path in root.rglob("*"):
        if any(part in {".venv", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        if path.is_file() and path.name in FORBIDDEN_DOC_NAMES:
            errors.append(f"forbidden in-skill doc file: {path.relative_to(root)}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the portable paper_reader skill bundle.")
    parser.add_argument("skill_root", nargs="?", default=".", help="Path to the skill root")
    args = parser.parse_args()

    errors = validate_skill(Path(args.skill_root))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Skill bundle is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
