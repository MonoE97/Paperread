import json
import threading
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from paperread_batch import cli as cli_module
from paperread_batch.cli import app


runner = CliRunner()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fake_paperread_root(tmp_path: Path, *, include_pyproject: bool = True) -> Path:
    root = tmp_path / "paperread"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: paperread\ndescription: Single paper skill.\n---\n# Paperread\n",
        encoding="utf-8",
    )
    if include_pyproject:
        (root / "pyproject.toml").write_text("[project]\nname = \"paperread\"\n", encoding="utf-8")
    return root


def _successful_result(tmp_path: Path, item_id: str = "001", *, write_ready: bool = False) -> Path:
    run_dir = tmp_path / f"paperread-run-{item_id}"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(json.dumps({"tldr": "结论"}), encoding="utf-8")
    (run_dir / "note.md").write_text("| 30 秒结论 | 结论 |\n", encoding="utf-8")
    (run_dir / "note.html").write_text("<h1>note</h1>", encoding="utf-8")
    gate_status = "write_ready" if write_ready else "blocked"
    (run_dir / "gate-report.json").write_text(json.dumps({"status": gate_status}), encoding="utf-8")
    write_payload = ""
    if write_ready:
        write_payload = str(run_dir / "write-payload.json")
        (run_dir / "write-payload.json").write_text(
            json.dumps(
                {
                    "action": "create",
                    "parentKey": "PARENT1",
                    "note_html_path": str(run_dir / "note.html"),
                    "contentSha256": "b" * 64,
                    "tags": ["paperread/summary"],
                }
            ),
            encoding="utf-8",
        )
    result = tmp_path / "items" / f"{item_id}.json"
    result.parent.mkdir(exist_ok=True)
    result.write_text(
        json.dumps(
            {
                "schema_version": "paperread-batch.item-result.v1",
                "item_id": item_id,
                "worker_id": f"worker-{item_id}",
                "attempt_count": 1,
                "status": "succeeded",
                "paperread_run_dir": str(run_dir),
                "summary_json": str(run_dir / "summary.json"),
                "note_md": str(run_dir / "note.md"),
                "note_html": str(run_dir / "note.html"),
                "gate_report": str(run_dir / "gate-report.json"),
                "write_payload": write_payload,
                "local_note_path": "",
                "local_gate_report": "",
                "thirty_second_takeaway": "结论",
                "takeaway_source_type": "rendered_note_30_second_row",
                "takeaway_source_path": str(run_dir / "note.md"),
                "takeaway_source_sha256": "abc",
                "failure_reason": "",
            }
        ),
        encoding="utf-8",
    )
    return result


def test_manifest_from_pdf_folder_command(tmp_path: Path) -> None:
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    output = tmp_path / "manifest.json"

    result = runner.invoke(
        app,
        ["manifest", "from-pdf-folder", str(tmp_path), "--batch-title", "folder batch", "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    manifest = _read_json(output)
    assert manifest["items"][0]["input_type"] == "pdf_path"


def test_batch_run_init_validate_next_record_report_retry(tmp_path: Path, monkeypatch) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\nSecond paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"
    paperread_root = _fake_paperread_root(tmp_path)

    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "cli batch", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    assert _read_json(manifest_path)["write_policy"] == "zotero_write"

    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output
    assert (batch_run / "manifest.json").exists()
    assert (batch_run / "state.json").exists()

    def fake_run(command, cwd, **_kwargs):
        assert command == ["uv", "run", "paperread", "--help"]
        assert Path(cwd) == paperread_root
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    result = runner.invoke(app, ["validate", str(batch_run), "--paperread-root", str(paperread_root)])
    assert result.exit_code == 0, result.output
    assert "batch_run_valid" in result.output

    result = runner.invoke(app, ["next", str(batch_run), "--limit", "1", "--now", "2026-07-02T10:01:00+08:00"])
    assert result.exit_code == 0, result.output
    selected = json.loads(result.output)
    assert selected[0]["item_id"] == "001"
    assert selected[0]["input"] == {"title": "First paper"}
    assert selected[0]["expected_output"] == "zotero_note_candidate"

    result_path = _successful_result(tmp_path)
    result = runner.invoke(
        app,
        [
            "record-result",
            str(batch_run),
            "001",
            "--result",
            str(result_path),
            "--now",
            "2026-07-02T10:02:00+08:00",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (batch_run / "items" / "001.json").exists()
    assert (batch_run / "items" / "001.attempt-1.json").exists()

    result = runner.invoke(app, ["report", str(batch_run), "--reported-at", "2026-07-02T10:03:00+08:00"])
    assert result.exit_code == 0, result.output
    assert (batch_run / "batch-report.json").exists()
    assert (batch_run / "batch-report.md").exists()

    state = _read_json(batch_run / "state.json")
    state["items"][1]["status"] = "failed"
    state["items"][1]["failure_reason"] = "test failure"
    (batch_run / "state.json").write_text(json.dumps(state), encoding="utf-8")

    result = runner.invoke(app, ["retry-failed", str(batch_run)])
    assert result.exit_code == 0, result.output
    state = _read_json(batch_run / "state.json")
    assert state["items"][1]["status"] == "pending"


def test_worker_prompt_for_pdf_item_preserves_local_only_rule(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"
    result = runner.invoke(
        app,
        ["manifest", "from-pdf-paths", str(tmp_path / "paths.txt"), "--batch-title", "pdf batch", "--output", str(manifest_path)],
    )
    assert result.exit_code != 0
    (tmp_path / "paths.txt").write_text(str(pdf), encoding="utf-8")
    result = runner.invoke(
        app,
        ["manifest", "from-pdf-paths", str(tmp_path / "paths.txt"), "--batch-title", "pdf batch", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["next", str(batch_run), "--limit", "1"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["worker-prompt", str(batch_run), "001"])

    assert result.exit_code == 0, result.output
    assert "input_type: pdf_path" in result.output
    assert "local-output only" in result.output
    assert "Do not search Zotero" in result.output


def test_validate_result_does_not_mutate_state_or_archive_result(tmp_path: Path) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"
    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "validate batch", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["next", str(batch_run), "--limit", "1", "--now", "2026-07-03T10:00:00+08:00"])
    assert result.exit_code == 0, result.output
    before = _read_json(batch_run / "state.json")

    result_path = _successful_result(tmp_path, write_ready=True)
    result = runner.invoke(
        app,
        ["validate-result", str(batch_run), "001", "--result", str(result_path), "--now", "2026-07-03T10:01:00+08:00"],
    )

    assert result.exit_code == 0, result.output
    assert _read_json(batch_run / "state.json") == before
    assert not (batch_run / "items" / "001.json").exists()


def test_worker_prompt_rejects_interrupted_assignment(tmp_path: Path) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"
    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "interrupted prompt", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["next", str(batch_run), "--limit", "1", "--now", "2026-07-03T10:00:00+08:00"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["resume", str(batch_run)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["worker-prompt", str(batch_run), "001"])

    assert result.exit_code == 1
    assert "item is not currently running" in result.output


def test_prepare_local_pdfs_records_prepared_pdf_bundle(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"
    paperread_root = _fake_paperread_root(tmp_path)
    analysis_dir = tmp_path / "paper_analysis"
    final_note = tmp_path / "paper_note.md"
    result = runner.invoke(
        app,
        ["manifest", "from-pdf-paths", str(paths), "--batch-title", "pdf batch", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output

    def fake_run(command, cwd, **_kwargs):
        assert command == ["uv", "run", "paperread", "--help"]
        assert Path(cwd) == paperread_root
        return SimpleNamespace(returncode=0, stderr="")

    def fake_prepare_pdf(*, paperread_root, pdf_path, timeout_seconds):
        analysis_dir.mkdir(exist_ok=True)
        manifest_file = analysis_dir / "run.json"
        manifest_file.write_text("{}", encoding="utf-8")
        return {
            "schema_version": "paperread-batch.local-prepare-result.v1",
            "status": "prepared",
            "analysis_dir": str(analysis_dir),
            "final_note_path": str(final_note),
            "manifest_path": str(manifest_file),
            "failure_reason": "",
        }

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    monkeypatch.setattr(cli_module, "prepare_pdf_bundle_subprocess", fake_prepare_pdf)

    result = runner.invoke(
        app,
        ["prepare-local-pdfs", str(batch_run), "--paperread-root", str(paperread_root), "--concurrency", "2"],
    )

    assert result.exit_code == 0, result.output
    assert (batch_run / "items" / "001.prepare.json").exists()
    state = _read_json(batch_run / "state.json")
    assert state["items"][0]["local_prepare_status"] == "prepared"

    result = runner.invoke(app, ["next", str(batch_run), "--limit", "1"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["worker-prompt", str(batch_run), "001"])
    assert result.exit_code == 0, result.output
    assert "Continue from the prepared local PDF bundle" in result.output
    assert str(analysis_dir) in result.output
    assert "Do not run prepare-pdf again" in result.output


def test_prepare_local_pdfs_skips_already_prepared_items(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"
    paperread_root = _fake_paperread_root(tmp_path)
    analysis_dir = tmp_path / "paper_analysis"
    manifest_file = analysis_dir / "run.json"

    result = runner.invoke(
        app,
        ["manifest", "from-pdf-paths", str(paths), "--batch-title", "pdf batch", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output

    def fake_run(command, cwd, **_kwargs):
        return SimpleNamespace(returncode=0, stderr="")

    calls: list[str] = []

    def fake_prepare_pdf(*, paperread_root, pdf_path, timeout_seconds):
        calls.append(pdf_path)
        analysis_dir.mkdir(exist_ok=True)
        manifest_file.write_text("{}", encoding="utf-8")
        return {
            "schema_version": "paperread-batch.local-prepare-result.v1",
            "status": "prepared",
            "analysis_dir": str(analysis_dir),
            "final_note_path": str(tmp_path / "paper_note.md"),
            "manifest_path": str(manifest_file),
            "failure_reason": "",
        }

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    monkeypatch.setattr(cli_module, "prepare_pdf_bundle_subprocess", fake_prepare_pdf)
    result = runner.invoke(
        app,
        ["prepare-local-pdfs", str(batch_run), "--paperread-root", str(paperread_root), "--concurrency", "2"],
    )
    assert result.exit_code == 0, result.output
    assert calls == [str(pdf.resolve())]

    calls.clear()
    result = runner.invoke(
        app,
        ["prepare-local-pdfs", str(batch_run), "--paperread-root", str(paperread_root), "--concurrency", "2"],
    )

    assert result.exit_code == 0, result.output
    assert "local_prepare_skipped: no pending pdf_path items" in result.output
    assert calls == []


def test_manifest_from_zotero_titles_command_accepts_prepare_only_override(tmp_path: Path) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\n", encoding="utf-8")
    output = tmp_path / "manifest.json"

    result = runner.invoke(
        app,
        [
            "manifest",
            "from-zotero-titles",
            str(titles),
            "--batch-title",
            "dry batch",
            "--write-policy",
            "prepare_only",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert _read_json(output)["write_policy"] == "prepare_only"


def test_next_write_and_record_write_commands(tmp_path: Path) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"

    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "write batch", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["next", str(batch_run), "--limit", "1", "--now", "2026-07-02T10:01:00+08:00"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        [
            "record-result",
            str(batch_run),
            "001",
            "--result",
            str(_successful_result(tmp_path, write_ready=True)),
            "--now",
            "2026-07-02T10:02:00+08:00",
        ],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["next-write", str(batch_run), "--limit", "1"])
    assert result.exit_code == 0, result.output
    pending = json.loads(result.output)
    assert pending[0]["item_id"] == "001"
    assert pending[0]["parentKey"] == "PARENT1"
    assert pending[0]["contentSha256"] == "b" * 64

    verify_report = tmp_path / "verify-report.json"
    verify_report.write_text(
        json.dumps(
            {
                "status": "passed",
                "noteKey": "NOTE1",
                "parentKey": "PARENT1",
                "contentSha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    write_result = tmp_path / "write-result.json"
    write_result.write_text(
        json.dumps(
            {
                "schema_version": "paperread-batch.write-result.v1",
                "item_id": "001",
                "status": "written",
                "note_key": "NOTE1",
                "parent_key": "PARENT1",
                "contentSha256": "b" * 64,
                "verify_report": str(verify_report),
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "record-write",
            str(batch_run),
            "001",
            "--result",
            str(write_result),
            "--now",
            "2026-07-02T10:03:00+08:00",
        ],
    )
    assert result.exit_code == 0, result.output
    state = _read_json(batch_run / "state.json")
    assert state["items"][0]["write_status"] == "written"
    assert state["items"][0]["zotero_note_key"] == "NOTE1"


def test_next_write_rejects_parallel_write_limit(tmp_path: Path) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"
    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "write limit", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["next-write", str(batch_run), "--limit", "2"])

    assert result.exit_code == 1
    assert "serial" in result.output


def test_init_without_output_allocates_default_run_dir(tmp_path: Path, monkeypatch) -> None:
    batch_root = tmp_path / "paperread-batch"
    batch_root.mkdir()
    monkeypatch.setattr(cli_module, "_batch_skill_root", lambda: batch_root)
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"

    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "CLI Batch", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--now", "2026-07-02T10:00:00+08:00"])

    expected_run = batch_root / "runs" / "2026-07-02" / "cli-batch"
    assert result.exit_code == 0, result.output
    assert f"batch_run_initialized: {expected_run}" in result.output
    assert (expected_run / "manifest.json").exists()
    assert (expected_run / "state.json").exists()


def test_validate_rejects_invalid_explicit_paperread_root_without_fallback(tmp_path: Path, monkeypatch) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"
    batch_root = tmp_path / "paperread-batch"
    batch_root.mkdir()
    fallback_root = tmp_path / "skill"
    fallback_root.mkdir()
    (fallback_root / "SKILL.md").write_text(
        "---\nname: paperread\ndescription: fallback.\n---\n# Paperread\n",
        encoding="utf-8",
    )
    explicit_root = tmp_path / "not-paperread"
    explicit_root.mkdir()
    monkeypatch.setattr(cli_module, "_batch_skill_root", lambda: batch_root)

    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "explicit root", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["validate", str(batch_run), "--paperread-root", str(explicit_root)])

    assert result.exit_code == 1
    assert "paperread_unavailable" in result.output
    assert str(explicit_root) in result.output


def test_validate_rejects_paperread_root_without_runnable_cli(tmp_path: Path) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"
    paperread_root = _fake_paperread_root(tmp_path, include_pyproject=False)

    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "missing cli", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["validate", str(batch_run), "--paperread-root", str(paperread_root)])

    assert result.exit_code == 1
    assert "paperread_unavailable" in result.output
    assert "pyproject.toml" in result.output


def test_init_rejects_existing_run_without_resetting_state(tmp_path: Path) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"

    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "resume batch", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output

    state = _read_json(batch_run / "state.json")
    state["items"][0]["status"] = "running"
    state["items"][0]["worker_id"] = "worker-001"
    state["items"][0]["attempt_count"] = 1
    (batch_run / "state.json").write_text(json.dumps(state), encoding="utf-8")

    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])

    assert result.exit_code == 1
    assert "batch_run_exists" in result.output
    assert _read_json(batch_run / "state.json")["items"][0]["status"] == "running"


def test_resume_records_existing_item_results_before_interrupting_remaining_running_items(tmp_path: Path) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\nSecond paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"

    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "resume batch", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["next", str(batch_run), "--limit", "2", "--now", "2026-07-02T10:01:00+08:00"])
    assert result.exit_code == 0, result.output

    source_result = _successful_result(tmp_path, "001")
    archived_result = batch_run / "items" / "001.json"
    archived_result.parent.mkdir(exist_ok=True)
    archived_result.write_text(source_result.read_text(encoding="utf-8"), encoding="utf-8")

    result = runner.invoke(app, ["resume", str(batch_run)])

    assert result.exit_code == 0, result.output
    state = _read_json(batch_run / "state.json")
    assert state["items"][0]["status"] == "succeeded"
    assert state["items"][0]["thirty_second_takeaway"] == "结论"
    assert state["items"][1]["status"] == "interrupted"


def test_resume_ignores_stale_archived_result_and_marks_item_interrupted(tmp_path: Path) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"
    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "resume batch", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["next", str(batch_run), "--limit", "1", "--now", "2026-07-03T10:00:00+08:00"])
    assert result.exit_code == 0, result.output
    state = _read_json(batch_run / "state.json")
    state["items"][0]["attempt_count"] = 2
    (batch_run / "state.json").write_text(json.dumps(state), encoding="utf-8")
    items_dir = batch_run / "items"
    items_dir.mkdir(exist_ok=True)
    stale = {
        "schema_version": "paperread-batch.item-result.v1",
        "item_id": "001",
        "worker_id": "worker-001",
        "attempt_count": 1,
        "status": "failed",
        "failure_reason": "late attempt",
    }
    (items_dir / "001.json").write_text(json.dumps(stale), encoding="utf-8")

    result = runner.invoke(app, ["resume", str(batch_run)])

    assert result.exit_code == 0, result.output
    state = _read_json(batch_run / "state.json")
    assert state["items"][0]["status"] == "interrupted"
    assert state["items"][0]["resume_decision"].startswith("archived_result_ignored")


def test_zotero_collection_command_rejects_mismatched_inventory(tmp_path: Path) -> None:
    inventory = tmp_path / "collection-items.json"
    inventory.write_text(
        json.dumps(
            {
                "collection": {"key": "COLL1", "name": "My Collection"},
                "items": [{"item_key": "KEY1", "title": "First item"}],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "manifest",
            "from-zotero-collection",
            "OTHER",
            "--items-json",
            str(inventory),
            "--batch-title",
            "collection batch",
            "--output",
            str(tmp_path / "manifest.json"),
        ],
    )

    assert result.exit_code == 1
    assert "collection_mismatch" in result.output


def test_concurrent_record_result_preserves_both_state_updates(tmp_path: Path, monkeypatch) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\nSecond paper\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    batch_run = tmp_path / "batch-run"

    result = runner.invoke(
        app,
        ["manifest", "from-zotero-titles", str(titles), "--batch-title", "race batch", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["init", "--manifest", str(manifest_path), "--output", str(batch_run)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["next", str(batch_run), "--limit", "2", "--now", "2026-07-02T10:01:00+08:00"])
    assert result.exit_code == 0, result.output

    original_read_object = cli_module._read_object
    read_barrier = threading.Barrier(2)

    def delayed_state_read(path: Path, label: str) -> dict:
        payload = original_read_object(path, label)
        if Path(path).name == "state.json":
            try:
                read_barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
        return payload

    monkeypatch.setattr(cli_module, "_read_object", delayed_state_read)
    errors: list[BaseException] = []

    def record(item_id: str) -> None:
        try:
            cli_module.record_result_command(
                batch_run,
                item_id,
                result=_successful_result(tmp_path, item_id),
                now=f"2026-07-02T10:0{item_id[-1]}:00+08:00",
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(item_id,)) for item_id in ["001", "002"]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    state = _read_json(batch_run / "state.json")
    assert [item["status"] for item in state["items"]] == ["succeeded", "succeeded"]
