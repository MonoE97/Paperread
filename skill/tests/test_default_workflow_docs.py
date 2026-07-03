import tomllib
import subprocess
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILL = SKILL_ROOT / "SKILL.md"
PYPROJECT = SKILL_ROOT / "pyproject.toml"
OPENAI_YAML = SKILL_ROOT / "agents" / "openai.yaml"
ZOTERO_REFERENCE = SKILL_ROOT / "references" / "zotero-workflow.md"
PDF_REFERENCE = SKILL_ROOT / "references" / "pdf-path-workflow.md"
SUMMARY_REFERENCE = SKILL_ROOT / "references" / "summary-schema.md"
CAPTURE_SCRIPT = SKILL_ROOT / "scripts" / "capture-secondary-url.mjs"
VALIDATE_SCRIPT = SKILL_ROOT / "scripts" / "validate-skill.py"
REPO_ROOT = SKILL_ROOT.parent
ROOT_DOCS_VALIDATE_SCRIPT = REPO_ROOT / "docs" / "superpowers" / "scripts" / "validate-root-docs.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = read(path)
    assert text.startswith("---\n")
    marker = "\n---\n"
    end = text.find(marker, 4)
    assert end != -1

    metadata: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        assert sep, line
        metadata[key.strip()] = value.strip().strip('"')
    return metadata, text[end + len(marker) :]


def test_skill_bundle_contains_required_runtime_assets() -> None:
    required_paths = [
        SKILL,
        OPENAI_YAML,
        PYPROJECT,
        SKILL_ROOT / "uv.lock",
        SKILL_ROOT / "src" / "paperread" / "cli.py",
        SKILL_ROOT / "src" / "paperread" / "note.py",
        SKILL_ROOT / "templates" / "zotero_note.md.j2",
        ZOTERO_REFERENCE,
        PDF_REFERENCE,
        SUMMARY_REFERENCE,
        CAPTURE_SCRIPT,
        VALIDATE_SCRIPT,
        SKILL_ROOT / "tests" / "fixtures" / "minimal.pdf",
    ]

    for path in required_paths:
        assert path.exists(), path


def test_root_docs_validator_referenced_by_agents_is_tracked() -> None:
    git_probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if git_probe.returncode != 0:
        pytest.skip("root docs git tracking is validated only in the source repository")

    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(ROOT_DOCS_VALIDATE_SCRIPT.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_skill_bundle_excludes_auxiliary_docs() -> None:
    forbidden_names = {
        "README.md",
        "INSTALLATION_GUIDE.md",
        "QUICK_REFERENCE.md",
        "CHANGELOG.md",
    }

    for path in SKILL_ROOT.rglob("*"):
        if any(part in {".venv", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        if path.is_file():
            assert path.name not in forbidden_names, path


def test_skill_frontmatter_is_portable_and_trigger_rich() -> None:
    metadata, _body = parse_frontmatter(SKILL)

    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "paperread"
    assert metadata["description"]
    for phrase in ["Zotero", "local PDF", "Chinese"]:
        assert phrase in metadata["description"]


def test_skill_body_routes_from_skill_root_and_preserves_boundaries() -> None:
    _metadata, body = parse_frontmatter(SKILL)

    for phrase in [
        "skill root",
        "uv --version",
        "uv sync --locked",
        "uv python install 3.13",
        "uv run paperread --help",
        "Typical Use",
        "references/pdf-path-workflow.md",
        "references/zotero-workflow.md",
        "full-PDF extraction",
        "context.md",
        "figure_context.md",
        "section_context.md",
        "Chinese-first",
        "MCP `write_note`",
    ]:
        assert phrase in body

    for stale_phrase in [
        "repo-local v1",
        "repo root",
        "not a standalone global skill installation",
        "Do not copy this directory by itself",
    ]:
        assert stale_phrase not in body


def test_openai_agent_metadata_matches_skill() -> None:
    text = read(OPENAI_YAML)

    for phrase in [
        'display_name: "Paperread"',
        "short_description:",
        "default_prompt:",
        "allow_implicit_invocation: true",
    ]:
        assert phrase in text

    assert "icon" not in text
    assert "brand" not in text


def test_project_metadata_is_skill_root_relative() -> None:
    pyproject = tomllib.loads(read(PYPROJECT))
    project = pyproject["project"]

    assert project["name"] == "paperread"
    assert "readme" not in project
    assert project["scripts"] == {"paperread": "paperread.cli:app"}
    assert pyproject["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert pyproject["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]


def test_references_use_skill_root_paths_and_workflow_terms() -> None:
    combined = "\n".join(
        read(path) for path in [SKILL, ZOTERO_REFERENCE, PDF_REFERENCE, SUMMARY_REFERENCE]
    )

    for phrase in [
        "uv --version",
        "uv sync --locked",
        "uv python install 3.13",
        "uv run paperread --help",
        "uv run paperread",
        "scripts/capture-secondary-url.mjs",
        "prepare-pdf",
        "prepare-write-candidate",
        "prepare-local-note-candidate",
        "write_note",
        "verify-zotero-note",
        "refresh-live-notes",
        "write-payload.json",
        "context.md",
        "figure_context.md",
        "section_context.md",
        "secondary_context_unavailable",
    ]:
        assert phrase in combined

    for stale_phrase in ["skill/scripts", "repo root", "uv sync\n"]:
        assert stale_phrase not in combined


def test_zotero_reference_keeps_single_paper_write_safety_contract() -> None:
    text = read(ZOTERO_REFERENCE)

    for phrase in [
        "search_library",
        "get_item_details",
        "write_note",
        "https://github.com/cookjohn/zotero-mcp#readme",
        "zotero-mcp-plugin",
        "Tools -> Add-ons",
        "runs/YYYY-MM-DD/<title-slug>/",
        "note.md",
        "note.html",
        "write-payload.json",
        "same normalized title",
        "stop before create-run",
        "save-item-details",
        "prepare-item",
        "section_context.md",
        "not a canonical evidence source",
        "prepare-write-candidate",
        'write_note(action="create"',
        "verify-zotero-note",
        "HTTP JSON-RPC fallback",
        "http://127.0.0.1:23120/mcp",
        "NO_PROXY",
        "Zotero local API and SQLite are read-only",
    ]:
        assert phrase in text

    assert ('write_note(action="' + "update" + '"') not in text


def test_pdf_path_reference_forbids_zotero_write_path() -> None:
    text = read(PDF_REFERENCE)

    for phrase in [
        "prepare-pdf",
        "<pdf_stem>_analysis/",
        "<pdf_stem>_note.md",
        "_v2",
        "prepare-local-note-candidate",
        "must not write Zotero",
        "must not call refresh-live-notes",
        "must not create write-payload.json",
        "context.md",
        "figure_context.md",
        "not a canonical evidence source",
        "arXiv source",
        "network timeout",
    ]:
        assert phrase in text


def test_summary_reference_documents_rendered_chinese_fields() -> None:
    text = read(SUMMARY_REFERENCE)

    for phrase in [
        "paper_type",
        "trust_status",
        "review_status",
        "one_sentence_summary",
        "abstract_translation",
        "research_question",
        "method_modules",
        "workflow_steps",
        "technical_details",
        "key_figures",
        "author_stated_limitations",
        "inferred_limits",
        "applicability_limits",
        "evidence_summary",
        "context.md",
        "figure_context.md",
        "Chinese-first",
        "Minimal write-ready example",
        '"paper_type": "method_paper"',
        '"context.md page 1 section Abstract"',
    ]:
        assert phrase in text


def test_capture_secondary_script_is_in_skill_bundle() -> None:
    assert CAPTURE_SCRIPT.exists()
    text = read(CAPTURE_SCRIPT)
    assert "secondary_context" in text
    assert "secondary_context_unavailable" in text
    assert "request-retries" in text
