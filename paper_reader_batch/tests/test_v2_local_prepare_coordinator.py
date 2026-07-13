from __future__ import annotations

from dataclasses import replace
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

import pytest

from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_json import canonical_json_bytes, canonical_sha256
from paper_reader_batch.v2_local_prepare import (
    MAX_CHILD_TIMEOUT_SECONDS,
    _ChildCommandResult,
    _ChildInvocation,
    _ChildProtocolError,
    _default_child_runner,
    _parse_child_envelope,
    _validate_initialized_child,
    claim_local_prepare,
    local_prepare_attempt_has_execution_side_effects,
    release_local_prepare,
    run_local_prepare,
)
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_run import initialize_run


PAPER_READER_ROOT = Path(__file__).resolve().parents[2] / "paper_reader"
FIXTURE_PDF = PAPER_READER_ROOT / "tests" / "fixtures" / "minimal.pdf"


def _batch_run(tmp_path: Path) -> tuple[Path, Path, dict]:
    skill = tmp_path / "batch-skill"
    skill.mkdir()
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    paths = tmp_path / "paths.txt"
    paths.write_text(str(source), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="coordinator",
        output=manifest_path,
        request_id="11111111-1111-4111-8111-111111111111",
        skill_root=skill,
        created_at="2026-07-11T00:00:00Z",
    )
    run_dir = tmp_path / "batch-run"
    initialize_run(
        manifest_path,
        request_id="22222222-2222-4222-8222-222222222222",
        skill_root=skill,
        output=run_dir,
        initialized_at="2026-07-11T00:00:00Z",
    )
    assignment = claim_local_prepare(
        run_dir,
        worker_id="preparer",
        request_id="33333333-3333-4333-8333-333333333333",
        now="2026-07-11T00:00:01Z",
    ).result["assignments"][0]
    return run_dir, source, assignment


def _run_kwargs(run_dir: Path, assignment: dict, request_id: str) -> dict:
    return {
        "run_dir": run_dir,
        "item_id": assignment["item_id"],
        "worker_id": assignment["worker_id"],
        "claim_id": assignment["claim_id"],
        "lease_token": assignment["lease_token"],
        "attempt_id": assignment["attempt_id"],
        "paper_reader_root": PAPER_READER_ROOT,
        "request_id": request_id,
        "now": "2026-07-11T00:00:02Z",
    }


def _run_tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _crash_coordinator(
    run_dir: str,
    assignment: dict,
    request_id: str,
    crash_stage: str,
) -> None:
    def crash(stage: str) -> None:
        if stage == crash_stage:
            os._exit(91)

    run_local_prepare(
        **_run_kwargs(Path(run_dir), assignment, request_id),
        fault=crash,
    )


def _plain_coordinator(run_dir: str, assignment: dict, request_id: str) -> None:
    run_local_prepare(**_run_kwargs(Path(run_dir), assignment, request_id))


def _plain_coordinator_with_timeout(
    run_dir: str,
    assignment: dict,
    request_id: str,
    timeout_seconds: int,
) -> None:
    run_local_prepare(
        **_run_kwargs(Path(run_dir), assignment, request_id),
        timeout_seconds=timeout_seconds,
    )


def _wait_for_path(path: Path, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"timed out waiting for {path}"


def _wait_for_lines(path: Path, count: int, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and len(path.read_text(encoding="utf-8").splitlines()) >= count:
            return
        time.sleep(0.02)
    pytest.fail(f"timed out waiting for {count} lines in {path}")


@pytest.mark.parametrize(
    "crash_stage",
    ["after_init_child", "after_prepare_child", "before_batch_event"],
)
def test_crash_resume_reuses_exact_single_run_without_allocating_v2(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    request_id = "44444444-4444-4444-8444-444444444444"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_coordinator,
        args=(str(run_dir), assignment, request_id, crash_stage),
    )
    process.start()
    process.join(timeout=120)

    assert process.exitcode == 91
    assert (source.parent / "paper_analysis").is_dir()
    assert not (source.parent / "paper_analysis_v2").exists()

    resumed = run_local_prepare(**_run_kwargs(run_dir, assignment, request_id))

    assert resumed.result["status"] == "prepared"
    assert not (source.parent / "paper_analysis_v2").exists()
    state = load_run_view(run_dir).state.items[0]
    assert state.local_prepare_status == "prepared"


def test_killed_coordinator_waits_for_original_child_and_never_relaunches_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    real_uv = shutil.which("uv")
    assert real_uv is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "child-calls.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$COORD_CHILD_LOG"\n'
        "sleep 2\n"
        'exec "$COORD_REAL_UV" "$@"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    monkeypatch.setenv("COORD_CHILD_LOG", str(log_path))
    monkeypatch.setenv("COORD_REAL_UV", real_uv)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    request_id = "77777777-7777-4777-8777-777777777777"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_plain_coordinator,
        args=(str(run_dir), assignment, request_id),
    )
    process.start()
    _wait_for_path(log_path)
    process.terminate()
    process.join(timeout=30)
    assert process.exitcode is not None and process.exitcode != 0

    resumed = run_local_prepare(**_run_kwargs(run_dir, assignment, request_id))

    assert resumed.result["status"] == "prepared"
    calls = log_path.read_text(encoding="utf-8").splitlines()
    assert sum("paper_reader run init-local" in call for call in calls) == 1
    assert sum("paper_reader run prepare" in call for call in calls) == 1
    assert not (source.parent / "paper_analysis_v2").exists()


def _fake_paper_reader_root(tmp_path: Path) -> Path:
    root = tmp_path / "paper-reader"
    (root / "src" / "paper_reader").mkdir(parents=True)
    (root / "references" / "schemas").mkdir(parents=True)
    (root / "SKILL.md").write_text("# paper_reader V2\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname="paper_reader"\nversion="2.0.0"\n[project.scripts]\n'
        'paper_reader="paper_reader.public_cli:app"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "src" / "paper_reader" / "public_cli.py").write_text("app = object()\n", encoding="utf-8")
    for name in [
        "paper_reader.run.v2.schema.json",
        "paper_reader.command-result.v2.schema.json",
        "paper_reader.review-package.v2.schema.json",
        "paper_reader.candidate.v2.schema.json",
    ]:
        (root / "references" / "schemas" / name).write_text("{}\n", encoding="utf-8")
    return root


def test_source_is_revalidated_after_reservation_immediately_before_child_spawn(tmp_path: Path) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(argv, _cwd, _timeout_seconds, _invocation):
        calls.append(argv)
        raise AssertionError("drifted source must not reach the child runner")

    def drift(stage: str) -> None:
        if stage == "after_init_invocation_reserved":
            source.write_bytes(source.read_bytes() + b"drift")

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_local_prepare(
            **_run_kwargs(run_dir, assignment, "24242424-2424-4424-8424-242424242424"),
            runner=runner,
            fault=drift,
        )

    assert exc_info.value.code == "source_drift"
    assert calls == []
    assert not (source.parent / "paper_analysis").exists()


def test_skill_root_is_revalidated_after_reservation_immediately_before_child_spawn(tmp_path: Path) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    root = _fake_paper_reader_root(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(argv, _cwd, _timeout_seconds, _invocation):
        calls.append(argv)
        raise AssertionError("drifted skill root must not reach the child runner")

    def drift(stage: str) -> None:
        if stage == "after_init_invocation_reserved":
            (root / "src" / "paper_reader" / "public_cli.py").write_text(
                "app = object()\n# drift\n",
                encoding="utf-8",
            )

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_local_prepare(
            **{
                **_run_kwargs(run_dir, assignment, "25252525-2525-4525-8525-252525252525"),
                "paper_reader_root": root,
            },
            runner=runner,
            fault=drift,
        )

    assert exc_info.value.code == "paper_reader_root_drift"
    assert calls == []
    assert not (source.parent / "paper_analysis").exists()


def test_run_uses_exact_grouped_argv_and_replays_failed_child_without_reexecution(
    tmp_path: Path,
) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    root = _fake_paper_reader_root(tmp_path)
    calls: list[tuple[tuple[str, ...], Path, int]] = []

    def runner(argv: tuple[str, ...], cwd: Path, timeout_seconds: int, start) -> int:
        calls.append((argv, cwd, timeout_seconds))
        start.mark_started()
        start.write_stdout(
            canonical_json_bytes(
                {
                    "schema_version": "paper_reader.command-result.v2",
                    "command": "run init-local",
                    "ok": False,
                    "code": "invalid_local_pdf",
                    "created_at": "2026-07-11T00:00:02Z",
                    "message": "fixture failure",
                    "data": {"source_pdf": str(source)},
                }
            )
            + b"\n"
        )
        return 1

    request_id = "44444444-4444-4444-8444-444444444444"
    failed = run_local_prepare(
        **{**_run_kwargs(run_dir, assignment, request_id), "paper_reader_root": root},
        runner=runner,
    )

    assert failed.result["status"] == "failed"
    assert calls == [
        (
            ("uv", "run", "--locked", "paper_reader", "run", "init-local", str(source)),
            root,
            60,
        )
    ]

    def forbidden_runner(*_args) -> int:
        pytest.fail("a committed local-prepare request executed a child again")

    replayed = run_local_prepare(
        **{**_run_kwargs(run_dir, assignment, request_id), "paper_reader_root": root},
        runner=forbidden_runner,
    )
    assert replayed.replayed is True
    assert replayed.result == failed.result


def test_child_stdout_must_be_exactly_one_strict_command_result(tmp_path: Path) -> None:
    run_dir, _source, assignment = _batch_run(tmp_path)
    root = _fake_paper_reader_root(tmp_path)
    calls = 0

    def runner(_argv: tuple[str, ...], _cwd: Path, _timeout: int, start) -> int:
        nonlocal calls
        calls += 1
        start.mark_started()
        envelope = canonical_json_bytes(
            {
                "schema_version": "paper_reader.command-result.v2",
                "command": "run init-local",
                "ok": False,
                "code": "invalid_local_pdf",
                "created_at": "2026-07-11T00:00:02Z",
                "message": "one",
                "data": {},
            }
        )
        start.write_stdout(envelope + b"\n" + envelope + b"\n")
        return 1

    outcome = run_local_prepare(
        **{
            **_run_kwargs(
                run_dir,
                assignment,
                "55555555-5555-4555-8555-555555555555",
            ),
            "paper_reader_root": root,
        },
        runner=runner,
    )

    assert outcome.result["status"] == "blocked"
    assert calls == 1
    result_path = Path(outcome.result["result_path"])
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["error"]["code"] == "invalid_child_envelope"


def test_initialized_child_requires_exact_target_parent_inode(tmp_path: Path) -> None:
    run_dir, _source_path, _assignment = _batch_run(tmp_path)
    source = load_run_view(run_dir).manifest.items[0].source
    source_path = Path(source.path)
    child_run_dir = source_path.parent / "child-run"
    (child_run_dir / "source").mkdir(parents=True)
    target_path = source_path.parent / "paper_note.md"
    run_id = "run_parent_inode_binding"
    source_payload = {
        "source_type": "local_pdf",
        "requested_path": source.path,
        "resolved_path": source.path,
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "device": source.file_identity.device,
        "inode": source.file_identity.inode,
    }
    parent_metadata = os.stat(source_path.parent, follow_symlinks=False)
    target_payload = {
        "target_type": "local",
        "resolved_path": str(target_path),
        "parent_device": parent_metadata.st_dev,
        "parent_inode": parent_metadata.st_ino,
    }
    child_run = {
        "schema_version": "paper_reader.run.v2",
        "run_id": run_id,
        "created_at": "2026-07-11T00:00:02Z",
        "source": source_payload,
        "target": target_payload,
        "status": "initialized",
        "artifacts": [],
        "gate": {
            "status": "not_evaluated",
            "blockers": [],
            "checks": [],
            "evaluated_at": None,
        },
        "live_preflight": None,
    }
    (child_run_dir / "run.json").write_bytes(canonical_json_bytes(child_run))
    (child_run_dir / "source" / "source.json").write_bytes(
        canonical_json_bytes(source_payload)
    )
    envelope = _ChildCommandResult(
        schema_version="paper_reader.command-result.v2",
        command="run init-local",
        ok=True,
        code="initialized",
        created_at="2026-07-11T00:00:02Z",
        data={
            "run_dir": str(child_run_dir),
            "run_id": run_id,
            "target_path": str(target_path),
        },
    )

    assert _validate_initialized_child(envelope, source) == (
        child_run_dir,
        run_id,
        target_path,
    )

    child_run["target"] = {
        **target_payload,
        "parent_inode": parent_metadata.st_ino + 1,
    }
    (child_run_dir / "run.json").write_bytes(canonical_json_bytes(child_run))
    with pytest.raises(_ChildProtocolError) as exc_info:
        _validate_initialized_child(envelope, source)
    assert exc_info.value.code == "child_artifact_mismatch"


def test_insufficient_remaining_lease_time_rejects_before_any_child_or_coordination(
    tmp_path: Path,
) -> None:
    run_dir, _source, original = _batch_run(tmp_path)
    # Replace the default claim with a short fresh claim without producing artifacts.
    release_local_prepare(
        run_dir,
        original["item_id"],
        worker_id=original["worker_id"],
        claim_id=original["claim_id"],
        lease_token=original["lease_token"],
        attempt_id=original["attempt_id"],
        acknowledge_no_side_effects=True,
        request_id="88888888-8888-4888-8888-888888888888",
        now="2026-07-11T00:00:02Z",
    )
    assignment = claim_local_prepare(
        run_dir,
        worker_id="short-lease",
        request_id="99999999-9999-4999-8999-999999999999",
        lease_seconds=100,
        now="2026-07-11T00:00:03Z",
    ).result["assignments"][0]

    def forbidden_runner(*_args) -> int:
        pytest.fail("insufficient lease budget launched a child")

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_local_prepare(
            **{
                **_run_kwargs(
                    run_dir,
                    assignment,
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                ),
                "now": "2026-07-11T00:00:04Z",
            },
            runner=forbidden_runner,
        )
    assert exc_info.value.code == "insufficient_lease_time"
    assert not (
        run_dir / "results" / "local-prepare" / ".coordination"
    ).exists()


def test_timeout_above_maximum_lease_budget_is_rejected_before_run_access(
    tmp_path: Path,
) -> None:
    with pytest.raises(BatchRuntimeError) as exc_info:
        run_local_prepare(
            run_dir=tmp_path / "missing-run",
            item_id="001",
            worker_id="preparer",
            claim_id="11111111-1111-4111-8111-111111111111",
            lease_token="opaque-lease-token",
            attempt_id="22222222-2222-4222-8222-222222222222",
            paper_reader_root=tmp_path / "missing-paper-reader",
            request_id="33333333-3333-4333-8333-333333333333",
            timeout_seconds=MAX_CHILD_TIMEOUT_SECONDS + 1,
        )

    assert exc_info.value.code == "invalid_timeout"
    assert str(MAX_CHILD_TIMEOUT_SECONDS) in exc_info.value.message


def test_maximum_timeout_retains_sixty_seconds_for_claim_to_run_coordination(
    tmp_path: Path,
) -> None:
    run_dir, source, original = _batch_run(tmp_path)
    release_local_prepare(
        run_dir,
        original["item_id"],
        worker_id=original["worker_id"],
        claim_id=original["claim_id"],
        lease_token=original["lease_token"],
        attempt_id=original["attempt_id"],
        acknowledge_no_side_effects=True,
        request_id="abababab-abab-4bab-8bab-abababababab",
        now="2026-07-11T00:00:02Z",
    )
    assignment = claim_local_prepare(
        run_dir,
        worker_id="max-lease",
        request_id="bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc",
        lease_seconds=3600,
        now="2026-07-11T00:00:03Z",
    ).result["assignments"][0]
    root = _fake_paper_reader_root(tmp_path)
    calls: list[tuple[str, ...]] = []

    def failing_init_runner(argv, _cwd, _timeout_seconds, invocation) -> int:
        calls.append(argv)
        invocation.mark_started()
        invocation.write_stdout(
            canonical_json_bytes(
                {
                    "schema_version": "paper_reader.command-result.v2",
                    "command": "run init-local",
                    "ok": False,
                    "code": "fixture_failure",
                    "created_at": "2026-07-11T00:01:03Z",
                    "message": "stop after proving the child launch boundary",
                    "data": {"source_pdf": str(source)},
                }
            )
            + b"\n"
        )
        return 1

    request_id = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
    outcome = run_local_prepare(
        **{
            **_run_kwargs(run_dir, assignment, request_id),
            "paper_reader_root": root,
            "now": "2026-07-11T00:01:03Z",
        },
        timeout_seconds=MAX_CHILD_TIMEOUT_SECONDS,
        runner=failing_init_runner,
    )

    assert outcome.result["status"] == "failed"
    assert len(calls) == 1
    record = json.loads(
        (
            run_dir
            / "results"
            / "local-prepare"
            / ".coordination"
            / request_id
            / "record.json"
        ).read_text(encoding="utf-8")
    )
    assert record["timeout_seconds"] == MAX_CHILD_TIMEOUT_SECONDS


def test_claim_rejects_source_drift_before_journal_or_state_mutation(tmp_path: Path) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    release_local_prepare(
        run_dir,
        assignment["item_id"],
        worker_id=assignment["worker_id"],
        claim_id=assignment["claim_id"],
        lease_token=assignment["lease_token"],
        attempt_id=assignment["attempt_id"],
        acknowledge_no_side_effects=True,
        request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        now="2026-07-11T00:00:02Z",
    )
    source.write_bytes(source.read_bytes() + b"drift")
    before = _run_tree(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_local_prepare(
            run_dir,
            worker_id="drifted",
            request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            now="2026-07-11T00:00:03Z",
        )

    assert exc_info.value.code == "source_drift"
    assert _run_tree(run_dir) == before


def test_run_rejects_source_drift_before_coordination_or_child_spawn(tmp_path: Path) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    source.write_bytes(source.read_bytes() + b"drift")
    before = _run_tree(run_dir)

    def forbidden_runner(*_args) -> int:
        pytest.fail("source drift launched a child")

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_local_prepare(
            **_run_kwargs(
                run_dir,
                assignment,
                "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            ),
            runner=forbidden_runner,
        )

    assert exc_info.value.code == "source_drift"
    assert _run_tree(run_dir) == before
    assert not (
        run_dir / "results" / "local-prepare" / ".coordination"
    ).exists()


def test_tampered_hmac_record_blocks_recovery_before_child_execution(tmp_path: Path) -> None:
    run_dir, _source, assignment = _batch_run(tmp_path)
    request_id = "66666666-6666-4666-8666-666666666666"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_coordinator,
        args=(str(run_dir), assignment, request_id, "after_init_child"),
    )
    process.start()
    process.join(timeout=120)
    assert process.exitcode == 91

    record_path = (
        run_dir
        / "results"
        / "local-prepare"
        / ".coordination"
        / request_id
        / "record.json"
    )
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["worker_id"] = "attacker"
    record_path.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_local_prepare(**_run_kwargs(run_dir, assignment, request_id))
    assert exc_info.value.code == "coordination_corrupt"


def test_crash_after_invocation_reservation_before_spawn_is_safely_recoverable(
    tmp_path: Path,
) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    request_id = "12121212-1212-4212-8212-121212121212"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_coordinator,
        args=(str(run_dir), assignment, request_id, "after_init_invocation_reserved"),
    )
    process.start()
    process.join(timeout=30)

    assert process.exitcode == 91
    assert not (source.parent / "paper_analysis").exists()
    empty_stdout = (
        run_dir
        / "results"
        / "local-prepare"
        / ".coordination"
        / request_id
        / "init.stdout"
    )
    empty_stdout.touch()
    view = load_run_view(run_dir)
    assert local_prepare_attempt_has_execution_side_effects(
        view,
        item_id=assignment["item_id"],
        claim_id=assignment["claim_id"],
        attempt_id=assignment["attempt_id"],
    ) is False

    resumed = run_local_prepare(**_run_kwargs(run_dir, assignment, request_id))

    assert resumed.result["status"] == "prepared"
    assert (source.parent / "paper_analysis").is_dir()
    assert not (source.parent / "paper_analysis_v2").exists()


def test_started_child_without_stdout_is_never_reexecuted_after_coordinator_death(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "child-calls.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$COORD_CHILD_LOG"\n'
        "sleep 2\n"
        "exit 91\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    monkeypatch.setenv("COORD_CHILD_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    request_id = "13131313-1313-4313-8313-131313131313"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_plain_coordinator,
        args=(str(run_dir), assignment, request_id),
    )
    process.start()
    _wait_for_path(log_path)
    process.terminate()
    process.join(timeout=30)
    assert process.exitcode is not None and process.exitcode != 0

    resumed = run_local_prepare(**_run_kwargs(run_dir, assignment, request_id))

    assert resumed.result["status"] == "blocked"
    result = json.loads(Path(resumed.result["result_path"]).read_text(encoding="utf-8"))
    assert result["error"]["code"] == "coordination_uncertain"
    assert sum(
        "paper_reader run init-local" in call
        for call in log_path.read_text(encoding="utf-8").splitlines()
    ) == 1
    assert not (source.parent / "paper_analysis").exists()
    view = load_run_view(run_dir)
    assert local_prepare_attempt_has_execution_side_effects(
        view,
        item_id=assignment["item_id"],
        claim_id=assignment["claim_id"],
        attempt_id=assignment["attempt_id"],
    ) is True


def test_child_owned_timeout_releases_stdout_flock_after_coordinator_death(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _source, assignment = _batch_run(tmp_path)
    real_uv = shutil.which("uv")
    assert real_uv is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "child-calls.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$COORD_CHILD_LOG"\n'
        'case "$*" in\n'
        '  *"paper_reader run prepare"*) sleep 60; exit 91 ;;\n'
        "esac\n"
        'exec "$COORD_REAL_UV" "$@"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    monkeypatch.setenv("COORD_CHILD_LOG", str(log_path))
    monkeypatch.setenv("COORD_REAL_UV", real_uv)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    request_id = "19191919-1919-4919-8919-191919191919"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_plain_coordinator_with_timeout,
        args=(str(run_dir), assignment, request_id, 1),
    )
    process.start()
    _wait_for_lines(log_path, 2)
    process.terminate()
    process.join(timeout=30)
    assert process.exitcode is not None and process.exitcode != 0

    started_at = time.monotonic()
    resumed = run_local_prepare(
        **_run_kwargs(run_dir, assignment, request_id),
        timeout_seconds=1,
    )
    elapsed = time.monotonic() - started_at

    assert elapsed < 10
    assert resumed.result["status"] == "blocked"
    result = json.loads(Path(resumed.result["result_path"]).read_text(encoding="utf-8"))
    assert result["error"]["code"] == "coordination_uncertain"
    calls = log_path.read_text(encoding="utf-8").splitlines()
    assert sum("paper_reader run init-local" in call for call in calls) == 1
    assert sum("paper_reader run prepare" in call for call in calls) == 1


def test_gated_executor_runs_exact_argv_when_supervisor_crashes_after_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    real_uv = shutil.which("uv")
    assert real_uv is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "child-calls.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$COORD_CHILD_LOG"\n'
        'exec "$COORD_REAL_UV" "$@"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    monkeypatch.setenv("COORD_CHILD_LOG", str(log_path))
    monkeypatch.setenv("COORD_REAL_UV", real_uv)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    def crashing_supervisor_runner(argv, cwd, timeout_seconds, invocation):
        return _default_child_runner(
            argv,
            cwd,
            timeout_seconds,
            replace(invocation, launcher_fault_stage="supervisor_after_marker"),
        )

    outcome = run_local_prepare(
        **_run_kwargs(
            run_dir,
            assignment,
            "20202020-2020-4020-8020-202020202020",
        ),
        runner=crashing_supervisor_runner,
    )

    assert outcome.result["status"] == "prepared"
    calls = log_path.read_text(encoding="utf-8").splitlines()
    assert sum("paper_reader run init-local" in call for call in calls) == 1
    assert sum("paper_reader run prepare" in call for call in calls) == 1
    assert (source.parent / "paper_analysis").is_dir()
    assert not (source.parent / "paper_analysis_v2").exists()


def test_launcher_ack_timeout_cannot_race_a_committed_start_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_path = tmp_path / "init.started"
    stdout_path = tmp_path / "init.stdout"
    started_payload = b'{"started":true}'
    metadata = tmp_path.stat()
    invocation = _ChildInvocation(
        started_path=started_path,
        stdout_path=stdout_path,
        started_payload=started_payload,
        request_dir_device=metadata.st_dev,
        request_dir_inode=metadata.st_ino,
        run_lock_descriptors=(),
    )

    import paper_reader_batch.v2_local_prepare as local_prepare_module

    real_select = local_prepare_module.select.select
    target_started = threading.Event()
    killed_process_groups: list[int] = []

    class ControlledLauncher:
        pid = 424242

        def __init__(self, argv) -> None:
            self.argv = argv
            self.returncode = None
            self.new_protocol = argv[5] == "commit-v1"
            if self.new_protocol:
                acknowledgement_fd = os.dup(int(argv[4]))
                decision_fd = os.dup(int(argv[6]))

                def commit_marker_after_parent_decision() -> None:
                    try:
                        os.write(acknowledgement_fd, b"R")
                        if os.read(decision_fd, 1) == b"1":
                            started_path.write_bytes(started_payload)
                            target_started.set()
                    finally:
                        os.close(acknowledgement_fd)
                        os.close(decision_fd)

                threading.Thread(
                    target=commit_marker_after_parent_decision,
                    daemon=True,
                ).start()

        def poll(self):
            if not self.new_protocol and not target_started.is_set():
                assert started_path.exists() is False
                started_path.write_bytes(started_payload)
                target_started.set()
            return self.returncode

        def wait(self, timeout=None):
            if self.new_protocol and not target_started.wait(timeout):
                raise subprocess.TimeoutExpired(self.argv, timeout)
            self.returncode = 0
            return 0

    controlled: ControlledLauncher | None = None

    def fake_popen(argv, **_kwargs):
        nonlocal controlled
        controlled = ControlledLauncher(argv)
        return controlled

    def select_without_guessing(readers, writers, errors, timeout):
        if controlled is not None and controlled.new_protocol:
            return real_select(readers, writers, errors, timeout)
        return [], [], []

    monkeypatch.setattr(local_prepare_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(local_prepare_module.select, "select", select_without_guessing)
    monkeypatch.setattr(
        local_prepare_module.os,
        "killpg",
        lambda process_group, _signal: killed_process_groups.append(process_group),
    )

    handle = _default_child_runner(
        (sys.executable, "-c", "raise SystemExit(0)"),
        tmp_path,
        30,
        invocation,
    )

    handle.wait()
    assert target_started.is_set()
    assert started_path.read_bytes() == started_payload
    assert killed_process_groups == []


def test_started_handle_uses_cli_envelope_instead_of_supervisor_exit_status(
    tmp_path: Path,
) -> None:
    started_path = tmp_path / "init.started"
    metadata = tmp_path.stat()
    invocation = _ChildInvocation(
        started_path=started_path,
        stdout_path=tmp_path / "init.stdout",
        started_payload=b'{"started":true}',
        request_dir_device=metadata.st_dev,
        request_dir_inode=metadata.st_ino,
        run_lock_descriptors=(),
    )
    envelope = canonical_json_bytes(
        {
            "schema_version": "paper_reader.command-result.v2",
            "command": "run init-local",
            "ok": False,
            "code": "fixture_failure",
            "created_at": "2026-07-11T00:00:02Z",
            "message": "target failed after durable start",
            "data": {},
        }
    ) + b"\n"

    handle = _default_child_runner(
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(bytes.fromhex('"
            + envelope.hex()
            + "')); raise SystemExit(7)",
        ),
        tmp_path,
        30,
        invocation,
    )

    assert handle.wait() is None
    assert started_path.read_bytes() == invocation.started_payload
    parsed = _parse_child_envelope(
        invocation.stdout_path.read_bytes(),
        expected_command="run init-local",
        returncode=None,
    )
    assert parsed.ok is False
    assert parsed.code == "fixture_failure"


def test_started_ack_resets_target_deadline_after_slow_marker_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader_batch.v2_local_prepare as local_prepare_module

    clock = [0.0]
    marker_committed = threading.Event()
    target_starts: list[int] = []
    target_escaped = threading.Event()
    killed_process_groups: list[int] = []
    wait_timeouts: list[float | None] = []
    metadata = tmp_path.stat()
    invocation = _ChildInvocation(
        started_path=tmp_path / "init.started",
        stdout_path=tmp_path / "init.stdout",
        started_payload=b'{"started":true}',
        request_dir_device=metadata.st_dev,
        request_dir_inode=metadata.st_ino,
        run_lock_descriptors=(),
    )

    class SlowMarkerLauncher:
        pid = 515151
        returncode = None

        def __init__(self, argv) -> None:
            acknowledgement_fd = os.dup(int(argv[4]))
            decision_fd = os.dup(int(argv[6]))

            def launch() -> None:
                try:
                    os.write(acknowledgement_fd, b"R")
                    assert os.read(decision_fd, 1) == b"1"
                    clock[0] = 12.0
                    invocation.started_path.write_bytes(invocation.started_payload)
                    target_starts.append(1)
                    try:
                        os.write(acknowledgement_fd, b"S")
                    except BrokenPipeError:
                        pass
                finally:
                    marker_committed.set()
                    os.close(acknowledgement_fd)
                    os.close(decision_fd)

            threading.Thread(target=launch, daemon=True).start()

        def wait(self, timeout=None):
            assert marker_committed.wait(1)
            wait_timeouts.append(timeout)
            if timeout is not None and timeout < 1:
                target_escaped.set()
                raise subprocess.TimeoutExpired("slow-marker-launcher", timeout)
            self.returncode = 0
            return 0

    monkeypatch.setattr(local_prepare_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        local_prepare_module.subprocess,
        "Popen",
        lambda argv, **_kwargs: SlowMarkerLauncher(argv),
    )
    monkeypatch.setattr(
        local_prepare_module.os,
        "killpg",
        lambda process_group, _signal: killed_process_groups.append(process_group),
    )

    handle = _default_child_runner(
        (sys.executable, "-c", "raise SystemExit(0)"),
        tmp_path,
        1,
        invocation,
    )
    assert marker_committed.wait(1)

    assert handle.wait() is None
    assert target_starts == [1]
    assert target_escaped.is_set() is False
    assert killed_process_groups == []
    assert wait_timeouts == [11.0]


def test_commit_without_started_ack_or_marker_cancels_entire_launcher_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader_batch.v2_local_prepare as local_prepare_module

    committed = threading.Event()
    killed_process_groups: list[int] = []
    metadata = tmp_path.stat()
    invocation = _ChildInvocation(
        started_path=tmp_path / "init.started",
        stdout_path=tmp_path / "init.stdout",
        started_payload=b'{"started":true}',
        request_dir_device=metadata.st_dev,
        request_dir_inode=metadata.st_ino,
        run_lock_descriptors=(),
    )

    class MissingMarkerLauncher:
        pid = 525252
        returncode = None

        def __init__(self, argv) -> None:
            acknowledgement_fd = os.dup(int(argv[4]))
            decision_fd = os.dup(int(argv[6]))

            def launch() -> None:
                try:
                    os.write(acknowledgement_fd, b"R")
                    assert os.read(decision_fd, 1) == b"1"
                finally:
                    committed.set()
                    os.close(acknowledgement_fd)
                    os.close(decision_fd)

            threading.Thread(target=launch, daemon=True).start()

        def wait(self, timeout=None):
            assert committed.wait(1)
            self.returncode = 96
            return 96

    monkeypatch.setattr(
        local_prepare_module.subprocess,
        "Popen",
        lambda argv, **_kwargs: MissingMarkerLauncher(argv),
    )
    monkeypatch.setattr(local_prepare_module.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(
        local_prepare_module.os,
        "killpg",
        lambda process_group, _signal: killed_process_groups.append(process_group),
    )

    with pytest.raises(_ChildProtocolError) as exc_info:
        _default_child_runner(
            (sys.executable, "-c", "raise SystemExit(0)"),
            tmp_path,
            30,
            invocation,
        )

    assert exc_info.value.code == "child_execution_failed"
    assert committed.is_set()
    assert killed_process_groups == [MissingMarkerLauncher.pid]
    assert invocation.started_path.exists() is False


def test_missing_started_ack_reads_marker_from_held_request_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader_batch.v2_local_prepare as local_prepare_module

    request_dir = tmp_path / "request"
    request_dir.mkdir()
    detached_request_dir = tmp_path / "request.detached"
    invocation = _ChildInvocation(
        started_path=request_dir / "init.started",
        stdout_path=request_dir / "init.stdout",
        started_payload=b'{"started":true}',
        request_dir_device=request_dir.stat().st_dev,
        request_dir_inode=request_dir.stat().st_ino,
        run_lock_descriptors=(),
    )
    committed_and_replaced = threading.Event()
    killed_process_groups: list[int] = []

    class ReplacedPathLauncher:
        pid = 535353
        returncode = None

        def __init__(self, argv) -> None:
            acknowledgement_fd = os.dup(int(argv[4]))
            decision_fd = os.dup(int(argv[6]))

            def launch() -> None:
                try:
                    os.write(acknowledgement_fd, b"R")
                    assert os.read(decision_fd, 1) == b"1"
                    invocation.started_path.write_bytes(invocation.started_payload)
                    request_dir.rename(detached_request_dir)
                    request_dir.mkdir()
                finally:
                    committed_and_replaced.set()
                    os.close(acknowledgement_fd)
                    os.close(decision_fd)

            threading.Thread(target=launch, daemon=True).start()

        def wait(self, timeout=None):
            assert committed_and_replaced.wait(timeout or 1)
            self.returncode = 98
            return 98

    monkeypatch.setattr(
        local_prepare_module.subprocess,
        "Popen",
        lambda argv, **_kwargs: ReplacedPathLauncher(argv),
    )
    monkeypatch.setattr(local_prepare_module.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(
        local_prepare_module.os,
        "killpg",
        lambda process_group, _signal: killed_process_groups.append(process_group),
    )

    handle = _default_child_runner(
        (sys.executable, "-c", "raise SystemExit(0)"),
        tmp_path,
        30,
        invocation,
    )

    assert handle.wait() is None
    assert (detached_request_dir / "init.started").read_bytes() == invocation.started_payload
    assert not (request_dir / "init.started").exists()
    assert killed_process_groups == []


def test_supervisor_crash_after_ready_breaks_commit_and_leaves_no_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader_batch.v2_local_prepare as local_prepare_module

    fault_stage = "supervisor_after_ready_before_decision"
    faulted_launcher = local_prepare_module._CHILD_LAUNCHER.replace(
        "    gate_value = os.read(gate_read, 1)\n",
        (
            f'    if fault_stage == "{fault_stage}":\n'
            "        signal.pause()\n"
            "    gate_value = os.read(gate_read, 1)\n"
        ),
        1,
    ).replace(
        'os.write(ack_fd, b"R")\ndecision = os.read(decision_fd, 1)\n',
        (
            'os.write(ack_fd, b"R")\n'
            f'if fault_stage == "{fault_stage}":\n'
            "    os.close(decision_fd)\n"
            "    os._exit(96)\n"
            "decision = os.read(decision_fd, 1)\n"
        ),
        1,
    )
    assert faulted_launcher != local_prepare_module._CHILD_LAUNCHER
    monkeypatch.setattr(local_prepare_module, "_CHILD_LAUNCHER", faulted_launcher)

    lock_path = tmp_path / "run.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    metadata = tmp_path.stat()
    invocation = _ChildInvocation(
        started_path=tmp_path / "init.started",
        stdout_path=tmp_path / "init.stdout",
        started_payload=b'{"started":true}',
        request_dir_device=metadata.st_dev,
        request_dir_inode=metadata.st_ino,
        run_lock_descriptors=(lock_fd,),
        launcher_fault_stage=fault_stage,
    )
    target_path = tmp_path / "target-ran"
    unexpected_handle = None
    error = None
    try:
        try:
            unexpected_handle = _default_child_runner(
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path(r'"
                    + str(target_path)
                    + "').write_text('ran', encoding='utf-8')",
                ),
                tmp_path,
                30,
                invocation,
            )
        except _ChildProtocolError as exc:
            error = exc
    finally:
        os.close(lock_fd)
        if unexpected_handle is not None:
            try:
                os.killpg(unexpected_handle.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            unexpected_handle.process.wait()

    assert error is not None
    assert error.code == "child_execution_failed"
    assert not target_path.exists()
    assert not invocation.started_path.exists()

    probe_fd = os.open(lock_path, os.O_RDWR)
    deadline = time.monotonic() + 2
    lock_released = False
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(0.01)
            else:
                lock_released = True
                break
    finally:
        os.close(probe_fd)
    assert lock_released, "orphan executor retained the inherited run lock"


def test_running_child_makes_release_read_only_and_prevents_second_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _source, assignment = _batch_run(tmp_path)
    real_uv = shutil.which("uv")
    assert real_uv is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "child-calls.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$COORD_CHILD_LOG"\n'
        "sleep 2\n"
        'exec "$COORD_REAL_UV" "$@"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    monkeypatch.setenv("COORD_CHILD_LOG", str(log_path))
    monkeypatch.setenv("COORD_REAL_UV", real_uv)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    request_id = "14141414-1414-4414-8414-141414141414"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_plain_coordinator,
        args=(str(run_dir), assignment, request_id),
    )
    process.start()
    _wait_for_path(log_path)
    before = _run_tree(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        release_local_prepare(
            run_dir,
            assignment["item_id"],
            worker_id=assignment["worker_id"],
            claim_id=assignment["claim_id"],
            lease_token=assignment["lease_token"],
            attempt_id=assignment["attempt_id"],
            acknowledge_no_side_effects=True,
            request_id="15151515-1515-4515-8515-151515151515",
            now="2026-07-11T00:00:03Z",
        )
    assert exc_info.value.code == "side_effects_detected"
    assert _run_tree(run_dir) == before

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_local_prepare(
            run_dir,
            worker_id="attempt-2",
            request_id="16161616-1616-4616-8616-161616161616",
            now="2026-07-11T00:00:04Z",
        )
    assert exc_info.value.code == "no_available_work"
    assert _run_tree(run_dir) == before

    process.join(timeout=120)
    assert process.exitcode == 0


def test_attempt_effect_helper_reads_exact_signed_owner_and_record(tmp_path: Path) -> None:
    run_dir, _source, assignment = _batch_run(tmp_path)
    request_id = "17171717-1717-4717-8717-171717171717"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_coordinator,
        args=(str(run_dir), assignment, request_id, "after_init_invocation_reserved"),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 91
    owner_path = (
        run_dir
        / "results"
        / "local-prepare"
        / ".coordination"
        / ".attempts"
        / f"{assignment['attempt_id']}.json"
    )
    payload = json.loads(owner_path.read_text(encoding="utf-8"))
    payload["claim_id"] = "18181818-1818-4818-8818-181818181818"
    owner_path.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(BatchRuntimeError) as exc_info:
        local_prepare_attempt_has_execution_side_effects(
            load_run_view(run_dir),
            item_id=assignment["item_id"],
            claim_id=assignment["claim_id"],
            attempt_id=assignment["attempt_id"],
        )
    assert exc_info.value.code == "coordination_corrupt"


def test_owned_attempt_missing_exact_record_is_corrupt_and_read_only(tmp_path: Path) -> None:
    run_dir, _source, assignment = _batch_run(tmp_path)
    request_id = "21212121-2121-4121-8121-212121212121"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_coordinator,
        args=(str(run_dir), assignment, request_id, "after_init_invocation_reserved"),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 91
    record_path = (
        run_dir
        / "results"
        / "local-prepare"
        / ".coordination"
        / request_id
        / "record.json"
    )
    os.replace(record_path, record_path.with_name("record.orphan"))
    before = _run_tree(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        local_prepare_attempt_has_execution_side_effects(
            load_run_view(run_dir),
            item_id=assignment["item_id"],
            claim_id=assignment["claim_id"],
            attempt_id=assignment["attempt_id"],
        )

    assert exc_info.value.code == "coordination_corrupt"
    assert _run_tree(run_dir) == before


@pytest.mark.parametrize("copy_record", [False, True])
def test_run_replay_rejects_replaced_request_directory_before_second_child(
    tmp_path: Path,
    copy_record: bool,
) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    request_id = "26262626-2626-4626-8626-262626262626"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_coordinator,
        args=(str(run_dir), assignment, request_id, "after_init_invocation_reserved"),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 91

    request_dir = (
        run_dir
        / "results"
        / "local-prepare"
        / ".coordination"
        / request_id
    )
    moved = request_dir.with_name(f"{request_id}.moved")
    request_dir.rename(moved)
    request_dir.mkdir()
    if copy_record:
        shutil.copyfile(moved / "record.json", request_dir / "record.json")

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_local_prepare(**_run_kwargs(run_dir, assignment, request_id))

    assert exc_info.value.code == "coordination_corrupt"
    assert not (source.parent / "paper_analysis").exists()


def test_run_replay_rejects_replaced_coordination_anchor_before_second_child(tmp_path: Path) -> None:
    run_dir, source, assignment = _batch_run(tmp_path)
    request_id = "27272727-2727-4727-8727-272727272727"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_coordinator,
        args=(str(run_dir), assignment, request_id, "after_init_invocation_reserved"),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 91

    reserved_view = load_run_view(run_dir)
    assert reserved_view.events[-1].data.kind == "local_prepare.coordination_reserved"
    reserved_item = reserved_view.state.items[0]
    assert reserved_item.local_prepare_coordination_request_id == request_id
    assert reserved_item.local_prepare_coordination_device is not None
    assert reserved_item.local_prepare_coordination_inode is not None

    coordination_root = run_dir / "results" / "local-prepare" / ".coordination"
    moved = coordination_root.with_name(".coordination.moved")
    coordination_root.rename(moved)
    (coordination_root / ".attempts").mkdir(parents=True)

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_local_prepare(**_run_kwargs(run_dir, assignment, request_id))

    assert exc_info.value.code == "coordination_corrupt"
    assert not (source.parent / "paper_analysis").exists()


def test_journal_rejects_coordination_reservation_with_non_derived_request_id(tmp_path: Path) -> None:
    run_dir, _source, assignment = _batch_run(tmp_path)
    request_id = "28282828-2828-4828-8828-282828282828"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_coordinator,
        args=(str(run_dir), assignment, request_id, "after_init_invocation_reserved"),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 91

    event_path = sorted((run_dir / "events").glob("*.json"))[-1]
    payload = json.loads(event_path.read_bytes())
    wrong_request_id = "29292929-2929-4929-8929-292929292929"
    payload["request_id"] = wrong_request_id
    payload["command_result"]["request_id"] = wrong_request_id
    payload.pop("event_sha256")
    payload["event_sha256"] = canonical_sha256(payload)
    event_path.write_bytes(canonical_json_bytes(payload))
    before = _run_tree(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        load_run_view(run_dir)

    assert exc_info.value.code == "journal_corrupt"
    assert _run_tree(run_dir) == before
