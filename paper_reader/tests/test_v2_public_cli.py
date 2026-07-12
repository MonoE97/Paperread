from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock, get_ident

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from paper_reader.contracts import PaperReaderCommandResult


EXPECTED_TOP_LEVEL = {"route", "run", "review", "candidate", "local", "zotero", "maintenance"}


def _public_cli_module():
    assert importlib.util.find_spec("paper_reader.public_cli") is not None, "V2 public CLI is missing"
    return importlib.import_module("paper_reader.public_cli")


def _invoke(arguments: list[str]):
    return CliRunner().invoke(_public_cli_module().app, arguments)


def _invoke_console(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    console_script = Path(sys.executable).with_name("paper_reader")
    assert console_script.is_file(), console_script
    return subprocess.run(
        [str(console_script), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _result_payload(result) -> dict:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    return payload


def _write_run(run_dir: Path, schema_version: str | None = "paper_reader.run.v2") -> Path:
    run_dir.mkdir()
    payload = {
        "run_id": "run_123",
        "created_at": "2026-07-10T09:30:00Z",
        "source": {
            "source_type": "local_pdf",
            "requested_path": "/tmp/paper.pdf",
            "resolved_path": "/tmp/paper.pdf",
            "sha256": "a" * 64,
            "size_bytes": 10,
            "device": 1,
            "inode": 2,
        },
        "target": None,
        "status": "initialized",
        "artifacts": [],
        "gate": {
            "status": "not_evaluated",
            "evaluated_at": None,
            "checks": [],
            "blockers": [],
        },
        "live_preflight": None,
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version
    manifest = run_dir / "run.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def test_console_script_points_only_to_the_v2_public_cli() -> None:
    skill_root = Path(__file__).parents[1]
    pyproject = (skill_root / "pyproject.toml").read_text(encoding="utf-8")

    assert 'paper_reader = "paper_reader.public_cli:app"' in pyproject
    assert 'paper_reader = "paper_reader.cli:app"' not in pyproject
    assert not (skill_root / "src/paper_reader/cli.py").exists()
    assert importlib.util.find_spec("paper_reader.cli") is None


def test_public_command_tree_contains_only_grouped_v2_surface() -> None:
    command = get_command(_public_cli_module().app)

    assert set(command.commands) == EXPECTED_TOP_LEVEL
    assert set(command.commands["run"].commands) == {
        "init-local",
        "init-zotero",
        "prepare",
        "status",
        "validate",
    }
    assert set(command.commands["review"].commands) == {"validate", "seal"}
    assert set(command.commands["candidate"].commands) == {"build"}
    assert set(command.commands["local"].commands) == {"publish"}
    assert set(command.commands["zotero"].commands) == {"authorize", "verify", "reconcile"}
    assert set(command.commands["maintenance"].commands) == {"extract-pdf"}


def test_top_level_help_shows_grouped_surface_and_hides_v1_flat_commands() -> None:
    result = _invoke(["--help"])

    assert result.exit_code == 0
    for name in EXPECTED_TOP_LEVEL:
        assert name in result.stdout
    for legacy_name in ("create-run", "prepare-pdf", "render-note", "gate-run", "write-note"):
        assert legacy_name not in result.stdout


@pytest.mark.parametrize("arguments", [["--help"], ["run", "--help"], ["zotero", "--help"]])
def test_help_has_no_shell_completion_installation_surface(arguments: list[str]) -> None:
    result = _invoke(arguments)

    assert result.exit_code == 0
    assert "--install-completion" not in result.stdout
    assert "--show-completion" not in result.stdout


@pytest.mark.parametrize("arguments", [[], ["run"], ["zotero"]])
def test_implicit_no_args_help_remains_human_only(arguments: list[str]) -> None:
    result = _invoke(arguments)

    assert "Usage:" in result.stdout
    assert "paper_reader.command-result.v2" not in result.stdout


def test_version_is_a_human_option_not_a_flat_command() -> None:
    result = _invoke(["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "2.0.0"
    flat = _invoke(["version"])
    assert flat.exit_code != 0


@pytest.mark.parametrize(
    ("arguments", "expected_command"),
    [
        (["route"], "route"),
        (["zotero", "authorize", "candidate.json", "--ttl-seconds", "not-an-integer"], "zotero authorize"),
    ],
)
def test_clirunner_parse_errors_emit_one_structured_result(
    arguments: list[str],
    expected_command: str,
) -> None:
    result = _invoke(arguments)

    assert result.exit_code != 0
    payload = _result_payload(result)
    assert payload["ok"] is False
    assert payload["code"] == "invalid_command_usage"
    assert payload["command"] == expected_command
    assert result.stderr.strip()


@pytest.mark.parametrize(
    ("arguments", "expected_command"),
    [
        (["route"], "route"),
        (["zotero", "authorize", "candidate.json", "--ttl-seconds", "not-an-integer"], "zotero authorize"),
    ],
)
def test_console_entrypoint_parse_errors_emit_one_structured_result(
    arguments: list[str],
    expected_command: str,
) -> None:
    result = _invoke_console(arguments)

    assert result.returncode != 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    assert payload["code"] == "invalid_command_usage"
    assert payload["command"] == expected_command
    assert result.stderr.strip()


@pytest.mark.parametrize(
    ("arguments", "callback_name", "expected_command"),
    [
        (["route", "A paper title fragment"], "route_input", "route"),
        (["run", "status", "/tmp/run"], "load_v2_run", "run status"),
    ],
)
def test_unexpected_operation_failure_emits_one_safe_structured_result(
    arguments: list[str],
    callback_name: str,
    expected_command: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _public_cli_module()

    def explode(_input_value: str):
        raise RuntimeError("sensitive local detail")

    monkeypatch.setattr(module, callback_name, explode)

    result = _invoke(arguments)

    assert result.exit_code != 0
    payload = _result_payload(result)
    assert payload == {
        "schema_version": "paper_reader.command-result.v2",
        "command": expected_command,
        "ok": False,
        "code": "internal_error",
        "created_at": payload["created_at"],
        "message": "unexpected internal error",
        "data": {"error_type": "RuntimeError"},
    }
    assert "internal_error" in result.stderr
    assert "sensitive local detail" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_unexpected_extract_pdf_failure_uses_safe_top_level_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_extract = importlib.import_module("paper_reader.pdf_extract")
    sensitive_path = tmp_path / "sensitive-paper.pdf"

    def explode(*_args, **_kwargs):
        raise RuntimeError("sensitive extraction detail")

    monkeypatch.setattr(pdf_extract, "extract_pdf", explode)

    result = _invoke(["maintenance", "extract-pdf", str(sensitive_path)])

    assert result.exit_code != 0
    payload = _result_payload(result)
    assert payload == {
        "schema_version": "paper_reader.command-result.v2",
        "command": "maintenance extract-pdf",
        "ok": False,
        "code": "internal_error",
        "created_at": payload["created_at"],
        "message": "unexpected internal error",
        "data": {"error_type": "RuntimeError"},
    }
    combined_output = result.stdout + result.stderr
    assert "sensitive extraction detail" not in combined_output
    assert str(sensitive_path) not in combined_output


def test_unexpected_failure_after_result_output_does_not_emit_a_second_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _public_cli_module()

    def emit_then_explode(*_args, **_kwargs) -> None:
        module._write_result(
            command="route",
            ok=False,
            code="known_failure",
            message="known failure",
        )
        raise RuntimeError("sensitive post-envelope detail")

    monkeypatch.setattr(module, "_finish", emit_then_explode)

    result = _invoke(["route", "A paper title fragment"])

    assert result.exit_code != 0
    payload = _result_payload(result)
    assert payload["code"] == "known_failure"
    assert "internal_error" in result.stderr
    assert "sensitive post-envelope detail" not in result.stdout + result.stderr


def test_result_emission_state_is_reset_between_consecutive_invocations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _public_cli_module()
    command = get_command(module.app)
    emitted_codes: list[str] = []

    def record_result(*, command, ok, code, data=None, message=None) -> None:
        emitted_codes.append(code)
        module._RESULT_EMITTED.set(True)

    def route_then_fail(input_value: str):
        if input_value == "emit":
            module._write_result(
                command="route",
                ok=False,
                code="known_failure",
                message="known failure",
            )
        raise RuntimeError("sensitive invocation detail")

    monkeypatch.setattr(module, "_write_result", record_result)
    monkeypatch.setattr(module, "route_input", route_then_fail)
    monkeypatch.setattr(module.typer, "echo", lambda *_args, **_kwargs: None)

    assert command.main(args=["route", "emit"], standalone_mode=False) == 1
    assert module._RESULT_EMITTED.get() is False
    assert command.main(args=["route", "no-emit"], standalone_mode=False) == 1
    assert module._RESULT_EMITTED.get() is False
    assert emitted_codes == ["known_failure", "internal_error"]


def test_result_emission_state_is_isolated_between_concurrent_invocations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _public_cli_module()
    command = get_command(module.app)
    barrier = Barrier(2)
    output_lock = Lock()
    emitted_codes: dict[int, list[str]] = {}

    def record_result(*, command, ok, code, data=None, message=None) -> None:
        with output_lock:
            emitted_codes.setdefault(get_ident(), []).append(code)
        module._RESULT_EMITTED.set(True)

    def route_then_fail(input_value: str):
        if input_value == "emit":
            module._write_result(
                command="route",
                ok=False,
                code="known_failure",
                message="known failure",
            )
        barrier.wait(timeout=5)
        raise RuntimeError("sensitive invocation detail")

    def invoke(input_value: str) -> tuple[str, int, int]:
        thread_id = get_ident()
        outcome = command.main(
            args=["route", input_value],
            standalone_mode=False,
        )
        return input_value, thread_id, outcome

    monkeypatch.setattr(module, "_write_result", record_result)
    monkeypatch.setattr(module, "route_input", route_then_fail)
    monkeypatch.setattr(module.typer, "echo", lambda *_args, **_kwargs: None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        invocations = list(executor.map(invoke, ("emit", "no-emit")))

    by_input = {input_value: (thread_id, outcome) for input_value, thread_id, outcome in invocations}
    assert by_input["emit"][1] == 1
    assert by_input["no-emit"][1] == 1
    assert emitted_codes[by_input["emit"][0]] == ["known_failure"]
    assert emitted_codes[by_input["no-emit"][0]] == ["internal_error"]


def test_route_is_path_first_for_pdf_directory_missing_path_and_title(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    directory = tmp_path / "papers"
    directory.mkdir()

    pdf_result = _invoke(["route", str(pdf)])
    directory_result = _invoke(["route", str(directory)])
    missing_result = _invoke(["route", str(tmp_path / "missing.pdf")])
    title_result = _invoke(["route", "A paper title fragment"])

    assert pdf_result.exit_code == 0
    assert _result_payload(pdf_result)["data"] == {
        "input": str(pdf),
        "resolved_path": str(pdf.resolve()),
        "route": "local_pdf",
    }
    assert directory_result.exit_code == 0
    assert _result_payload(directory_result)["data"]["route"] == "local_pdf_directory"
    assert missing_result.exit_code == 1
    assert _result_payload(missing_result)["code"] == "unsupported_local_path"
    assert title_result.exit_code == 0
    assert _result_payload(title_result)["data"] == {
        "input": "A paper title fragment",
        "query": "A paper title fragment",
        "route": "zotero_title",
    }


def test_route_treats_an_unexpandable_home_path_as_a_missing_local_path() -> None:
    result = _invoke(["route", "~paper_reader_user_that_does_not_exist"])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "unsupported_local_path"


@pytest.mark.parametrize("command", ["status", "validate"])
def test_status_and_validate_load_v2_runs_read_only(command: str, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    manifest = _write_run(run_dir)
    before = manifest.read_bytes()

    result = _invoke(["run", command, str(run_dir)])

    assert result.exit_code == 0
    payload = _result_payload(result)
    assert payload["ok"] is True
    assert payload["data"]["run_id"] == "run_123"
    assert payload["data"]["schema_version"] == "paper_reader.run.v2"
    assert manifest.read_bytes() == before
    assert sorted(path.name for path in run_dir.iterdir()) == ["run.json"]


def test_missing_run_directory_is_resolved_read_only_to_its_run_manifest(tmp_path: Path) -> None:
    missing_run = tmp_path / "missing-run"

    result = _invoke(["run", "validate", str(missing_run)])

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["code"] == "run_manifest_missing"
    assert payload["data"]["manifest_path"] == str(missing_run / "run.json")
    assert not missing_run.exists()


@pytest.mark.parametrize("schema_version", [None, "paper_reader.run.v1", "paper_reader.run.v3"])
@pytest.mark.parametrize("command", ["status", "validate"])
def test_v2_loader_rejects_historical_or_unknown_runs_before_mutation(
    schema_version: str | None,
    command: str,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    manifest = _write_run(run_dir, schema_version)
    before = manifest.read_bytes()

    result = _invoke(["run", command, str(run_dir)])

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["ok"] is False
    assert payload["code"] == "unsupported_run_schema"
    assert manifest.read_bytes() == before
    assert sorted(path.name for path in run_dir.iterdir()) == ["run.json"]


def test_init_local_invalid_pdf_returns_structured_failure_without_mutation(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"source")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    result = _invoke(["run", "init-local", str(source)])

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["code"] == "invalid_local_pdf"
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert after == before


def test_run_prepare_parses_preview_limits_before_missing_run_failure(tmp_path: Path) -> None:
    run_path = tmp_path / "run"

    result = _invoke(
        [
            "run",
            "prepare",
            str(run_path),
            "--preview-pages",
            "3",
            "--figure-limit",
            "4",
        ]
    )

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["code"] == "run_manifest_missing"
    assert payload["data"] == {"manifest_path": str(run_path / "run.json")}
    assert not run_path.exists()


def test_grouped_maintenance_extract_pdf_emits_one_v2_result() -> None:
    fixture = Path(__file__).parent / "fixtures" / "minimal.pdf"

    result = _invoke(["maintenance", "extract-pdf", str(fixture), "--max-pages", "1"])

    assert result.exit_code == 0
    payload = _result_payload(result)
    assert payload["command"] == "maintenance extract-pdf"
    assert payload["data"]["extraction"]["page_count"] == 1
