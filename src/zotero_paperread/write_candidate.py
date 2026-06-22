from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from zotero_paperread.gate import build_gate_report
from zotero_paperread.note import build_note_labels, render_note, render_note_html, validate_note
from zotero_paperread.write_payload import build_write_payload
from zotero_paperread.zotero_details import next_version_suffix_from_details
from zotero_paperread.zotero_live import fetch_item_children_notes, refresh_details_with_live_notes


FetchLiveNotes = Callable[..., list[dict[str, Any]]]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _metadata_path(run_dir: Path) -> Path:
    metadata_path = run_dir / "metadata.json"
    return metadata_path if metadata_path.exists() else run_dir / "item-details.json"


def prepare_write_candidate(
    run_dir: Path,
    *,
    paper_title: str,
    generated_date: str,
    base_url: str = "http://127.0.0.1:23119",
    fetch_live_notes: FetchLiveNotes = fetch_item_children_notes,
    refreshed_at: str | None = None,
) -> dict[str, Any]:
    """Prepare local files for a Zotero MCP create write without writing to Zotero."""
    run_dir = Path(run_dir)
    item_details_path = run_dir / "item-details.json"
    summary_path = run_dir / "summary.json"
    review_path = run_dir / "review.json"
    note_md_path = run_dir / "note.md"
    note_html_path = run_dir / "note.html"
    gate_report_path = run_dir / "gate-report.json"
    write_payload_path = run_dir / "write-payload.json"
    if write_payload_path.exists():
        write_payload_path.unlink()

    details = _read_json(item_details_path)
    item_key = str(details.get("key", "")).strip()
    if not item_key:
        raise ValueError("item-details.json missing key")

    live_notes = fetch_live_notes(item_key, base_url=base_url)
    refreshed_details = refresh_details_with_live_notes(
        details,
        live_notes=live_notes,
        base_url=base_url,
        refreshed_at=refreshed_at,
    )
    _write_json(item_details_path, refreshed_details)

    version_suffix = next_version_suffix_from_details(
        refreshed_details,
        paper_title=paper_title,
        generated_date=generated_date,
    )

    summary = _read_json(summary_path)
    note_md = render_note(
        _read_json(_metadata_path(run_dir)),
        summary,
        generated_date=generated_date,
        version_suffix=version_suffix,
    )
    note_errors = validate_note(note_md)
    if note_errors:
        raise ValueError("; ".join(note_errors))
    note_md_path.write_text(note_md, encoding="utf-8")
    note_html = render_note_html(note_md)
    note_html_path.write_text(note_html, encoding="utf-8")
    (run_dir / "preview-note-md.txt").write_text(note_md, encoding="utf-8")
    (run_dir / "preview-note-html.txt").write_text(note_html, encoding="utf-8")
    _write_json(run_dir / "note-tags.json", build_note_labels(summary))

    if not review_path.exists():
        raise ValueError(f"missing review.json: {review_path}")
    gate_report = build_gate_report(run_dir, paper_title=paper_title, generated_date=generated_date)
    _write_json(gate_report_path, gate_report)
    if gate_report["status"] != "write_ready":
        return {
            "status": "blocked",
            "version_suffix": version_suffix,
            "gate_report_path": str(gate_report_path),
            "blockers": gate_report["blockers"],
        }

    write_payload = build_write_payload(gate_report)
    _write_json(write_payload_path, write_payload)
    return {
        "status": "write_ready",
        "version_suffix": version_suffix,
        "note_md_path": str(note_md_path),
        "note_html_path": str(note_html_path),
        "gate_report_path": str(gate_report_path),
        "write_payload_path": str(write_payload_path),
    }
