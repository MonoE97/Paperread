from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_workflow_docs_do_not_reintroduce_stale_write_defaults() -> None:
    paths = [
        PROJECT_ROOT / "AGENTS.md",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md",
    ]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "--max-pages 15" not in text
        assert "content=<note markdown>" not in text
        assert "content=<contents of note.md>" not in text

    assert 'write_note(action="update", noteKey=<note_key>, content=<converted_html>)' in (
        (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        + (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")
    )


def test_single_paper_workflow_documents_duplicate_title_hard_stop() -> None:
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "same normalized title" in skill
    assert "stop before create-run" in skill
    assert "请先在 Zotero 中去重" in skill
    assert "duplicate Zotero entries" in readme
    assert "do not choose among duplicate items" in readme


def test_single_paper_workflow_avoids_plugin_hash_paths() -> None:
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")

    assert "/plugins/cache/openai-curated/superpowers/" not in skill
    assert "rg --files -g 'SKILL.md'" in skill


def test_secondary_context_is_documented_as_non_evidence() -> None:
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "capture-secondary-url" in skill
    assert "source_status: secondary_context" in skill
    assert "60000" in skill
    assert "secondary_context_unavailable" in skill
    assert "navigation_timeout" in skill
    assert "evidence_summary" in skill
    assert "must not cite secondary context" in readme
    assert "secondary_context_unavailable" in readme
    assert "navigation_timeout" in readme


def test_docs_explain_zotero_extra_secondary_sources() -> None:
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for text in (skill, readme):
        assert "secondary_sources.json" in text
        assert "Extra" in text or "其他" in text
        assert "secondary_contexts" in text
        assert "cross-check only" in text
        assert "must not be cited in evidence_summary" in text
        assert "--no-sqlite-extra-fallback" in text


def test_docs_show_smoothed_write_gate_command_chain() -> None:
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for text in (skill, readme):
        assert "save-item-details" in text
        assert "lint-summary" in text
        assert "gate-run" in text
        assert "prepare-write-payload" in text
        assert "write_note" in text
        assert "prepare-write-payload does not write to Zotero" in text
