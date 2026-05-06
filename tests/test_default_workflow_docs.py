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
