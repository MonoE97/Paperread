from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


SKILL_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = SKILL_ROOT / "scripts" / "validate-skill.py"


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("paper_reader_bundle_validator", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_bundle(
    root: Path,
    validator: ModuleType,
    *,
    omit: set[str] | None = None,
    project_version: str = "2.0.0",
    entrypoint: str = "paper_reader.public_cli:app",
    lock_version: str = "2.0.0",
) -> None:
    omitted = omit or set()
    for relative in validator.REQUIRED_PATHS:
        if relative in omitted:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder\n", encoding="utf-8")

    (root / "SKILL.md").write_text(
        "---\nname: paper_reader\ndescription: Portable V2 paper reader.\n---\n",
        encoding="utf-8",
    )
    (root / "src/paper_reader/__init__.py").write_text(
        '__version__ = "2.0.0"\n',
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "paper_reader"',
                f'version = "{project_version}"',
                "[project.scripts]",
                f'paper_reader = "{entrypoint}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        "\n".join(
            [
                'version = 1',
                'revision = 1',
                'requires-python = ">=3.13"',
                "",
                "[[package]]",
                'name = "paper-reader"',
                f'version = "{lock_version}"',
                'source = { editable = "." }',
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_validator_requires_full_v2_runtime_closure(tmp_path: Path) -> None:
    validator = _load_validator()
    required = set(validator.REQUIRED_PATHS)
    expected = {
        "src/paper_reader/evidence_bundle.py",
        "src/paper_reader/review_package.py",
        "src/paper_reader/candidate_builder.py",
        "src/paper_reader/candidate_integrity.py",
        "src/paper_reader/local_publish.py",
        "src/paper_reader/pdf_extract.py",
    }
    assert expected <= required

    missing = "src/paper_reader/evidence_bundle.py"
    _build_bundle(tmp_path, validator, omit={missing})
    assert f"missing required path: {missing}" in validator.validate_skill(tmp_path)


def test_validator_rejects_stale_project_entrypoint_and_lock(tmp_path: Path) -> None:
    validator = _load_validator()
    _build_bundle(
        tmp_path,
        validator,
        project_version="9.9.9",
        entrypoint="paper_reader.cli:app",
        lock_version="0.1.0",
    )

    errors = validator.validate_skill(tmp_path)
    assert "pyproject project.version must be 2.0.0" in errors
    assert "pyproject paper_reader entrypoint must be paper_reader.public_cli:app" in errors
    assert "uv.lock paper-reader package version must be 2.0.0" in errors


def test_validator_accepts_minimal_closed_v2_bundle(tmp_path: Path) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)

    assert validator.validate_skill(tmp_path) == []
