from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from paper_reader.note import build_note_labels, validate_trusted_summary
from paper_reader.summary_lint import lint_summary
from paper_reader.zotero_details import next_version_suffix_from_details


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _has_live_notes_refresh(item_details: dict[str, Any], *, parent_key: str) -> bool:
    if not parent_key:
        return False
    paper_reader = item_details.get("_paper_reader", {})
    if not isinstance(paper_reader, dict):
        return False
    enrichment = paper_reader.get("enrichment", {})
    if not isinstance(enrichment, dict):
        return False
    live_notes = enrichment.get("live_notes", {})
    if not isinstance(live_notes, dict):
        return False
    return (
        live_notes.get("status") == "refreshed"
        and live_notes.get("source") == "zotero_local_api_readonly"
        and str(live_notes.get("item_key", "")).strip() == parent_key
        and bool(str(live_notes.get("refreshed_at", "")).strip())
    )


class _H1Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside_h1 = False
        self._parts: list[str] = []
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "h1":
            self._inside_h1 = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._inside_h1:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "h1" and self._inside_h1:
            self.title = " ".join("".join(self._parts).split())
            self._inside_h1 = False
            self._parts = []


def _markdown_h1_title(path: Path) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _html_h1_title(path: Path) -> str:
    if not path.exists():
        return ""
    parser = _H1Parser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser.title


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

    parent_key = str(item_details.get("key", "")).strip()
    if item_details and not parent_key:
        blockers.append("item-details.json key is required")
    if item_details and not _has_live_notes_refresh(item_details, parent_key=parent_key):
        blockers.append("item-details.json live_notes refresh missing or stale")

    version_suffix = ""
    if item_details:
        version_suffix = next_version_suffix_from_details(
            item_details,
            paper_title=paper_title,
            generated_date=generated_date,
        )
    note_title = f"[Codex Summary] {paper_title} - {generated_date}{version_suffix}"

    if note_md_path.exists():
        md_title = _markdown_h1_title(note_md_path)
        if md_title != note_title:
            blockers.append(f"note.md title mismatch: expected {note_title}, got {md_title}")
    if note_html_path.exists():
        html_title = _html_h1_title(note_html_path)
        if html_title != note_title:
            blockers.append(f"note.html h1 mismatch: expected {note_title}, got {html_title}")

    tags = build_note_labels(summary) if summary else []

    return {
        "status": "blocked" if blockers else "write_ready",
        "blockers": blockers,
        "run_dir": str(run_dir),
        "parentKey": parent_key,
        "paper_title": paper_title,
        "generated_date": generated_date,
        "version_suffix": version_suffix,
        "note_title": note_title,
        "note_md_path": str(note_md_path),
        "note_html_path": str(note_html_path),
        "tags": tags,
        "review_status": summary.get("review_status"),
        "improvement_status": summary.get("improvement_status"),
        "trust_status": summary.get("trust_status"),
    }
