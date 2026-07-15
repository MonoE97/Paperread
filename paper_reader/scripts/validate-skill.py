#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import os
import stat
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
FORBIDDEN_V1_RUNTIME_CALLABLES = {
    "src/paper_reader/pdf_workflow.py": {
        "PDFOutputPaths",
        "allocate_pdf_output_paths",
    },
    "src/paper_reader/runs.py": {"allocate_run_dir", "write_run_manifest"},
    "src/paper_reader/workflow.py": {
        "_prepare_bundle_from_metadata",
        "prepare_item_bundle",
        "prepare_pdf_bundle",
    },
}
FORBIDDEN_V1_RUNTIME_MODULES = {
    "src/paper_reader/cli.py",
    "src/paper_reader/gate.py",
    "src/paper_reader/local_candidate.py",
    "src/paper_reader/local_gate.py",
    "src/paper_reader/local_publication.py",
    "src/paper_reader/note_table_migration.py",
    "src/paper_reader/review.py",
    "src/paper_reader/write_candidate.py",
    "src/paper_reader/write_payload.py",
    "src/paper_reader/zotero_details.py",
}
ACTIVE_SCHEMA_PATHS = {
    "references/schemas/paper_reader.run.v2.schema.json",
    "references/schemas/paper_reader.summary.v2.schema.json",
    "references/schemas/paper_reader.review.v2.schema.json",
    "references/schemas/paper_reader.review-package.v2.schema.json",
    "references/schemas/paper_reader.candidate.v2.schema.json",
    "references/schemas/paper_reader.write-authorization.v2.schema.json",
    "references/schemas/paper_reader.verification.v2.schema.json",
    "references/schemas/paper_reader.reconciliation.v2.schema.json",
    "references/schemas/paper_reader.command-result.v2.schema.json",
}
RUNTIME_STATE_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "runs",
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
    "src/paper_reader/mupdf_diagnostics.py",
    "src/paper_reader/note.py",
    "src/paper_reader/note_hash.py",
    "src/paper_reader/pdf_extract.py",
    "src/paper_reader/pdf_workflow.py",
    "src/paper_reader/raw_schema.py",
    "src/paper_reader/resource_policy.py",
    "src/paper_reader/review_package.py",
    "src/paper_reader/run_lock.py",
    "src/paper_reader/run_size.py",
    "src/paper_reader/runs.py",
    "src/paper_reader/secondary_sources.py",
    "src/paper_reader/summary_lint.py",
    "src/paper_reader/summary_preflight_cli.py",
    "src/paper_reader/workflow.py",
    "src/paper_reader/zotero_artifact_paths.py",
    "src/paper_reader/zotero_lifecycle.py",
    "src/paper_reader/zotero_authorization.py",
    "src/paper_reader/zotero_authorization_loader.py",
    "src/paper_reader/zotero_authorization_reservations.py",
    "src/paper_reader/zotero_candidate.py",
    "src/paper_reader/zotero_discovery.py",
    "src/paper_reader/zotero_discovery_cli.py",
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
    "scripts/discover-zotero-item.py",
    "scripts/lint-summary.py",
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


def _validate_release_metadata(
    root: Path,
    errors: list[str],
    regular_required_paths: set[str],
) -> None:
    pyproject_path = root / "pyproject.toml"
    if "pyproject.toml" in regular_required_paths and (
        pyproject := _read_toml(pyproject_path, "pyproject.toml", errors)
    ):
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
    if "uv.lock" in regular_required_paths and (
        lock := _read_toml(lock_path, "uv.lock", errors)
    ):
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


def _validate_required_files(root: Path, errors: list[str]) -> set[str]:
    regular_paths: set[str] = set()
    for relative_path in REQUIRED_PATHS:
        try:
            metadata = os.lstat(root / relative_path)
        except FileNotFoundError:
            errors.append(f"missing required path: {relative_path}")
        except OSError as exc:
            errors.append(f"cannot inspect required path: {relative_path}: {exc}")
        else:
            if stat.S_ISREG(metadata.st_mode):
                regular_paths.add(relative_path)
            else:
                errors.append(f"required path is not a regular file: {relative_path}")
    return regular_paths


def _validate_no_v1_runtime_modules(root: Path, errors: list[str]) -> None:
    for relative_path in sorted(FORBIDDEN_V1_RUNTIME_MODULES):
        try:
            os.lstat(root / relative_path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"cannot inspect forbidden V1 runtime module: {relative_path}: {exc}")
        else:
            errors.append(f"forbidden V1 runtime module: {relative_path}")


def _validate_active_schema_namespace(root: Path, errors: list[str]) -> None:
    schema_root = root / "references/schemas"
    active_names = {Path(relative_path).name for relative_path in ACTIVE_SCHEMA_PATHS}
    try:
        with os.scandir(schema_root) as iterator:
            entry_names = sorted(entry.name for entry in iterator)
    except OSError as exc:
        errors.append(f"cannot inspect schema directory: {exc}")
        return
    for entry_name in entry_names:
        relative_path = f"references/schemas/{entry_name}"
        if entry_name in active_names:
            continue
        if entry_name.endswith(".schema.json"):
            errors.append(f"unexpected schema file: {relative_path}")
        else:
            errors.append(f"unexpected schema namespace entry: {relative_path}")


def _validate_no_v1_runtime_callables(
    root: Path,
    errors: list[str],
    regular_required_paths: set[str],
) -> None:
    for relative_path, forbidden_names in FORBIDDEN_V1_RUNTIME_CALLABLES.items():
        if relative_path not in regular_required_paths:
            continue
        source_path = root / relative_path
        try:
            module = ast.parse(source_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            errors.append(f"cannot inspect {relative_path} for V1 runtime callables: {exc}")
            continue
        defined_names = {
            node.name
            for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        for forbidden_name in sorted(forbidden_names & defined_names):
            errors.append(
                f"forbidden V1 runtime callable in {relative_path}: {forbidden_name}"
            )


def _validate_release_bundle_state(root: Path, errors: list[str]) -> None:
    try:
        root_metadata = os.lstat(root)
    except OSError as exc:
        errors.append(f"cannot inspect release bundle root: {exc}")
        return
    if stat.S_ISLNK(root_metadata.st_mode):
        errors.append("symlink is forbidden in a release bundle: .")
        return
    if not stat.S_ISDIR(root_metadata.st_mode):
        errors.append("special file is forbidden in a release bundle: .")
        return

    def record_walk_error(exc: OSError) -> None:
        failed = Path(exc.filename) if exc.filename is not None else root
        try:
            relative = failed.relative_to(root).as_posix()
        except ValueError:
            relative = "."
        errors.append(f"cannot inspect release bundle directory: {relative}")

    for current_root, dirnames, filenames in os.walk(
        root,
        topdown=True,
        onerror=record_walk_error,
        followlinks=False,
    ):
        current_path = Path(current_root)
        for dirname in sorted(tuple(dirnames)):
            path = current_path / dirname
            relative = path.relative_to(root).as_posix()
            try:
                metadata = os.lstat(path)
            except OSError as exc:
                errors.append(f"cannot inspect release bundle entry: {relative}: {exc}")
                dirnames.remove(dirname)
                continue
            if stat.S_ISLNK(metadata.st_mode):
                errors.append(f"symlink is forbidden in a release bundle: {relative}")
                dirnames.remove(dirname)
            elif not stat.S_ISDIR(metadata.st_mode):
                errors.append(f"special file is forbidden in a release bundle: {relative}")
                dirnames.remove(dirname)
            elif dirname in RUNTIME_STATE_PARTS:
                errors.append(f"runtime state is forbidden in a release bundle: {relative}")
                dirnames.remove(dirname)
        for filename in sorted(filenames):
            path = current_path / filename
            relative = path.relative_to(root).as_posix()
            try:
                metadata = os.lstat(path)
            except OSError as exc:
                errors.append(f"cannot inspect release bundle entry: {relative}: {exc}")
                continue
            if stat.S_ISLNK(metadata.st_mode):
                errors.append(f"symlink is forbidden in a release bundle: {relative}")
                continue
            if not stat.S_ISREG(metadata.st_mode):
                errors.append(f"special file is forbidden in a release bundle: {relative}")
                continue
            if (
                filename not in RUNTIME_STATE_PARTS
                and filename != ".DS_Store"
                and not filename.endswith(".pyc")
            ):
                continue
            errors.append(
                f"runtime state is forbidden in a release bundle: {relative}"
            )


def validate_skill(skill_root: Path, *, release_bundle: bool = False) -> list[str]:
    errors: list[str] = []
    root = (
        Path(os.path.abspath(os.fspath(skill_root.expanduser())))
        if release_bundle
        else skill_root.resolve()
    )

    if release_bundle:
        _validate_release_bundle_state(root, errors)
        if errors:
            return errors

    regular_required_paths = _validate_required_files(root, errors)

    skill_md = root / "SKILL.md"
    if "SKILL.md" in regular_required_paths:
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
    if (
        "src/paper_reader/__init__.py" in regular_required_paths
        and '__version__ = "2.0.0"' not in init_py.read_text(encoding="utf-8")
    ):
        errors.append("paper_reader package version must be 2.0.0")

    _validate_release_metadata(root, errors, regular_required_paths)
    _validate_no_v1_runtime_modules(root, errors)
    _validate_active_schema_namespace(root, errors)
    _validate_no_v1_runtime_callables(root, errors, regular_required_paths)
    for current_root, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = sorted(
            dirname for dirname in dirnames if dirname not in RUNTIME_STATE_PARTS
        )
        current_path = Path(current_root)
        for filename in sorted(filenames):
            if filename in FORBIDDEN_DOC_NAMES:
                errors.append(
                    f"forbidden in-skill doc file: {(current_path / filename).relative_to(root)}"
                )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the portable paper_reader skill bundle.")
    parser.add_argument("skill_root", nargs="?", default=".", help="Path to the skill root")
    parser.add_argument(
        "--release-bundle",
        action="store_true",
        help="Reject runtime state that may exist in a working or installed skill root",
    )
    args = parser.parse_args()

    errors = validate_skill(Path(args.skill_root), release_bundle=args.release_bundle)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Skill bundle is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
