import paperread.note as note_module
from paperread.note import render_note, render_note_html, validate_note


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
            "title_short": "Overall pipeline",
            "caption": "Figure 1. Overall pipeline.",
            "page": 1,
            "priority_score": 5.2,
            "why_it_matters_short": "定义方法对象和信息流",
            "why_it_matters": "这张图定义了整篇论文的方法对象和信息流。",
            "evidence_level": "medium",
            "image_quality": "ok",
            "figure_quality_note": "图像质量可用于辅助理解，结论仍以正文为准。",
            "analysis": "图 1 展示了从输入结构到扩散采样再到性质打分的主链路。",
        }
    ],
}

LEARNING_FIELDS = {
    "research_object": "Au(100)/NaCl(aq) electrochemical interface",
    "research_question_short": "如何加速 finite-field electrochemical interface simulations？",
    "core_method_short": "FIREANN 学外场相关原子力，MLEDR 学电子密度响应。",
    "core_result_short": "实现约 4 个数量级加速，并预测电容、极化和界面水取向。",
    "relevance_to_user": "对 AI4S、电池界面模拟和 learned observable workflow 有直接参考价值。",
    "reading_decision": "strongly_recommended",
    "main_risk_short": "Figure 2-4 crop 过小，图像细节不能独立复核。",
    "tldr": "本文把动力学采样模型和电子响应模型拆开训练，用于电化学界面长时间尺度采样。",
    "background_problem": "电化学界面需要同时描述电势、电解液极化、离子吸附和界面水取向。",
    "existing_gap": "finite-field AIMD 成本高，经典力场难以描述电子响应。",
    "paper_entry_point": "用外场相关机器学习力场和电子密度响应模型替代昂贵的 AIMD 采样。",
    "method_overview": "方法由 FIREANN 力场和 MLEDR 电子密度响应模型组成。",
    "method_modules": [
        {"name": "FIREANN", "input": "原子结构 + 外场", "target": "外场相关原子力", "output": "MLMD 力场", "role": "加速界面结构采样"},
        {"name": "MLEDR", "input": "原子结构 + 外场 + ghost atoms", "target": "电子密度响应", "output": "charge response field", "role": "计算表面电荷和 Helmholtz capacitance"},
    ],
    "workflow_steps": "1. 生成 AIMD 数据。\n2. 训练 FIREANN。\n3. 训练 MLEDR。\n4. 执行 MLMD。\n5. 积分得到电化学可观测量。",
    "technical_details": ["训练体系为 Au(100)/5.5 M NaCl(aq)。", "MLMD 使用 0.5 fs timestep。"],
    "key_results_table": [
        {"result": "加速效果", "value": "约 4 个数量级", "meaning": "支持 ns 级界面采样"},
        {"result": "最大 Helmholtz capacitance", "value": "约 20.8 μF/cm²", "meaning": "0 V 附近出现最大电容"},
    ],
    "applicability_limits": [
        "适合研究需要外场、电势、界面极化和电子响应的电化学界面体系。",
        "不能直接推广到复杂电极、多组分电解液、真实 SEI 或反应性界面。",
    ],
    "transferable_insight": "把科学问题拆成动力学采样模型和可观测量响应模型。",
    "workflow_lessons": [
        "用 field-conditioned ML potential 学习外场下的结构动力学。",
        "用单独 response model 学习电子密度、电荷、极化或谱学响应。",
    ],
    "follow_up_questions": [
        "该 framework 能否迁移到电池 SEI / 电解液分解界面？",
        "MLEDR 是否可以替换为 charge density foundation model？",
    ],
    "concept_cards": [
        {"term": "finite-field molecular dynamics", "short_definition": "在周期体系中施加外电场的分子动力学方法。", "role_in_paper": "提供 constant-potential-like 全电池模拟框架。", "related_keywords": ["finite field", "electric field", "electrochemical interface"]},
        {"term": "MLEDR", "short_definition": "用机器学习预测电子密度响应的模型。", "role_in_paper": "从结构和外场预测 charge response。", "related_keywords": ["electron density response", "charge response", "learned observable"]},
    ],
}

TRUSTED_FIELDS = {
    "paper_type": "research_article",
    "trust_status": "trusted",
    "trust_rationale": "正文和关键图支持主要方法与实验结论。",
    "review_status": "passed_with_caveats",
    "evidence_summary": [
        {
            "claim": "The method uses a learned inverse-design model.",
            "evidence": [
                {
                    "type": "text",
                    "locator": "context.md page 3 section Methods",
                    "summary": "The method section describes the learned mapping from target response to structure parameters.",
                },
                {
                    "type": "figure",
                    "locator": "figure_context.md fig_p1_1",
                    "summary": "The framework figure shows the optimization loop.",
                },
            ],
            "confidence": "high",
        }
    ],
    "review_issues": [
        {
            "severity": "low",
            "issue": "Figure evidence is available but page evidence is brief.",
            "suggested_fix": "Keep caveat in trust rationale.",
        }
    ],
    "improvement_status": "completed",
    "improvement_notes": [
        {
            "issue": "Method section was too generic.",
            "action": "Added page-grounded method detail.",
            "source": "context.md",
        }
    ],
}


def test_render_note_contains_required_learning_sections() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-23")

    expected_sections = [
        "## 0. 阅读结论",
        "## 1. 速读信息",
        "## 2. 论文主张",
        "## 3. 方法与设计",
        "## 4. 图表导读",
        "## 5. 边界与机会",
    ]
    for section in expected_sections:
        assert section in note
    positions = [note.index(section) for section in expected_sections]
    assert positions == sorted(positions)

    forbidden_sections = [
        "## 3. 结果可信度",
        "## 6. 我能怎么用",
        "## 7. 术语与检索",
        "## 9. 元数据",
        "## 10. 证据链附录",
        "## 11. 补充优化记录",
    ]
    for section in forbidden_sections:
        assert section not in note

    assert "# [Codex Summary] A Useful Materials Paper - 2026-04-23" in note
    assert "zotero://select/library/items/ABC123" not in note


def test_render_note_uses_reading_thread_sections_without_audit_appendices() -> None:
    note = render_note(METADATA, {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS}, generated_date="2026-06-18")

    expected_sections = [
        "## 0. 阅读结论",
        "## 1. 速读信息",
        "## 2. 论文主张",
        "## 3. 方法与设计",
        "## 4. 图表导读",
        "## 5. 边界与机会",
    ]
    for section in expected_sections:
        assert section in note

    assert "## 3. 结果可信度" not in note
    assert "## 6. 我能怎么用" not in note
    assert "## 7. 术语与检索" not in note
    assert "## 9. 元数据" not in note
    assert "## 10. 证据链附录" not in note
    assert "## 11. 补充优化记录" not in note
    assert note.index("## 0. 阅读结论") < note.index("## 1. 速读信息")
    assert note.index("## 3. 方法与设计") < note.index("## 4. 图表导读")
    assert note.index("## 5. 边界与机会") < note.index("---\n\nTags: codex-summary, paper-summary")


def test_render_note_separates_dynamic_lists_from_following_headings() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        **LEARNING_FIELDS,
        "recommended_sections": [
            {"section": "Methods", "reason": "Read method details.", "locator": "context.md page 2"}
        ],
        "recommended_figures": [
            {"figure_id": "Fig. 1", "reason": "Read figure details.", "locator": "context.md page 3"}
        ],
        "baseline_or_comparison": [
            {"target": "Baseline", "result": "Comparison result.", "locator": "context.md page 4"}
        ],
        "result_evidence_notes": [
            {
                "result": "Main result",
                "evidence": "Evidence text.",
                "locator": "context.md page 5",
                "confidence": "high",
            }
        ],
    }

    note = render_note(METADATA, summary, generated_date="2026-06-18")

    assert "## 3. 结果可信度" not in note
    assert "## 6. 我能怎么用" not in note
    assert "## 7. 术语与检索" not in note
    assert "### 推荐先读章节" not in note
    assert "### 推荐先看图表" not in note
    assert "baseline / comparison" not in note
    assert "结果证据说明" not in note
    assert "证据质量" not in note
    assert "Read method details." not in note
    assert "Comparison result." not in note


def test_render_note_uses_decision_and_quick_info_tables() -> None:
    note = render_note(
        METADATA,
        {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS, **LEARNING_FIELDS},
        generated_date="2026-04-23",
    )

    assert "## 0. 阅读结论\n\n| 项目 | 内容 |\n| --- | --- |" in note
    assert "| 30 秒结论 | 本文把动力学采样模型和电子响应模型拆开训练，用于电化学界面长时间尺度采样。 |" in note
    assert "| 主要风险 | Figure 2-4 crop 过小，图像细节不能独立复核。 |" in note
    assert "| 阅读决策 | 强烈建议精读 |" in note

    assert "## 1. 速读信息\n\n| 项目 | 内容 |\n| --- | --- |" in note
    assert "| 论文类型 | 研究论文 |" in note
    assert "| 研究对象 | Au(100)/NaCl(aq) electrochemical interface |" in note
    assert "| 核心问题 | 如何加速 finite-field electrochemical interface simulations？ |" in note
    assert "| 核心方法 | FIREANN 学外场相关原子力，MLEDR 学电子密度响应。 |" in note
    assert "| 核心结果 | 实现约 4 个数量级加速，并预测电容、极化和界面水取向。 |" in note


def test_render_note_localizes_internal_enum_values() -> None:
    note = render_note(
        METADATA,
        {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS, **LEARNING_FIELDS},
        generated_date="2026-04-23",
    )

    assert "| 论文类型 | 研究论文 |" in note
    assert "| 阅读决策 | 强烈建议精读 |" in note
    assert "| Figure 1 | 可读 |" in note
    assert "research_article" not in note
    assert "strongly_recommended" not in note
    assert "| Figure 1 | ok |" not in note


def test_render_note_renders_recommendations_result_evidence_and_gap_fields() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        **LEARNING_FIELDS,
        "recommended_sections": [
            {
                "section": "Methods",
                "locator": "context.md page 2 section Methods",
                "reason": "Best source for model design.",
            }
        ],
        "recommended_figures": [
            {
                "figure_id": "fig_p1_1",
                "locator": "figure_context.md fig_p1_1",
                "reason": "Shows the overall workflow.",
            }
        ],
        "baseline_or_comparison": [
            {
                "target": "DFT baseline",
                "result": "Lower MAE on formation energy prediction.",
                "locator": "context.md page 3 section Results table_candidate 1",
            }
        ],
        "result_evidence_notes": [
            {
                "result": "Conductivity improved.",
                "evidence": "Reported with numeric comparison.",
                "locator": "context.md page 3 section Results table_candidate 1",
                "confidence": "medium",
            }
        ],
        "author_stated_limitations": [
            {
                "text": "The authors evaluate one material family.",
                "locator": "context.md page 8 section Discussion",
                "source_type": "author_stated",
            }
        ],
        "inferred_limits": [
            {
                "text": "Transfer to sulfide solid electrolytes is not established.",
                "basis": "The experiments cover oxide examples only.",
                "locator": "context.md page 6 section Results",
                "source_type": "inferred",
            }
        ],
        "potential_gaps": [
            {
                "text": "Reactive battery interfaces remain open.",
                "basis": "The paper validates non-reactive examples.",
                "locator": "context.md page 7 section Results",
                "uncertainty": "AI inference",
            }
        ],
        "evidence_quality_summary": "Full text and figure context are available; table candidates are medium-confidence.",
    }

    rendered = render_note(METADATA, summary, generated_date="2026-06-18")

    assert "## 0. 阅读结论" in rendered
    assert "## 1. 速读信息" in rendered
    assert "## 3. 结果可信度" not in rendered
    assert "## 6. 我能怎么用" not in rendered
    assert "## 7. 术语与检索" not in rendered
    assert "### 推荐先读章节" not in rendered
    assert "### 推荐先看图表" not in rendered
    assert "Methods: Best source for model design. (context.md page 2 section Methods)" not in rendered
    assert "fig_p1_1: Shows the overall workflow. (figure_context.md fig_p1_1)" not in rendered
    assert "baseline / comparison" not in rendered
    assert "DFT baseline" not in rendered
    assert "Conductivity improved." not in rendered
    assert "结果证据说明" not in rendered
    assert "Full text and figure context are available" not in rendered
    assert "### 作者明示局限" in rendered
    assert "The authors evaluate one material family. (context.md page 8 section Discussion)" in rendered
    assert "### LLM 推断限制" in rendered
    assert "Transfer to sulfide solid electrolytes is not established." in rendered
    assert "依据: The experiments cover oxide examples only." in rendered
    assert "### 潜在 gap / 后续问题" not in rendered
    assert "潜在 gap" not in rendered
    assert "Reactive battery interfaces remain open." not in rendered
    assert "uncertainty: AI inference" not in rendered
    assert "\n- **阅读决策**:" not in rendered
    assert "\n- **与我的研究关系**:" not in rendered
    assert "\n- Transfer to sulfide solid electrolytes is not established." in rendered
    assert ")- **" not in rendered
    assert ")- Transfer to sulfide solid electrolytes is not established." not in rendered


def test_render_note_does_not_duplicate_follow_up_questions_as_potential_gaps() -> None:
    summary = {
        **SUMMARY,
        "follow_up_questions": [
            "Can this workflow transfer to SEI interfaces?",
            "Can the response model predict charge density?",
        ],
    }

    rendered = render_note(METADATA, summary, generated_date="2026-06-18")

    assert "### 潜在 gap / 后续问题" not in rendered
    assert "## 6. 我能怎么用" not in rendered
    assert "## 7. 术语与检索" not in rendered
    assert "Can this workflow transfer to SEI interfaces?" not in rendered
    assert "Can the response model predict charge density?" not in rendered


def test_render_note_escapes_pipe_characters_inside_markdown_table_cells() -> None:
    note = render_note(
        {**METADATA, "title": "Alpha | Beta"},
        {
            **SUMMARY,
            "research_object": "Battery | Interface",
            "method_modules": [
                {
                    "name": "Module | A",
                    "input": "Input | structure",
                    "target": "Target | property",
                    "output": "Output | score",
                    "role": "Role | ranking",
                }
            ],
        },
        generated_date="2026-04-23",
    )

    assert "| 研究对象 | Battery \\| Interface |" in note
    assert "| Module \\| A | Input \\| structure | Target \\| property | Output \\| score | Role \\| ranking |" in note
    assert "# [Codex Summary] Alpha | Beta - 2026-04-23" in note
    assert "| Module | A | Input | structure |" not in note


def test_render_note_html_converts_markdown_tables_to_zotero_ready_html() -> None:
    note = render_note(
        {**METADATA, "title": "Alpha | Beta"},
        {
            **SUMMARY,
            "research_object": "Battery | Interface",
            "method_modules": [
                {
                    "name": "Module | A",
                    "input": "Input | structure",
                    "target": "Target | property",
                    "output": "Output | score",
                    "role": "Role | ranking",
                }
            ],
        },
        generated_date="2026-04-23",
    )

    html = render_note_html(note)

    assert "<table>" in html
    assert "<th>项目</th>" in html
    assert "<td>Battery | Interface</td>" in html
    assert "<h1>[Codex Summary] Alpha | Beta - 2026-04-23</h1>" in html
    assert "<td>Module | A</td>" in html
    assert "| --- | --- |" not in html


def test_render_note_html_escapes_raw_html_from_note_source() -> None:
    html = render_note_html(
        "# Test\n\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        "| unsafe | <script>alert(1)</script> & text |\n"
    )

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; text" in html


def test_render_note_old_summary_uses_safe_fallbacks() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-23")

    assert "| 研究对象 | 未知 |" in note
    assert "| 核心问题 | 如何更可靠地预测材料性质？ |" in note
    assert "| 核心方法 | 作者结合图神经网络和物理约束。 |" in note
    assert "| 核心结果 | 这篇论文提出一种用于材料发现的机器学习框架。 |" in note
    assert "### 背景问题" in note
    assert "### 本文切入点 + 贡献" in note
    assert "### 方法总览" in note
    assert "### 作者明示局限" in note
    assert "## 7. 术语与检索" not in note
    assert "### 核心概念" not in note
    assert "本文摘要的中文翻译。" not in note
    assert "实验覆盖多个材料数据集。" not in note


def test_render_note_accepts_workflow_steps_as_list() -> None:
    note = render_note(
        METADATA,
        {**SUMMARY, "workflow_steps": ["生成 AIMD 数据", "训练 FIREANN", "训练 MLEDR"]},
        generated_date="2026-04-23",
    )

    assert "### 工作流" in note
    assert "1. 生成 AIMD 数据\n2. 训练 FIREANN\n3. 训练 MLEDR" in note


def test_render_note_prefers_specific_visual_quality_warning() -> None:
    summary = {
        **SUMMARY,
        "figure_overview": "图表总览。",
        "key_figures": [
            {
                "figure_id": "fig_p1_1",
                "caption": "Figure 1. Overall pipeline.",
                "page": 1,
                "why_it_matters": "测试图作用。",
                "image_quality": "poor",
                "visual_quality": {"status": "poor", "warnings": ["image_too_small"]},
                "analysis": "图像质量不足，只能基于正文和 caption 分析。",
            }
        ],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")

    assert "| 图 | 图像抽取质量 | 图片描述内容 |" in note
    assert "| Figure 1 | 图像过小 |" in note
    assert "图像质量不足，只能基于正文和 caption 分析。" in note
    assert "支撑的核心主张：测试图作用。" in note
    assert "图像抽取质量较低，以上判断仅基于正文/图注证据。" in note


def test_render_note_uses_why_it_matters_short_in_figure_description() -> None:
    summary = {
        **SUMMARY,
        "key_figures": [
            {
                "caption": "Figure 1. Workflow.",
                "image_quality": "ok",
                "analysis": "图 1 展示主要 workflow。",
                "why_it_matters_short": "支撑方法主链路判断",
            }
        ],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")

    assert "图 1 展示主要 workflow。" in note
    assert "支撑的核心主张：支撑方法主链路判断" in note


def test_render_note_keeps_audit_evidence_out_of_rendered_note() -> None:
    note = render_note(
        METADATA,
        {
            **SUMMARY_WITH_FIGURES,
            **TRUSTED_FIELDS,
            "extraction_warnings": ["figure_visual_quality:fig_p1_1:image_too_small"],
            "main_risk_short": "Visual crop risk.",
        },
        generated_date="2026-04-23",
    )

    warning = "figure_visual_quality:fig_p1_1:image_too_small"
    quality_report_marker = "## 10. 自动抽取质量报告"
    claim_text = "The method uses a learned inverse-design model."
    page_evidence_line = "- context.md page 3 section Methods: The method section describes the learned mapping"

    assert quality_report_marker not in note
    assert warning not in note
    assert claim_text not in note
    assert page_evidence_line not in note
    assert "## 9. 元数据" not in note
    assert "## 10. 证据链附录" not in note
    assert "## 11. 补充优化记录" not in note
    assert "\n-   - 证据:" not in note
    assert "\n  - 证据:" not in note


def test_render_note_uses_date_only_for_first_version_suffix() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-25", version_suffix="")

    assert "# [Codex Summary] A Useful Materials Paper - 2026-04-25" in note


def test_render_note_appends_same_day_version_suffix() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-25", version_suffix=" (v2)")

    assert "# [Codex Summary] A Useful Materials Paper - 2026-04-25 (v2)" in note


def test_next_same_day_version_suffix_skips_existing_versions() -> None:
    suffix = note_module.next_same_day_version_suffix(
        [
            "[Codex Summary] A Useful Materials Paper - 2026-04-25",
            "[Codex Summary] A Useful Materials Paper - 2026-04-25 (v2)",
        ],
        paper_title="A Useful Materials Paper",
        generated_date="2026-04-25",
    )

    assert suffix == " (v3)"


def test_render_note_contains_figure_sections() -> None:
    note = render_note(METADATA, SUMMARY_WITH_FIGURES, generated_date="2026-04-23")

    assert "## 4. 图表导读" in note
    assert "| 图 | 图像抽取质量 | 图片描述内容 |" in note
    assert "| Figure 1 | 可读 |" in note
    assert "图 1 展示了从输入结构到扩散采样再到性质打分的主链路。" in note
    assert "这张图定义了整篇论文的方法对象和信息流。" in note
    assert "### 图表总览" not in note
    assert "### 图表索引" not in note
    assert "### 展开图表" not in note
    assert "### Figure 1：Overall pipeline" not in note


def test_render_note_uses_single_figure_table() -> None:
    note = render_note(
        METADATA,
        {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS, **LEARNING_FIELDS},
        generated_date="2026-04-23",
    )

    assert "## 4. 图表导读\n\n| 图 | 图像抽取质量 | 图片描述内容 |\n| --- | --- | --- |" in note
    assert "| Figure 1 | 可读 |" in note
    assert "图 1 展示了从输入结构到扩散采样再到性质打分的主链路。" in note
    assert "这张图定义了整篇论文的方法对象和信息流。" in note
    assert "### 图表总览" not in note
    assert "### 图表索引" not in note
    assert "### 展开图表" not in note
    assert "证据等级" not in note


def test_render_note_figure_section_stays_table_when_no_figures() -> None:
    note = render_note(
        METADATA,
        {**SUMMARY, **TRUSTED_FIELDS, **LEARNING_FIELDS, "key_figures": []},
        generated_date="2026-04-23",
    )

    assert "## 4. 图表导读\n\n| 图 | 图像抽取质量 | 图片描述内容 |\n| --- | --- | --- |" in note
    assert "| 无 | 未知 | 未抽取到可用图表；图表导读不可用，请以正文与证据摘要为准。 |" in note
    figure_section = note.split("## 4. 图表导读", maxsplit=1)[1].split("## 5. 边界与机会", maxsplit=1)[0]
    assert "- none" not in figure_section


def test_render_note_falls_back_to_ordered_figure_labels_without_caption_number() -> None:
    summary = {
        **SUMMARY,
        "figure_overview": "图表总览。",
        "key_figures": [
            {
                "figure_id": "p1-f1",
                "caption": "Overview of the model pipeline.",
                "title_short": "Pipeline",
                "page": 1,
                "why_it_matters": "第一张图。",
                "analysis": "第一张图分析。",
            },
            {
                "figure_id": "source-2-image-panel",
                "caption": "",
                "title_short": "Results",
                "page": 2,
                "why_it_matters": "第二张图。",
                "analysis": "第二张图分析。",
            },
        ],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")

    assert "| Figure 1 | 未知 |" in note
    assert "| Figure 2 | 未知 |" in note
    assert "第一张图分析。" in note
    assert "第二张图分析。" in note
    assert "| p1-f1 |" not in note
    assert "| source-2-image-panel |" not in note
    assert "### Figure 1：Pipeline" not in note
    assert "### Figure 2：Results" not in note


def test_render_note_fallback_figure_labels_ignore_skipped_items() -> None:
    summary = {
        **SUMMARY,
        "figure_overview": "图表总览。",
        "key_figures": [
            "not-a-dict",
            {
                "figure_id": "p1-f1",
                "caption": "Overview of the model pipeline.",
                "title_short": "Pipeline",
                "page": 1,
                "why_it_matters": "第一张图。",
                "analysis": "第一张图分析。",
            },
        ],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")

    assert "| Figure 1 | 未知 |" in note
    assert "| Figure 2 | 未知 |" not in note
    assert "### Figure 1：Pipeline" not in note


def test_render_note_normalizes_common_figure_label_forms() -> None:
    summary = {
        **SUMMARY,
        "figure_overview": "图表总览。",
        "key_figures": [
            {
                "figure_id": "source-0-rawfig",
                "caption": "Fig. 2a. Conductivity comparison.",
                "title_short": "Conductivity",
                "page": 3,
                "why_it_matters": "展示电导率对比。",
                "analysis": "图 2a 分析。",
            },
            {
                "figure_id": "source-1-scheme",
                "caption": "Scheme 1. Synthesis workflow.",
                "title_short": "Workflow",
                "page": 4,
                "why_it_matters": "展示合成流程。",
                "analysis": "Scheme 1 分析。",
            },
            {
                "figure_id": "source-2-range",
                "caption": "Figure 3-4. Stability analysis.",
                "title_short": "Stability",
                "page": 5,
                "why_it_matters": "展示稳定性分析。",
                "analysis": "图 3-4 分析。",
            },
        ],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")

    assert "| Figure 2a |" in note
    assert "| Scheme 1 |" in note
    assert "| Figure 3-4 |" in note
    assert "图 2a 分析。" in note
    assert "Scheme 1 分析。" in note
    assert "图 3-4 分析。" in note
    assert "### Figure 2a：Conductivity" not in note
    assert "### Scheme 1：Workflow" not in note
    assert "### Figure 3-4：Stability" not in note


def test_render_note_hides_gate_and_audit_only_fields() -> None:
    note = render_note(
        METADATA,
        {
            **SUMMARY_WITH_FIGURES,
            **TRUSTED_FIELDS,
            **LEARNING_FIELDS,
            "potential_gaps": [
                {
                    "text": "需要真实高面容量软包验证。",
                    "basis": "当前实验仍是扣式或实验室尺度。",
                    "uncertainty": "medium",
                    "locator": "context.md page 7",
                }
            ],
        },
        generated_date="2026-04-23",
    )
    html = render_note_html(note)

    forbidden_snippets = [
        "可信状态",
        "可信 (trusted)",
        "trust_status",
        "与我的研究关系",
        "质量评分",
        "关键结果表",
        "baseline / comparison",
        "结果证据说明",
        "证据质量",
        "可迁移启发",
        "工作流经验",
        "后续问题",
        "核心概念",
        "后续检索关键词",
        "潜在 gap",
        "需要真实高面容量软包验证。",
        "The method uses a learned inverse-design model.",
        "context.md page 3 section Methods",
        "Method section was too generic.",
    ]
    for snippet in forbidden_snippets:
        assert snippet not in note
        assert snippet not in html


def test_render_note_places_trailing_tags_after_reading_sections() -> None:
    note = render_note(METADATA, {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS}, generated_date="2026-04-23")

    assert "## 本文标签" not in note
    assert note.count("\nTags: codex-summary, paper-summary") == 1
    assert "## 10. 自动抽取质量报告" not in note
    assert "## 10. 证据链附录" not in note
    assert "## 11. 补充优化记录" not in note
    assert "## 3. 结果可信度" not in note
    assert "## 6. 我能怎么用" not in note
    assert "## 7. 术语与检索" not in note
    assert note.index("## 5. 边界与机会") < note.index("---\n\nTags: codex-summary, paper-summary")


def test_clean_evidence_summary_keeps_evidence_bullets_contiguous() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        "evidence_summary": [
            {
                "claim": "The method uses a learned inverse-design model.",
                "evidence": [
                    {
                        "type": "text",
                        "locator": "context.md page 3 section Methods",
                        "summary": "The method section describes the learned mapping from target response to structure parameters.",
                    },
                    {
                        "type": "figure",
                        "locator": "figure_context.md fig_p1_1",
                        "summary": "The framework figure shows the optimization loop.",
                    },
                ],
                "confidence": "high",
            },
            {
                "claim": "The experiments compare against multiple baselines.",
                "evidence": [
                    {
                        "type": "text",
                        "locator": "context.md page 5 section Results",
                        "summary": "Table 2 compares the proposed model with three baselines.",
                    }
                ],
                "confidence": "medium",
            },
        ],
    }

    evidence_items = note_module.clean_evidence_summary(summary)
    rendered_lines = []
    for index, item in enumerate(evidence_items, start=1):
        rendered_lines.append(f"### Claim {index}")
        rendered_lines.append("")
        rendered_lines.append(f"**结论**: {item['claim']}")
        rendered_lines.append("")
        rendered_lines.append("**证据**:")
        rendered_lines.extend(f"- {evidence['line']}" for evidence in item["evidence"])
        if index != len(evidence_items):
            rendered_lines.append("")
    evidence_section = "\n".join(rendered_lines)

    assert (
        evidence_section
        == "### Claim 1\n\n"
        "**结论**: The method uses a learned inverse-design model.\n\n"
        "**证据**:\n"
        "- context.md page 3 section Methods: "
        "The method section describes the learned mapping from target response to structure parameters.\n"
        "- figure_context.md fig_p1_1: The framework figure shows the optimization loop.\n\n"
        "### Claim 2\n\n"
        "**结论**: The experiments compare against multiple baselines.\n\n"
        "**证据**:\n"
        "- context.md page 5 section Results: Table 2 compares the proposed model with three baselines."
    )
    assert "\n\n  - 证据:" not in evidence_section
    assert "\n-   - 证据:" not in evidence_section


def test_clean_evidence_summary_formats_evidence_lines_when_locator_or_summary_is_missing() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        "evidence_summary": [
            {
                "claim": "Mixed evidence coverage.",
                "evidence": [
                    {
                        "type": "text",
                        "locator": "",
                        "summary": "Only summary is available.",
                    },
                    {
                        "type": "figure",
                        "locator": "figure_context.md fig_p1_2",
                        "summary": "",
                    },
                ],
                "confidence": "medium",
            }
        ],
        "review_issues": [],
        "improvement_notes": [],
    }

    evidence_section = "\n".join(
        f"- {evidence['line']}" for item in note_module.clean_evidence_summary(summary) for evidence in item["evidence"]
    )

    assert "- Only summary is available." in evidence_section
    assert "- figure_context.md fig_p1_2" in evidence_section
    assert "- :" not in evidence_section


def test_clean_evidence_summary_flattens_multiline_evidence_into_single_bullet() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        "evidence_summary": [
            {
                "claim": "Evidence text should not break Markdown list structure.",
                "evidence": [
                    {
                        "type": "text",
                        "locator": "context.md page 4 section Results\n- nested locator bullet",
                        "summary": "line 1 summary\n- nested summary bullet",
                    }
                ],
                "confidence": "high",
            }
        ],
        "review_issues": [],
        "improvement_notes": [],
    }

    item = note_module.clean_evidence_summary(summary)[0]
    evidence_section = (
        "### Claim 1\n\n"
        f"**结论**: {item['claim']}\n\n"
        "**证据**:\n"
        f"- {item['evidence'][0]['line']}"
    )

    assert (
        evidence_section
        == "### Claim 1\n\n"
        "**结论**: Evidence text should not break Markdown list structure.\n\n"
        "**证据**:\n"
        "- context.md page 4 section Results - nested locator bullet: line 1 summary - nested summary bullet"
    )
    assert "\n- nested locator bullet" not in evidence_section
    assert "\n- nested summary bullet" not in evidence_section


def test_render_note_omits_review_issue_bullets_but_separates_improvements() -> None:
    note = render_note(
        METADATA,
        {
            **SUMMARY_WITH_FIGURES,
            **TRUSTED_FIELDS,
            "review_issues": [
                {"severity": "medium", "issue": "First issue.", "suggested_fix": "Fix first."},
                {"severity": "low", "issue": "Second issue.", "suggested_fix": "Fix second."},
            ],
            "improvement_notes": [
                {"issue": "First improvement.", "action": "Done.", "source": "review.json"},
                {"issue": "Second improvement.", "action": "Done.", "source": "review.json"},
            ],
        },
        generated_date="2026-04-26",
    )

    assert "- medium: First issue. 建议: Fix first." not in note
    assert "- low: Second issue. 建议: Fix second." not in note
    assert "- First improvement.: Done. (source: review.json)" not in note
    assert "- Second improvement.: Done. (source: review.json)" not in note


def test_clean_evidence_summary_flattens_multiline_claim_into_single_bullet() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        "evidence_summary": [
            {
                "claim": "Primary conclusion line\n- looks like a nested claim bullet",
                "evidence": [
                    {
                        "type": "text",
                        "locator": "context.md page 6 section Discussion",
                        "summary": "Supporting text stays on one evidence bullet.",
                    }
                ],
                "confidence": "high",
            }
        ],
        "review_issues": [],
        "improvement_notes": [],
    }

    item = note_module.clean_evidence_summary(summary)[0]
    evidence_section = (
        "### Claim 1\n\n"
        f"**结论**: {item['claim']}\n\n"
        "**证据**:\n"
        f"- {item['evidence'][0]['line']}"
    )

    assert (
        evidence_section
        == "### Claim 1\n\n"
        "**结论**: Primary conclusion line - looks like a nested claim bullet\n\n"
        "**证据**:\n"
        "- context.md page 6 section Discussion: Supporting text stays on one evidence bullet."
    )
    assert "\n- looks like a nested claim bullet" not in evidence_section


def test_render_note_ignores_string_values_for_list_sections() -> None:
    summary = {
        **SUMMARY,
        "key_points": "not-a-list",
        "contributions": "not-a-list",
        "limitations": "not-a-list",
        "follow_up_keywords": "not-a-list",
        "key_figures": "not-a-list",
        "figure_overview": "图像概览。",
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")

    assert "\n- n\n- o\n- t\n" not in note
    assert "### n" not in note
    figure_section = note.split("## 4. 图表导读\n\n", maxsplit=1)[1].split(
        "## 5. 边界与机会", maxsplit=1
    )[0]

    assert "| 图 | 图像抽取质量 | 图片描述内容 |" in figure_section
    assert "| 无 | 未知 | 未抽取到可用图表；图表导读不可用，请以正文与证据摘要为准。 |" in figure_section
    assert "### 图表总览" not in figure_section
    assert "### 图表索引" not in figure_section
    assert "### 展开图表" not in figure_section


def test_render_note_keeps_audit_sections_hidden_without_review_or_improvement_blocks() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        "review_issues": [],
        "improvement_notes": [],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")

    assert "figure_context.md fig_p1_1: The framework figure shows the optimization loop." not in note
    assert "\n\n  - 证据:" not in note
    assert "### 审查问题\n\n- none" not in note
    assert "## 11. 补充优化记录\n\n- none" not in note
    assert "- **改进状态**: completed\n\n- none" not in note
    assert "可信状态" not in note
    assert "## 3. 结果可信度" not in note
    assert "## 6. 我能怎么用" not in note
    assert "## 7. 术语与检索" not in note


def test_render_note_moves_normalized_note_labels_to_trailing_tags() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        "note_labels": [
            "Deep Learning",
            "inverse-design",
            "materials discovery",
            "physics-informed ML",
            "extra label should not render",
            "deep_learning",
        ],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")

    assert "## 本文标签" not in note
    assert (
        "\nTags: codex-summary, paper-summary, deep_learning, inverse_design, "
        "materials_discovery, physics_informed_ml\n"
    ) in note
    assert "- codex-summary" not in note
    assert "- paper-summary" not in note
    assert "- deep_learning" not in note
    assert "- inverse_design" not in note
    assert "- materials_discovery" not in note
    assert "- physics_informed_ml" not in note
    assert "extra_label_should_not_render" not in note
    assert note.count("deep_learning") == 1


def test_validate_note_accepts_complete_note() -> None:
    note = render_note(METADATA, SUMMARY_WITH_FIGURES, generated_date="2026-04-23")

    errors = validate_note(note)

    assert errors == []


def test_validate_note_requires_figure_overview_section() -> None:
    note = render_note(METADATA, SUMMARY_WITH_FIGURES, generated_date="2026-04-23")

    errors = validate_note(note.replace("## 4. 图表导读", "## 图片"))

    assert "missing_section: 4. 图表导读" in errors


def test_validate_note_requires_level_two_required_sections() -> None:
    note = render_note(METADATA, SUMMARY_WITH_FIGURES, generated_date="2026-04-23")

    errors = validate_note(note.replace("## 1. 速读信息", "### 1. 速读信息"))

    assert "missing_section: 1. 速读信息" in errors


def test_validate_note_reports_missing_new_sections_and_forbidden_old_sections() -> None:
    old_note = """# [Codex Summary] Old - 2026-04-23

## 0. 阅读结论

## 1. 论文主张

## 2. 方法与设计

## 3. 结果可信度

## 4. 图表导读

## 5. 边界与机会

## 6. 我能怎么用

## 7. 术语与检索

---

Tags: codex-summary, paper-summary
"""

    errors = validate_note(old_note)

    assert "missing_section: 1. 速读信息" in errors
    assert "missing_section: 3. 方法与设计" in errors
    assert "forbidden_section: 3. 结果可信度" in errors
    assert "forbidden_section: 6. 我能怎么用" in errors
    assert "forbidden_section: 7. 术语与检索" in errors


def test_validate_note_rejects_missing_required_section() -> None:
    errors = validate_note("# title\n\n## 旧结构\ncontent")

    assert "missing_section: 0. 阅读结论" in errors
    assert "missing_section: 1. 速读信息" in errors


def test_validate_note_rejects_rendered_trust_status_display_labels() -> None:
    note = render_note(METADATA, SUMMARY_WITH_FIGURES, generated_date="2026-04-23")

    trust_status_labels = [
        "可信 (trusted)",
        "可用但需注意限制 (usable_with_caveats)",
        "仅元数据可用 (metadata_only)",
        "需要人工复核 (needs_manual_review)",
    ]
    for label in trust_status_labels:
        errors = validate_note(note.replace("## 5. 边界与机会", f"{label}\n\n## 5. 边界与机会"))

        assert f"forbidden_content: {label}" in errors


def test_validate_trusted_summary_still_requires_audit_only_fields() -> None:
    summary = {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS, **LEARNING_FIELDS}
    summary["limitations"] = []
    summary["follow_up_keywords"] = []

    errors = note_module.validate_trusted_summary(summary)

    assert "limitations must contain at least one item" in errors
    assert "follow_up_keywords must contain at least one item" in errors
