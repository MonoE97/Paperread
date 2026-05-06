from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zotero_paperread.note import build_note_labels, validate_trusted_summary
from zotero_paperread.summary_lint import lint_summary
from zotero_paperread.zotero_details import next_version_suffix_from_details


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_gate_report(run_dir: Path, *, paper_title: str, generated_date: str) -> dict[str, Any]:
    """Build a single write-readiness report for a run directory."""
    run_dir = Path(run_dir)
    blockers: list[str] = []

    summary_path = run_dir / "summary.json"
    review_path = run_dir / "review.json"
    item_details_path = run_dir / "item-details.json"
    note_md_path = run_dir / "note.md"
    note_html_path = run_dir / "note.html"

    summary = _read_json(summary_path) if summary_path.exists() else {}
    review = _read_json(review_path) if review_path.exists() else {}
    item_details = _read_json(item_details_path) if item_details_path.exists() else {}

    if not summary_path.exists():
        blockers.append("missing summary.json")
    if not review_path.exists():
        blockers.append("missing review.json")
    if not item_details_path.exists():
        blockers.append("missing item-details.json")
    if not note_md_path.exists():
        blockers.append("missing note.md")
    if not note_html_path.exists():
        blockers.append("missing note.html")

    trusted_errors = validate_trusted_summary(summary) if summary else ["summary.json unavailable"]
    blockers.extend(f"trusted summary: {error}" for error in trusted_errors)

    lint_issues = lint_summary(summary) if summary else []
    blockers.extend(f"summary lint: {issue['code']}" for issue in lint_issues)

    if review.get("needs_improvement") is not False:
        blockers.append("review.json needs_improvement is not false")

    version_suffix = ""
    if item_details:
        version_suffix = next_version_suffix_from_details(
            item_details,
            paper_title=paper_title,
            generated_date=generated_date,
        )

    tags = build_note_labels(summary) if summary else []

    return {
        "status": "blocked" if blockers else "write_ready",
        "blockers": blockers,
        "run_dir": str(run_dir),
        "parentKey": str(item_details.get("key", "")),
        "paper_title": paper_title,
        "generated_date": generated_date,
        "version_suffix": version_suffix,
        "note_title": f"[Codex Summary] {paper_title} - {generated_date}{version_suffix}",
        "note_md_path": str(note_md_path),
        "note_html_path": str(note_html_path),
        "tags": tags,
        "review_status": summary.get("review_status"),
        "improvement_status": summary.get("improvement_status"),
        "trust_status": summary.get("trust_status"),
    }
