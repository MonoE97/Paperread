import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "zotero-batch-note-writing"
FIXTURES = ROOT / "tests" / "fixtures"


def run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    script = SKILL / "scripts" / name
    return subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def parse_json_stdout(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def test_validate_manifest_accepts_valid_manifest():
    result = run_script(
        "validate_manifest.py",
        FIXTURES / "batch_manifest_valid.json",
    )
    assert result.returncode == 0, result.stderr
    payload = parse_json_stdout(result)
    assert payload["ok"] is True
    assert payload["errors"] == []


def test_validate_manifest_rejects_unblocked_duplicate_titles():
    result = run_script(
        "validate_manifest.py",
        FIXTURES / "batch_manifest_invalid_duplicate.json",
    )
    assert result.returncode == 1
    payload = parse_json_stdout(result)
    assert payload["ok"] is False
    assert any("duplicate normalized_title" in error for error in payload["errors"])


def test_validate_manifest_rejects_write_ready_without_note_identity(tmp_path):
    manifest = json.loads((FIXTURES / "batch_manifest_valid.json").read_text())
    manifest["items"][0]["note_title"] = "  "
    manifest["items"][0]["note_tags"] = []
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = run_script("validate_manifest.py", manifest_path)

    assert result.returncode == 1
    payload = parse_json_stdout(result)
    assert payload["ok"] is False
    assert any("note_title" in error for error in payload["errors"])
    assert any("note_tags" in error for error in payload["errors"])


def test_validate_manifest_rejects_invalid_top_level_string_fields(tmp_path):
    manifest = json.loads((FIXTURES / "batch_manifest_valid.json").read_text())
    manifest["batch_id"] = "   "
    manifest["target_collection_path"] = ""
    manifest["generated_date"] = None
    manifest["state"] = {"phase": "frozen"}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = run_script("validate_manifest.py", manifest_path)

    assert result.returncode == 1
    payload = parse_json_stdout(result)
    assert payload["ok"] is False
    assert any("batch_id" in error for error in payload["errors"])
    assert any("target_collection_path" in error for error in payload["errors"])
    assert any("generated_date" in error for error in payload["errors"])
    assert any("state" in error for error in payload["errors"])


def test_build_batch_preview_is_compact_and_hides_error_detail(tmp_path):
    output = tmp_path / "write-preview.md"
    result = run_script(
        "build_batch_preview.py",
        FIXTURES / "batch_manifest_for_preview.json",
        "--output",
        output,
    )
    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    assert "# Zotero Batch Write Preview" in text
    assert "READY1" in text
    assert "primary_pdf_missing" in text
    assert "long traceback" not in text
    assert "full diagnostic" not in text


def test_build_batch_preview_includes_note_tags(tmp_path):
    output = tmp_path / "write-preview.md"
    result = run_script(
        "build_batch_preview.py",
        FIXTURES / "batch_manifest_for_preview.json",
        "--output",
        output,
    )
    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    assert "| Item Key | Title | Review Status | Trust Status | Secondary Captured | Note Title | Note Tags |" in text
    assert "codex-summary" in text
    assert "paper-summary" in text


def test_verify_write_report_accepts_html_escaped_note_title():
    result = run_script(
        "verify_write_report.py",
        FIXTURES / "batch_write_report_escaped_title.json",
    )
    assert result.returncode == 0, result.stderr
    payload = parse_json_stdout(result)
    assert payload["ok"] is True
    assert payload["verified"] == ["READY1"]


def make_write_report_without_default_readback_tags(expected_note_tags):
    write = {
        "item_key": "READY1",
        "expected_note_title": "[Codex Summary] Ready paper - 2026-05-11",
        "write_response": {
            "key": "NOTE123",
            "title": "[Codex Summary] Ready paper - 2026-05-11",
        },
        "readback": {
            "child_notes": [
                {
                    "key": "NOTE123",
                    "title": "[Codex Summary] Ready paper - 2026-05-11",
                    "tags": ["codex-summary"],
                }
            ]
        },
    }
    if expected_note_tags != "__absent__":
        write["expected_note_tags"] = expected_note_tags
    return {"batch_id": "fixture-preview-001", "writes": [write]}


def make_verified_write_report_with_readback_parent(
    field_name: str,
    readback_item_key: str,
) -> dict:
    return {
        "batch_id": "fixture-preview-001",
        "writes": [
            {
                "item_key": "READY1",
                "expected_note_title": "[Codex Summary] Ready paper - 2026-05-11",
                "expected_note_tags": ["codex-summary", "paper-summary"],
                "write_response": {
                    "key": "NOTE123",
                    "title": "[Codex Summary] Ready paper - 2026-05-11",
                },
                "readback": {
                    field_name: readback_item_key,
                    "child_notes": [
                        {
                            "key": "NOTE123",
                            "title": "[Codex Summary] Ready paper - 2026-05-11",
                            "tags": ["codex-summary", "paper-summary"],
                        }
                    ],
                },
            }
        ],
    }


def test_verify_write_report_uses_default_tags_when_expected_tags_absent(tmp_path):
    report_path = tmp_path / "write-report.json"
    report = make_write_report_without_default_readback_tags("__absent__")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = run_script("verify_write_report.py", report_path)

    assert result.returncode == 1
    payload = parse_json_stdout(result)
    assert payload["ok"] is False
    assert any("paper-summary" in error for error in payload["errors"])


def test_verify_write_report_rejects_readback_item_key_mismatch(tmp_path):
    report_path = tmp_path / "write-report.json"
    report = make_verified_write_report_with_readback_parent(
        "item_key",
        "WRONG_PARENT",
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = run_script("verify_write_report.py", report_path)

    assert result.returncode == 1
    payload = parse_json_stdout(result)
    assert payload["ok"] is False
    assert any(
        "readback item" in error or "WRONG_PARENT" in error
        for error in payload["errors"]
    )


def test_verify_write_report_rejects_readback_parent_item_key_mismatch(tmp_path):
    report_path = tmp_path / "write-report.json"
    report = make_verified_write_report_with_readback_parent(
        "parent_item_key",
        "WRONG_PARENT",
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = run_script("verify_write_report.py", report_path)

    assert result.returncode == 1
    payload = parse_json_stdout(result)
    assert payload["ok"] is False
    assert any(
        "readback item" in error or "WRONG_PARENT" in error
        for error in payload["errors"]
    )


def test_verify_write_report_accepts_matching_readback_item_key(tmp_path):
    report_path = tmp_path / "write-report.json"
    report = make_verified_write_report_with_readback_parent("item_key", "READY1")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = run_script("verify_write_report.py", report_path)

    assert result.returncode == 0, result.stderr
    payload = parse_json_stdout(result)
    assert payload["ok"] is True
    assert payload["verified"] == ["READY1"]


def test_verify_write_report_uses_default_tags_when_expected_tags_empty(tmp_path):
    report_path = tmp_path / "write-report.json"
    report = make_write_report_without_default_readback_tags([])
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = run_script("verify_write_report.py", report_path)

    assert result.returncode == 1
    payload = parse_json_stdout(result)
    assert payload["ok"] is False
    assert any("paper-summary" in error for error in payload["errors"])
