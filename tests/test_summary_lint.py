from __future__ import annotations

from zotero_paperread.summary_lint import lint_summary


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
