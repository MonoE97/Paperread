import subprocess
from pathlib import Path


BATCH_ROOT = Path(__file__).resolve().parents[1]


def test_paper_reader_batch_bundle_contains_required_runtime_assets() -> None:
    required_paths = [
        BATCH_ROOT / "SKILL.md",
        BATCH_ROOT / "agents" / "openai.yaml",
        BATCH_ROOT / "pyproject.toml",
        BATCH_ROOT / "uv.lock",
        BATCH_ROOT / "src" / "paper_reader_batch" / "__init__.py",
        BATCH_ROOT / "src" / "paper_reader_batch" / "v2_cli.py",
        BATCH_ROOT / "src" / "paper_reader_batch" / "v2_contracts.py",
        BATCH_ROOT / "src" / "paper_reader_batch" / "v2_journal.py",
        BATCH_ROOT / "src" / "paper_reader_batch" / "v2_write.py",
        BATCH_ROOT / "references" / "batch-workflow.md",
        BATCH_ROOT / "scripts" / "validate-skill.py",
    ]

    for path in required_paths:
        assert path.exists(), path


def test_paper_reader_batch_frontmatter_names_paper_reader_batch() -> None:
    text = (BATCH_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert "\nname: paper_reader_batch\n" in text
    assert "description:" in text
    assert "paper_reader" in text
    assert "batch" in text


def test_paper_reader_batch_excludes_auxiliary_docs() -> None:
    forbidden_names = {
        "README.md",
        "INSTALLATION_GUIDE.md",
        "QUICK_REFERENCE.md",
        "CHANGELOG.md",
    }

    for path in BATCH_ROOT.rglob("*"):
        if any(part in {".venv", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        if path.is_file():
            assert path.name not in forbidden_names, path


def test_paper_reader_batch_validator_passes() -> None:
    result = subprocess.run(
        ["python", "scripts/validate-skill.py", "."],
        cwd=BATCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Batch skill bundle is valid." in result.stdout
