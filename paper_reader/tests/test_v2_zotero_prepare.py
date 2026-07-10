from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paper_reader.contracts import PaperReaderCommandResult
from paper_reader.public_cli import app

from test_v2_zotero_init import FIXTURE_PDF, _bundle, _initialize


def _invoke(arguments: list[str]):
    return CliRunner().invoke(app, arguments)


def _result_payload(result) -> dict[str, object]:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    return payload


def _zotero_run(tmp_path: Path, *, extra: str = "") -> tuple[Path, Path]:
    pdf_path = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, pdf_path)
    payload = _bundle(pdf_path)
    payload["selected_item"]["extra"] = extra
    bundle_path = tmp_path / "discovery.json"
    bundle_path.write_text(json.dumps(payload), encoding="utf-8")
    skill_root = tmp_path / "installed-skill"
    skill_root.mkdir()
    return _initialize(bundle_path, "PARENT1", skill_root).run_dir, pdf_path


def test_prepare_zotero_reuses_evidence_pipeline_with_normalized_metadata(
    tmp_path: Path,
) -> None:
    run_dir, pdf_path = _zotero_run(
        tmp_path,
        extra="Background https://example.test/context and https://example.test/context",
    )

    result = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["code"] == "prepared"
    evidence_dir = Path(payload["data"]["evidence_dir"])
    metadata = json.loads((evidence_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata == {
        "key": "PARENT1",
        "title": "A Useful Paper & Result",
        "creators": "Ada Lovelace",
        "date": "2026",
        "DOI": "10.1000/example.doi",
        "url": "https://example.test/paper",
        "zoteroUrl": "zotero://select/library/items/PARENT1",
        "abstractNote": "Abstract",
        "pdf_path": str(pdf_path.resolve()),
        "pdf_attachment_key": "ATTACH1",
        "pdf_filename": "paper.pdf",
    }
    secondary = json.loads(
        (evidence_dir / "secondary_sources.json").read_text(encoding="utf-8")
    )
    assert secondary["item_key"] == "PARENT1"
    assert secondary["sources"] == [
        {
            "source_id": "secondary-001",
            "url": "https://example.test/context",
            "source_field": "extra",
            "source_provenance": "mcp_payload",
            "capture_status": "pending_capture",
        }
    ]
    evidence = json.loads((evidence_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["source_sha256"] == hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "prepared"
    assert run["source"]["source_type"] == "zotero"
    assert any(item["role"] == "evidence_manifest" for item in run["artifacts"])


def test_prepare_zotero_enables_only_guarded_figure_source_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, pdf_path = _zotero_run(tmp_path)
    observed: dict[str, object] = {}

    def fake_extract_figures(source_path: Path, output_dir: Path, **kwargs) -> dict[str, object]:
        observed["source_path"] = source_path
        observed.update(kwargs)
        output_dir.mkdir(parents=True)
        return {
            "arxiv_id": None,
            "pdf_path": str(source_path),
            "candidate_count": 0,
            "selected_figures": [],
            "source_attempts": [{"stage": "resolve", "status": "skipped", "reason": "test"}],
            "warnings": [],
        }

    monkeypatch.setattr("paper_reader.evidence_figures.extract_figures", fake_extract_figures)

    result = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "1"])

    assert result.exit_code == 0, result.stderr
    assert observed["source_path"] == pdf_path.resolve()
    assert observed["allow_network_source"] is True
    assert observed["max_candidates"] == 200


@pytest.mark.parametrize("tamper", ["pdf", "normalized_source"])
def test_prepare_zotero_revalidates_nested_pdf_and_normalized_source_before_mutation(
    tamper: str,
    tmp_path: Path,
) -> None:
    run_dir, pdf_path = _zotero_run(tmp_path)
    run_before = (run_dir / "run.json").read_bytes()
    run = json.loads(run_before)
    if tamper == "pdf":
        pdf_path.write_bytes(pdf_path.read_bytes() + b"\n% drift")
        expected_code = "source_changed"
    else:
        normalized_ref = run["source"]["normalized_source"]
        (run_dir / normalized_ref["path"]).write_bytes(b"{}")
        expected_code = "source_snapshot_tampered"

    result = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == expected_code
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "evidence").exists()
