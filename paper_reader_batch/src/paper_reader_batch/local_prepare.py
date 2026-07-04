from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any


LOCAL_PREPARE_SCHEMA_VERSION = "paper_reader_batch.local-prepare-result.v1"


def _prepared_result_from_payload(payload: dict[str, Any], *, warning: str = "") -> dict[str, Any]:
    result = {
        "schema_version": LOCAL_PREPARE_SCHEMA_VERSION,
        "status": "prepared",
        "analysis_dir": str(payload.get("analysis_dir", "")).strip(),
        "final_note_path": str(payload.get("final_note_path", "")).strip(),
        "manifest_path": str(payload.get("manifest_path", "")).strip(),
        "failure_reason": "",
    }
    if warning:
        result["warning"] = warning
    return result


def _failed_result(reason: str) -> dict[str, Any]:
    return {
        "schema_version": LOCAL_PREPARE_SCHEMA_VERSION,
        "status": "failed",
        "analysis_dir": "",
        "final_note_path": "",
        "manifest_path": "",
        "failure_reason": reason,
    }


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _candidate_manifest_paths(pdf_path: Path) -> list[Path]:
    parent = pdf_path.parent
    prefix = f"{pdf_path.stem}_analysis"
    candidates = [path / "run.json" for path in parent.glob(f"{prefix}*") if path.is_dir()]
    return sorted(
        [path for path in candidates if path.exists()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _manifest_file_exists(manifest: dict[str, Any], key: str) -> bool:
    value = str(manifest.get(key, "")).strip()
    return bool(value) and Path(value).expanduser().is_file()


def _manifest_has_prepared_artifacts(manifest_path: Path, manifest: dict[str, Any]) -> bool:
    required_manifest_paths = [
        "metadata_json",
        "extract_json",
        "section_context_md",
        "secondary_sources_json",
    ]
    if not all(_manifest_file_exists(manifest, key) for key in required_manifest_paths):
        return False
    return (manifest_path.parent / "context.md").is_file()


def _recover_from_run_manifest(pdf_path: str) -> dict[str, Any] | None:
    for manifest_path in _candidate_manifest_paths(Path(pdf_path).expanduser()):
        manifest = _read_json_file(manifest_path)
        if not manifest:
            continue
        if str(manifest.get("source_type", "")).strip() != "pdf_path":
            continue
        if str(manifest.get("status", "")).strip() != "prepared":
            continue
        if not _manifest_has_prepared_artifacts(manifest_path, manifest):
            continue
        final_note_path = str(manifest.get("final_note_path", "")).strip()
        if not final_note_path:
            continue
        return _prepared_result_from_payload(
            {
                "analysis_dir": str(manifest_path.parent),
                "final_note_path": final_note_path,
                "manifest_path": str(manifest_path),
            },
            warning="recovered from run.json after prepare-pdf did not emit machine JSON",
        )
    return None


def prepare_pdf_bundle_subprocess(
    *,
    paper_reader_root: Path,
    pdf_path: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="paper-reader-prepare-") as temp_dir:
        json_output = Path(temp_dir) / "prepare-result.json"
        result = subprocess.run(
            ["uv", "run", "paper_reader", "prepare-pdf", pdf_path, "--json-output", str(json_output)],
            cwd=paper_reader_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        payload = _read_json_file(json_output)
    if result.returncode != 0:
        return _failed_result(result.stderr.strip() or result.stdout.strip() or "prepare-pdf failed")
    if payload is not None:
        return _prepared_result_from_payload(payload)
    try:
        stdout_payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        recovered = _recover_from_run_manifest(pdf_path)
        if recovered is not None:
            return recovered
        return _failed_result(f"prepare-pdf returned invalid JSON: {exc}")
    if not isinstance(stdout_payload, dict):
        recovered = _recover_from_run_manifest(pdf_path)
        if recovered is not None:
            return recovered
        return _failed_result("prepare-pdf returned non-object JSON")
    return _prepared_result_from_payload(stdout_payload)
