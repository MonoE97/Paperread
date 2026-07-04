import pytest

from paper_reader.review import apply_review_to_summary, review_allows_write


def test_apply_review_to_summary_copies_gate_fields() -> None:
    summary = {"one_sentence_summary": "ok", "review_status": "not_reviewed"}
    review = {
        "review_status": "passed_with_caveats",
        "review_issues": [{"severity": "low", "issue": "minor", "suggested_fix": "none"}],
        "trust_status_recommendation": "usable_with_caveats",
        "needs_improvement": False,
        "improvement_requests": [],
    }

    updated = apply_review_to_summary(summary, review)

    assert updated["review_status"] == "passed_with_caveats"
    assert updated["review_issues"] == review["review_issues"]
    assert updated["trust_status"] == "usable_with_caveats"
    assert updated["improvement_status"] == "not_needed"


def test_apply_review_to_summary_clears_stale_improvement_state() -> None:
    summary = {
        "one_sentence_summary": "ok",
        "review_status": "failed",
        "improvement_status": "needed",
        "improvement_notes": [{"issue": "Old issue", "action": "", "source": "previous review"}],
    }
    review = {
        "review_status": "passed",
        "review_issues": [],
        "trust_status_recommendation": "trusted",
        "needs_improvement": False,
        "improvement_requests": [],
    }

    updated = apply_review_to_summary(summary, review)

    assert updated["review_status"] == "passed"
    assert updated["improvement_status"] == "not_needed"
    assert updated["improvement_notes"] == []


def test_apply_review_to_summary_marks_needed_improvement() -> None:
    summary = {"one_sentence_summary": "ok"}
    review = {
        "review_status": "failed",
        "review_issues": [{"severity": "high", "issue": "missing evidence"}],
        "trust_status_recommendation": "needs_manual_review",
        "needs_improvement": True,
        "improvement_requests": ["Add evidence locators."],
    }

    updated = apply_review_to_summary(summary, review)

    assert updated["review_status"] == "failed"
    assert updated["trust_status"] == "needs_manual_review"
    assert updated["improvement_status"] == "needed"
    assert updated["improvement_notes"] == [
        {
            "issue": "Add evidence locators.",
            "action": "",
            "source": "review.json",
        }
    ]


def test_apply_review_to_summary_preserves_completed_improvement_state() -> None:
    summary = {
        "one_sentence_summary": "ok",
        "improvement_status": "completed",
        "improvement_notes": [{"issue": "Missing locator", "action": "Added page 2.", "source": "review.json"}],
    }
    review = {
        "review_status": "passed",
        "review_issues": [],
        "trust_status_recommendation": "trusted",
        "needs_improvement": False,
        "improvement_requests": [],
    }

    updated = apply_review_to_summary(summary, review)

    assert updated["improvement_status"] == "completed"
    assert updated["improvement_notes"] == summary["improvement_notes"]


def test_review_allows_write() -> None:
    assert review_allows_write({"review_status": "passed", "needs_improvement": False}) is True
    assert review_allows_write({"review_status": "passed_with_caveats", "needs_improvement": False}) is True
    assert review_allows_write({"review_status": "failed", "needs_improvement": False}) is False
    assert review_allows_write({"review_status": "passed", "needs_improvement": True}) is False


def test_apply_review_to_summary_rejects_missing_needs_improvement_without_clearing_stale_state() -> None:
    summary = {
        "one_sentence_summary": "ok",
        "review_status": "failed",
        "trust_status": "needs_manual_review",
        "improvement_status": "needed",
        "improvement_notes": [{"issue": "Old issue", "action": "", "source": "previous review"}],
    }
    review = {
        "review_status": "passed",
        "review_issues": [],
        "trust_status_recommendation": "trusted",
        "improvement_requests": [],
    }

    with pytest.raises(ValueError, match="needs_improvement"):
        apply_review_to_summary(summary, review)

    assert summary["improvement_status"] == "needed"
    assert summary["improvement_notes"] == [{"issue": "Old issue", "action": "", "source": "previous review"}]


def test_apply_review_to_summary_rejects_non_bool_needs_improvement() -> None:
    summary = {"one_sentence_summary": "ok", "improvement_status": "needed"}
    review = {
        "review_status": "passed",
        "review_issues": [],
        "trust_status_recommendation": "trusted",
        "needs_improvement": "false",
        "improvement_requests": [],
    }

    with pytest.raises(ValueError, match="needs_improvement"):
        apply_review_to_summary(summary, review)

    assert summary["improvement_status"] == "needed"


def test_apply_review_to_summary_rejects_missing_review_issues() -> None:
    summary = {"one_sentence_summary": "ok", "improvement_status": "needed"}
    review = {
        "review_status": "passed",
        "trust_status_recommendation": "trusted",
        "needs_improvement": False,
        "improvement_requests": [],
    }

    with pytest.raises(ValueError, match="review_issues"):
        apply_review_to_summary(summary, review)

    assert summary["improvement_status"] == "needed"


def test_apply_review_to_summary_rejects_missing_improvement_requests() -> None:
    summary = {"one_sentence_summary": "ok", "improvement_status": "needed"}
    review = {
        "review_status": "passed",
        "review_issues": [],
        "trust_status_recommendation": "trusted",
        "needs_improvement": False,
    }

    with pytest.raises(ValueError, match="improvement_requests"):
        apply_review_to_summary(summary, review)

    assert summary["improvement_status"] == "needed"


def test_apply_review_to_summary_rejects_missing_trust_recommendation_without_preserving_stale_trusted_state() -> None:
    summary = {
        "one_sentence_summary": "ok",
        "trust_status": "trusted",
        "improvement_status": "needed",
    }
    review = {
        "review_status": "passed",
        "review_issues": [],
        "needs_improvement": False,
        "improvement_requests": [],
    }

    with pytest.raises(ValueError, match="trust_status_recommendation"):
        apply_review_to_summary(summary, review)

    assert summary["trust_status"] == "trusted"
    assert summary["improvement_status"] == "needed"


def test_review_allows_write_requires_exact_false_needs_improvement() -> None:
    assert review_allows_write({"review_status": "passed"}) is False
    assert review_allows_write({"review_status": "passed", "needs_improvement": None}) is False
    assert review_allows_write({"review_status": "passed", "needs_improvement": 0}) is False
    assert review_allows_write({"review_status": "passed", "needs_improvement": "false"}) is False
