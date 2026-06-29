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


def test_docs_describe_secondary_capture_retry_behavior() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")

    for text in (readme, skill):
        assert "--request-retries" in text
        normalized = text.lower()
        assert "transient cdp request failures are retried" in normalized
        assert "persistent cdp failures write secondary_context_unavailable" in normalized


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


def test_docs_describe_section_context_and_two_layer_note_contract() -> None:
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    batch_skill = (PROJECT_ROOT / "skills" / "zotero-batch-note-writing" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    worker_contract = (
        PROJECT_ROOT / "skills" / "zotero-batch-note-writing" / "references" / "worker-contract.md"
    ).read_text(encoding="utf-8")

    for text in (skill, readme, agents, batch_skill, worker_contract):
        assert "section_context.md" in text
        assert "context.md page 3 section Methods" in text
        assert "context.md page 6 section Results table_candidate 1" in text
        assert "not a canonical evidence source" in text

    for text in (skill, readme):
        assert "author_stated_limitations" in text
        assert "inferred_limits" in text
        assert "potential_gaps" in text


def test_single_paper_write_contract_uses_versioned_create_only() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")
    batch_skill = (PROJECT_ROOT / "skills" / "zotero-batch-note-writing" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    required = [
        "single-paper summary writes always create a new versioned Zotero child note",
        "Zotero local API is read-only in this project",
        "prepare-write-candidate",
        "refresh-live-notes",
        'write_note(action="create"',
    ]
    for text in (readme, skill, agents):
        for phrase in required:
            assert phrase in text

    assert 'write_note(action="update"' in readme
    assert "historical note migration" in readme
    assert "stop and report the failed update readback" in readme
    assert "refresh-live-notes" in batch_skill
    assert "prepare-write-payload" in batch_skill


def test_write_gate_documents_prepare_candidate_and_readback_order() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")
    batch_skill = (PROJECT_ROOT / "skills" / "zotero-batch-note-writing" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    expected_order = [
        "prepare-write-candidate",
        "refresh-live-notes",
        "next-version-suffix",
        "finalize-note",
        "gate-run",
        "prepare-write-payload",
        'write_note(action="create"',
        "verify-zotero-note",
        "--expected-title",
        "--expected-content-sha256",
    ]
    for text in (readme, skill):
        section = text[text.index("prepare-write-candidate") :]
        positions = [section.index(item) for item in expected_order]
        assert positions == sorted(positions)
    assert "refresh-live-notes" in batch_skill
    assert "prepare-write-payload" in batch_skill


def test_docs_describe_current_rendered_note_layout_and_readback_gate() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")

    for text in (readme, skill):
        assert "0. 阅读结论" in text
        assert "1. 速读信息" in text
        assert "5. 边界与机会" in text
        assert '--required-heading "1. 速读信息"' in text
        assert '--forbidden-heading "3. 结果可信度"' in text
        assert '--forbidden-heading "6. 我能怎么用"' in text
        assert '--forbidden-heading "7. 术语与检索"' in text

    assert "compact 0-5 reading card" in readme
    assert "当前 Zotero note 正文只渲染 0-5" in skill
    assert "legacy rendering fallback" in skill
    assert "只有旧 summary 缺少结构化局限字段时" in skill


def test_current_redesign_plan_lists_forbidden_trust_status_display_labels() -> None:
    plan = (
        PROJECT_ROOT / "docs" / "superpowers" / "plans" / "2026-06-29-zotero-note-template-redesign.md"
    ).read_text(encoding="utf-8")

    for label in [
        "可信 (trusted)",
        "可用但需注意限制 (usable_with_caveats)",
        "仅元数据可用 (metadata_only)",
        "需要人工复核 (needs_manual_review)",
    ]:
        assert label in plan


def test_skill_manual_gate_block_refreshes_live_notes_before_version_suffix() -> None:
    skill = (PROJECT_ROOT / "skills" / "zotero-paper-summary" / "SKILL.md").read_text(encoding="utf-8")
    block = skill[
        skill.index("uv run zotero-paperread validate-summary-json <run_dir>/summary.json") : skill.index(
            "10. 补充优化"
        )
    ]

    assert "refresh-live-notes" in block
    assert block.index("refresh-live-notes") < block.index("next-version-suffix")
