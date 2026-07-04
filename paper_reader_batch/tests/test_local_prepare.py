import json
from pathlib import Path
from types import SimpleNamespace

from paper_reader_batch import local_prepare
from paper_reader_batch.local_prepare import prepare_pdf_bundle_subprocess


def test_prepare_pdf_subprocess_reads_json_output_file_when_stdout_is_noisy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paper_reader_root = tmp_path / "paper_reader"
    paper_reader_root.mkdir()
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    analysis_dir = tmp_path / "paper_analysis"
    manifest_path = analysis_dir / "run.json"
    final_note = tmp_path / "paper_note.md"

    def fake_run(command, cwd, **kwargs):
        assert command[:3] == ["uv", "run", "paper_reader"]
        assert command[3] == "prepare-pdf"
        assert "--json-output" in command
        json_output = Path(command[command.index("--json-output") + 1])
        json_output.parent.mkdir(parents=True, exist_ok=True)
        analysis_dir.mkdir()
        manifest_path.write_text("{}", encoding="utf-8")
        json_output.write_text(
            json.dumps(
                {
                    "analysis_dir": str(analysis_dir),
                    "final_note_path": str(final_note),
                    "manifest_path": str(manifest_path),
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="warning before json\n", stderr="")

    monkeypatch.setattr(local_prepare.subprocess, "run", fake_run)

    result = prepare_pdf_bundle_subprocess(
        paper_reader_root=paper_reader_root,
        pdf_path=str(pdf_path),
        timeout_seconds=30,
    )

    assert result == {
        "schema_version": "paper_reader_batch.local-prepare-result.v1",
        "status": "prepared",
        "analysis_dir": str(analysis_dir),
        "final_note_path": str(final_note),
        "manifest_path": str(manifest_path),
        "failure_reason": "",
    }


def test_prepare_pdf_subprocess_recovers_from_manifest_when_machine_json_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paper_reader_root = tmp_path / "paper_reader"
    paper_reader_root.mkdir()
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    analysis_dir = tmp_path / "paper_analysis"
    final_note = tmp_path / "paper_note.md"
    manifest_path = analysis_dir / "run.json"
    metadata_json = analysis_dir / "metadata.json"
    extract_json = analysis_dir / "extract.json"
    section_context = analysis_dir / "section_context.md"
    secondary_sources = analysis_dir / "secondary_sources.json"
    context_md = analysis_dir / "context.md"

    def fake_run(command, cwd, **kwargs):
        assert "--json-output" in command
        analysis_dir.mkdir()
        metadata_json.write_text("{}", encoding="utf-8")
        extract_json.write_text("{}", encoding="utf-8")
        section_context.write_text("# Sections\n", encoding="utf-8")
        secondary_sources.write_text("{}", encoding="utf-8")
        context_md.write_text("# Context\n", encoding="utf-8")
        manifest_path.write_text(
            json.dumps(
                {
                    "status": "prepared",
                    "source_type": "pdf_path",
                    "final_note_path": str(final_note),
                    "metadata_json": str(metadata_json),
                    "extract_json": str(extract_json),
                    "section_context_md": str(section_context),
                    "secondary_sources_json": str(secondary_sources),
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="non-json progress output\n", stderr="")

    monkeypatch.setattr(local_prepare.subprocess, "run", fake_run)

    result = prepare_pdf_bundle_subprocess(
        paper_reader_root=paper_reader_root,
        pdf_path=str(pdf_path),
        timeout_seconds=30,
    )

    assert result["status"] == "prepared"
    assert result["analysis_dir"] == str(analysis_dir)
    assert result["final_note_path"] == str(final_note)
    assert result["manifest_path"] == str(manifest_path)
    assert result["failure_reason"] == ""
    assert "recovered" in result["warning"]


def test_prepare_pdf_subprocess_does_not_recover_from_initialized_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paper_reader_root = tmp_path / "paper_reader"
    paper_reader_root.mkdir()
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    analysis_dir = tmp_path / "paper_analysis"
    manifest_path = analysis_dir / "run.json"
    final_note = tmp_path / "paper_note.md"

    def fake_run(command, cwd, **kwargs):
        assert "--json-output" in command
        analysis_dir.mkdir()
        manifest_path.write_text(
            json.dumps(
                {
                    "status": "initialized",
                    "source_type": "pdf_path",
                    "final_note_path": str(final_note),
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="non-json progress output\n", stderr="")

    monkeypatch.setattr(local_prepare.subprocess, "run", fake_run)

    result = prepare_pdf_bundle_subprocess(
        paper_reader_root=paper_reader_root,
        pdf_path=str(pdf_path),
        timeout_seconds=30,
    )

    assert result["status"] == "failed"
    assert "prepare-pdf returned invalid JSON" in result["failure_reason"]
