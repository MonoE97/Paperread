import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from paper_reader.contracts import PaperReaderReview, PaperReaderSummary


SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parent
SKILL = SKILL_ROOT / "SKILL.md"
PYPROJECT = SKILL_ROOT / "pyproject.toml"
OPENAI_YAML = SKILL_ROOT / "agents" / "openai.yaml"
ZOTERO_REFERENCE = SKILL_ROOT / "references" / "zotero-workflow.md"
PDF_REFERENCE = SKILL_ROOT / "references" / "pdf-path-workflow.md"
SUMMARY_REFERENCE = SKILL_ROOT / "references" / "summary-schema.md"
CAPTURE_SCRIPT = SKILL_ROOT / "scripts" / "capture-secondary-url.mjs"
VALIDATE_SCRIPT = SKILL_ROOT / "scripts" / "validate-skill.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def json_example_after_heading(text: str, heading: str) -> str:
    section_start = text.index(heading)
    fence_start = text.index("```json\n", section_start) + len("```json\n")
    fence_end = text.index("\n```", fence_start)
    return text[fence_start:fence_end]


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
        SKILL_ROOT / "src" / "paper_reader" / "public_cli.py",
        SKILL_ROOT / "src" / "paper_reader" / "contracts.py",
        SKILL_ROOT / "src" / "paper_reader" / "storage.py",
        SKILL_ROOT / "src" / "paper_reader" / "local_lifecycle.py",
        SKILL_ROOT / "src" / "paper_reader" / "zotero_lifecycle.py",
        SKILL_ROOT / "references" / "schemas" / "paper_reader.run.v2.schema.json",
        SKILL_ROOT / "references" / "schemas" / "paper_reader.command-result.v2.schema.json",
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
    assert metadata["name"] == "paper_reader"
    assert metadata["description"]
    for phrase in ["Zotero", "local PDF", "Chinese"]:
        assert phrase in metadata["description"]


def test_skill_body_routes_from_skill_root_and_preserves_boundaries() -> None:
    _metadata, body = parse_frontmatter(SKILL)

    for phrase in [
        "Paper Reader 2.0 runtime contract",
        "grouped CLI",
        "paper_reader.run.v2",
        "paper_reader.summary.v2",
        "paper_reader.review.v2",
        "paper_reader.review-package.v2",
        "paper_reader.candidate.v2",
        "paper_reader.write-authorization.v2",
        "paper_reader.verification.v2",
        "paper_reader.reconciliation.v2",
        "paper_reader.command-result.v2",
        "extra=forbid",
        "skill root",
        "uv --version",
        "uv sync --locked",
        "uv python install 3.13",
        "uv run paper_reader --help",
        "uv run paper_reader route",
        "uv run paper_reader run init-local",
        "uv run paper_reader run init-zotero",
        "uv run paper_reader run prepare",
        "uv run paper_reader run status",
        "uv run paper_reader run validate",
        "uv run paper_reader review validate",
        "uv run paper_reader review seal",
        "uv run paper_reader candidate build",
        "uv run paper_reader local publish",
        "uv run paper_reader zotero authorize",
        "uv run paper_reader zotero verify",
        "uv run paper_reader zotero reconcile",
        "uv run paper_reader maintenance",
        "Typical Use",
        "references/pdf-path-workflow.md",
        "references/zotero-workflow.md",
        "Local PDF path and directory path inputs skip Zotero lookup and duplicate checks",
        "Existing local paths are not Zotero title fragments",
        "full-PDF extraction",
        "context.md",
        "figure_context.md",
        "section_context.md",
        "Chinese-first",
        "immutable candidate",
        "immutable authorization",
        "unsupported_run_schema",
        "V1/unversioned artifacts are historical-only",
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
        'display_name: "paper_reader"',
        "short_description:",
        "default_prompt:",
        "allow_implicit_invocation: true",
        "Paper Reader 2.0",
        "grouped CLI",
        "released",
        "historical-only",
    ]:
        assert phrase in text

    assert "icon" not in text
    assert "brand" not in text
    assert "staged" not in text
    assert "without treating this metadata as proof" not in text


def test_project_metadata_is_skill_root_relative() -> None:
    pyproject = tomllib.loads(read(PYPROJECT))
    project = pyproject["project"]

    assert project["name"] == "paper_reader"
    assert project["version"] == "2.0.0"
    assert "readme" not in project
    assert project["scripts"] == {"paper_reader": "paper_reader.public_cli:app"}
    assert pyproject["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert pyproject["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]


def test_references_use_skill_root_paths_and_workflow_terms() -> None:
    zotero = read(ZOTERO_REFERENCE)
    pdf = read(PDF_REFERENCE)
    summary = read(SUMMARY_REFERENCE)

    for text in [zotero, pdf]:
        for phrase in [
            "uv --version",
            "uv sync --locked",
            "uv python install 3.13",
            "uv run paper_reader --help",
        ]:
            assert phrase in text

    for text in [zotero, pdf, summary]:
        for phrase in [
            "Paper Reader 2.0 Runtime Contract",
            "historical-only",
            "unsupported_run_schema",
        ]:
            assert phrase in text

    for phrase in [
        "scripts/capture-secondary-url.mjs",
        "uv run paper_reader route",
        "uv run paper_reader run init-zotero",
        "uv run paper_reader run prepare",
        "uv run paper_reader run validate",
        "uv run paper_reader review validate",
        "uv run paper_reader review seal",
        "uv run paper_reader candidate build",
        "uv run paper_reader zotero authorize",
        "uv run paper_reader zotero verify",
        "uv run paper_reader zotero reconcile",
        "Local PDF path and directory path inputs skip Zotero lookup and duplicate checks",
        "Existing local paths are not Zotero title fragments",
        "write_note",
        "canonical HTML hash",
        "context.md",
        "figure_context.md",
        "section_context.md",
        "secondary_context_unavailable",
        "unsupported_run_schema",
    ]:
        assert phrase in zotero

    for phrase in [
        "uv run paper_reader route",
        "uv run paper_reader run init-local",
        "uv run paper_reader run prepare",
        "uv run paper_reader run status",
        "uv run paper_reader run validate",
        "uv run paper_reader review validate",
        "uv run paper_reader review seal",
        "uv run paper_reader candidate build",
        "uv run paper_reader local publish",
        "Local PDF path and directory path inputs skip Zotero lookup and duplicate checks",
        "Existing local paths are not Zotero title fragments",
        "context.md",
        "figure_context.md",
        "section_context.md",
        "unsupported_run_schema",
    ]:
        assert phrase in pdf

    for path in [SKILL, ZOTERO_REFERENCE, PDF_REFERENCE, SUMMARY_REFERENCE]:
        text = read(path)
        for stale_phrase in [
            "repo root",
            "uv sync\n",
            "uv run paper_reader create-run",
            "uv run paper_reader prepare-pdf",
            "uv run paper_reader prepare-write-candidate",
            "uv run paper_reader prepare-local-note-candidate",
            "trusted-summary",
            "--max-pages",
        ]:
            assert stale_phrase not in text


def test_zotero_reference_keeps_single_paper_write_safety_contract() -> None:
    text = read(ZOTERO_REFERENCE)

    for phrase in [
        "search_library",
        "get_item_details",
        "raw discovery bundle",
        "search_library response",
        "selected item details",
        "write_note",
        "https://github.com/cookjohn/zotero-mcp#readme",
        "zotero-mcp-plugin",
        "Tools -> Add-ons",
        "runs/YYYY-MM-DD/<title-slug>/",
        "note.md",
        "note.html",
        "same normalized title",
        "uv run paper_reader run init-zotero",
        "uv run paper_reader run prepare",
        "section_context.md",
        "not a canonical evidence source",
        "paper_reader.candidate.v2",
        "paper_reader.write-authorization.v2",
        "immutable candidate",
        "--external-claim-id <claim_id>",
        "--write-attempt-id <write_attempt_id>",
        "candidate digest",
        "does not bind lease_token",
        "TTL",
        "300 seconds",
        "at most once",
        'write_note(action="create"',
        "uv run paper_reader zotero verify",
        "uv run paper_reader zotero reconcile",
        "HTTP JSON-RPC fallback",
        "http://127.0.0.1:23120/mcp",
        "NO_PROXY",
        "Zotero local API and SQLite are read-only",
        "An exact parent + title + canonical HTML hash match locates one note but does not verify it",
        "only after full verification passes exact parent, note key, exact title, complete tags, required headings, minimum length, and canonical HTML hash",
    ]:
        assert phrase in text

    assert ('write_note(action="' + "update" + '"') not in text
    assert "one match -> verified" not in text


def test_zotero_reference_defines_direct_authorization_identity() -> None:
    text = read(ZOTERO_REFERENCE)

    for phrase in [
        "Direct single-paper authorize",
        "\nuv run paper_reader zotero authorize <candidate>\n",
        "two distinct `direct_<uuid>` identities",
        "same atomic authorization transaction",
        "persisted in `paper_reader.write-authorization.v2`",
        "returned in `paper_reader.command-result.v2`",
        "caller must not synthesize or override",
    ]:
        assert phrase in text


def test_zotero_reference_defines_batch_authorization_identity() -> None:
    text = read(ZOTERO_REFERENCE)

    for phrase in [
        "Batch authorize",
        "uv run paper_reader zotero authorize <candidate> --external-claim-id <claim_id> --write-attempt-id <write_attempt_id>",
        "both options must appear together",
        "partial input is rejected",
        "batch claim and candidate digest",
        "must not generate `direct_<uuid>` identities",
    ]:
        assert phrase in text


def test_pdf_path_reference_forbids_zotero_write_path() -> None:
    text = read(PDF_REFERENCE)

    for phrase in [
        "uv run paper_reader run init-local",
        "uv run paper_reader run prepare",
        "uv run paper_reader review seal",
        "uv run paper_reader candidate build",
        "uv run paper_reader local publish",
        "<pdf_stem>_analysis/",
        "<pdf_stem>_note.md",
        "_v2",
        "immutable candidate",
        "no-replace",
        "rebuild the candidate",
        "must not write Zotero",
        "must not create a Zotero authorization",
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
        "paper_reader.summary.v2",
        "paper_reader.review.v2",
        "paper_reader.review-package.v2",
        "extra=forbid",
        "gate-required",
        "quality-recommended",
        "missing quality-recommended fields",
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

    hard_required_section = text.split("## `quality-recommended`", maxsplit=1)[0]
    for quality_field in [
        "method_modules",
        "workflow_steps",
        "technical_details",
        "key_figures",
        "author_stated_limitations",
        "inferred_limits",
        "applicability_limits",
    ]:
        assert quality_field not in hard_required_section


def test_summary_reference_required_fields_and_examples_match_v2_models() -> None:
    text = read(SUMMARY_REFERENCE)
    summary_section, review_and_examples = text.split("## `review.json`", maxsplit=1)
    review_section = review_and_examples.split("## Minimal write-ready example", maxsplit=1)[0]

    for field in PaperReaderSummary.model_json_schema(mode="validation")["required"]:
        assert f"`{field}`" in summary_section, field
    for field in PaperReaderReview.model_json_schema(mode="validation")["required"]:
        assert f"`{field}`" in review_section, field

    summary_example = json_example_after_heading(text, "## Minimal write-ready example")
    PaperReaderSummary.model_validate_json(summary_example)
    review_example = json_example_after_heading(text, "## Minimal review example")
    PaperReaderReview.model_validate_json(review_example)


def test_root_agents_defines_breaking_v2_public_contract() -> None:
    agents = REPO_ROOT / "AGENTS.md"
    if not agents.exists():
        pytest.skip("root AGENTS contract is validated only in the source repository")

    text = read(agents)

    for phrase in [
        "Paper Reader 2.0",
        "released runtime contract",
        "2.0.0",
        "grouped CLI",
        "paper_reader.run.v2",
        "paper_reader.summary.v2",
        "paper_reader.review.v2",
        "paper_reader.review-package.v2",
        "paper_reader.candidate.v2",
        "paper_reader.write-authorization.v2",
        "paper_reader.verification.v2",
        "paper_reader.reconciliation.v2",
        "paper_reader.command-result.v2",
        "extra=forbid",
        "unsupported_run_schema",
        "no aliases",
        "historical-only",
        "second explicit deletion authorization",
        "append-only hash-chain",
        "lease",
        "external agent",
        "MCP `write_note`",
        "An exact parent + title + canonical HTML hash match locates one note but does not verify it",
        "Direct single-paper authorize",
        "direct_<uuid>",
        "both batch identity options must appear together",
        "write.lease_expired_uncertain",
        "clean install",
    ]:
        assert phrase in text


def test_default_workflow_docs_pass_in_isolated_skill_copy(tmp_path: Path) -> None:
    if os.environ.get("PAPER_READER_DOCS_ISOLATION_CHILD") == "1":
        pytest.skip("outer test owns isolated-copy execution")

    isolated_root = tmp_path / "paper_reader"
    shutil.copytree(
        SKILL_ROOT,
        isolated_root,
        ignore=shutil.ignore_patterns(
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "*.pyc",
            "runs",
        ),
    )
    env = os.environ.copy()
    env["PAPER_READER_DOCS_ISOLATION_CHILD"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_default_workflow_docs.py", "-q"],
        cwd=isolated_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_root_readmes_publish_v2_clean_install_contract() -> None:
    if not (REPO_ROOT / "README.md").exists():
        pytest.skip("root documentation is validated only in the source repository")

    for path in [REPO_ROOT / "README.md", REPO_ROOT / "README.zh-CN.md"]:
        text = read(path)
        for phrase in [
            "Paper Reader 2.0",
            "2.0.0",
            "clean install",
            "uv sync --locked",
            "uv run paper_reader --version",
            "uv run paper_reader_batch --version",
            "uv run paper_reader maintenance extract-pdf tests/fixtures/minimal.pdf",
            "unsupported_run_schema",
        ]:
            assert phrase in text


def test_single_validator_tracks_v2_runtime_and_schemas() -> None:
    validator = read(VALIDATE_SCRIPT)

    for phrase in [
        "src/paper_reader/public_cli.py",
        "src/paper_reader/contracts.py",
        "src/paper_reader/evidence_bundle.py",
        "src/paper_reader/review_package.py",
        "src/paper_reader/candidate_builder.py",
        "src/paper_reader/candidate_integrity.py",
        "src/paper_reader/local_publish.py",
        "src/paper_reader/pdf_extract.py",
        "src/paper_reader/zotero_lifecycle.py",
        "references/schemas/paper_reader.run.v2.schema.json",
        "references/schemas/paper_reader.command-result.v2.schema.json",
        "paper_reader.public_cli:app",
        "pyproject project.version must be 2.0.0",
        "uv.lock paper-reader package version must be 2.0.0",
    ]:
        assert phrase in validator


def test_capture_secondary_script_is_in_skill_bundle() -> None:
    assert CAPTURE_SCRIPT.exists()
    text = read(CAPTURE_SCRIPT)
    assert "secondary_context" in text
    assert "secondary_context_unavailable" in text
    assert "request-retries" in text
