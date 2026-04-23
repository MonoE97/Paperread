from zotero_paperread.note import render_note, validate_note


METADATA = {
    "key": "ABC123",
    "title": "A Useful Materials Paper",
    "creators": "Ada Lovelace, Chen Ning",
    "date": "2026",
    "DOI": "10.1000/example",
    "url": "https://example.org/paper",
    "zoteroUrl": "zotero://select/library/items/ABC123",
}

SUMMARY = {
    "one_sentence_summary": "这篇论文提出一种用于材料发现的机器学习框架。",
    "abstract_translation": "本文摘要的中文翻译。",
    "key_points": ["提出新框架", "验证材料性质预测"],
    "research_question": "如何更可靠地预测材料性质？",
    "method": "作者结合图神经网络和物理约束。",
    "experiments": "实验覆盖多个材料数据集。",
    "contributions": ["物理约束建模", "系统实验验证"],
    "limitations": ["数据集规模有限"],
    "ai4s_relevance": "可迁移到 AI for Science 的材料性质预测任务。",
    "follow_up_keywords": ["materials discovery", "physics-informed ML"],
    "quality_score": "8.0/10",
    "extraction_warnings": [],
}


def test_render_note_contains_required_sections() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-23")

    assert "# [Codex Summary] A Useful Materials Paper - 2026-04-23" in note
    assert "## 核心结论" in note
    assert "## 研究问题" in note
    assert "## 方法拆解" in note
    assert "## AI+物理/材料启发" in note
    assert "zotero://select/library/items/ABC123" in note


def test_validate_note_accepts_complete_note() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-23")

    errors = validate_note(note)

    assert errors == []


def test_validate_note_rejects_missing_required_section() -> None:
    errors = validate_note("# title\n\n## 核心结论\ncontent")

    assert "missing_section: 元数据" in errors
    assert "missing_section: 研究问题" in errors
