from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest


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
    project_version: str = "2.2.0",
    entrypoint: str = "paper_reader.public_cli:app",
    lock_version: str = "2.2.0",
    pydantic_specifier: str = ">=2.12,<3",
    lock_pydantic_specifier: str = ">=2.12,<3",
) -> None:
    omitted = omit or set()
    for relative in validator.REQUIRED_PATHS:
        if relative in omitted:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "tests/fixtures/minimal.pdf":
            path.write_bytes((SKILL_ROOT / relative).read_bytes())
        else:
            path.write_text("placeholder\n", encoding="utf-8")

    (root / "SKILL.md").write_text(
        "---\nname: paper_reader\ndescription: Portable V2 paper reader.\n---\n",
        encoding="utf-8",
    )
    (root / "src/paper_reader/__init__.py").write_text(
        '__version__ = "2.2.0"\n',
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "paper_reader"',
                f'version = "{project_version}"',
                f'dependencies = ["pydantic{pydantic_specifier}"]',
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
                'dependencies = [{ name = "pydantic" }]',
                "",
                "[package.metadata]",
                (
                    'requires-dist = [{ name = "pydantic", specifier = '
                    f'"{lock_pydantic_specifier}" }}]'
                ),
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
        "src/paper_reader/raw_schema.py",
        "src/paper_reader/zotero_authorization_reservations.py",
        "scripts/lib/raw-cdp-capture.mjs",
        "scripts/lib/secondary-network-policy.mjs",
        "scripts/lib/strict-egress-proxy.mjs",
        "scripts/export-v2-schemas.py",
    }
    assert expected <= required

    imported_modules: set[str] = set()
    for source_path in (SKILL_ROOT / "src/paper_reader").glob("*.py"):
        for node in ast.walk(ast.parse(source_path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("paper_reader."):
                imported_modules.add(node.module.removeprefix("paper_reader.").split(".", 1)[0])
            elif isinstance(node, ast.Import):
                imported_modules.update(
                    alias.name.removeprefix("paper_reader.").split(".", 1)[0]
                    for alias in node.names
                    if alias.name.startswith("paper_reader.")
                )
    imported_paths = {f"src/paper_reader/{module}.py" for module in imported_modules}
    assert imported_paths <= required

    missing = "src/paper_reader/evidence_bundle.py"
    _build_bundle(tmp_path, validator, omit={missing})
    assert f"missing required path: {missing}" in validator.validate_skill(tmp_path)


def test_release_validator_requires_schema_exporter(tmp_path: Path) -> None:
    validator = _load_validator()
    exporter = "scripts/export-v2-schemas.py"
    assert (SKILL_ROOT / exporter).is_file()
    _build_bundle(tmp_path, validator, omit={exporter})

    assert f"missing required path: {exporter}" in validator.validate_skill(
        tmp_path,
        release_bundle=True,
    )


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
    assert "pyproject project.version must be 2.2.0" in errors
    assert "pyproject paper_reader entrypoint must be paper_reader.public_cli:app" in errors
    assert "uv.lock paper-reader package version must be 2.2.0" in errors


def test_validator_rejects_pydantic_constraint_without_exclude_if_support(
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(
        tmp_path,
        validator,
        pydantic_specifier=">=2.11,<3",
        lock_pydantic_specifier=">=2.11,<3",
    )

    errors = validator.validate_skill(tmp_path)
    assert "pyproject pydantic dependency must be pydantic>=2.12,<3" in errors
    assert "uv.lock pydantic dependency must use >=2.12,<3" in errors


def test_validator_accepts_minimal_closed_v2_bundle(tmp_path: Path) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)

    assert validator.validate_skill(tmp_path) == []


def test_validator_rejects_required_file_replaced_by_directory(tmp_path: Path) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    required_path = tmp_path / "src/paper_reader/note_hash.py"
    required_path.unlink()
    required_path.mkdir()

    expected = "required path is not a regular file: src/paper_reader/note_hash.py"
    assert expected in validator.validate_skill(tmp_path)
    assert expected in validator.validate_skill(tmp_path, release_bundle=True)


@pytest.mark.parametrize(
    "relative_path",
    [
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
        "paper_reader.run.v1.schema.json",
        "paper_reader.summary.schema.json",
        "paper_reader.unknown.v2.schema.json",
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
            "historical/paper_reader.run.v1.schema.json",
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


@pytest.mark.parametrize(
    ("relative_path", "forbidden_name"),
    [
        ("src/paper_reader/runs.py", "write_run_manifest"),
        ("src/paper_reader/runs.py", "allocate_run_dir"),
        ("src/paper_reader/pdf_workflow.py", "PDFOutputPaths"),
        ("src/paper_reader/pdf_workflow.py", "allocate_pdf_output_paths"),
        ("src/paper_reader/workflow.py", "_prepare_bundle_from_metadata"),
        ("src/paper_reader/workflow.py", "prepare_item_bundle"),
        ("src/paper_reader/workflow.py", "prepare_pdf_bundle"),
    ],
)
def test_active_source_does_not_expose_v1_mutators(
    relative_path: str,
    forbidden_name: str,
) -> None:
    source = (SKILL_ROOT / relative_path).read_text(encoding="utf-8")
    functions = {
        node.name
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }

    assert forbidden_name not in functions


def test_release_validator_rejects_hidden_v1_mutators(tmp_path: Path) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    workflow_path = tmp_path / "src/paper_reader/workflow.py"
    workflow_path.write_text(
        "def prepare_item_bundle(details, workdir):\n    return {}\n",
        encoding="utf-8",
    )

    errors = validator.validate_skill(tmp_path)

    assert "forbidden V1 runtime callable in src/paper_reader/workflow.py: prepare_item_bundle" in errors


def test_release_validator_rejects_required_file_symlink_to_external_path(
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    bundle = tmp_path / "bundle"
    external = tmp_path / "external.py"
    _build_bundle(bundle, validator)
    external.write_text("outside the release bundle\n", encoding="utf-8")
    linked = bundle / "src/paper_reader/note_hash.py"
    linked.unlink()
    linked.symlink_to(external)

    errors = validator.validate_skill(bundle, release_bundle=True)

    assert (
        "symlink is forbidden in a release bundle: "
        "src/paper_reader/note_hash.py"
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
        (".venv/pyvenv.cfg", ".venv"),
        (".pytest_cache/CACHEDIR.TAG", ".pytest_cache"),
        (
            "src/paper_reader/__pycache__/workflow.cpython-313.pyc",
            "src/paper_reader/__pycache__",
        ),
        ("runs/2026-07-13/example/run.json", "runs"),
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


@pytest.mark.parametrize(
    "runtime_directory",
    [
        ".zotero-authorization-reservations",
        ".zotero-authorization-reservation-index",
        ".zotero-parent-locks",
    ],
)
def test_release_validator_rejects_zotero_authorization_and_lock_state(
    runtime_directory: str,
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    state_file = tmp_path / runtime_directory / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("sensitive runtime state\n", encoding="utf-8")

    assert validator.validate_skill(tmp_path) == []
    assert validator.validate_skill(tmp_path, release_bundle=True) == [
        f"runtime state is forbidden in a release bundle: {runtime_directory}"
    ]


@pytest.mark.parametrize(
    ("relative_path", "reported_path"),
    [
        (".git", ".git"),
        (".git/HEAD", ".git"),
        ("dist/paper_reader.whl", "dist"),
        ("build/lib/paper_reader.py", "build"),
        ("htmlcov/index.html", "htmlcov"),
        ("src/paper_reader.egg-info/PKG-INFO", "src/paper_reader.egg-info"),
        (".coverage", ".coverage"),
    ],
)
def test_release_validator_rejects_vcs_build_and_coverage_state(
    relative_path: str,
    reported_path: str,
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    state_file = tmp_path / relative_path
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("generated state\n", encoding="utf-8")

    assert validator.validate_skill(tmp_path) == []
    assert validator.validate_skill(tmp_path, release_bundle=True) == [
        f"runtime state is forbidden in a release bundle: {reported_path}"
    ]


def test_release_validator_runtime_names_use_exact_or_suffix_rules(
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    allowed_files = [
        ".gitignore",
        ".coverage.example",
        "src/paper_reader/build.py",
        "assets/distribution/readme.txt",
        "assets/htmlcoverage/index.txt",
        "assets/project.egg-info.backup/metadata.txt",
    ]
    for relative_path in allowed_files:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("legitimate source\n", encoding="utf-8")

    assert validator.validate_skill(tmp_path, release_bundle=True) == []


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        ".env.local",
        "diagnostics/debug.log",
        "secrets/server.pem",
        "secrets/server.key",
        "secrets/identity.p12",
        "secrets/identity.pfx",
        "secrets/id_rsa",
        "secrets/id_ed25519",
        "outputs/paper_note.md",
        "outputs/paper_note_v2.md",
        "outputs/paper.summary.json",
        "outputs/paper.extract.json",
        "outputs/paper.pdf.txt",
        "papers/private-paper.pdf",
        "tests/fixtures/second.pdf",
        "zotero.sqlite",
        "snapshots/library.SQLITE3",
        "state/reader.db",
        "state/reader.sqlite-wal",
        "state/reader.sqlite-shm",
        "state/reader.sqlite-journal",
        "state/reader.sqlite3-wal",
        "state/reader.sqlite3-shm",
        "state/reader.sqlite3-journal",
        "state/reader.db-wal",
        "state/reader.db-shm",
        "state/reader.DB-JOURNAL",
    ],
)
def test_release_validator_rejects_private_and_generated_files(
    relative_path: str,
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    private_path = tmp_path / relative_path
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_text("private or generated data\n", encoding="utf-8")

    assert validator.validate_skill(tmp_path) == []
    assert validator.validate_skill(tmp_path, release_bundle=True) == [
        f"private or generated artifact is forbidden in a release bundle: {relative_path}"
    ]


@pytest.mark.parametrize(
    "relative_directory",
    [
        "outputs/paper_analysis",
        "outputs/paper_analysis_v2",
    ],
)
def test_release_validator_rejects_reader_analysis_directories(
    relative_directory: str,
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    output = tmp_path / relative_directory / "run.json"
    output.parent.mkdir(parents=True)
    output.write_text("generated run\n", encoding="utf-8")

    assert validator.validate_skill(tmp_path) == []
    assert validator.validate_skill(tmp_path, release_bundle=True) == [
        "private or generated artifact is forbidden in a release bundle: "
        f"{relative_directory}"
    ]


def test_release_validator_allows_only_the_committed_minimal_pdf_fixture(
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)

    assert (tmp_path / "tests/fixtures/minimal.pdf").is_file()
    assert validator.validate_skill(tmp_path, release_bundle=True) == []


def test_release_validator_rejects_arbitrary_content_at_minimal_pdf_path(
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    fixture = tmp_path / "tests/fixtures/minimal.pdf"
    fixture.write_bytes(b"private PDF content\n")

    assert validator.validate_skill(tmp_path) == []
    assert validator.validate_skill(tmp_path, release_bundle=True) == [
        "release fixture content does not match approved artifact: "
        "tests/fixtures/minimal.pdf"
    ]


def test_release_validator_rejects_same_size_content_at_minimal_pdf_path(
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    fixture = tmp_path / "tests/fixtures/minimal.pdf"
    fixture.write_bytes(b"X" * fixture.stat().st_size)

    assert validator.validate_skill(tmp_path) == []
    assert validator.validate_skill(tmp_path, release_bundle=True) == [
        "release fixture content does not match approved artifact: "
        "tests/fixtures/minimal.pdf"
    ]


def test_release_validator_private_rules_do_not_match_similar_source_names(
    tmp_path: Path,
) -> None:
    validator = _load_validator()
    _build_bundle(tmp_path, validator)
    allowed_files = [
        ".environment",
        "diagnostics/debug.logger",
        "references/certificate.pem.txt",
        "tests/fixtures/id_rsa.pub",
        "outputs/paper_analysis_notes/readme.txt",
        "templates/paper_note.md.j2",
        "references/paper_summary.json",
        "references/paper.extract.json.template",
        "references/paper.pdf.text",
        "tests/fixtures/minimal.pdf.metadata",
        "references/database.sqlite.md",
        "references/schema.db.json",
        "src/paper_reader/sqlite3.py",
        "references/library.sqlite-wal.md",
        "references/library.sqlite-journal.md",
    ]
    for relative_path in allowed_files:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("legitimate source\n", encoding="utf-8")

    assert validator.validate_skill(tmp_path, release_bundle=True) == []


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
