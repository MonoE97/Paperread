from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest


BATCH_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = BATCH_ROOT / "scripts" / "validate-skill.py"


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("paper_reader_batch_bundle_validator", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_bundle(
    root: Path,
    validator: ModuleType,
    *,
    omit: set[str] | None = None,
    project_version: str = "2.1.0",
    entrypoint: str = "paper_reader_batch.v2_cli:app",
    lock_version: str = "2.1.0",
) -> None:
    omitted = omit or set()
    for relative in validator.REQUIRED_PATHS:
        if relative in omitted:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder\n", encoding="utf-8")

    (root / "SKILL.md").write_text(
        "---\nname: paper_reader_batch\ndescription: Portable V2 batch reader.\n---\n",
        encoding="utf-8",
    )
    (root / "src/paper_reader_batch/__init__.py").write_text(
        '__version__ = "2.1.0"\n',
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "paper_reader_batch"',
                f'version = "{project_version}"',
                "[project.scripts]",
                f'paper_reader_batch = "{entrypoint}"',
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
                'name = "paper-reader-batch"',
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
        "src/paper_reader_batch/v2_errors.py",
        "src/paper_reader_batch/v2_manifest.py",
        "src/paper_reader_batch/v2_receipts.py",
        "src/paper_reader_batch/v2_run.py",
    }
    assert expected <= required

    missing = "src/paper_reader_batch/v2_errors.py"
    _build_bundle(tmp_path, validator, omit={missing})
    assert f"missing required path: {missing}" in validator.validate_skill(tmp_path)


def test_validator_rejects_stale_project_entrypoint_and_lock(tmp_path: Path) -> None:
    validator = _load_validator()
    _build_bundle(
        tmp_path,
        validator,
        project_version="9.9.9",
        entrypoint="paper_reader_batch.cli:app",
        lock_version="0.1.0",
    )

    errors = validator.validate_skill(tmp_path)
    assert "pyproject project.version must be 2.1.0" in errors
    assert "pyproject paper_reader_batch entrypoint must be paper_reader_batch.v2_cli:app" in errors
    assert "uv.lock paper-reader-batch package version must be 2.1.0" in errors


def test_validator_accepts_minimal_closed_v2_bundle(tmp_path: Path) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)

    assert validator.validate_skill(tmp_path) == []


def test_validator_rejects_required_file_replaced_by_directory(tmp_path: Path) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    required_path = tmp_path / "src/paper_reader_batch/v2_errors.py"
    required_path.unlink()
    required_path.mkdir()

    expected = (
        "required path is not a regular file: src/paper_reader_batch/v2_errors.py"
    )
    assert expected in validator.validate_skill(tmp_path)
    assert expected in validator.validate_skill(tmp_path, release_bundle=True)


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/paper_reader_batch/cli.py",
        "src/paper_reader_batch/io.py",
        "src/paper_reader_batch/local_prepare.py",
        "src/paper_reader_batch/manifest.py",
        "src/paper_reader_batch/report.py",
        "src/paper_reader_batch/runs.py",
        "src/paper_reader_batch/state.py",
        "src/paper_reader_batch/takeaway.py",
        "src/paper_reader_batch/worker_contract.py",
    ],
)
def test_validator_rejects_reintroduced_v1_runtime_module(
    relative_path: str,
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    legacy_module = tmp_path / relative_path
    legacy_module.parent.mkdir(parents=True, exist_ok=True)
    legacy_module.write_text("# historical V1 runtime surface\n", encoding="utf-8")

    expected = f"forbidden V1 runtime module: {relative_path}"
    assert expected in validator.validate_skill(tmp_path)
    assert expected in validator.validate_skill(tmp_path, release_bundle=True)


@pytest.mark.parametrize(
    "schema_name",
    [
        "paper_reader_batch.state.v1.schema.json",
        "paper_reader_batch.event.schema.json",
        "paper_reader_batch.unknown.v2.schema.json",
    ],
)
def test_validator_rejects_schema_outside_active_v2_namespace(
    schema_name: str,
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    schema_path = tmp_path / "references/schemas" / schema_name
    schema_path.write_text("{}\n", encoding="utf-8")

    expected = f"unexpected schema file: references/schemas/{schema_name}"
    assert expected in validator.validate_skill(tmp_path)
    assert expected in validator.validate_skill(tmp_path, release_bundle=True)


@pytest.mark.parametrize(
    ("relative_path", "reported_path"),
    [
        (
            "historical/paper_reader_batch.state.v1.schema.json",
            "references/schemas/historical",
        ),
        ("schema-notes.txt", "references/schemas/schema-notes.txt"),
    ],
)
def test_validator_rejects_any_extra_schema_namespace_entry(
    relative_path: str,
    reported_path: str,
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    extra_path = tmp_path / "references/schemas" / relative_path
    extra_path.parent.mkdir(parents=True, exist_ok=True)
    extra_path.write_text("{}\n", encoding="utf-8")

    expected = f"unexpected schema namespace entry: {reported_path}"
    assert expected in validator.validate_skill(tmp_path)
    assert expected in validator.validate_skill(tmp_path, release_bundle=True)


def test_release_validator_rejects_required_file_symlink_to_external_path(
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    bundle = tmp_path / "bundle"
    external = tmp_path / "external.py"
    _build_bundle(bundle, validator)
    external.write_text("outside the release bundle\n", encoding="utf-8")
    linked = bundle / "src/paper_reader_batch/v2_errors.py"
    linked.unlink()
    linked.symlink_to(external)

    errors = validator.validate_skill(bundle, release_bundle=True)

    assert (
        "symlink is forbidden in a release bundle: "
        "src/paper_reader_batch/v2_errors.py"
    ) in errors


def test_release_validator_rejects_unexpected_symlink_anywhere(
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    bundle = tmp_path / "bundle"
    external = tmp_path / "external.txt"
    _build_bundle(bundle, validator)
    external.write_text("outside the release bundle\n", encoding="utf-8")
    linked = bundle / "assets/external.txt"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(external)

    errors = validator.validate_skill(bundle, release_bundle=True)

    assert "symlink is forbidden in a release bundle: assets/external.txt" in errors


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO files are unavailable")
def test_release_validator_rejects_special_file_anywhere(tmp_path: Path) -> None:
    validator = _load_validator()
    bundle = tmp_path / "bundle"
    _build_bundle(bundle, validator)
    fifo = bundle / "assets/channel"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)

    errors = validator.validate_skill(bundle, release_bundle=True)

    assert "special file is forbidden in a release bundle: assets/channel" in errors


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes are required")
def test_release_validator_fails_closed_on_unreadable_directory(tmp_path: Path) -> None:
    validator = _load_validator()
    bundle = tmp_path / "bundle"
    _build_bundle(bundle, validator)
    private = bundle / "assets/private"
    private.mkdir(parents=True)
    private.chmod(0)
    try:
        errors = validator.validate_skill(bundle, release_bundle=True)
    finally:
        private.chmod(0o700)

    assert "cannot inspect release bundle directory: assets/private" in errors


@pytest.mark.parametrize(
    ("relative_path", "reported_path"),
    [
        (".venv/lib/deep/package.py", ".venv"),
        (".pytest_cache/v/cache/nodeids", ".pytest_cache"),
        (".mypy_cache/3.13/cache.json", ".mypy_cache"),
        (".ruff_cache/0.14/cache", ".ruff_cache"),
        (
            ".paper_reader_batch/request-receipts/request.json",
            ".paper_reader_batch",
        ),
        (
            "src/paper_reader_batch/__pycache__/v2_cli.cpython-313.pyc",
            "src/paper_reader_batch/__pycache__",
        ),
        ("runs/2026-07-13/example/manifest.json", "runs"),
        (".DS_Store", ".DS_Store"),
        ("src/paper_reader_batch/stray.pyc", "src/paper_reader_batch/stray.pyc"),
    ],
)
def test_release_validator_rejects_runtime_state_but_normal_validation_allows_it(
    relative_path: str,
    reported_path: str,
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    runtime_path = tmp_path / relative_path
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text("runtime state\n", encoding="utf-8")

    assert validator.validate_skill(tmp_path) == []
    errors = validator.validate_skill(tmp_path, release_bundle=True)

    assert errors.count(
        f"runtime state is forbidden in a release bundle: {reported_path}"
    ) == 1
    assert len([error for error in errors if "runtime state is forbidden" in error]) == 1


def test_release_bundle_cli_flag_rejects_runtime_state(tmp_path: Path) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    runtime_path = tmp_path / "runs/2026-07-13/example/manifest.json"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text("runtime state\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VALIDATOR_PATH), str(tmp_path), "--release-bundle"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 1
    assert (
        "runtime state is forbidden in a release bundle: "
        "runs"
    ) in result.stderr


@pytest.mark.parametrize(
    "runtime_directory",
    [
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "runs",
    ],
)
def test_normal_validator_prunes_every_runtime_state_subtree(
    runtime_directory: str,
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    forbidden_doc = tmp_path / runtime_directory / "nested" / "README.md"
    forbidden_doc.parent.mkdir(parents=True, exist_ok=True)
    forbidden_doc.write_text("runtime-only documentation\n", encoding="utf-8")

    assert validator.validate_skill(tmp_path) == []
    release_errors = validator.validate_skill(tmp_path, release_bundle=True)
    assert release_errors == [
        f"runtime state is forbidden in a release bundle: {runtime_directory}"
    ]
