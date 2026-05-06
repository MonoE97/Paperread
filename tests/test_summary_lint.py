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
