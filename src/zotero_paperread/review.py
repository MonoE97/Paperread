from __future__ import annotations

from copy import deepcopy
from typing import Any

VALID_REVIEW_STATUSES = {"not_reviewed", "passed", "passed_with_caveats", "failed"}
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


def validate_review_payload(review: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    review_status = review.get("review_status")
    if not isinstance(review_status, str) or not review_status.strip():
        errors.append("review_status is required")
    elif review_status.strip() not in VALID_REVIEW_STATUSES:
        allowed = ", ".join(sorted(VALID_REVIEW_STATUSES))
        errors.append(f"review_status must be one of: {allowed}")

    if "needs_improvement" not in review:
        errors.append("needs_improvement is required")
    elif not isinstance(review["needs_improvement"], bool):
        errors.append("needs_improvement must be an explicit boolean")

    trust_recommendation = review.get("trust_status_recommendation")
    if not isinstance(trust_recommendation, str) or not trust_recommendation.strip():
        errors.append("trust_status_recommendation is required")

    return errors


def apply_review_to_summary(summary: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    errors = validate_review_payload(review)
    if errors:
        raise ValueError(f"review_payload_invalid: {'; '.join(errors)}")

    updated = deepcopy(summary)
    review_status = review["review_status"].strip()
    needs_improvement = review["needs_improvement"]
    trust_recommendation = review["trust_status_recommendation"].strip()

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
        and review.get("needs_improvement") is False
    )
