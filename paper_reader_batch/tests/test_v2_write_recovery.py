from __future__ import annotations

import json
from pathlib import Path
import subprocess
from uuid import UUID

import pytest

from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import canonical_json_bytes, sha256_bytes
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_run import recover_run
from paper_reader_batch.v2_write import (
    begin_write,
    claim_write,
    reconcile_write,
    release_write,
    retry_write,
)
from test_v2_write_runtime import (
    REQUEST_WRITE_BEGIN,
    REQUEST_WRITE_CLAIM,
    _make_authorization,
    _make_reconciliation_matches,
    _make_reconciliation_not_found,
    _ready_write_run,
)


REQUEST_RECOVER_CLAIMED = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
REQUEST_RECOVER_STARTED = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
REQUEST_RECOVER_CONTINUE = "abababab-abab-4bab-8bab-abababababab"
REQUEST_RECOVER_RECEIPT = "acacacac-acac-4cac-8cac-acacacacacac"
REQUEST_RECONCILE_OTHER = "adadadad-adad-4dad-8dad-adadadadadad"
REQUEST_RETRY_OTHER = "aeaeaeae-aeae-4eae-8eae-aeaeaeaeaeae"
REQUEST_CLAIM_AFTER = "afafafaf-afaf-4faf-8faf-afafafafafaf"
REQUEST_BEGIN_AFTER = "b0b0b0b0-b0b0-40b0-80b0-b0b0b0b0b0b0"
REQUEST_RECOVER_AFTER = "b1b1b1b1-b1b1-41b1-81b1-b1b1b1b1b1b1"
REQUEST_RELEASE_ONE = "ffffffff-ffff-4fff-8fff-ffffffffffff"
REQUEST_CLAIM_TWO = "12121212-1212-4212-8212-121212121212"
REQUEST_RELEASE_TWO = "13131313-1313-4313-8313-131313131313"
REQUEST_REUSED_HISTORY = "14141414-1414-4414-8414-141414141414"


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
    (root / "src" / "paper_reader" / "public_cli.py").write_text(
        "app = object()\n",
        encoding="utf-8",
    )
    for name in [
        "paper_reader.run.v2.schema.json",
        "paper_reader.command-result.v2.schema.json",
        "paper_reader.review-package.v2.schema.json",
        "paper_reader.candidate.v2.schema.json",
    ]:
        (root / "references" / "schemas" / name).write_text("{}\n", encoding="utf-8")
    return root


def _started_write(tmp_path: Path):
    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, authorization = _make_authorization(ready, claimed)
    begin_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        authorization_path=authorization_path,
        request_id=REQUEST_WRITE_BEGIN,
        now="2026-07-10T00:00:06Z",
    )
    return ready, claimed, authorization_path, authorization


def _child_result(
    *,
    reconciliation_path: Path,
    authorization_path: Path,
    outcome: str,
    matched_note_keys: tuple[str, ...],
    created_at: str = "2026-07-10T00:02:04.000000Z",
) -> subprocess.CompletedProcess[bytes]:
    reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
    ok = outcome == "verified"
    code = "reconciliation_verified" if ok else f"reconciliation_{outcome}"
    envelope = {
        "schema_version": "paper_reader.command-result.v2",
        "command": "zotero reconcile",
        "ok": ok,
        "code": code,
        "created_at": created_at,
        "message": None if ok else f"Zotero reconciliation ended as {outcome}",
        "data": {
            "reconciliation_path": str(reconciliation_path),
            "reconciliation_id": reconciliation["reconciliation_id"],
            "authorization_digest": sha256_bytes(authorization_path.read_bytes()),
            "outcome": outcome,
            "match_count": len(matched_note_keys),
            "matched_note_keys": list(matched_note_keys),
            "retry_confirmation_required": outcome == "not_found",
            "replayed": False,
            "verification_path": (
                str(reconciliation_path.parent.parent / reconciliation["verification"]["path"])
                if reconciliation["verification"] is not None
                else None
            ),
        },
    }
    return subprocess.CompletedProcess(
        args=[],
        returncode=0 if ok else 1,
        stdout=canonical_json_bytes(envelope) + b"\n",
        stderr=b"" if ok else f"{outcome}\n".encode(),
    )


def test_recover_expired_claimed_write_returns_only_that_attempt_to_queue(tmp_path: Path) -> None:
    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result

    first = recover_run(
        ready.run_dir,
        request_id=REQUEST_RECOVER_CLAIMED,
        now="2026-07-10T00:02:03Z",
    )
    replay = recover_run(
        ready.run_dir,
        request_id=REQUEST_RECOVER_CLAIMED,
        now="2026-07-10T00:02:03Z",
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.result == first.result
    assert first.result["expired_write_claimed_items"] == ["001"]
    assert first.result["expired_write_started_items"] == []
    view = load_run_view(ready.run_dir)
    assert view.events[-1].data.kind == "write.lease_expired"
    assert view.state.items[0].write_status == "queued"
    assert view.state.items[0].write_lease is None
    assert claimed["write_attempt_id"] == view.state.items[0].write_last_attempt_id


def test_recover_expired_started_write_is_uncertain_and_never_requeued(tmp_path: Path) -> None:
    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(ready, claimed)
    begin = begin_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        authorization_path=authorization_path,
        request_id=REQUEST_WRITE_BEGIN,
        now="2026-07-10T00:00:06Z",
    )
    assert begin.result["delivery_rule"] == "send_only_when_command_result.replayed_is_false"

    recovered = recover_run(
        ready.run_dir,
        request_id=REQUEST_RECOVER_STARTED,
        now="2026-07-10T00:02:03Z",
    )

    assert recovered.result["expired_write_claimed_items"] == []
    assert recovered.result["expired_write_started_items"] == ["001"]
    assert "mcp_envelope" not in recovered.result
    view = load_run_view(ready.run_dir)
    assert view.events[-1].data.kind == "write.lease_expired_uncertain"
    item = view.state.items[0]
    assert item.write_status == "uncertain"
    assert item.write_lease is None
    assert item.write_failure_code == "write_outcome_uncertain"
    assert item.write_last_attempt_id == claimed["write_attempt_id"]
    assert recovered.result["reconciliation_required"] == [
        {
            "item_id": "001",
            "authorization_sha256": item.authorization_sha256,
            "next_action": (
                "rerun run recover with an explicit --paper-reader-root "
                "and a new request id"
            ),
        }
    ]


@pytest.mark.parametrize(
    ("matched_note_keys", "expected_outcome", "expected_status"),
    [
        (("NOTE1",), "verified", "written"),
        ((), "not_found", "retry_confirmation_required"),
        (("NOTE1", "NOTE2"), "ambiguous", "blocked"),
    ],
)
def test_recover_expired_started_write_delegates_read_only_reconciliation(
    tmp_path: Path,
    matched_note_keys: tuple[str, ...],
    expected_outcome: str,
    expected_status: str,
) -> None:
    ready, _claimed, authorization_path, authorization = _started_write(tmp_path)
    paper_reader_root = _fake_paper_reader_root(tmp_path)
    calls: list[tuple[tuple[str, ...], Path, int]] = []

    def runner(argv: tuple[str, ...], cwd: Path, timeout_seconds: int):
        calls.append((argv, cwd, timeout_seconds))
        if expected_outcome == "not_found":
            reconciliation_path = _make_reconciliation_not_found(
                ready,
                authorization_path,
                authorization,
            )
        else:
            reconciliation_path = _make_reconciliation_matches(
                ready,
                authorization_path,
                authorization,
                note_keys=matched_note_keys,
            )
        return _child_result(
            reconciliation_path=reconciliation_path,
            authorization_path=authorization_path,
            outcome=expected_outcome,
            matched_note_keys=matched_note_keys,
        )

    recovered = recover_run(
        ready.run_dir,
        request_id=REQUEST_RECOVER_STARTED,
        now="2026-07-10T00:02:03Z",
        paper_reader_root=paper_reader_root,
        reconciliation_runner=runner,
        reconciliation_timeout_seconds=45,
    )

    assert calls == [
        (
            (
                "uv",
                "run",
                "--locked",
                "paper_reader",
                "zotero",
                "reconcile",
                str(authorization_path),
            ),
            paper_reader_root,
            45,
        )
    ]
    assert recovered.result["expired_write_started_items"] == ["001"]
    delegated = recovered.result["reconciliation"]
    UUID(delegated["request_id"])
    assert delegated["item_id"] == "001"
    assert delegated["child_outcome"] == expected_outcome
    assert delegated["status"] == expected_status
    view = load_run_view(ready.run_dir)
    assert [event.data.kind for event in view.events[-2:]] == [
        "write.lease_expired_uncertain",
        "write.reconciled",
    ]
    assert view.state.items[0].write_status == expected_status


def test_recover_retries_read_only_child_after_uncertain_event_without_reissuing_write(
    tmp_path: Path,
) -> None:
    ready, _claimed, authorization_path, authorization = _started_write(tmp_path)
    paper_reader_root = _fake_paper_reader_root(tmp_path)
    reconciliation_path: Path | None = None
    calls = 0

    def runner(argv: tuple[str, ...], cwd: Path, timeout_seconds: int):
        nonlocal calls, reconciliation_path
        calls += 1
        assert argv[-1] == str(authorization_path)
        assert cwd == paper_reader_root
        assert timeout_seconds == 60
        if reconciliation_path is None:
            reconciliation_path = _make_reconciliation_matches(
                ready,
                authorization_path,
                authorization,
                note_keys=("NOTE1",),
            )
        if calls == 1:
            raise OSError("injected launcher result loss")
        return _child_result(
            reconciliation_path=reconciliation_path,
            authorization_path=authorization_path,
            outcome="verified",
            matched_note_keys=("NOTE1",),
        )

    with pytest.raises(BatchRuntimeError) as first_error:
        recover_run(
            ready.run_dir,
            request_id=REQUEST_RECOVER_STARTED,
            now="2026-07-10T00:02:03Z",
            paper_reader_root=paper_reader_root,
            reconciliation_runner=runner,
            reconciliation_timeout_seconds=60,
        )
    assert first_error.value.code == "reconciliation_child_failed"
    first_view = load_run_view(ready.run_dir)
    assert first_view.state.items[0].write_status == "uncertain"
    assert sum(event.data.kind == "write.lease_expired_uncertain" for event in first_view.events) == 1
    assert sum(event.data.kind == "write.reconciled" for event in first_view.events) == 0

    replay = recover_run(
        ready.run_dir,
        request_id=REQUEST_RECOVER_STARTED,
        now="2026-07-10T00:02:03Z",
        paper_reader_root=paper_reader_root,
        reconciliation_runner=runner,
        reconciliation_timeout_seconds=60,
    )

    assert replay.replayed is True
    assert replay.result["reconciliation"]["status"] == "written"
    final_view = load_run_view(ready.run_dir)
    assert final_view.state.items[0].write_status == "written"
    assert sum(event.data.kind == "write.lease_expired_uncertain" for event in final_view.events) == 1
    assert sum(event.data.kind == "write.reconciled" for event in final_view.events) == 1
    assert all("mcp_envelope" not in event.command_result.model_dump_json() for event in final_view.events)


def test_rootless_recover_continues_uncertain_write_with_new_root_bound_request(
    tmp_path: Path,
) -> None:
    ready, _claimed, authorization_path, authorization = _started_write(tmp_path)
    rootless = recover_run(
        ready.run_dir,
        request_id=REQUEST_RECOVER_STARTED,
        now="2026-07-10T00:02:03Z",
    )
    assert rootless.result["expired_write_started_items"] == ["001"]
    next_action = rootless.result["reconciliation_required"][0]["next_action"]

    paper_reader_root = _fake_paper_reader_root(tmp_path)
    calls = 0

    def runner(_argv: tuple[str, ...], _cwd: Path, _timeout_seconds: int):
        nonlocal calls
        calls += 1
        reconciliation_path = _make_reconciliation_matches(
            ready,
            authorization_path,
            authorization,
            note_keys=("NOTE1",),
        )
        return _child_result(
            reconciliation_path=reconciliation_path,
            authorization_path=authorization_path,
            outcome="verified",
            matched_note_keys=("NOTE1",),
        )

    continued = recover_run(
        ready.run_dir,
        request_id=REQUEST_RECOVER_CONTINUE,
        now="2026-07-10T00:02:05Z",
        paper_reader_root=paper_reader_root,
        reconciliation_runner=runner,
        reconciliation_timeout_seconds=45,
    )
    assert continued.result["reconciliation"]["status"] == "written"
    assert "new request id" in next_action
    assert calls == 1

    def forbidden_runner(*_args):
        pytest.fail("a committed root-bound continuation re-executed reconciliation")

    replay = recover_run(
        ready.run_dir,
        request_id=REQUEST_RECOVER_CONTINUE,
        now="2026-07-10T00:02:05Z",
        paper_reader_root=paper_reader_root,
        reconciliation_runner=forbidden_runner,
        reconciliation_timeout_seconds=45,
    )
    assert replay.replayed is True
    assert replay.result == continued.result

    with pytest.raises(BatchRuntimeError) as drift_error:
        recover_run(
            ready.run_dir,
            request_id=REQUEST_RECOVER_CONTINUE,
            now="2026-07-10T00:02:05Z",
            paper_reader_root=paper_reader_root,
            reconciliation_runner=forbidden_runner,
            reconciliation_timeout_seconds=46,
        )
    assert drift_error.value.code == "idempotency_conflict"

    view = load_run_view(ready.run_dir)
    kinds = [event.data.kind for event in view.events]
    assert kinds.count("write.lease_expired_uncertain") == 1
    assert kinds.count("run.recovered") == 1
    assert kinds.count("write.reconciled") == 1


def test_continuation_receipt_replay_rejects_a_new_uncertain_attempt_before_child(
    tmp_path: Path,
) -> None:
    ready, first_claim, first_authorization_path, first_authorization = _started_write(
        tmp_path
    )
    recover_run(
        ready.run_dir,
        request_id=REQUEST_RECOVER_STARTED,
        now="2026-07-10T00:02:03Z",
    )
    paper_reader_root = _fake_paper_reader_root(tmp_path)

    class InjectedCrash(RuntimeError):
        pass

    def crash_after_receipt(stage: str) -> None:
        if stage == "after_event":
            raise InjectedCrash("receipt committed before reconciliation")

    def forbidden_initial_runner(*_args):
        pytest.fail("reconciliation child ran after the injected receipt crash")

    with pytest.raises(InjectedCrash):
        recover_run(
            ready.run_dir,
            request_id=REQUEST_RECOVER_RECEIPT,
            now="2026-07-10T00:02:05Z",
            paper_reader_root=paper_reader_root,
            reconciliation_runner=forbidden_initial_runner,
            reconciliation_timeout_seconds=45,
            fault=crash_after_receipt,
        )

    first_not_found_path = _make_reconciliation_not_found(
        ready,
        first_authorization_path,
        first_authorization,
    )
    reconcile_write(
        ready.run_dir,
        "001",
        readback_path=first_not_found_path,
        request_id=REQUEST_RECONCILE_OTHER,
        now="2026-07-10T00:02:06Z",
    )
    retry_write(
        ready.run_dir,
        "001",
        acknowledge_no_match=True,
        request_id=REQUEST_RETRY_OTHER,
        now="2026-07-10T00:02:07Z",
    )
    second_claim = claim_write(
        ready.run_dir,
        writer_id="writer-2",
        request_id=REQUEST_CLAIM_AFTER,
        now="2026-07-10T00:02:08Z",
    ).result
    second_authorization_path, _second_authorization = _make_authorization(
        ready,
        second_claim,
        created_at="2026-07-10T00:02:09Z",
        expires_at="2026-07-10T00:07:09Z",
        nonce="nonce_" + "b" * 37,
    )
    begin_write(
        ready.run_dir,
        "001",
        writer_id="writer-2",
        claim_id=second_claim["claim_id"],
        lease_token=second_claim["lease_token"],
        write_attempt_id=second_claim["write_attempt_id"],
        authorization_path=second_authorization_path,
        request_id=REQUEST_BEGIN_AFTER,
        now="2026-07-10T00:02:10Z",
    )
    recover_run(
        ready.run_dir,
        request_id=REQUEST_RECOVER_AFTER,
        now="2026-07-10T00:04:09Z",
    )

    current = load_run_view(ready.run_dir).state.items[0]
    assert current.write_status == "uncertain"
    assert current.write_last_attempt_id == second_claim["write_attempt_id"]
    assert current.write_last_attempt_id != first_claim["write_attempt_id"]
    calls = 0

    def forbidden_replay_runner(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("receipt replay selected the new uncertain attempt")

    with pytest.raises(BatchRuntimeError) as drift_error:
        recover_run(
            ready.run_dir,
            request_id=REQUEST_RECOVER_RECEIPT,
            now="2026-07-10T00:02:05Z",
            paper_reader_root=paper_reader_root,
            reconciliation_runner=forbidden_replay_runner,
            reconciliation_timeout_seconds=45,
        )

    assert drift_error.value.code == "recovery_target_drift"
    assert calls == 0


@pytest.mark.parametrize("drift", ["paper_reader_root", "reconciliation_timeout"])
def test_recover_replay_rejects_changed_reconciliation_inputs(
    tmp_path: Path,
    drift: str,
) -> None:
    ready, _claimed, _authorization_path, _authorization = _started_write(tmp_path)
    paper_reader_root = _fake_paper_reader_root(tmp_path)
    calls = 0

    def runner(_argv: tuple[str, ...], _cwd: Path, _timeout_seconds: int):
        nonlocal calls
        calls += 1
        raise OSError("injected launcher result loss")

    with pytest.raises(BatchRuntimeError) as first_error:
        recover_run(
            ready.run_dir,
            request_id=REQUEST_RECOVER_STARTED,
            now="2026-07-10T00:02:03Z",
            paper_reader_root=paper_reader_root,
            reconciliation_runner=runner,
            reconciliation_timeout_seconds=45,
        )
    assert first_error.value.code == "reconciliation_child_failed"
    event_count = len(load_run_view(ready.run_dir).events)

    timeout_seconds = 45
    if drift == "paper_reader_root":
        (paper_reader_root / "SKILL.md").write_text(
            "# paper_reader V2 changed\n",
            encoding="utf-8",
        )
    else:
        timeout_seconds = 46

    with pytest.raises(BatchRuntimeError) as replay_error:
        recover_run(
            ready.run_dir,
            request_id=REQUEST_RECOVER_STARTED,
            now="2026-07-10T00:02:03Z",
            paper_reader_root=paper_reader_root,
            reconciliation_runner=runner,
            reconciliation_timeout_seconds=timeout_seconds,
        )

    assert replay_error.value.code == "idempotency_conflict"
    assert calls == 1
    assert len(load_run_view(ready.run_dir).events) == event_count


def test_recover_reconciliation_event_uses_trusted_batch_time_not_future_child_time(
    tmp_path: Path,
) -> None:
    ready, _claimed, authorization_path, authorization = _started_write(tmp_path)
    paper_reader_root = _fake_paper_reader_root(tmp_path)

    def runner(_argv: tuple[str, ...], _cwd: Path, _timeout_seconds: int):
        reconciliation_path = _make_reconciliation_matches(
            ready,
            authorization_path,
            authorization,
            note_keys=("NOTE1",),
        )
        return _child_result(
            reconciliation_path=reconciliation_path,
            authorization_path=authorization_path,
            outcome="verified",
            matched_note_keys=("NOTE1",),
            created_at="2099-01-01T00:00:00.000000Z",
        )

    recover_run(
        ready.run_dir,
        request_id=REQUEST_RECOVER_STARTED,
        now="2026-07-10T00:02:03Z",
        paper_reader_root=paper_reader_root,
        reconciliation_runner=runner,
        reconciliation_timeout_seconds=45,
    )

    view = load_run_view(ready.run_dir)
    assert view.events[-1].data.kind == "write.reconciled"
    assert view.events[-1].occurred_at == "2026-07-10T00:02:03Z"


def test_append_rejects_reusing_an_older_write_identity_before_poisoning_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ready = _ready_write_run(tmp_path)
    first = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    release_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=first["claim_id"],
        lease_token=first["lease_token"],
        write_attempt_id=first["write_attempt_id"],
        request_id=REQUEST_RELEASE_ONE,
        now="2026-07-10T00:00:04Z",
    )
    second = claim_write(
        ready.run_dir,
        writer_id="writer-2",
        request_id=REQUEST_CLAIM_TWO,
        now="2026-07-10T00:00:05Z",
    ).result
    release_write(
        ready.run_dir,
        "001",
        writer_id="writer-2",
        claim_id=second["claim_id"],
        lease_token=second["lease_token"],
        write_attempt_id=second["write_attempt_id"],
        request_id=REQUEST_RELEASE_TWO,
        now="2026-07-10T00:00:06Z",
    )
    before = load_run_view(ready.run_dir)
    event_count = len(before.events)
    identities = iter([UUID(first["claim_id"]), UUID(first["write_attempt_id"])])
    monkeypatch.setattr("paper_reader_batch.v2_write.uuid4", lambda: next(identities))

    with pytest.raises(BatchRuntimeError) as exc_info:
        claim_write(
            ready.run_dir,
            writer_id="writer-3",
            request_id=REQUEST_REUSED_HISTORY,
            now="2026-07-10T00:00:07Z",
        )

    assert exc_info.value.code == "journal_corrupt"
    after = load_run_view(ready.run_dir)
    assert len(after.events) == event_count
    assert after.state.items[0].write_status == "queued"
