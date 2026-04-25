import zotero_paperread.note as note_module
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
                    "locator": "page 3 method section",
                    "summary": "The method section describes the learned mapping from target response to structure parameters.",
                },
                {
                    "type": "figure",
                    "locator": "fig_p1_1",
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


def test_render_note_contains_required_sections() -> None:
    note = render_note(METADATA, SUMMARY, generated_date="2026-04-23")

    assert "# [Codex Summary] A Useful Materials Paper - 2026-04-23" in note
    assert "## 核心结论" in note
    assert "## 研究问题" in note
    assert "## 方法拆解" in note
    assert "## AI+物理/材料启发" in note
    assert "zotero://select/library/items/ABC123" in note


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

    assert "## 关键图片总览" in note
    assert "### fig_p1_1" in note
    assert "Figure 1. Overall pipeline." in note


def test_render_note_contains_trust_and_evidence_section() -> None:
    note = render_note(METADATA, {**SUMMARY_WITH_FIGURES, **TRUSTED_FIELDS}, generated_date="2026-04-23")

    assert "## 可信度与证据" in note
    assert "- **论文类型**: research_article" in note
    assert "- **可信状态**: trusted" in note
    assert "- **审查状态**: passed_with_caveats" in note
    assert "- **改进状态**: completed" in note
    assert "The method uses a learned inverse-design model." in note
    assert "page 3 method section" in note
    assert "fig_p1_1" in note
    assert "Method section was too generic." in note
    assert "\n  - 证据: page 3 method section;" in note
    assert "\n  - 证据: fig_p1_1;" in note


def test_render_note_keeps_evidence_bullets_contiguous() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        "evidence_summary": [
            {
                "claim": "The method uses a learned inverse-design model.",
                "evidence": [
                    {
                        "type": "text",
                        "locator": "page 3 method section",
                        "summary": "The method section describes the learned mapping from target response to structure parameters.",
                    },
                    {
                        "type": "figure",
                        "locator": "fig_p1_1",
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
                        "locator": "page 5 results section",
                        "summary": "Table 2 compares the proposed model with three baselines.",
                    }
                ],
                "confidence": "medium",
            },
        ],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")

    evidence_section = note.split("### 关键证据\n\n", maxsplit=1)[1].split("\n### 审查问题", maxsplit=1)[0]

    assert (
        evidence_section
        == "- 结论: The method uses a learned inverse-design model.\n"
        "  - 证据: page 3 method section; "
        "The method section describes the learned mapping from target response to structure parameters.\n"
        "  - 证据: fig_p1_1; The framework figure shows the optimization loop.\n"
        "- 结论: The experiments compare against multiple baselines.\n"
        "  - 证据: page 5 results section; Table 2 compares the proposed model with three baselines.\n"
    )
    assert "\n\n  - 证据:" not in evidence_section
    assert "\n\n- 结论:" not in evidence_section


def test_render_note_formats_evidence_lines_when_locator_or_summary_is_missing() -> None:
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
                        "locator": "fig_p1_2",
                        "summary": "",
                    },
                ],
                "confidence": "medium",
            }
        ],
        "review_issues": [],
        "improvement_notes": [],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")
    evidence_section = note.split("### 关键证据\n\n", maxsplit=1)[1].split("\n\n## 核心结论", maxsplit=1)[0].strip()

    assert "- 证据: Only summary is available." in evidence_section
    assert "- 证据: fig_p1_2" in evidence_section
    assert "- 证据: ;" not in evidence_section


def test_render_note_flattens_multiline_evidence_into_single_bullet() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        "evidence_summary": [
            {
                "claim": "Evidence text should not break Markdown list structure.",
                "evidence": [
                    {
                        "type": "text",
                        "locator": "page 4 results\n- nested locator bullet",
                        "summary": "line 1 summary\n- nested summary bullet",
                    }
                ],
                "confidence": "high",
            }
        ],
        "review_issues": [],
        "improvement_notes": [],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")
    evidence_section = note.split("### 关键证据\n\n", maxsplit=1)[1].split("\n\n## 核心结论", maxsplit=1)[0].strip()

    assert (
        evidence_section
        == "- 结论: Evidence text should not break Markdown list structure.\n"
        "  - 证据: page 4 results - nested locator bullet; line 1 summary - nested summary bullet"
    )
    assert "\n- nested locator bullet" not in evidence_section
    assert "\n- nested summary bullet" not in evidence_section


def test_render_note_separates_review_issue_bullets() -> None:
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

    assert "- medium: First issue. 建议: Fix first.\n\n- low: Second issue." in note
    assert "- First improvement.: Done. (source: review.json)\n\n- Second improvement." in note


def test_render_note_flattens_multiline_claim_into_single_bullet() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        "evidence_summary": [
            {
                "claim": "Primary conclusion line\n- looks like a nested claim bullet",
                "evidence": [
                    {
                        "type": "text",
                        "locator": "page 6 discussion",
                        "summary": "Supporting text stays on one evidence bullet.",
                    }
                ],
                "confidence": "high",
            }
        ],
        "review_issues": [],
        "improvement_notes": [],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")
    evidence_section = note.split("### 关键证据\n\n", maxsplit=1)[1].split("\n\n## 核心结论", maxsplit=1)[0].strip()

    assert (
        evidence_section
        == "- 结论: Primary conclusion line - looks like a nested claim bullet\n"
        "  - 证据: page 6 discussion; Supporting text stays on one evidence bullet."
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
    figure_section = note.split("## 关键图片总览\n\n", maxsplit=1)[1].split("## 实验与证据", maxsplit=1)[0]

    assert figure_section.strip() == "图像概览。"


def test_render_note_keeps_evidence_section_stable_without_review_or_improvement_blocks() -> None:
    summary = {
        **SUMMARY_WITH_FIGURES,
        **TRUSTED_FIELDS,
        "review_issues": [],
        "improvement_notes": [],
    }

    note = render_note(METADATA, summary, generated_date="2026-04-23")
    evidence_section = note.split("### 关键证据\n\n", maxsplit=1)[1].split("\n\n## 核心结论", maxsplit=1)[0].strip()

    assert evidence_section.endswith("  - 证据: fig_p1_1; The framework figure shows the optimization loop.")
    assert "\n\n  - 证据:" not in evidence_section
    assert "\n### 审查问题" not in note
    assert "\n### 补充优化记录" not in note


def test_render_note_contains_normalized_note_labels_with_limit() -> None:
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

    assert "## 本文标签" in note
    assert "- codex-summary" in note
    assert "- paper-summary" in note
    assert "- deep_learning" in note
    assert "- inverse_design" in note
    assert "- materials_discovery" in note
    assert "- physics_informed_ml" in note
    assert "extra_label_should_not_render" not in note
    assert note.count("- deep_learning") == 1


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
