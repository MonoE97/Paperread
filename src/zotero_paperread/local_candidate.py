from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from zotero_paperread.local_gate import build_local_gate_report
from zotero_paperread.note import build_note_labels, render_note, render_note_html, validate_note


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_local_note_candidate(analysis_dir: Path, *, generated_date: str) -> dict[str, Any]:
    """Render and gate a local PDF note without preparing any Zotero payload."""
    analysis_dir = Path(analysis_dir)
    metadata_path = analysis_dir / "metadata.json"
    summary_path = analysis_dir / "summary.json"
    run_manifest_path = analysis_dir / "run.json"
    note_md_path = analysis_dir / "note.md"
    note_html_path = analysis_dir / "note.html"
    gate_report_path = analysis_dir / "local-gate-report.json"

    metadata = _read_json(metadata_path)
    summary = _read_json(summary_path)
    run_manifest = _read_json(run_manifest_path) if run_manifest_path.exists() else {}

    note_md = render_note(metadata, summary, generated_date=generated_date)
    note_errors = validate_note(note_md)
    if note_errors:
        raise ValueError("; ".join(note_errors))

    note_md_path.write_text(note_md, encoding="utf-8")
    note_html = render_note_html(note_md)
    note_html_path.write_text(note_html, encoding="utf-8")
    (analysis_dir / "preview-note-md.txt").write_text(note_md, encoding="utf-8")
    (analysis_dir / "preview-note-html.txt").write_text(note_html, encoding="utf-8")
    _write_json(analysis_dir / "note-tags.json", build_note_labels(summary))

    gate_report = build_local_gate_report(analysis_dir, generated_date=generated_date)
    _write_json(gate_report_path, gate_report)
    if gate_report["status"] != "local_ready":
        return {
            "status": "blocked",
            "gate_report_path": str(gate_report_path),
            "blockers": gate_report["blockers"],
        }

    final_note_path = str(run_manifest.get("final_note_path", "")).strip()
    if final_note_path:
        target = Path(final_note_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(note_md_path, target)

    return {
        "status": "local_ready",
        "note_md_path": str(note_md_path),
        "note_html_path": str(note_html_path),
        "gate_report_path": str(gate_report_path),
        "final_note_path": final_note_path,
    }
