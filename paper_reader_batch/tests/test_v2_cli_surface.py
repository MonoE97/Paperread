import json
from pathlib import Path
import subprocess

from typer.testing import CliRunner

from paper_reader_batch.v2_cli import app
from paper_reader_batch.v2_contracts import COMMAND_RESULT_SCHEMA_VERSION
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import canonical_json_bytes
from paper_reader_batch.v2_receipts import RequestOutcome


runner = CliRunner()
BATCH_ROOT = Path(__file__).resolve().parents[1]
OLD_FLAT_COMMANDS = [
    "init",
    "validate",
    "next",
    "worker-prompt",
    "record-result",
    "validate-result",
    "prepare-local-pdfs",
    "next-write",
    "record-write",
    "report",
    "retry-failed",
    "resume",
]
PUBLIC_COMMANDS = {
    "manifest": ["from-pdf-folder", "from-pdf-paths", "from-zotero-titles", "from-zotero-collection", "validate"],
    "run": ["init", "validate", "status", "recover", "report"],
    "worker": ["claim", "prompt", "renew", "finish", "release", "retry"],
    "local-prepare": ["claim", "renew", "finish", "release", "run"],
    "write": ["claim", "preview", "renew", "release", "begin", "commit", "mark-uncertain", "reconcile", "retry"],
}
MUTATING_COMMANDS = {
    ("manifest", "from-pdf-folder"),
    ("manifest", "from-pdf-paths"),
    ("manifest", "from-zotero-titles"),
    ("manifest", "from-zotero-collection"),
    ("run", "init"),
    ("run", "recover"),
    ("worker", "claim"),
    ("worker", "renew"),
    ("worker", "finish"),
    ("worker", "release"),
    ("worker", "retry"),
    ("local-prepare", "claim"),
    ("local-prepare", "renew"),
    ("local-prepare", "finish"),
    ("local-prepare", "release"),
    ("local-prepare", "run"),
    ("write", "claim"),
    ("write", "renew"),
    ("write", "release"),
    ("write", "begin"),
    ("write", "commit"),
    ("write", "mark-uncertain"),
    ("write", "reconcile"),
    ("write", "retry"),
}


def _tree(path: Path) -> list[str]:
    return sorted(str(candidate.relative_to(path)) for candidate in path.rglob("*"))


def _invoke_json(arguments: list[str], *, expected_command: str, ok: bool) -> dict:
    result = runner.invoke(app, arguments)
    assert len(result.stdout.splitlines()) == 1, (arguments, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == COMMAND_RESULT_SCHEMA_VERSION
    assert payload["command"] == expected_command
    assert payload["ok"] is ok
    assert result.exit_code == (0 if ok else 2), (arguments, result.stdout, result.stderr)
    return payload


def test_project_entrypoint_uses_only_v2_grouped_cli() -> None:
    pyproject = (BATCH_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'paper_reader_batch = "paper_reader_batch.v2_cli:app"' in pyproject
    assert not (BATCH_ROOT / "src/paper_reader_batch/cli.py").exists()

    help_result = runner.invoke(app, ["--help"], terminal_width=38)
    assert help_result.exit_code == 0, help_result.output
    for group in ["manifest", "run", "worker", "local-prepare", "write"]:
        assert group in help_result.output
    for forbidden in OLD_FLAT_COMMANDS:
        assert forbidden not in help_result.output

    for group, commands in PUBLIC_COMMANDS.items():
        result = runner.invoke(app, [group, "--help"], terminal_width=38)
        assert result.exit_code == 0, result.output
        for command in commands:
            assert command in result.output


def test_version_is_the_only_non_json_operational_exception_besides_help() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("paper_reader_batch ")
    assert len(result.stdout.splitlines()) == 1


def test_run_recover_exposes_explicit_read_only_reconciliation_delegation() -> None:
    result = runner.invoke(app, ["run", "recover", "--help"], terminal_width=200)

    assert result.exit_code == 0, result.output
    assert "--paper-reader-root" in result.output
    assert "--reconciliation-timeout-seconds" in result.output


def test_every_public_command_has_human_help_and_strict_required_arguments() -> None:
    for group, commands in PUBLIC_COMMANDS.items():
        for command in commands:
            help_result = runner.invoke(app, [group, command, "--help"], terminal_width=200)
            assert help_result.exit_code == 0, (group, command, help_result.output)
            assert help_result.stdout
            assert "paper_reader_batch.command-result.v2" not in help_result.stdout
            assert "--now" not in help_result.stdout
            if (group, command) in MUTATING_COMMANDS:
                assert "--request-id" in help_result.stdout, (group, command, help_result.stdout)
            else:
                assert "--request-id" not in help_result.stdout, (group, command, help_result.stdout)

            missing_result = runner.invoke(app, [group, command])
            assert missing_result.exit_code != 0, (group, command, missing_result.output)
            assert len(missing_result.stdout.splitlines()) == 1, (
                group,
                command,
                missing_result.stdout,
                missing_result.stderr,
            )
            payload = json.loads(missing_result.stdout)
            assert payload["command"] == f"{group}.{command}"
            assert payload["error"]["code"] == "invalid_cli_usage"


def test_old_flat_commands_are_unreachable_and_do_not_mutate(tmp_path: Path) -> None:
    before = _tree(tmp_path)

    for command in OLD_FLAT_COMMANDS:
        result = runner.invoke(app, [command, str(tmp_path), "--output", str(tmp_path / "out")])
        assert result.exit_code != 0, (command, result.output)
        lines = result.stdout.splitlines()
        assert len(lines) == 1, (command, result.stdout, result.stderr)
        payload = json.loads(lines[0])
        assert payload["schema_version"] == COMMAND_RESULT_SCHEMA_VERSION
        assert payload["command"] == command
        assert payload["request_id"] is None
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_cli_usage"
        assert _tree(tmp_path) == before


def test_v2_does_not_expose_flat_report_local_retry_or_hidden_now(tmp_path: Path) -> None:
    cases = [
        ["report", str(tmp_path)],
        ["local-prepare", "retry", str(tmp_path)],
        ["worker", "claim", str(tmp_path), "--now", "2026-07-10T00:00:00Z"],
        ["write", "claim", str(tmp_path), "--writer-id", "writer", "--request-id", "11111111-1111-4111-8111-111111111111", "--now", "2026-07-10T00:00:00Z"],
    ]
    before = _tree(tmp_path)
    for arguments in cases:
        result = runner.invoke(app, arguments)
        assert result.exit_code != 0, (arguments, result.output)
        assert len(result.stdout.splitlines()) == 1, (arguments, result.stdout, result.stderr)
        assert json.loads(result.stdout)["error"]["code"] == "invalid_cli_usage"
        assert _tree(tmp_path) == before


def test_grouped_cli_is_wired_to_manifest_run_worker_and_local_lease_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    monkeypatch.setattr("paper_reader_batch.v2_cli._batch_root", lambda: skill_root)
    pdf = tmp_path / "论文🙂.pdf"
    pdf.write_bytes(b"%PDF-1.7\ncli wiring\n")
    paths_file = tmp_path / "paths.txt"
    paths_file.write_text(str(pdf), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    run_dir = tmp_path / "run"

    manifest_result = _invoke_json(
        [
            "manifest",
            "from-pdf-paths",
            str(paths_file),
            "--batch-title",
            "批处理🙂",
            "--output",
            str(manifest),
            "--request-id",
            "11111111-1111-4111-8111-111111111111",
        ],
        expected_command="manifest.from-pdf-paths",
        ok=True,
    )
    assert Path(manifest_result["result"]["manifest_path"]) == manifest.resolve()
    assert _invoke_json(
        ["manifest", "validate", str(manifest)],
        expected_command="manifest.validate",
        ok=True,
    )["result"]["item_count"] == 1

    _invoke_json(
        [
            "run",
            "init",
            "--manifest",
            str(manifest),
            "--output",
            str(run_dir),
            "--request-id",
            "22222222-2222-4222-8222-222222222222",
        ],
        expected_command="run.init",
        ok=True,
    )
    replay = _invoke_json(
        [
            "run",
            "init",
            "--manifest",
            str(manifest),
            "--output",
            str(run_dir),
            "--request-id",
            "22222222-2222-4222-8222-222222222222",
        ],
        expected_command="run.init",
        ok=True,
    )
    assert replay["replayed"] is True
    assert _invoke_json(
        ["run", "validate", str(run_dir)],
        expected_command="run.validate",
        ok=True,
    )["result"]["valid"] is True
    assert _invoke_json(
        ["run", "status", str(run_dir)],
        expected_command="run.status",
        ok=True,
    )["result"]["state"]["batch_status"] == "ready"
    recovery = _invoke_json(
        [
            "run",
            "recover",
            str(run_dir),
            "--request-id",
            "33333333-3333-4333-8333-333333333333",
        ],
        expected_command="run.recover",
        ok=False,
    )
    assert recovery["error"]["code"] == "nothing_to_recover"

    worker_claim = _invoke_json(
        [
            "worker",
            "claim",
            str(run_dir),
            "--worker-id",
            "工作者🙂",
            "--request-id",
            "44444444-4444-4444-8444-444444444444",
        ],
        expected_command="worker.claim",
        ok=True,
    )["result"]["assignments"][0]
    worker_identity = [
        "--worker-id",
        worker_claim["worker_id"],
        "--claim-id",
        worker_claim["claim_id"],
        "--lease-token",
        worker_claim["lease_token"],
        "--attempt-id",
        worker_claim["attempt_id"],
    ]
    prompt = _invoke_json(
        ["worker", "prompt", str(run_dir), worker_claim["item_id"], *worker_identity],
        expected_command="worker.prompt",
        ok=True,
    )
    assert prompt["result"]["attempt_id"] == worker_claim["attempt_id"]
    finish_failure = _invoke_json(
        [
            "worker",
            "finish",
            str(run_dir),
            worker_claim["item_id"],
            *worker_identity,
            "--result",
            str(tmp_path / "missing-worker-result.json"),
            "--request-id",
            "55555555-5555-4555-8555-555555555555",
        ],
        expected_command="worker.finish",
        ok=False,
    )
    assert finish_failure["error"]["code"] != "not_implemented"
    _invoke_json(
        [
            "worker",
            "renew",
            str(run_dir),
            worker_claim["item_id"],
            *worker_identity,
            "--request-id",
            "66666666-6666-4666-8666-666666666666",
        ],
        expected_command="worker.renew",
        ok=True,
    )
    _invoke_json(
        [
            "worker",
            "release",
            str(run_dir),
            worker_claim["item_id"],
            *worker_identity,
            "--acknowledge-no-side-effects",
            "--request-id",
            "77777777-7777-4777-8777-777777777777",
        ],
        expected_command="worker.release",
        ok=True,
    )
    retry_failure = _invoke_json(
        [
            "worker",
            "retry",
            str(run_dir),
            worker_claim["item_id"],
            "--request-id",
            "88888888-8888-4888-8888-888888888888",
        ],
        expected_command="worker.retry",
        ok=False,
    )
    assert retry_failure["error"]["code"] == "retry_not_allowed"

    local_claim = _invoke_json(
        [
            "local-prepare",
            "claim",
            str(run_dir),
            "--worker-id",
            "本地工作者🙂",
            "--request-id",
            "99999999-9999-4999-8999-999999999999",
        ],
        expected_command="local-prepare.claim",
        ok=True,
    )["result"]["assignments"][0]
    local_identity = [
        "--worker-id",
        local_claim["worker_id"],
        "--claim-id",
        local_claim["claim_id"],
        "--lease-token",
        local_claim["lease_token"],
        "--attempt-id",
        local_claim["attempt_id"],
    ]
    local_finish_failure = _invoke_json(
        [
            "local-prepare",
            "finish",
            str(run_dir),
            local_claim["item_id"],
            *local_identity,
            "--result",
            str(tmp_path / "missing-local-result.json"),
            "--request-id",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        ],
        expected_command="local-prepare.finish",
        ok=False,
    )
    assert local_finish_failure["error"]["code"] != "not_implemented"
    _invoke_json(
        [
            "local-prepare",
            "renew",
            str(run_dir),
            local_claim["item_id"],
            *local_identity,
            "--request-id",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        ],
        expected_command="local-prepare.renew",
        ok=True,
    )
    _invoke_json(
        [
            "local-prepare",
            "release",
            str(run_dir),
            local_claim["item_id"],
            *local_identity,
            "--acknowledge-no-side-effects",
            "--request-id",
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        ],
        expected_command="local-prepare.release",
        ok=True,
    )


def test_local_prepare_run_cli_binds_exact_identity_root_and_timeout(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def fake_run_local_prepare(run_dir: Path, item_id: str, **kwargs) -> RequestOutcome:
        captured.update({"run_dir": run_dir, "item_id": item_id, **kwargs})
        return RequestOutcome(result={"item_id": item_id, "status": "prepared"}, replayed=False)

    monkeypatch.setattr("paper_reader_batch.v2_cli.run_local_prepare", fake_run_local_prepare)
    run_dir = tmp_path / "batch-run"
    paper_reader_root = tmp_path / "paper-reader"
    payload = _invoke_json(
        [
            "local-prepare",
            "run",
            str(run_dir),
            "001",
            "--worker-id",
            "preparer🙂",
            "--claim-id",
            "11111111-1111-4111-8111-111111111111",
            "--lease-token",
            "opaque-lease-token",
            "--attempt-id",
            "22222222-2222-4222-8222-222222222222",
            "--paper-reader-root",
            str(paper_reader_root),
            "--request-id",
            "33333333-3333-4333-8333-333333333333",
            "--timeout-seconds",
            "321",
        ],
        expected_command="local-prepare.run",
        ok=True,
    )
    assert payload["request_id"] == "33333333-3333-4333-8333-333333333333"
    assert captured == {
        "run_dir": run_dir,
        "item_id": "001",
        "worker_id": "preparer🙂",
        "claim_id": "11111111-1111-4111-8111-111111111111",
        "lease_token": "opaque-lease-token",
        "attempt_id": "22222222-2222-4222-8222-222222222222",
        "paper_reader_root": paper_reader_root,
        "request_id": "33333333-3333-4333-8333-333333333333",
        "timeout_seconds": 321,
    }


def test_all_cli_parse_failures_use_one_command_result_envelope() -> None:
    cases = [
        ["manifest", "from-pdf-paths"],
        [
            "manifest",
            "from-pdf-paths",
            "/tmp/paths.txt",
            "--batch-title",
            "x",
            "--output",
            "/tmp/out.json",
            "--request-id",
            "11111111-1111-4111-8111-111111111111",
            "--default-concurrency",
            "not-an-int",
        ],
        ["run", "status", "/tmp/run", "--unknown-option"],
        ["unknown-command"],
    ]

    for args in cases:
        result = runner.invoke(app, args)
        assert result.exit_code != 0, args
        assert len(result.stdout.splitlines()) == 1, (args, result.stdout, result.stderr)
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == COMMAND_RESULT_SCHEMA_VERSION
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_cli_usage"


def test_invalid_request_id_is_rejected_before_callback_runtime(tmp_path: Path, monkeypatch) -> None:
    paths = tmp_path / "paths.txt"
    paths.write_text("/tmp/paper.pdf\n", encoding="utf-8")

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("runtime executed before request-id validation")

    monkeypatch.setattr("paper_reader_batch.v2_cli.create_pdf_paths_manifest", must_not_run)
    result = runner.invoke(
        app,
        [
            "manifest",
            "from-pdf-paths",
            str(paths),
            "--batch-title",
            "x",
            "--output",
            str(tmp_path / "manifest.json"),
            "--request-id",
            "not-a-uuid",
        ],
    )
    assert result.exit_code != 0
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["command"] == "manifest.from-pdf-paths"
    assert payload["request_id"] is None
    assert payload["error"]["code"] == "invalid_request_id"


def test_real_console_parse_failures_are_stable_json_without_traceback() -> None:
    cases = [
        (["manifest", "from-pdf-paths"], "manifest.from-pdf-paths"),
        (["run", "init", "--manifest", "/tmp/missing.json"], "run.init"),
        (["worker", "claim", "/tmp/run", "--unknown-option"], "worker.claim"),
    ]
    for arguments, expected_command in cases:
        result = subprocess.run(
            ["uv", "run", "paper_reader_batch", *arguments],
            cwd=BATCH_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode != 0
        assert len(result.stdout.splitlines()) == 1, (arguments, result.stdout, result.stderr)
        payload = json.loads(result.stdout)
        assert payload["command"] == expected_command
        assert payload["error"]["code"] == "invalid_cli_usage"
        assert "Traceback" not in result.stderr
        assert str(BATCH_ROOT) not in result.stderr


def test_every_public_command_real_console_missing_arguments_is_one_json() -> None:
    for group, commands in PUBLIC_COMMANDS.items():
        for command in commands:
            result = subprocess.run(
                ["uv", "run", "paper_reader_batch", group, command],
                cwd=BATCH_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert result.returncode != 0, (group, command, result.stdout, result.stderr)
            assert len(result.stdout.splitlines()) == 1, (group, command, result.stdout, result.stderr)
            payload = json.loads(result.stdout)
            assert payload["command"] == f"{group}.{command}"
            assert payload["error"]["code"] == "invalid_cli_usage"
            assert "Traceback" not in result.stderr
            assert str(BATCH_ROOT) not in result.stderr


def test_unexpected_callback_error_still_has_one_safe_envelope(tmp_path: Path, monkeypatch) -> None:
    paths = tmp_path / "paths.txt"
    paths.write_text("/tmp/paper.pdf\n", encoding="utf-8")

    def explode(*_args, **_kwargs):
        raise RuntimeError("sensitive local detail")

    monkeypatch.setattr("paper_reader_batch.v2_cli.create_pdf_paths_manifest", explode)
    result = runner.invoke(
        app,
        [
            "manifest",
            "from-pdf-paths",
            str(paths),
            "--batch-title",
            "x",
            "--output",
            str(tmp_path / "out.json"),
            "--request-id",
            "11111111-1111-4111-8111-111111111111",
        ],
    )
    assert result.exit_code != 0
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "internal_error"
    assert payload["request_id"] == "11111111-1111-4111-8111-111111111111"
    assert "sensitive local detail" not in result.stdout + result.stderr


def test_manifest_validate_reports_nul_path_as_invalid_manifest_not_internal_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    monkeypatch.setattr("paper_reader_batch.v2_cli._batch_root", lambda: skill_root)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\ncontract boundary\n")
    paths_file = tmp_path / "paths.txt"
    paths_file.write_text(str(pdf), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    created = runner.invoke(
        app,
        [
            "manifest",
            "from-pdf-paths",
            str(paths_file),
            "--batch-title",
            "NUL boundary",
            "--output",
            str(manifest_path),
            "--request-id",
            "11111111-1111-4111-8111-111111111111",
        ],
    )
    assert created.exit_code == 0, (created.stdout, created.stderr)
    payload = json.loads(manifest_path.read_bytes())
    payload["items"][0]["source"]["path"] = "/tmp/paper\x00.pdf"
    manifest_path.write_bytes(canonical_json_bytes(payload))

    result = runner.invoke(app, ["manifest", "validate", str(manifest_path)])

    assert result.exit_code != 0
    assert len(result.stdout.splitlines()) == 1
    command_result = json.loads(result.stdout)
    assert command_result["command"] == "manifest.validate"
    assert command_result["error"]["code"] == "invalid_manifest"
    assert "internal_error" not in result.stdout + result.stderr


def test_long_unicode_domain_failure_remains_exactly_one_json_line(tmp_path: Path, monkeypatch) -> None:
    message = "证据链路失败🙂" * 2048

    def fail(_manifest: Path):
        raise BatchRuntimeError("unicode_failure", message, details={"说明": message})

    monkeypatch.setattr("paper_reader_batch.v2_cli.validate_manifest_file", fail)
    result = runner.invoke(app, ["manifest", "validate", str(tmp_path / "不存在.json")], terminal_width=20)
    assert result.exit_code != 0
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["command"] == "manifest.validate"
    assert payload["error"] == {"code": "unicode_failure", "message": message, "details": {"说明": message}}
    assert result.stderr == ""
