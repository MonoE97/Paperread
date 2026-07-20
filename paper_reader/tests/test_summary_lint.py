from __future__ import annotations

from paper_reader.summary_lint import lint_rendered_markdown, lint_summary


def test_lint_summary_flags_single_line_numbered_workflow() -> None:
    summary = {
        "workflow_steps": "1. First. 2. Second. 3. Third.",
        "evidence_summary": [],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "workflow_steps_single_line_numbered_list" for issue in issues)


def test_lint_summary_flags_secondary_context_evidence_locator() -> None:
    summary = {
        "workflow_steps": "1. First.\n2. Second.",
        "evidence_summary": [
            {
                "claim": "Claim",
                "evidence": [
                    {"type": "text", "locator": "secondary_context.md", "summary": "Not allowed"}
                ],
            }
        ],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "secondary_context_used_as_evidence" for issue in issues)


def test_lint_summary_flags_secondary_contexts_directory_locator() -> None:
    summary = {
        "workflow_steps": "1. First.\n2. Second.",
        "evidence_summary": [
            {
                "claim": "Claim",
                "evidence": [
                    {"type": "text", "locator": "secondary_contexts/001.md", "summary": "Not allowed"}
                ],
            }
        ],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "secondary_context_used_as_evidence" for issue in issues)


def test_lint_summary_flags_secondary_sources_json_locator() -> None:
    summary = {
        "workflow_steps": "1. First.\n2. Second.",
        "evidence_summary": [
            {
                "claim": "Claim",
                "evidence": [
                    {"type": "text", "locator": "secondary_sources.json", "summary": "Not allowed"}
                ],
            }
        ],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "secondary_context_used_as_evidence" for issue in issues)


def test_lint_summary_flags_low_quality_figure_without_note() -> None:
    summary = {
        "workflow_steps": "1. First.\n2. Second.",
        "evidence_summary": [],
        "key_figures": [
            {"figure_id": "fig1", "image_quality": "image_too_small", "figure_quality_note": ""}
        ],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "low_quality_figure_missing_quality_note" for issue in issues)


def test_lint_summary_flags_section_context_locator() -> None:
    summary = {
        "evidence_summary": [
            {
                "claim": "Claim",
                "evidence": [{"locator": "section_context.md section Methods", "summary": "Not canonical"}],
            }
        ],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "malformed_trusted_evidence_locator" for issue in issues)


def test_lint_summary_flags_table_candidate_without_section() -> None:
    summary = {
        "evidence_summary": [
            {
                "claim": "Claim",
                "evidence": [{"locator": "context.md page 6 table_candidate 1", "summary": "Missing section"}],
            }
        ],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "malformed_trusted_evidence_locator" for issue in issues)


def test_lint_summary_allows_canonical_context_and_figure_locators() -> None:
    summary = {
        "evidence_summary": [
            {
                "claim": "Claim",
                "evidence": [
                    {"locator": "context.md page 3 section Methods", "summary": "Text"},
                    {"locator": "context.md page 6 section Results table_candidate 1", "summary": "Table hint"},
                    {"locator": "figure_context.md fig_p4_1", "summary": "Figure"},
                ],
            }
        ],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert not any(issue["code"] == "malformed_trusted_evidence_locator" for issue in issues)


def test_lint_summary_flags_structured_limitation_source_type_mismatch() -> None:
    summary = {
        "author_stated_limitations": [{"text": "Claimed limit.", "source_type": "inferred"}],
        "inferred_limits": [{"text": "Reader limit.", "source_type": "author_stated"}],
        "evidence_summary": [],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    codes = [issue["code"] for issue in issues]
    assert "author_stated_limitation_source_type_invalid" in codes
    assert "inferred_limit_source_type_invalid" in codes


def test_lint_summary_flags_english_prose_in_rendered_note_fields() -> None:
    summary = {
        "research_object": "NMC811 ASSLB",
        "method_modules": [
            {
                "name": "DFT/MLFF mechanism analysis",
                "input": "Li3PO4 and TaCl5 at x = 1/3",
                "target": "Identify which substructure governs lithium transport",
                "output": "Freeze Cl suppresses conductivity",
                "role": "Provides causal evidence",
            },
            {
                "name": "FIREANN",
                "input": "含外场的原子结构",
                "target": "预测外场相关原子力",
                "output": "MLMD 力场",
                "role": "加速界面采样",
            },
        ],
        "technical_details": ["NMC811 ASSLB", "MLFF 使用 VASP on-the-fly 训练。"],
        "evidence_summary": [],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    messages = [issue["message"] for issue in issues if issue["code"] == "rendered_note_field_english_prose"]
    assert any("method_modules[0].name" in message for message in messages)
    assert any("method_modules[0].input" in message for message in messages)
    assert any("method_modules[0].target" in message for message in messages)
    assert any("method_modules[0].output" in message for message in messages)
    assert any("method_modules[0].role" in message for message in messages)
    assert not any("research_object" in message for message in messages)
    assert not any("technical_details[0]" in message for message in messages)
    assert not any("method_modules[1]" in message for message in messages)


def test_lint_summary_flags_mostly_english_prose_even_with_cjk() -> None:
    summary = {
        "method_modules": [
            {
                "name": "DFT mechanism analysis 中文",
                "input": "含外场的原子结构",
                "target": "Identify which substructure governs transport。中文补充。",
                "output": "MLFF 使用 VASP on-the-fly 训练。",
                "role": "加速界面采样",
            }
        ],
        "technical_details": ["Use EIS to test conductivity。这里是中文。"],
        "evidence_summary": [],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    messages = [issue["message"] for issue in issues if issue["code"] == "rendered_note_field_english_prose"]
    assert any("method_modules[0].name" in message for message in messages)
    assert any("method_modules[0].target" in message for message in messages)
    assert any("technical_details[0]" in message for message in messages)
    assert not any("method_modules[0].output" in message for message in messages)


def test_lint_summary_allows_scattered_technical_terms_units_and_formulas() -> None:
    summary = {
        "one_sentence_summary": (
            "本文提出低成本 Li3PO4-TaCl5 非晶氧氯磷酸盐固态电解质，1/3-LPTC 以 "
            "1.3 mS cm^-1 室温电导率、0.310 eV 活化能和 NMC811 ASSLB 长循环结果支撑设计路线。"
        ),
        "core_method_short": "低能球磨 + EIS/XRD 优化 + Raman/FTIR/PDF/7Li NMR + DFT/MLFF constrained MD。",
        "method_modules": [
            {
                "name": "DFT/MLFF 约束动力学",
                "input": "DFT 松弛的 1/3-LPTC 局域模型和 VASP on-the-fly MLFF",
                "target": "识别哪个子结构主导 Li 传输",
                "output": "all-mobile 为 2.21 mS cm^-1，freeze PO4 为 1.74 mS cm^-1，freeze Cl 为 0.14 mS cm^-1",
                "role": "提供 Cl 子晶格畸变控制传导的因果证据",
            }
        ],
        "evidence_summary": [],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert not any(issue["code"] == "rendered_note_field_english_prose" for issue in issues)


def test_lint_summary_allows_common_experimental_terms_in_chinese_prose() -> None:
    summary = {
        "method": "用 XPS depth profiling、DC polarization 和 TOF-SIMS 建立界面证据链。",
        "technical_details": [
            "Li metal interface 的 post-mortem 证据来自 XPS depth profiling 与 TOF-SIMS。",
            "sulfide SSE 成本下降和低湿制造兼容是本文的主要工程价值。",
        ],
        "inferred_limits": [
            {
                "text": "保护层机制可信但仍偏 ex situ/post-mortem 证据。",
                "basis": "界面产物主要来自 cycling 后 XPS depth profiling、TOF-SIMS 与 DFT 推断。",
            }
        ],
        "applicability_limits": ["适合关注 sulfide SSE 成本下降和 Li metal interface 的材料筛选。"],
        "evidence_summary": [],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert not any(issue["code"] == "rendered_note_field_english_prose" for issue in issues)


def test_lint_summary_flags_generic_cycling_english_phrase() -> None:
    summary = {
        "technical_details": [
            "cycling performance 是核心结果。",
            "solid-state electrolyte performance 是核心结果。",
            "XPS depth profiling analysis 是核心证据。",
            "on-the-fly training 是主要方法。",
            "cycling 后 performance 是核心结果。",
        ],
        "evidence_summary": [],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    messages = [issue["message"] for issue in issues if issue["code"] == "rendered_note_field_english_prose"]
    assert any("technical_details[0]" in message for message in messages)
    assert any("technical_details[1]" in message for message in messages)
    assert any("technical_details[2]" in message for message in messages)
    assert any("technical_details[3]" in message for message in messages)
    assert any("technical_details[4]" in message for message in messages)


def test_lint_summary_does_not_let_locator_prefix_hide_english_prose() -> None:
    summary = {
        "technical_details": ["context.md page 3 section Results and discussion supports the claim."],
        "evidence_summary": [],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    messages = [issue["message"] for issue in issues if issue["code"] == "rendered_note_field_english_prose"]
    assert any("technical_details[0]" in message for message in messages)


def test_lint_summary_allows_context_locators_with_section_names() -> None:
    summary = {
        "technical_details": ["证据位置：context.md page 3 section Results and discussion。"],
        "evidence_summary": [],
        "key_figures": [],
    }

    issues = lint_summary(summary)

    assert not any(issue["code"] == "rendered_note_field_english_prose" for issue in issues)


def test_lint_summary_flags_english_caption_when_it_would_render_as_fallback() -> None:
    summary = {
        "key_figures": [{"caption": "Figure 1. Overview of the model pipeline.", "analysis": ""}],
        "evidence_summary": [],
    }

    issues = lint_summary(summary)

    messages = [issue["message"] for issue in issues if issue["code"] == "rendered_note_field_english_prose"]
    assert any("key_figures[0].caption" in message for message in messages)


def test_lint_summary_allows_english_caption_when_chinese_analysis_will_render() -> None:
    summary = {
        "key_figures": [
            {
                "caption": "Figure 1. Overview of the model pipeline.",
                "analysis": "图 1 展示模型主流程。",
            }
        ],
        "evidence_summary": [],
    }

    issues = lint_summary(summary)

    assert not any(issue["code"] == "rendered_note_field_english_prose" for issue in issues)


def test_lint_summary_does_not_block_non_rendered_figure_quality_note_prose() -> None:
    summary = {
        "key_figures": [
            {
                "caption": "Figure 1. Overview of the model pipeline.",
                "analysis": "图 1 展示模型主流程。",
                "figure_quality_note": "embedded-image caption confidence is low.",
            }
        ],
        "evidence_summary": [],
    }

    issues = lint_summary(summary)

    assert not any(issue["code"] == "rendered_note_field_english_prose" for issue in issues)


def test_lint_summary_does_not_trust_arbitrary_parenthesized_markdown_links() -> None:
    summary = {
        "technical_details": [
            "中文引导（[This external article completely contradicts the reported mechanism]"
            "(https://example.org)）"
        ]
    }

    issues = lint_summary(summary)

    assert any(issue["code"] == "rendered_note_field_english_prose" for issue in issues)


def test_lint_rendered_markdown_does_not_trust_arbitrary_parenthesized_links() -> None:
    note = (
        "- 中文引导（[This external article completely contradicts the reported mechanism]"
        "(https://example.org)）\n"
    )

    issues = lint_rendered_markdown(note)

    assert any(issue["code"] == "rendered_note_english_prose" for issue in issues)
