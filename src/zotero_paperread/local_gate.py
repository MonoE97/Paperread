from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from zotero_paperread.note import build_note_labels, validate_trusted_summary
from zotero_paperread.summary_lint import lint_summary


def _read_json_if_present(path: Path, blockers: list[str]) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        blockers.append(f"invalid {path.name}: {exc}")
        return {}
    if not isinstance(payload, dict):
        blockers.append(f"invalid {path.name}: expected top-level JSON object")
        return {}
    return payload


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


def build_local_gate_report(analysis_dir: Path, *, generated_date: str) -> dict[str, Any]:
    """Build a local PDF note readiness report without Zotero write fields."""
    analysis_dir = Path(analysis_dir)
    blockers: list[str] = []

    metadata_path = analysis_dir / "metadata.json"
    summary_path = analysis_dir / "summary.json"
    review_path = analysis_dir / "review.json"
    note_md_path = analysis_dir / "note.md"
    note_html_path = analysis_dir / "note.html"
    run_manifest_path = analysis_dir / "run.json"
    write_payload_path = analysis_dir / "write-payload.json"

    metadata = _read_json_if_present(metadata_path, blockers)
    summary = _read_json_if_present(summary_path, blockers)
    review = _read_json_if_present(review_path, blockers)
    run_manifest = _read_json_if_present(run_manifest_path, blockers)

    if not metadata_path.exists():
        blockers.append("missing metadata.json")
    if not summary_path.exists():
        blockers.append("missing summary.json")
    if not review_path.exists():
        blockers.append("missing review.json")
    if not run_manifest_path.exists():
        blockers.append("missing run.json")
    if not note_md_path.exists():
        blockers.append("missing note.md")
    if not note_html_path.exists():
        blockers.append("missing note.html")
    if write_payload_path.exists():
        blockers.append("unexpected write-payload.json in local PDF analysis")
    if metadata and str(metadata.get("source_type", "")).strip() != "pdf_path":
        blockers.append("metadata.json source_type must be pdf_path")

    trusted_errors = validate_trusted_summary(summary) if summary else ["summary.json unavailable"]
    blockers.extend(f"trusted summary: {error}" for error in trusted_errors)

    lint_issues = lint_summary(summary) if summary else []
    blockers.extend(f"summary lint: {issue['code']}" for issue in lint_issues)

    if review.get("needs_improvement") is not False:
        blockers.append("review.json needs_improvement is not false")

    paper_title = str(metadata.get("title", "")).strip()
    note_title = f"[Codex Summary] {paper_title} - {generated_date}" if paper_title else ""
    if metadata and not paper_title:
        blockers.append("metadata.json title is required")

    if note_title and note_md_path.exists():
        md_title = _markdown_h1_title(note_md_path)
        if md_title != note_title:
            blockers.append(f"note.md title mismatch: expected {note_title}, got {md_title}")
    if note_title and note_html_path.exists():
        html_title = _html_h1_title(note_html_path)
        if html_title != note_title:
            blockers.append(f"note.html h1 mismatch: expected {note_title}, got {html_title}")

    final_note_path = str(run_manifest.get("final_note_path", "")).strip()
    if run_manifest and not final_note_path:
        blockers.append("run.json final_note_path is required")
    return {
        "status": "blocked" if blockers else "local_ready",
        "blockers": blockers,
        "analysis_dir": str(analysis_dir),
        "final_note_path": final_note_path,
        "paper_title": paper_title,
        "generated_date": generated_date,
        "note_title": note_title,
        "note_md_path": str(note_md_path),
        "note_html_path": str(note_html_path),
        "tags": build_note_labels(summary) if summary else [],
        "review_status": summary.get("review_status"),
        "improvement_status": summary.get("improvement_status"),
        "trust_status": summary.get("trust_status"),
    }
