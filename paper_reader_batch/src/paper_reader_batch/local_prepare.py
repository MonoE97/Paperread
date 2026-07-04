from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def prepare_pdf_bundle_subprocess(
    *,
    paper_reader_root: Path,
    pdf_path: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    result = subprocess.run(
        ["uv", "run", "paper_reader", "prepare-pdf", pdf_path],
        cwd=paper_reader_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        return {
            "schema_version": "paper_reader_batch.local-prepare-result.v1",
            "status": "failed",
            "analysis_dir": "",
            "final_note_path": "",
            "manifest_path": "",
            "failure_reason": result.stderr.strip() or result.stdout.strip() or "prepare-pdf failed",
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {
            "schema_version": "paper_reader_batch.local-prepare-result.v1",
            "status": "failed",
            "analysis_dir": "",
            "final_note_path": "",
            "manifest_path": "",
            "failure_reason": f"prepare-pdf returned invalid JSON: {exc}",
        }
    return {
        "schema_version": "paper_reader_batch.local-prepare-result.v1",
        "status": "prepared",
        "analysis_dir": str(payload.get("analysis_dir", "")).strip(),
        "final_note_path": str(payload.get("final_note_path", "")).strip(),
        "manifest_path": str(payload.get("manifest_path", "")).strip(),
        "failure_reason": "",
    }
