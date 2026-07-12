from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paper_reader.contracts import (
    PaperReaderCommandResult,
    PaperReaderVerification,
    PaperReaderWriteAuthorization,
)
from paper_reader.public_cli import app
from paper_reader.storage import canonical_json_bytes

from test_v2_zotero_authorization import (
    _authorize,
    _candidate,
    _filesystem_snapshot,
    _inject_root_swap_at_anchor_recheck,
    _install_unsafe_artifact_layout,
)
from test_v2_zotero_candidate import InMemoryZoteroProvider, _build


def _module():
    module_name = "paper_reader.zotero_verification"
    assert importlib.util.find_spec(module_name) is not None, "Zotero verification module is missing"
    return importlib.import_module(module_name)


def _authorized(tmp_path: Path):
    candidate_path, provider = _candidate(tmp_path)
    authorized = _authorize(candidate_path, provider, ttl_seconds=1)
    authorization_path = authorized.authorization_path
    return authorization_path, authorized.authorization


def _note_snapshot(
    authorization: PaperReaderWriteAuthorization,
    *,
    requested_key: str = "NOTE1",
    snapshot_key: str | None = None,
    parent: str | None = None,
    content: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, object]:
    key = snapshot_key or requested_key
    return {
        "key": key,
        "version": 1,
        "library": {"type": "user", "id": 0, "name": "My Library"},
        "links": {"self": {"href": f"http://127.0.0.1:23119/api/users/0/items/{key}"}},
        "meta": {},
        "data": {
            "key": key,
            "version": 1,
            "itemType": "note",
            "parentItem": parent or authorization.target.parent_key,
            "note": authorization.content_html if content is None else content,
            "tags": [
                {"tag": tag, "type": 1}
                for tag in (list(authorization.tags) if tags is None else tags)
            ],
            "collections": [],
            "relations": {},
            "dateAdded": "2026-07-10T12:05:01Z",
            "dateModified": "2026-07-10T12:05:01Z",
        },
    }


def _verify(authorization_path: Path, provider, *, note_key: str = "NOTE1"):
    return _module().verify_zotero_authorization(
        authorization_path,
        note_key=note_key,
        provider=provider,
    )


def _rewrite_bound_terminal_ref(run_dir: Path, *, role: str, raw: bytes) -> None:
    run_path = run_dir / "run.json"
    run = json.loads(run_path.read_bytes())
    for artifact in run["artifacts"]:
        if artifact["role"] == role:
            artifact["sha256"] = hashlib.sha256(raw).hexdigest()
            artifact["size_bytes"] = len(raw)
    run_path.write_bytes(canonical_json_bytes(run))


def _mutate_verification_terminal(
    verification_path: Path,
    verification_dir: Path,
    *,
    case: str,
) -> None:
    record_path = verification_dir / "record.json"
    if case == "extra_file":
        (verification_dir / "extra.bin").write_bytes(b"unbound")
        return
    if case == "nested_directory":
        nested = verification_dir / "nested"
        nested.mkdir()
        (nested / "member.bin").write_bytes(b"unbound")
        return
    if case == "record_mismatch":
        record_path.write_bytes(b"{}")
        return
    if case in {"record_symlink", "record_hardlink"}:
        outside = verification_dir.parent / f"outside-{case}.json"
        outside.write_bytes(record_path.read_bytes())
        record_path.unlink()
        if case == "record_symlink":
            record_path.symlink_to(outside)
        else:
            os.link(outside, record_path)
        return
    if case == "role_filename_swap":
        source = verification_path if verification_path.exists() else record_path
        payload = json.loads(source.read_bytes())
        artifacts = {item["role"]: item for item in payload["artifacts"]}
        authorization = artifacts["authorization_snapshot"]
        note = artifacts["zotero_note_readback"]
        for field in ("path", "sha256", "size_bytes", "media_type"):
            authorization[field], note[field] = note[field], authorization[field]
        payload["authorization"] = copy.deepcopy(authorization)
        payload["note_snapshot"] = copy.deepcopy(note)
        rewritten = canonical_json_bytes(payload)
        record_path.write_bytes(rewritten)
        if verification_path.exists():
            verification_path.write_bytes(rewritten)
            _rewrite_bound_terminal_ref(
                verification_path.parents[2],
                role="zotero_verification",
                raw=rewritten,
            )
        return
    raise AssertionError(case)


def test_verify_expired_authorization_saves_exact_passed_readback_and_publishes_run(
    tmp_path: Path,
) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    snapshot = _note_snapshot(authorization)
    provider = InMemoryZoteroProvider(notes={"NOTE1": snapshot})

    verified = _verify(authorization_path, provider)

    verification_path = verified.verification_path
    verification = PaperReaderVerification.model_validate_json(
        verification_path.read_bytes()
    )
    assert verification.verified is True
    assert verification.note_key == "NOTE1"
    assert verification.authorization_digest == verified.authorization_digest
    assert verification.note_snapshot in verification.artifacts
    assert verification.checks_snapshot in verification.artifacts
    assert all(check.passed for check in verification.checks)
    assert sorted(path.name for path in verified.verification_dir.iterdir()) == [
        "authorization.json",
        "checks.json",
        "note.json",
        "record.json",
    ]
    assert json.loads((verified.verification_dir / "note.json").read_text()) == snapshot
    run_dir = authorization_path.parent.parent
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "published"
    assert [item["role"] for item in run["artifacts"]].count("zotero_verification") == 1


def test_verification_main_artifact_uses_authorization_and_note_key_topology(
    tmp_path: Path,
) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    provider = InMemoryZoteroProvider(notes={"NOTE1": _note_snapshot(authorization)})

    verified = _verify(authorization_path, provider)

    expected = (
        verified.run_dir
        / "verifications"
        / authorization.authorization_id
        / "NOTE1.json"
    )
    assert verified.verification_path == expected
    assert expected.is_file()
    assert verified.verification_dir == expected.with_suffix("")


@pytest.mark.parametrize(
    "case",
    [
        "root_symlink",
        "intermediate_symlink",
        "sidecar_symlink",
        "main_symlink",
        "main_hardlink",
    ],
)
def test_verify_rejects_unsafe_deterministic_paths_before_provider_or_publication(
    case: str,
    tmp_path: Path,
) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    run_dir = authorization_path.parent.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    _install_unsafe_artifact_layout(
        run_dir=run_dir,
        outside=outside,
        root_name="verifications",
        parent_parts=(authorization.authorization_id,),
        stem="NOTE1",
        case=case,
    )
    run_before = _filesystem_snapshot(run_dir)
    outside_before = _filesystem_snapshot(outside)

    class ProviderSpy:
        calls = 0

        def get_note(self, _note_key: str):
            self.calls += 1
            raise AssertionError("unsafe verification path reached provider")

    provider = ProviderSpy()

    with pytest.raises(Exception) as exc_info:
        _verify(authorization_path, provider)

    assert getattr(exc_info.value, "code", None) == "unsafe_artifact_path"
    assert provider.calls == 0
    assert _filesystem_snapshot(run_dir) == run_before
    assert _filesystem_snapshot(outside) == outside_before


def test_verify_root_swap_before_sidecar_publication_cannot_escape_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    run_dir = authorization_path.parent.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    provider_calls: list[str] = []

    class ProviderSpy:
        def get_note(self, note_key: str):
            provider_calls.append("note")
            return _note_snapshot(authorization, requested_key=note_key)

    detached, state = _inject_root_swap_at_anchor_recheck(
        monkeypatch,
        run_dir=run_dir,
        root_name="verifications",
        outside=outside,
        provider_calls=provider_calls,
    )
    run_before = (run_dir / "run.json").read_bytes()
    outside_before = _filesystem_snapshot(outside)

    with pytest.raises(Exception) as exc_info:
        _verify(authorization_path, ProviderSpy())

    assert getattr(exc_info.value, "code", None) == "unsafe_artifact_path"
    assert state == {"triggered": True, "calls_at_swap": ("note",)}
    assert provider_calls == ["note"]
    assert (run_dir / "run.json").read_bytes() == run_before
    assert _filesystem_snapshot(outside) == outside_before
    detached_parent = detached / authorization.authorization_id
    assert detached_parent.is_dir()
    assert not (detached_parent / "NOTE1").exists()
    assert not os.path.lexists(detached_parent / "NOTE1.json")
    assert not (outside / authorization.authorization_id).exists()
    assert not tuple(run_dir.glob(".*.staging"))


@pytest.mark.parametrize("note_key", ["NOTE:1", "NOTE/1", "../NOTE1", "NOTE..1"])
def test_verify_rejects_nonportable_note_key_before_provider_access(
    note_key: str,
    tmp_path: Path,
) -> None:
    authorization_path, _authorization = _authorized(tmp_path)

    class ProviderSpy:
        called = False

        def get_note(self, _note_key: str):
            self.called = True
            raise AssertionError("invalid note key reached provider")

    provider = ProviderSpy()

    with pytest.raises(Exception) as exc_info:
        _verify(authorization_path, provider, note_key=note_key)

    assert getattr(exc_info.value, "code", None) == "invalid_note_key"
    assert provider.called is False


def _mutated_case(
    authorization: PaperReaderWriteAuthorization,
    case: str,
) -> tuple[dict[str, object], str]:
    snapshot = _note_snapshot(authorization)
    data = snapshot["data"]
    assert isinstance(data, dict)
    if case == "key":
        snapshot["key"] = "WRONGKEY"
        data["key"] = "WRONGKEY"
        return snapshot, "note_key"
    if case == "parent":
        data["parentItem"] = "WRONGPARENT"
        return snapshot, "parent_key"
    if case == "title":
        data["note"] = re.sub(
            r"<h1>.*?</h1>",
            "<h1>Wrong Title</h1>",
            str(data["note"]),
            count=1,
            flags=re.DOTALL,
        )
        return snapshot, "note_title"
    if case == "missing_tag":
        data["tags"] = [{"tag": authorization.tags[0], "type": 1}]
        return snapshot, "tag_set"
    if case == "extra_tag":
        data["tags"] = [*data["tags"], {"tag": "unexpected", "type": 1}]
        return snapshot, "tag_set"
    if case == "missing_heading":
        heading = authorization.required_headings[0]
        data["note"] = str(data["note"]).replace(f"<h2>{heading}</h2>", "", 1)
        return snapshot, "required_headings"
    if case == "forbidden_heading":
        data["note"] = str(data["note"]) + f"<h2>{authorization.forbidden_headings[0]}</h2>"
        return snapshot, "forbidden_headings"
    if case == "hash":
        data["note"] = str(data["note"]).replace("Tags:", "Changed tags marker:", 1)
        return snapshot, "content_sha256"
    if case == "length":
        data["note"] = "<h1>too short</h1>"
        return snapshot, "minimum_content_length"
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "key",
        "parent",
        "title",
        "missing_tag",
        "extra_tag",
        "missing_heading",
        "forbidden_heading",
        "hash",
        "length",
    ],
)
def test_verify_matrix_persists_only_blocked_verification(
    case: str,
    tmp_path: Path,
) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    snapshot, failed_check = _mutated_case(authorization, case)
    provider = InMemoryZoteroProvider(notes={"NOTE1": snapshot})

    verified = _verify(authorization_path, provider)

    verification = verified.verification
    checks = {check.name: check for check in verification.checks}
    assert verification.verified is False
    assert verification.gate.status == "blocked"
    assert checks[failed_check].passed is False
    run_dir = authorization_path.parent.parent
    run = json.loads((run_dir / "run.json").read_text())
    bound_records = [item for item in run["artifacts"] if item["role"] == "zotero_verification"]
    assert len(bound_records) == 1
    bound_verification = PaperReaderVerification.model_validate_json(
        (run_dir / bound_records[0]["path"]).read_bytes()
    )
    assert bound_verification.verified is False
    assert json.loads((verified.verification_dir / "note.json").read_text()) == snapshot


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_top_key",
        "missing_data_key",
        "wrong_top_key_only",
        "wrong_data_key_only",
    ],
)
def test_verify_requires_both_snapshot_keys_to_exist_and_match_exactly(
    mutation: str,
    tmp_path: Path,
) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    snapshot = _note_snapshot(authorization)
    data = snapshot["data"]
    assert isinstance(data, dict)
    if mutation == "missing_top_key":
        del snapshot["key"]
    elif mutation == "missing_data_key":
        del data["key"]
    elif mutation == "wrong_top_key_only":
        snapshot["key"] = "WRONGKEY"
    else:
        data["key"] = "WRONGKEY"
    provider = InMemoryZoteroProvider(notes={"NOTE1": snapshot})

    verified = _verify(authorization_path, provider)

    checks = {check.name: check for check in verified.verification.checks}
    assert verified.verification.verified is False
    assert checks["note_key"].passed is False


@pytest.mark.parametrize("operation", ["verify", "reconcile"])
def test_old_authorization_remains_usable_after_new_suffix_candidate_updates_run_target(
    operation: str,
    tmp_path: Path,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorized = _authorize(candidate_path, provider, ttl_seconds=1)
    authorization_path = authorized.authorization_path
    note = _note_snapshot(authorized.authorization)
    provider.children = [note]
    provider.notes = {"NOTE1": note}
    second_candidate = _build(authorized.run_dir, provider)
    assert second_candidate.candidate.note_title != authorized.authorization.note_title

    if operation == "verify":
        result = _verify(authorization_path, provider)
        assert result.verification.verified is True
    else:
        from paper_reader.zotero_reconciliation import reconcile_zotero_authorization

        result = reconcile_zotero_authorization(authorization_path, provider=provider)
        assert result.reconciliation.outcome == "verified"


def test_verify_is_idempotent_for_same_authorization_and_note_key(tmp_path: Path) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    first_snapshot = _note_snapshot(authorization)
    provider = InMemoryZoteroProvider(notes={"NOTE1": first_snapshot})

    first = _verify(authorization_path, provider)
    provider.notes["NOTE1"] = _note_snapshot(
        authorization,
        content="<h1>changed after terminal verification</h1>",
    )
    second = _verify(authorization_path, provider)

    assert second.verification_dir == first.verification_dir
    assert second.verification == first.verification
    assert second.replayed is True


_TERMINAL_SIDECAR_CASES = (
    "extra_file",
    "nested_directory",
    "record_mismatch",
    "record_symlink",
    "record_hardlink",
    "role_filename_swap",
)


@pytest.mark.parametrize("case", _TERMINAL_SIDECAR_CASES)
@pytest.mark.parametrize("recovery_mode", ["bound_replay", "unbound_main"])
def test_verify_replay_and_unbound_main_recovery_reject_tampered_closed_sidecar(
    case: str,
    recovery_mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    authorization_path, authorization = _authorized(tmp_path)
    provider = InMemoryZoteroProvider(
        notes={"NOTE1": _note_snapshot(authorization)}
    )
    if recovery_mode == "bound_replay":
        first = _verify(authorization_path, provider)
    else:
        original_write = module.atomic_write_json
        failed = False

        def fail_run_binding_once(path: Path, value, **kwargs):
            nonlocal failed
            if Path(path).name == "run.json" and not failed:
                failed = True
                raise OSError("injected verification run binding failure")
            return original_write(path, value, **kwargs)

        monkeypatch.setattr(module, "atomic_write_json", fail_run_binding_once)
        with pytest.raises(module.ZoteroVerificationError) as fault:
            _verify(authorization_path, provider)
        assert fault.value.code == "verification_status_update_failed"
        monkeypatch.setattr(module, "atomic_write_json", original_write)
        verification_path = (
            authorization_path.parent.parent
            / "verifications"
            / authorization.authorization_id
            / "NOTE1.json"
        )
        first = type(
            "UnboundVerification",
            (),
            {
                "verification_path": verification_path,
                "verification_dir": verification_path.with_suffix(""),
            },
        )()

    _mutate_verification_terminal(
        first.verification_path,
        first.verification_dir,
        case=case,
    )

    class ProviderMustNotRun:
        def get_note(self, _note_key: str):
            raise AssertionError("tampered terminal verification reached provider")

    with pytest.raises(module.ZoteroVerificationError) as exc_info:
        _verify(authorization_path, ProviderMustNotRun())

    assert exc_info.value.code == "verification_tampered"


@pytest.mark.parametrize(
    "case",
    [
        "extra_file",
        "nested_directory",
        "record_symlink",
        "record_hardlink",
        "role_filename_swap",
    ],
)
def test_verify_sidecar_only_orphan_recovery_rejects_tampered_closed_sidecar(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    authorization_path, authorization = _authorized(tmp_path)
    provider = InMemoryZoteroProvider(
        notes={"NOTE1": _note_snapshot(authorization)}
    )
    original_write = module.atomic_write_json

    def fail_run_binding(path: Path, value, **kwargs):
        if Path(path).name == "run.json":
            raise OSError("injected verification run binding failure")
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(module, "atomic_write_json", fail_run_binding)
    with pytest.raises(module.ZoteroVerificationError) as fault:
        _verify(authorization_path, provider)
    assert fault.value.code == "verification_status_update_failed"
    monkeypatch.setattr(module, "atomic_write_json", original_write)
    verification_path = (
        authorization_path.parent.parent
        / "verifications"
        / authorization.authorization_id
        / "NOTE1.json"
    )
    verification_dir = verification_path.with_suffix("")
    verification_path.unlink()
    _mutate_verification_terminal(
        verification_path,
        verification_dir,
        case=case,
    )

    class ProviderMustNotRun:
        def get_note(self, _note_key: str):
            raise AssertionError("tampered terminal verification reached provider")

    with pytest.raises(module.ZoteroVerificationError) as exc_info:
        _verify(authorization_path, ProviderMustNotRun())

    assert exc_info.value.code == "verification_tampered"


def test_verify_rejects_authorization_or_candidate_tamper_before_readback(tmp_path: Path) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    candidate_snapshot = authorization_path.with_suffix("") / "candidate.json"
    candidate_snapshot.write_bytes(candidate_snapshot.read_bytes() + b"\n")
    provider = InMemoryZoteroProvider(
        notes={"NOTE1": _note_snapshot(authorization)}
    )

    with pytest.raises(Exception) as exc_info:
        _verify(authorization_path, provider)

    assert getattr(exc_info.value, "code", None) == "authorization_tampered"
    run_dir = authorization_path.parent.parent
    assert not (run_dir / "verifications").exists()


def test_verify_cli_emits_one_structured_passed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    authorization_path, authorization = _authorized(tmp_path)
    provider = InMemoryZoteroProvider(
        notes={"NOTE1": _note_snapshot(authorization)}
    )
    monkeypatch.setattr(module, "LocalApiZoteroReadProvider", lambda: provider)

    result = CliRunner().invoke(
        app,
        ["zotero", "verify", str(authorization_path), "--note-key", "NOTE1"],
    )

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    assert payload["code"] == "verified"
    assert payload["data"]["verified"] is True
    assert Path(payload["data"]["verification_path"]).is_file()


def test_verify_provider_failure_keeps_original_exception_only_as_cause(
    tmp_path: Path,
) -> None:
    module = _module()
    authorization_path, _authorization = _authorized(tmp_path)
    secret = "secret-verify-provider-token"

    class FailingProvider:
        def get_note(self, _note_key: str):
            raise OSError(secret)

    with pytest.raises(module.ZoteroVerificationError) as exc_info:
        _verify(authorization_path, FailingProvider())

    assert exc_info.value.code == "zotero_read_failed"
    assert str(exc_info.value) == "read-only Zotero note readback failed"
    assert exc_info.value.data == {}
    assert isinstance(exc_info.value.__cause__, OSError)
    assert secret in str(exc_info.value.__cause__)


def test_verify_cli_does_not_leak_provider_failure_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    authorization_path, _authorization = _authorized(tmp_path)
    secret = "secret-verify-provider-token"

    class FailingProvider:
        def get_note(self, _note_key: str):
            raise OSError(secret)

    monkeypatch.setattr(module, "LocalApiZoteroReadProvider", FailingProvider)

    result = CliRunner().invoke(
        app,
        ["zotero", "verify", str(authorization_path), "--note-key", "NOTE1"],
    )

    assert result.exit_code == 1
    assert secret not in result.stdout
    assert secret not in result.stderr
    payload = json.loads(result.stdout)
    PaperReaderCommandResult.model_validate(payload)
    assert payload["code"] == "zotero_read_failed"
    assert payload["message"] == "read-only Zotero note readback failed"
    assert payload["data"] == {}


def test_concurrent_verify_converges_on_one_bound_terminal_tree(tmp_path: Path) -> None:
    authorization_path, authorization = _authorized(tmp_path)
    provider = InMemoryZoteroProvider(
        notes={"NOTE1": _note_snapshot(authorization)}
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda _index: _verify(authorization_path, provider),
                range(2),
            )
        )

    assert outcomes[0].verification_dir == outcomes[1].verification_dir
    assert {item.replayed for item in outcomes} == {False, True}
    run_dir = authorization_path.parent.parent
    run = json.loads((run_dir / "run.json").read_text())
    assert [item["role"] for item in run["artifacts"]].count("zotero_verification") == 1


def test_verification_size_and_run_binding_faults_do_not_create_false_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    from paper_reader.resource_policy import V2_RESOURCE_POLICY

    size_dir = tmp_path / "size"
    size_dir.mkdir()
    authorization_path, authorization = _authorized(size_dir)
    provider = InMemoryZoteroProvider(
        notes={"NOTE1": _note_snapshot(authorization)}
    )
    run_dir = authorization_path.parent.parent
    run_before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(
        module,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, run_max_bytes=1),
    )

    with pytest.raises(Exception) as size_error:
        _verify(authorization_path, provider)

    assert getattr(size_error.value, "code", None) == "run_size_limit_exceeded"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "verifications").exists()

    monkeypatch.setattr(module, "V2_RESOURCE_POLICY", V2_RESOURCE_POLICY)
    fault_dir = tmp_path / "fault"
    fault_dir.mkdir()
    authorization_path, authorization = _authorized(fault_dir)
    provider = InMemoryZoteroProvider(
        notes={"NOTE1": _note_snapshot(authorization)}
    )
    run_dir = authorization_path.parent.parent
    run_before = (run_dir / "run.json").read_bytes()
    original_write = module.atomic_write_json
    failed = False

    def fail_once(path: Path, value, **kwargs):
        nonlocal failed
        if Path(path).name == "run.json" and not failed:
            failed = True
            raise OSError("injected verification run binding failure")
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(module, "atomic_write_json", fail_once)

    with pytest.raises(Exception) as fault_error:
        _verify(authorization_path, provider)

    assert getattr(fault_error.value, "code", None) == "verification_status_update_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    orphan_main = (
        run_dir
        / "verifications"
        / authorization.authorization_id
        / "NOTE1.json"
    )
    assert orphan_main.is_file()

    retry = _verify(authorization_path, provider)

    assert retry.verification_path == orphan_main
    assert retry.replayed is True
    run = json.loads((run_dir / "run.json").read_text())
    bound = [item for item in run["artifacts"] if item["role"] == "zotero_verification"]
    assert len(bound) == 1
    assert run_dir / bound[0]["path"] == retry.verification_path
