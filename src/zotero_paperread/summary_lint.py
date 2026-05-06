from __future__ import annotations

import re
from typing import Any


LOW_QUALITY_IMAGE_VALUES = {"poor", "image_too_small", "caption_only"}


def lint_summary(summary: dict[str, Any]) -> list[dict[str, str]]:
    """Return non-fatal summary issues that should be fixed before write-through."""
    issues: list[dict[str, str]] = []

    workflow_steps = summary.get("workflow_steps")
    if isinstance(workflow_steps, str) and "\n" not in workflow_steps and re.search(r"\b1\..*\b2\.", workflow_steps):
        issues.append(
            {
                "code": "workflow_steps_single_line_numbered_list",
                "message": "workflow_steps looks like a numbered list but has no line breaks",
            }
        )

    for claim_index, claim in enumerate(summary.get("evidence_summary", []) or []):
        if not isinstance(claim, dict):
            continue
        for evidence_index, evidence in enumerate(claim.get("evidence", []) or []):
            if not isinstance(evidence, dict):
                continue
            locator = str(evidence.get("locator", ""))
            if locator.startswith(("secondary_context", "wechat-context")):
                issues.append(
                    {
                        "code": "secondary_context_used_as_evidence",
                        "message": f"evidence_summary[{claim_index}].evidence[{evidence_index}] cites secondary context",
                    }
                )

    for index, figure in enumerate(summary.get("key_figures", []) or []):
        if not isinstance(figure, dict):
            continue
        image_quality = str(figure.get("image_quality", ""))
        figure_quality_note = str(figure.get("figure_quality_note", "")).strip()
        if image_quality in LOW_QUALITY_IMAGE_VALUES and not figure_quality_note:
            issues.append(
                {
                    "code": "low_quality_figure_missing_quality_note",
                    "message": f"key_figures[{index}] has {image_quality} without figure_quality_note",
                }
            )

    return issues
