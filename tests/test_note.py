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

SUMMARY_WITH_FIGURES = {
    **SUMMARY,
    "figure_overview": "论文的关键证据主要集中在框架图和定量对比图。",
    "key_figures": [
        {
            "figure_id": "fig_p1_1",
            "caption": "Figure 1. Overall pipeline.",
            "page": 1,
            "priority_score": 5.2,
            "why_it_matters": "这张图定义了整篇论文的方法对象和信息流。",
            "analysis": "图 1 展示了从输入结构到扩散采样再到性质打分的主链路。",
        }
    ],
}


def test_render_note_contains_required_sections() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-23")

    assert "# [Codex Summary] A Useful Materials Paper - 2026-04-23" in note
    assert "## 核心结论" in note
    assert "## 研究问题" in note
    assert "## 方法拆解" in note
    assert "## AI+物理/材料启发" in note
    assert "zotero://select/library/items/ABC123" in note


def test_render_note_contains_figure_sections() -> None:
    note = render_note(METADATA, SUMMARY_WITH_FIGURES, generated_date="2026-04-23")

    assert "## 关键图片总览" in note
    assert "### fig_p1_1" in note
    assert "Figure 1. Overall pipeline." in note


def test_validate_note_accepts_complete_note() -> None:
    note = render_note(METADATA, SUMMARY_WITH_FIGURES, generated_date="2026-04-23")

    errors = validate_note(note)

    assert errors == []


def test_validate_note_requires_figure_overview_section() -> None:
    note = render_note(METADATA, SUMMARY_WITH_FIGURES, generated_date="2026-04-23")

    errors = validate_note(note.replace("## 关键图片总览", "## 图片"))

    assert "missing_section: 关键图片总览" in errors


def test_validate_note_rejects_missing_required_section() -> None:
    errors = validate_note("# title\n\n## 核心结论\ncontent")

    assert "missing_section: 元数据" in errors
    assert "missing_section: 研究问题" in errors
