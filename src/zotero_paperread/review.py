from __future__ import annotations

from copy import deepcopy
from typing import Any

WRITE_READY_REVIEW_STATUSES = {"passed", "passed_with_caveats"}


def _clean_review_issues(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    issues: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue", "")).strip()
        if not issue:
            continue
        issues.append(
            {
                "severity": str(item.get("severity", "")).strip() or "medium",
                "issue": issue,
                "suggested_fix": str(item.get("suggested_fix", "")).strip(),
            }
        )
    return issues


def apply_review_to_summary(summary: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(summary)
    review_status = str(review.get("review_status", "not_reviewed")).strip() or "not_reviewed"
    needs_improvement = bool(review.get("needs_improvement", False))
    trust_recommendation = str(review.get("trust_status_recommendation", "")).strip()

    updated["review_status"] = review_status
    updated["review_issues"] = _clean_review_issues(review.get("review_issues", []))
    if trust_recommendation:
        updated["trust_status"] = trust_recommendation

    if needs_improvement:
        updated["improvement_status"] = "needed"
        requests = review.get("improvement_requests", [])
        if not isinstance(requests, list):
            requests = [str(requests)]
        updated["improvement_notes"] = [
            {"issue": str(request).strip(), "action": "", "source": "review.json"}
            for request in requests
            if str(request).strip()
        ]
    elif updated.get("improvement_status") == "completed":
        updated["improvement_status"] = "completed"
        updated["improvement_notes"] = updated.get("improvement_notes", [])
    else:
        updated["improvement_status"] = "not_needed"
        updated["improvement_notes"] = []

    return updated


def review_allows_write(review: dict[str, Any]) -> bool:
    return (
        str(review.get("review_status", "")).strip() in WRITE_READY_REVIEW_STATUSES
        and bool(review.get("needs_improvement", False)) is False
    )
