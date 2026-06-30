from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
README = PROJECT_ROOT / "README.md"
AGENTS = PROJECT_ROOT / "AGENTS.md"
PAPERREAD_SKILL = PROJECT_ROOT / "skills_paperread" / "SKILL.md"
ZOTERO_REFERENCE = PROJECT_ROOT / "skills_paperread" / "references" / "zotero-workflow.md"
PDF_REFERENCE = PROJECT_ROOT / "skills_paperread" / "references" / "pdf-path-workflow.md"
SUMMARY_REFERENCE = PROJECT_ROOT / "skills_paperread" / "references" / "summary-schema.md"
CAPTURE_SCRIPT = PROJECT_ROOT / "skills_paperread" / "scripts" / "capture-secondary-url.mjs"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_public_docs_use_single_repo_local_skill_entry() -> None:
    combined = "\n".join(read(path) for path in [README, AGENTS, PAPERREAD_SKILL])

    assert "skills_paperread/" in combined
    assert "repo-local" in combined
    assert "uv sync" in combined
    assert "from the repo root" in combined
    assert "not a standalone global skill installation" in read(PAPERREAD_SKILL)
    assert ("skills/" + "zotero-paper-summary") not in combined
    assert ("skills/" + "zotero-" + "batch-note-writing") not in combined
    assert ".agents/skills/paperread" not in combined
    assert ".claude/skills/paperread" not in combined


def test_public_docs_describe_supported_workflows_and_outputs() -> None:
    combined = "\n".join(read(path) for path in [README, AGENTS, PAPERREAD_SKILL])

    for phrase in [
        "Zotero title",
        "local PDF path",
        "prepare-pdf",
        "<pdf_stem>_analysis",
        "<pdf_stem>_note.md",
        "prepare-write-candidate",
        "prepare-local-note-candidate",
    ]:
        assert phrase in combined


def test_zotero_reference_keeps_single_paper_write_safety_contract() -> None:
    text = read(ZOTERO_REFERENCE)

    for phrase in [
        "search_library",
        "get_item_details",
        "write_note",
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

    assert 'write_note(action="update"' not in text


def test_secondary_context_contract_uses_public_script_path() -> None:
    readme = read(README)
    zotero = read(ZOTERO_REFERENCE)

    for text in (readme, zotero):
        assert "skills_paperread/scripts/capture-secondary-url.mjs" in text
        assert "secondary_sources.json" in text
        assert "secondary_contexts" in text
        assert "source_status: secondary_context" in text
        assert "secondary_context_unavailable" in text
        assert "navigation_timeout" in text
        assert "must not cite secondary context" in text
        assert "--request-retries" in text


def test_pdf_path_reference_forbids_zotero_write_path() -> None:
    text = read(PDF_REFERENCE)

    for phrase in [
        "prepare-pdf",
        "prepare-local-note-candidate",
        "must not write Zotero",
        "must not call refresh-live-notes",
        "must not create write-payload.json",
        "context.md",
        "figure_context.md",
        "not a canonical evidence source",
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
    ]:
        assert phrase in text


def test_gitignore_documents_private_outputs_and_local_state() -> None:
    text = read(PROJECT_ROOT / ".gitignore")

    for phrase in [
        ".venv/",
        ".superpowers/",
        ".worktrees/",
        "runs/",
        "papers/",
        "*_analysis/",
        "*_analysis_v[0-9]*/",
        "*_note.md",
        "*_note_v[0-9]*.md",
        "*.extract.json",
        "*.summary.json",
        "*.note.md",
    ]:
        assert phrase in text


def test_capture_secondary_script_is_in_public_skill_bundle() -> None:
    assert CAPTURE_SCRIPT.exists()
    text = read(CAPTURE_SCRIPT)
    assert "secondary_context" in text
    assert "secondary_context_unavailable" in text
    assert "request-retries" in text
