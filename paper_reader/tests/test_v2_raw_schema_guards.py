from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

import paper_reader.candidate_builder as candidate_builder_module
import paper_reader.local_publish as local_publish_module
import paper_reader.review_package as review_package_module
import paper_reader.zotero_authorization as zotero_authorization_module
import paper_reader.zotero_authorization_loader as zotero_authorization_loader_module
import paper_reader.zotero_candidate as zotero_candidate_module
import paper_reader.zotero_reconciliation as zotero_reconciliation_module
import paper_reader.zotero_verification as zotero_verification_module
import paper_reader.zotero_lock as zotero_lock_module
from paper_reader.candidate_integrity import LocalPublicationError
from paper_reader.contracts import PaperReaderCommandResult
from paper_reader.public_cli import app
from paper_reader.storage import canonical_json_bytes
from paper_reader.v2_loader import RunLoadError
from paper_reader.zotero_authorization_loader import (
    ZoteroAuthorizationBindingError,
    inspect_authorization_target,
)

from test_v2_local_publication import _built_candidate, _sealed_run
from test_v2_review_package import _prepared_run, _write_summary_and_review
from test_v2_zotero_authorization import (
    _authorize,
    _candidate,
    _filesystem_snapshot,
)
from test_v2_zotero_candidate import InMemoryZoteroProvider, _sealed_zotero_run
from test_v2_zotero_verification import _note_snapshot


UNSUPPORTED_SCHEMA_CASES = ("missing", "v1", "unknown", "valid_non_object")


def _unsupported_schema_version(expected: str, case: str) -> str | None:
    if case == "missing":
        return None
    suffix = "v1" if case == "v1" else "v3"
    return f"{expected.removesuffix('v2')}{suffix}"


def _write_unsupported_schema_case(path: Path, expected: str, case: str) -> None:
    if case == "valid_non_object":
        path.write_bytes(canonical_json_bytes([]))
        return
    _replace_schema_version(path, _unsupported_schema_version(expected, case))


def _replace_schema_version(path: Path, schema_version: str | None) -> None:
    payload = json.loads(path.read_bytes())
    if schema_version is None:
        payload.pop("schema_version", None)
    else:
        payload["schema_version"] = schema_version
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _forbidden_lock(*_args, **_kwargs):
    raise AssertionError("schema guard must run before lock acquisition")


def _replace_sealed_review_snapshot_schema(
    run_dir: Path,
    *,
    artifact_role: str,
    replacement: object,
) -> Path:
    run_payload = json.loads((run_dir / "run.json").read_bytes())
    package_ref = next(
        item for item in run_payload["artifacts"] if item["role"] == "review_package"
    )
    package_path = run_dir / package_ref["path"]
    package_payload = json.loads(package_path.read_bytes())
    snapshot_ref = next(
        item for item in package_payload["artifacts"] if item["role"] == artifact_role
    )
    snapshot_path = run_dir / snapshot_ref["path"]
    snapshot_bytes = canonical_json_bytes(replacement)
    snapshot_path.write_bytes(snapshot_bytes)
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    updated_ref = {
        **snapshot_ref,
        "sha256": snapshot_sha256,
        "size_bytes": len(snapshot_bytes),
    }
    package_payload["artifacts"] = [
        updated_ref if item["role"] == artifact_role else item
        for item in package_payload["artifacts"]
    ]
    if artifact_role == "summary_snapshot":
        package_payload["summary"] = updated_ref
        package_payload["summary_sha256"] = snapshot_sha256
    else:
        package_payload["review"] = updated_ref
        package_payload["review_sha256"] = snapshot_sha256
    package_bytes = canonical_json_bytes(package_payload)
    package_path.write_bytes(package_bytes)
    package_ref.update(
        sha256=hashlib.sha256(package_bytes).hexdigest(),
        size_bytes=len(package_bytes),
    )
    (run_dir / "run.json").write_bytes(canonical_json_bytes(run_payload))
    return snapshot_path


def _replace_authorization_candidate_schema(
    authorization_path: Path,
    *,
    replacement: object,
) -> Path:
    authorization = json.loads(authorization_path.read_bytes())
    run_dir = authorization_path.parent.parent
    candidate_path = run_dir / authorization["candidate"]["path"]
    candidate_bytes = canonical_json_bytes(replacement)
    candidate_path.write_bytes(candidate_bytes)
    candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    updated_ref = {
        **authorization["candidate"],
        "sha256": candidate_sha256,
        "size_bytes": len(candidate_bytes),
    }
    authorization["candidate"] = updated_ref
    authorization["candidate_digest"] = candidate_sha256
    authorization["artifacts"] = [
        updated_ref if item["role"] == "candidate_snapshot" else item
        for item in authorization["artifacts"]
    ]
    authorization_bytes = canonical_json_bytes(authorization)
    authorization_path.write_bytes(authorization_bytes)
    (authorization_path.with_suffix("") / "record.json").write_bytes(
        authorization_bytes
    )
    return candidate_path


def _replace_bound_artifact_schema(
    run_dir: Path,
    *,
    role: str,
    expected: str,
    schema_case: str,
) -> Path:
    run_payload = json.loads((run_dir / "run.json").read_bytes())
    artifact_ref = next(item for item in run_payload["artifacts"] if item["role"] == role)
    artifact_path = run_dir / artifact_ref["path"]
    _write_unsupported_schema_case(artifact_path, expected, schema_case)
    artifact_bytes = artifact_path.read_bytes()
    artifact_ref["sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
    artifact_ref["size_bytes"] = len(artifact_bytes)
    (run_dir / "run.json").write_bytes(canonical_json_bytes(run_payload))
    return artifact_path


def _replace_candidate_review_package_schema(
    candidate_path: Path,
    *,
    schema_case: str,
) -> Path:
    run_dir = candidate_path.parent.parent.parent
    candidate_payload = json.loads(candidate_path.read_bytes())
    package_ref = next(
        item
        for item in candidate_payload["artifacts"]
        if item["role"] == "review_package_snapshot"
    )
    package_path = run_dir / package_ref["path"]
    _write_unsupported_schema_case(
        package_path,
        "paper_reader.review-package.v2",
        schema_case,
    )
    package_bytes = package_path.read_bytes()
    package_ref["sha256"] = hashlib.sha256(package_bytes).hexdigest()
    package_ref["size_bytes"] = len(package_bytes)
    candidate_payload["sealed_review"] = package_ref
    candidate_bytes = canonical_json_bytes(candidate_payload)
    candidate_path.write_bytes(candidate_bytes)

    run_payload = json.loads((run_dir / "run.json").read_bytes())
    candidate_relative = candidate_path.relative_to(run_dir).as_posix()
    candidate_ref = next(
        item
        for item in run_payload["artifacts"]
        if item["role"] == "candidate" and item["path"] == candidate_relative
    )
    candidate_ref["sha256"] = hashlib.sha256(candidate_bytes).hexdigest()
    candidate_ref["size_bytes"] = len(candidate_bytes)
    (run_dir / "run.json").write_bytes(canonical_json_bytes(run_payload))
    return package_path


@pytest.mark.parametrize("artifact_name", ["summary.json", "review.json"])
@pytest.mark.parametrize("schema_case", UNSUPPORTED_SCHEMA_CASES)
def test_review_inputs_reject_unsupported_schema_before_lock_or_mutation(
    artifact_name: str,
    schema_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    artifact_path = run_dir / artifact_name
    expected = (
        "paper_reader.summary.v2"
        if artifact_name == "summary.json"
        else "paper_reader.review.v2"
    )
    _write_unsupported_schema_case(artifact_path, expected, schema_case)
    before = _filesystem_snapshot(run_dir)
    monkeypatch.setattr(review_package_module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(RunLoadError) as validate_error:
        review_package_module.validate_review_run(run_dir)
    with pytest.raises(RunLoadError) as seal_error:
        review_package_module.seal_review_run(run_dir)

    assert validate_error.value.code == "unsupported_run_schema"
    assert validate_error.value.manifest_path == artifact_path
    assert seal_error.value.code == "unsupported_run_schema"
    assert seal_error.value.manifest_path == artifact_path
    assert _filesystem_snapshot(run_dir) == before
    assert not (run_dir / "reviews").exists()


@pytest.mark.parametrize("schema_case", UNSUPPORTED_SCHEMA_CASES)
def test_local_candidate_input_rejects_unsupported_schema_before_lock_or_publish(
    schema_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    _write_unsupported_schema_case(
        candidate_path,
        "paper_reader.candidate.v2",
        schema_case,
    )
    before = _filesystem_snapshot(run_dir)
    monkeypatch.setattr(local_publish_module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(RunLoadError) as exc_info:
        local_publish_module.publish_local_candidate(candidate_path)

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == candidate_path
    assert _filesystem_snapshot(run_dir) == before
    assert not (tmp_path / "paper_note.md").exists()
    assert not (run_dir / "receipts").exists()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_local_candidate_preflight_rejects_links_before_lock_or_publish(
    link_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    detached = tmp_path / "detached-candidate.json"
    if link_kind == "symlink":
        candidate_path.rename(detached)
        candidate_path.symlink_to(detached)
    else:
        os.link(candidate_path, detached)
    before = _filesystem_snapshot(run_dir)
    monkeypatch.setattr(local_publish_module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(LocalPublicationError) as exc_info:
        local_publish_module.publish_local_candidate(candidate_path)

    assert exc_info.value.code == "candidate_tampered"
    assert _filesystem_snapshot(run_dir) == before
    assert not (tmp_path / "paper_note.md").exists()
    assert not (run_dir / "publication-intent.json").exists()


def test_local_candidate_publish_rejects_cloned_run_swap_before_lock_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    replacement = tmp_path / "replacement-local-publish-run"
    detached = tmp_path / "detached-local-publish-run"
    shutil.copytree(run_dir, replacement, ignore=shutil.ignore_patterns(".run.lock"))
    original_locked_v2_run = local_publish_module.locked_v2_run
    swapped = False

    @contextmanager
    def replace_before_lock(path: Path, **kwargs):
        nonlocal swapped
        if not swapped:
            run_dir.rename(detached)
            replacement.rename(run_dir)
            swapped = True
        with original_locked_v2_run(path, **kwargs) as loaded:
            yield loaded

    monkeypatch.setattr(local_publish_module, "locked_v2_run", replace_before_lock)

    with pytest.raises(Exception) as exc_info:
        local_publish_module.publish_local_candidate(candidate_path)

    assert getattr(exc_info.value, "code", None) == "run_directory_changed"
    assert not (run_dir / ".run.lock").exists()
    assert (detached / ".run.lock").is_file()
    assert not (tmp_path / "paper_note.md").exists()
    assert not (run_dir / "publication-intent.json").exists()
    assert not (detached / "publication-intent.json").exists()


def test_local_candidate_swap_after_preflight_is_rejected_before_run_lock_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_run, original_candidate = _built_candidate(tmp_path)
    run_dir = tmp_path / "copied-local-publish-run"
    shutil.copytree(
        original_run,
        run_dir,
        ignore=shutil.ignore_patterns(".run.lock"),
    )
    candidate_path = run_dir / original_candidate.relative_to(original_run)
    original_locked_v2_run = local_publish_module.locked_v2_run
    swapped = False

    @contextmanager
    def swap_candidate_before_lock(path: Path, **kwargs):
        nonlocal swapped
        if not swapped:
            candidate_path.write_bytes(
                b'{"schema_version":"paper_reader.candidate.v1"}'
            )
            swapped = True
        with original_locked_v2_run(path, **kwargs) as loaded:
            yield loaded

    monkeypatch.setattr(
        local_publish_module,
        "locked_v2_run",
        swap_candidate_before_lock,
    )

    with pytest.raises(RunLoadError) as exc_info:
        local_publish_module.publish_local_candidate(candidate_path)

    assert exc_info.value.code == "unsupported_run_schema"
    assert swapped is True
    assert not (run_dir / ".run.lock").exists()
    assert not (tmp_path / "paper_note.md").exists()
    assert not (run_dir / "publication-intent.json").exists()


@pytest.mark.parametrize("schema_case", UNSUPPORTED_SCHEMA_CASES)
def test_zotero_candidate_input_rejects_unsupported_schema_before_lock_or_network(
    schema_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, _build_provider = _candidate(tmp_path)
    _write_unsupported_schema_case(
        candidate_path,
        "paper_reader.candidate.v2",
        schema_case,
    )
    run_dir = candidate_path.parent.parent.parent
    before = _filesystem_snapshot(run_dir)

    class ProviderSpy:
        calls = 0

        def get_parent(self, _item_key: str):
            self.calls += 1
            raise AssertionError("schema guard must run before Zotero read")

        def get_children(self, _parent_key: str):
            self.calls += 1
            raise AssertionError("schema guard must run before Zotero read")

    provider = ProviderSpy()
    monkeypatch.setattr(
        zotero_authorization_module,
        "locked_zotero_parent",
        _forbidden_lock,
    )
    monkeypatch.setattr(
        zotero_authorization_module,
        "locked_v2_run",
        _forbidden_lock,
    )

    with pytest.raises(RunLoadError) as exc_info:
        zotero_authorization_module.authorize_zotero_candidate(
            candidate_path,
            provider=provider,
        )

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == candidate_path
    assert provider.calls == 0
    assert _filesystem_snapshot(run_dir) == before
    assert not (run_dir / "authorizations").exists()


def test_zotero_candidate_swap_after_preflight_is_rejected_before_parent_lock_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, _provider = _candidate(tmp_path)
    run_dir = candidate_path.parent.parent.parent
    original_parent_lock = zotero_authorization_module._locked_parent_for_preflight
    swapped = False

    @contextmanager
    def swap_candidate_before_parent_lock(inspected):
        nonlocal swapped
        if not swapped:
            candidate_path.write_bytes(
                b'{"schema_version":"paper_reader.candidate.v1"}'
            )
            swapped = True
        with original_parent_lock(inspected) as locked:
            yield locked

    class ProviderSpy:
        calls = 0

        def get_parent(self, _item_key: str):
            self.calls += 1
            raise AssertionError("changed candidate reached Zotero read")

        def get_children(self, _parent_key: str):
            self.calls += 1
            raise AssertionError("changed candidate reached Zotero read")

    def forbidden_lock_directory(*_args, **_kwargs):
        raise AssertionError("changed candidate reached parent lock-file open")

    provider = ProviderSpy()
    monkeypatch.setattr(
        zotero_authorization_module,
        "_locked_parent_for_preflight",
        swap_candidate_before_parent_lock,
    )
    monkeypatch.setattr(
        zotero_lock_module,
        "_open_or_create_lock_directory",
        forbidden_lock_directory,
    )

    with pytest.raises(RunLoadError) as exc_info:
        zotero_authorization_module.authorize_zotero_candidate(
            candidate_path,
            provider=provider,
        )

    assert exc_info.value.code == "unsupported_run_schema"
    assert swapped is True
    assert provider.calls == 0
    assert not (run_dir / "authorizations").exists()


@pytest.mark.parametrize("operation", ["verify", "reconcile"])
@pytest.mark.parametrize("schema_case", UNSUPPORTED_SCHEMA_CASES)
def test_authorization_input_rejects_unsupported_schema_before_lock_network_or_mutation(
    operation: str,
    schema_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorization_path = _authorize(candidate_path, provider).authorization_path
    _write_unsupported_schema_case(
        authorization_path,
        "paper_reader.write-authorization.v2",
        schema_case,
    )
    run_dir = authorization_path.parent.parent
    before = _filesystem_snapshot(run_dir)

    class ProviderSpy:
        calls = 0

        def get_children(self, _parent_key: str):
            self.calls += 1
            raise AssertionError("schema guard must run before Zotero read")

        def get_note(self, _note_key: str):
            self.calls += 1
            raise AssertionError("schema guard must run before Zotero read")

    read_provider = ProviderSpy()
    module = (
        zotero_verification_module
        if operation == "verify"
        else zotero_reconciliation_module
    )
    monkeypatch.setattr(module, "locked_zotero_parent", _forbidden_lock)
    monkeypatch.setattr(module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(RunLoadError) as exc_info:
        if operation == "verify":
            zotero_verification_module.verify_zotero_authorization(
                authorization_path,
                note_key="NOTE1",
                provider=read_provider,
            )
        else:
            zotero_reconciliation_module.reconcile_zotero_authorization(
                authorization_path,
                provider=read_provider,
            )

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == authorization_path
    assert read_provider.calls == 0
    assert _filesystem_snapshot(run_dir) == before
    assert not (run_dir / "verifications").exists()
    assert not (run_dir / "reconciliations").exists()


@pytest.mark.parametrize("operation", ["verify", "reconcile"])
@pytest.mark.parametrize("schema_case", UNSUPPORTED_SCHEMA_CASES)
def test_authorization_preflight_rejects_unsupported_run_before_parent_lock(
    operation: str,
    schema_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorization_path = _authorize(candidate_path, provider).authorization_path
    run_dir = authorization_path.parent.parent
    run_manifest = run_dir / "run.json"
    _write_unsupported_schema_case(
        run_manifest,
        "paper_reader.run.v2",
        schema_case,
    )
    before = _filesystem_snapshot(run_dir)
    module = (
        zotero_verification_module
        if operation == "verify"
        else zotero_reconciliation_module
    )
    monkeypatch.setattr(module, "locked_zotero_parent", _forbidden_lock)

    with pytest.raises(RunLoadError) as exc_info:
        if operation == "verify":
            zotero_verification_module.verify_zotero_authorization(
                authorization_path,
                note_key="NOTE1",
                provider=provider,
            )
        else:
            zotero_reconciliation_module.reconcile_zotero_authorization(
                authorization_path,
                provider=provider,
            )

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == run_manifest
    assert _filesystem_snapshot(run_dir) == before
    assert not (run_dir / "verifications").exists()
    assert not (run_dir / "reconciliations").exists()


@pytest.mark.parametrize("operation", ["verify", "reconcile"])
@pytest.mark.parametrize("schema_case", UNSUPPORTED_SCHEMA_CASES)
def test_terminal_artifact_rejects_unsupported_schema_before_lock_or_network(
    operation: str,
    schema_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorized = _authorize(candidate_path, provider)
    authorization_path = authorized.authorization_path
    run_dir = authorization_path.parent.parent
    if operation == "verify":
        note = _note_snapshot(authorized.authorization)
        provider.notes = {"NOTE1": note}
        terminal = zotero_verification_module.verify_zotero_authorization(
            authorization_path,
            note_key="NOTE1",
            provider=provider,
        )
        terminal_path = terminal.verification_path
        terminal_dir = terminal.verification_dir
        terminal_role = "zotero_verification"
        expected_schema = "paper_reader.verification.v2"
        module = zotero_verification_module
    else:
        provider.children = []
        provider.notes = {}
        terminal = zotero_reconciliation_module.reconcile_zotero_authorization(
            authorization_path,
            provider=provider,
        )
        terminal_path = terminal.reconciliation_path
        terminal_dir = terminal.reconciliation_dir
        terminal_role = "zotero_reconciliation"
        expected_schema = "paper_reader.reconciliation.v2"
        module = zotero_reconciliation_module

    _write_unsupported_schema_case(terminal_path, expected_schema, schema_case)
    terminal_bytes = terminal_path.read_bytes()
    (terminal_dir / "record.json").write_bytes(terminal_bytes)
    run_payload = json.loads((run_dir / "run.json").read_bytes())
    terminal_ref = next(
        item for item in run_payload["artifacts"] if item["role"] == terminal_role
    )
    terminal_ref["sha256"] = hashlib.sha256(terminal_bytes).hexdigest()
    terminal_ref["size_bytes"] = len(terminal_bytes)
    (run_dir / "run.json").write_bytes(canonical_json_bytes(run_payload))
    before = _filesystem_snapshot(run_dir)

    class ProviderMustNotRun:
        def get_note(self, _note_key: str):
            raise AssertionError("terminal schema guard must run before Zotero read")

        def get_children(self, _parent_key: str):
            raise AssertionError("terminal schema guard must run before Zotero read")

    monkeypatch.setattr(module, "locked_zotero_parent", _forbidden_lock)
    monkeypatch.setattr(module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(RunLoadError) as exc_info:
        if operation == "verify":
            module.verify_zotero_authorization(
                authorization_path,
                note_key="NOTE1",
                provider=ProviderMustNotRun(),
            )
        else:
            module.reconcile_zotero_authorization(
                authorization_path,
                provider=ProviderMustNotRun(),
            )

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == terminal_path
    assert _filesystem_snapshot(run_dir) == before


@pytest.mark.parametrize("operation", ["verify", "reconcile"])
def test_terminal_sidecar_record_rejects_v1_before_lock_when_main_is_valid_v2(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorized = _authorize(candidate_path, provider)
    authorization_path = authorized.authorization_path
    run_dir = authorization_path.parent.parent
    if operation == "verify":
        provider.notes = {"NOTE1": _note_snapshot(authorized.authorization)}
        terminal = zotero_verification_module.verify_zotero_authorization(
            authorization_path,
            note_key="NOTE1",
            provider=provider,
        )
        terminal_path = terminal.verification_path
        terminal_dir = terminal.verification_dir
        module = zotero_verification_module
    else:
        provider.children = []
        provider.notes = {}
        terminal = zotero_reconciliation_module.reconcile_zotero_authorization(
            authorization_path,
            provider=provider,
        )
        terminal_path = terminal.reconciliation_path
        terminal_dir = terminal.reconciliation_dir
        module = zotero_reconciliation_module

    record_path = terminal_dir / "record.json"
    _write_unsupported_schema_case(record_path, "unused.v2", "v1")
    before = _filesystem_snapshot(run_dir)
    monkeypatch.setattr(module, "locked_zotero_parent", _forbidden_lock)
    monkeypatch.setattr(module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(RunLoadError) as exc_info:
        if operation == "verify":
            module.verify_zotero_authorization(
                authorization_path,
                note_key="NOTE1",
                provider=provider,
            )
        else:
            module.reconcile_zotero_authorization(
                authorization_path,
                provider=provider,
            )

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == record_path
    assert terminal_path.read_bytes() != record_path.read_bytes()
    assert _filesystem_snapshot(run_dir) == before


@pytest.mark.parametrize("operation", ["verify", "reconcile"])
@pytest.mark.parametrize(
    "tamper_case",
    ["record_mismatch", "extra_file", "nested_directory", "record_hardlink", "record_symlink"],
)
def test_authorization_preflight_requires_exact_closed_sidecar_before_lock_or_network(
    operation: str,
    tamper_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorization_path = _authorize(candidate_path, provider).authorization_path
    authorization_dir = authorization_path.with_suffix("")
    record_path = authorization_dir / "record.json"
    if tamper_case == "record_mismatch":
        record = json.loads(record_path.read_bytes())
        record["nonce"] = record["nonce"][::-1]
        record_path.write_bytes(canonical_json_bytes(record))
    elif tamper_case == "extra_file":
        (authorization_dir / "extra.json").write_bytes(b"{}")
    elif tamper_case == "nested_directory":
        (authorization_dir / "nested").mkdir()
    else:
        detached_record = tmp_path / f"detached-{tamper_case}.json"
        record_path.rename(detached_record)
        if tamper_case == "record_hardlink":
            os.link(detached_record, record_path)
        else:
            record_path.symlink_to(detached_record)
    run_dir = authorization_path.parent.parent
    before = _filesystem_snapshot(run_dir)

    class ProviderSpy:
        calls = 0

        def get_children(self, _parent_key: str):
            self.calls += 1
            raise AssertionError("tampered authorization reached Zotero read")

        def get_note(self, _note_key: str):
            self.calls += 1
            raise AssertionError("tampered authorization reached Zotero read")

    read_provider = ProviderSpy()
    module = (
        zotero_verification_module
        if operation == "verify"
        else zotero_reconciliation_module
    )
    monkeypatch.setattr(module, "locked_zotero_parent", _forbidden_lock)
    error_type = (
        zotero_verification_module.ZoteroVerificationError
        if operation == "verify"
        else zotero_reconciliation_module.ZoteroReconciliationError
    )

    with pytest.raises(error_type) as exc_info:
        if operation == "verify":
            zotero_verification_module.verify_zotero_authorization(
                authorization_path,
                note_key="NOTE1",
                provider=read_provider,
            )
        else:
            zotero_reconciliation_module.reconcile_zotero_authorization(
                authorization_path,
                provider=read_provider,
            )

    assert exc_info.value.code == "authorization_tampered"
    assert read_provider.calls == 0
    assert _filesystem_snapshot(run_dir) == before
    assert not (run_dir / "verifications").exists()
    assert not (run_dir / "reconciliations").exists()


def test_malformed_json_retains_existing_validation_error_categories(tmp_path: Path) -> None:
    review_case = tmp_path / "review"
    review_case.mkdir()
    run_dir, evidence_digest = _prepared_run(review_case)
    _write_summary_and_review(run_dir, evidence_digest)
    (run_dir / "summary.json").write_bytes(b"{")

    validation = review_package_module.validate_review_run(run_dir)

    assert "invalid_summary_schema" in {item.code for item in validation.blockers}

    candidate_case = tmp_path / "candidate"
    candidate_case.mkdir()
    _candidate_run, candidate_path = _built_candidate(candidate_case)
    candidate_path.write_bytes(b"{")

    with pytest.raises(LocalPublicationError) as candidate_error:
        local_publish_module.publish_local_candidate(candidate_path)

    assert candidate_error.value.code == "candidate_tampered"

    authorization_case = tmp_path / "authorization"
    authorization_case.mkdir()
    candidate_path, provider = _candidate(authorization_case)
    authorization_path = _authorize(candidate_path, provider).authorization_path
    authorization_path.write_bytes(b"{")

    with pytest.raises(ZoteroAuthorizationBindingError) as authorization_error:
        inspect_authorization_target(authorization_path)

    assert authorization_error.value.code == "authorization_unreadable"


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
@pytest.mark.parametrize("artifact_name", ["summary.json", "review.json"])
def test_review_inputs_reject_links_without_following_run_external_bytes(
    link_kind: str,
    artifact_name: str,
    tmp_path: Path,
) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    artifact_path = run_dir / artifact_name
    external_path = tmp_path / f"external-{artifact_name}"
    artifact_path.rename(external_path)
    if link_kind == "symlink":
        artifact_path.symlink_to(external_path)
    else:
        os.link(external_path, artifact_path)

    validation = review_package_module.validate_review_run(run_dir)

    assert getattr(validation, artifact_name.removesuffix(".json")) is None
    blocker_code = f"invalid_{artifact_name.removesuffix('.json')}_schema"
    assert blocker_code in {item.code for item in validation.blockers}
    assert external_path.read_bytes() == artifact_path.read_bytes()


@pytest.mark.parametrize("artifact_name", ["summary.json", "review.json"])
def test_review_seal_rejects_unsafe_input_before_lock_creation(
    artifact_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    artifact_path = run_dir / artifact_name
    external_path = tmp_path / f"external-{artifact_name}"
    artifact_path.rename(external_path)
    artifact_path.symlink_to(external_path)
    before = _filesystem_snapshot(run_dir)
    monkeypatch.setattr(review_package_module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(RunLoadError) as exc_info:
        review_package_module.seal_review_run(run_dir)

    assert exc_info.value.code == "run_artifact_unsafe"
    assert exc_info.value.manifest_path == artifact_path
    assert _filesystem_snapshot(run_dir) == before


def test_review_seal_rechecks_anchored_inputs_after_preflight_toctou(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    summary_path = run_dir / "summary.json"
    detached_path = run_dir / ".summary.detached.json"
    original_locked_v2_run = review_package_module.locked_v2_run

    @contextmanager
    def swap_to_symlink_then_lock(path: Path, **kwargs):
        summary_path.rename(detached_path)
        summary_path.symlink_to(detached_path)
        with original_locked_v2_run(path, **kwargs) as loaded:
            yield loaded

    monkeypatch.setattr(
        review_package_module,
        "locked_v2_run",
        swap_to_symlink_then_lock,
    )

    with pytest.raises(review_package_module.ReviewSealError) as exc_info:
        review_package_module.seal_review_run(run_dir)

    assert exc_info.value.code == "review_blocked"
    assert "invalid_summary_schema" in {item.code for item in exc_info.value.blockers}
    assert not (run_dir / "reviews").exists()


def test_review_seal_rejects_run_directory_replacement_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    replacement = tmp_path / "replacement-review-run"
    detached = tmp_path / "detached-review-run"
    shutil.copytree(run_dir, replacement)
    original_locked_v2_run = review_package_module.locked_v2_run
    swapped = False

    @contextmanager
    def replace_before_lock(path: Path, **kwargs):
        nonlocal swapped
        if not swapped:
            run_dir.rename(detached)
            replacement.rename(run_dir)
            swapped = True
        with original_locked_v2_run(path, **kwargs) as loaded:
            yield loaded

    monkeypatch.setattr(review_package_module, "locked_v2_run", replace_before_lock)

    with pytest.raises(Exception) as exc_info:
        review_package_module.seal_review_run(run_dir)

    assert getattr(exc_info.value, "code", None) == "run_directory_changed"
    assert not (run_dir / "reviews").exists()
    assert not (detached / "reviews").exists()


@pytest.mark.parametrize(
    "schema_case",
    UNSUPPORTED_SCHEMA_CASES,
)
def test_candidate_build_preflights_review_package_schema_before_lock(
    schema_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _sealed_run(tmp_path)
    package_path = _replace_bound_artifact_schema(
        run_dir,
        role="review_package",
        expected="paper_reader.review-package.v2",
        schema_case=schema_case,
    )
    before = _filesystem_snapshot(run_dir)
    monkeypatch.setattr(candidate_builder_module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(RunLoadError) as exc_info:
        candidate_builder_module.build_local_candidate(run_dir)

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == package_path
    assert _filesystem_snapshot(run_dir) == before
    assert not (run_dir / "candidates").exists()


def test_candidate_build_rejects_v1_review_package_before_ref_integrity_or_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _sealed_run(tmp_path)
    run_payload = json.loads((run_dir / "run.json").read_bytes())
    package_ref = next(
        item for item in run_payload["artifacts"] if item["role"] == "review_package"
    )
    package_path = run_dir / package_ref["path"]
    _write_unsupported_schema_case(
        package_path,
        "paper_reader.review-package.v2",
        "v1",
    )
    before = _filesystem_snapshot(run_dir)
    monkeypatch.setattr(candidate_builder_module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(RunLoadError) as exc_info:
        candidate_builder_module.build_local_candidate(run_dir)

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == package_path
    assert _filesystem_snapshot(run_dir) == before
    assert not (run_dir / "candidates").exists()


@pytest.mark.parametrize("operation", ["local_publish", "zotero_authorize"])
@pytest.mark.parametrize("schema_case", UNSUPPORTED_SCHEMA_CASES)
def test_candidate_review_package_snapshot_rejects_unsupported_schema_before_side_effect(
    operation: str,
    schema_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if operation == "local_publish":
        run_dir, candidate_path = _built_candidate(tmp_path)
        module = local_publish_module
    else:
        candidate_path, _provider = _candidate(tmp_path)
        run_dir = candidate_path.parent.parent.parent
        module = zotero_authorization_module
    package_path = _replace_candidate_review_package_schema(
        candidate_path,
        schema_case=schema_case,
    )
    before = _filesystem_snapshot(run_dir)
    monkeypatch.setattr(module, "locked_v2_run", _forbidden_lock)

    if operation == "local_publish":
        with pytest.raises(RunLoadError) as exc_info:
            local_publish_module.publish_local_candidate(candidate_path)
        assert not (tmp_path / "paper_note.md").exists()
    else:
        class ProviderMustNotRun:
            def get_parent(self, _item_key: str):
                raise AssertionError("candidate schema guard must run before Zotero read")

            def get_children(self, _parent_key: str):
                raise AssertionError("candidate schema guard must run before Zotero read")

        monkeypatch.setattr(
            zotero_authorization_module,
            "locked_zotero_parent",
            _forbidden_lock,
        )
        with pytest.raises(RunLoadError) as exc_info:
            zotero_authorization_module.authorize_zotero_candidate(
                candidate_path,
                provider=ProviderMustNotRun(),
            )
        assert not (run_dir / "authorizations").exists()

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == package_path
    assert _filesystem_snapshot(run_dir) == before


@pytest.mark.parametrize(
    "replacement",
    [
        {"schema_version": "paper_reader.summary.v1"},
        [],
    ],
    ids=["v1", "valid-non-object"],
)
def test_local_candidate_build_preflights_sealed_summary_before_lock(
    replacement: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _sealed_run(tmp_path)
    snapshot_path = _replace_sealed_review_snapshot_schema(
        run_dir,
        artifact_role="summary_snapshot",
        replacement=replacement,
    )
    before = _filesystem_snapshot(run_dir)
    monkeypatch.setattr(candidate_builder_module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(RunLoadError) as exc_info:
        candidate_builder_module.build_local_candidate(run_dir)

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == snapshot_path
    assert _filesystem_snapshot(run_dir) == before


def test_local_candidate_build_rejects_run_directory_replacement_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _sealed_run(tmp_path)
    replacement = tmp_path / "replacement-candidate-run"
    detached = tmp_path / "detached-candidate-run"
    shutil.copytree(run_dir, replacement)
    original_locked_v2_run = candidate_builder_module.locked_v2_run
    swapped = False

    @contextmanager
    def replace_before_lock(path: Path, **kwargs):
        nonlocal swapped
        if not swapped:
            run_dir.rename(detached)
            replacement.rename(run_dir)
            swapped = True
        with original_locked_v2_run(path, **kwargs) as loaded:
            yield loaded

    monkeypatch.setattr(candidate_builder_module, "locked_v2_run", replace_before_lock)

    with pytest.raises(Exception) as exc_info:
        candidate_builder_module.build_local_candidate(run_dir)

    assert getattr(exc_info.value, "code", None) == "run_directory_changed"
    assert not (run_dir / "candidates").exists()
    assert not (detached / "candidates").exists()


def test_zotero_candidate_build_preflights_sealed_review_before_parent_lock_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _sealed_zotero_run(tmp_path)
    snapshot_path = _replace_sealed_review_snapshot_schema(
        run_dir,
        artifact_role="review_snapshot",
        replacement={"schema_version": "paper_reader.review.v1"},
    )
    before = _filesystem_snapshot(run_dir)
    provider = InMemoryZoteroProvider()
    monkeypatch.setattr(zotero_candidate_module, "locked_zotero_parent", _forbidden_lock)
    monkeypatch.setattr(zotero_candidate_module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(RunLoadError) as exc_info:
        zotero_candidate_module.build_zotero_candidate(run_dir, provider=provider)

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == snapshot_path
    assert _filesystem_snapshot(run_dir) == before


@pytest.mark.parametrize("entrypoint", ["direct", "dispatcher"])
def test_zotero_candidate_retry_rejects_unproved_target_transition(
    entrypoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_root = tmp_path / entrypoint
    case_root.mkdir()
    run_dir = _sealed_zotero_run(case_root)
    run_manifest = run_dir / "run.json"
    original_parent_lock = zotero_candidate_module.locked_zotero_parent
    changed = False

    @contextmanager
    def change_target_before_parent_lock(path: Path, parent_key: str, **kwargs):
        nonlocal changed
        if not changed:
            payload = json.loads(run_manifest.read_bytes())
            source = payload["source"]
            payload["target"] = {
                "target_type": "zotero",
                "parent_key": source["item_key"],
                "parent_fingerprint": source["parent_fingerprint"],
                "note_title": "[Codex Summary] unproved target",
            }
            run_manifest.write_bytes(canonical_json_bytes(payload))
            changed = True
        with original_parent_lock(path, parent_key, **kwargs) as locked:
            yield locked

    class ProviderSpy:
        calls = 0

        def get_parent(self, _item_key: str):
            self.calls += 1
            raise AssertionError("unproved target reached Zotero read")

        def get_children(self, _parent_key: str):
            self.calls += 1
            raise AssertionError("unproved target reached Zotero read")

    provider = ProviderSpy()
    monkeypatch.setattr(
        zotero_candidate_module,
        "locked_zotero_parent",
        change_target_before_parent_lock,
    )

    with pytest.raises(LocalPublicationError) as exc_info:
        if entrypoint == "direct":
            zotero_candidate_module.build_zotero_candidate(
                run_dir,
                provider=provider,
            )
        else:
            candidate_builder_module.build_candidate(run_dir, provider=provider)

    assert exc_info.value.code == "sealed_artifact_tampered"
    assert changed is True
    assert provider.calls == 0
    assert not (run_dir / "candidates").exists()


def test_zotero_candidate_does_not_load_run_before_root_first_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_root = tmp_path / "case"
    case_root.mkdir()
    run_dir = _sealed_zotero_run(case_root)

    def forbidden_early_load(_path: Path):
        raise AssertionError("Zotero builder loaded the run before root-first preflight")

    monkeypatch.setattr(
        zotero_candidate_module,
        "load_v2_run",
        forbidden_early_load,
        raising=False,
    )

    built = zotero_candidate_module.build_zotero_candidate(
        run_dir,
        provider=InMemoryZoteroProvider(),
    )

    assert built.run_dir == run_dir


def test_zotero_candidate_binds_skill_root_before_run_preflight_can_be_grafted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_root = tmp_path / "case"
    case_root.mkdir()
    run_dir = _sealed_zotero_run(case_root)
    skill_root = run_dir.parent.parent.parent
    run_relative = run_dir.relative_to(skill_root)
    detached_root = tmp_path / "detached-candidate-skill-root"
    original_preflight = zotero_candidate_module.preflight_sealed_review_schema_versions
    grafted = False

    def preflight_then_graft(path: Path):
        nonlocal grafted
        preflight = original_preflight(path)
        if not grafted:
            skill_root.rename(detached_root)
            run_dir.parent.mkdir(parents=True, exist_ok=True)
            (detached_root / run_relative).rename(run_dir)
            grafted = True
        return preflight

    monkeypatch.setattr(
        zotero_candidate_module,
        "preflight_sealed_review_schema_versions",
        preflight_then_graft,
    )

    with pytest.raises(Exception) as exc_info:
        zotero_candidate_module.build_zotero_candidate(
            run_dir,
            provider=InMemoryZoteroProvider(),
        )

    assert getattr(exc_info.value, "code", None) == "run_directory_changed"
    assert not (run_dir / "candidates").exists()


def test_candidate_dispatcher_routes_from_root_first_preflight_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_root = tmp_path / "case"
    case_root.mkdir()
    run_dir = _sealed_zotero_run(case_root)
    skill_root = run_dir.parent.parent.parent
    run_relative = run_dir.relative_to(skill_root)
    detached_root = tmp_path / "detached-dispatch-skill-root"
    original_load = candidate_builder_module.load_v2_run
    grafted = False

    def load_then_graft(path: Path):
        nonlocal grafted
        loaded = original_load(path)
        if not grafted:
            skill_root.rename(detached_root)
            run_dir.parent.mkdir(parents=True, exist_ok=True)
            (detached_root / run_relative).rename(run_dir)
            grafted = True
        return loaded

    monkeypatch.setattr(candidate_builder_module, "load_v2_run", load_then_graft)

    with pytest.raises(Exception) as exc_info:
        candidate_builder_module.build_candidate(
            run_dir,
            provider=InMemoryZoteroProvider(),
        )

    assert getattr(exc_info.value, "code", None) == "run_directory_changed"
    assert not (run_dir / "candidates").exists()


@pytest.mark.parametrize("operation", ["verify", "reconcile"])
@pytest.mark.parametrize(
    "replacement",
    [
        {"schema_version": "paper_reader.candidate.v1"},
        None,
    ],
    ids=["v1", "valid-non-object"],
)
def test_authorization_embedded_candidate_is_preflighted_before_lock_or_network(
    operation: str,
    replacement: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorization_path = _authorize(candidate_path, provider).authorization_path
    embedded_candidate_path = _replace_authorization_candidate_schema(
        authorization_path,
        replacement=replacement,
    )
    run_dir = authorization_path.parent.parent
    before = _filesystem_snapshot(run_dir)
    module = (
        zotero_verification_module
        if operation == "verify"
        else zotero_reconciliation_module
    )
    monkeypatch.setattr(module, "locked_zotero_parent", _forbidden_lock)
    monkeypatch.setattr(module, "locked_v2_run", _forbidden_lock)

    with pytest.raises(RunLoadError) as exc_info:
        if operation == "verify":
            zotero_verification_module.verify_zotero_authorization(
                authorization_path,
                note_key="NOTE1",
                provider=provider,
            )
        else:
            zotero_reconciliation_module.reconcile_zotero_authorization(
                authorization_path,
                provider=provider,
            )

    assert exc_info.value.code == "unsupported_run_schema"
    assert exc_info.value.manifest_path == embedded_candidate_path
    assert _filesystem_snapshot(run_dir) == before


def test_valid_non_object_review_input_emits_one_unsupported_schema_envelope(
    tmp_path: Path,
) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    summary_path = run_dir / "summary.json"
    summary_path.write_bytes(canonical_json_bytes([]))

    result = CliRunner().invoke(app, ["review", "validate", str(run_dir)])

    assert result.exit_code != 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    assert payload["command"] == "review validate"
    assert payload["code"] == "unsupported_run_schema"


@pytest.mark.parametrize("operation", ["verify", "reconcile"])
def test_authorization_is_rebound_to_preflight_bytes_across_lock_toctou(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorization_path = _authorize(candidate_path, provider).authorization_path
    run_dir = authorization_path.parent.parent
    module = (
        zotero_verification_module
        if operation == "verify"
        else zotero_reconciliation_module
    )
    original_parent_lock = module.locked_zotero_parent
    swapped = False

    @contextmanager
    def swap_main_before_parent_lock(path: Path, parent_key: str, **kwargs):
        nonlocal swapped
        if not swapped:
            authorization = json.loads(authorization_path.read_bytes())
            authorization["nonce"] = authorization["nonce"][::-1]
            replacement_bytes = canonical_json_bytes(authorization)
            authorization_path.write_bytes(replacement_bytes)
            (authorization_path.with_suffix("") / "record.json").write_bytes(
                replacement_bytes
            )
            swapped = True
        with original_parent_lock(path, parent_key, **kwargs):
            yield

    monkeypatch.setattr(module, "locked_zotero_parent", swap_main_before_parent_lock)

    def forbidden_lock_directory(*_args, **_kwargs):
        raise AssertionError("changed authorization reached parent lock-file open")

    monkeypatch.setattr(
        zotero_lock_module,
        "_open_or_create_lock_directory",
        forbidden_lock_directory,
    )

    error_type = (
        zotero_verification_module.ZoteroVerificationError
        if operation == "verify"
        else zotero_reconciliation_module.ZoteroReconciliationError
    )
    with pytest.raises(error_type) as exc_info:
        if operation == "verify":
            zotero_verification_module.verify_zotero_authorization(
                authorization_path,
                note_key="NOTE1",
                provider=provider,
            )
        else:
            zotero_reconciliation_module.reconcile_zotero_authorization(
                authorization_path,
                provider=provider,
            )

    assert exc_info.value.code == "authorization_tampered"
    assert not (run_dir / "verifications").exists()
    assert not (run_dir / "reconciliations").exists()


def test_authorization_preflight_binds_skill_root_before_opening_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorization_path = _authorize(candidate_path, provider).authorization_path
    run_dir = authorization_path.parent.parent
    skill_root = run_dir.parent.parent.parent
    run_relative = run_dir.relative_to(skill_root)
    detached_root = tmp_path / "detached-authorization-skill-root"
    original_open = zotero_authorization_loader_module.DirectoryAnchor.open
    grafted = False

    def open_then_graft(path: Path, *, manifest_path: Path):
        nonlocal grafted
        anchor = original_open(path, manifest_path=manifest_path)
        if Path(os.path.abspath(path)) == run_dir and not grafted:
            skill_root.rename(detached_root)
            run_dir.parent.mkdir(parents=True, exist_ok=True)
            (detached_root / run_relative).rename(run_dir)
            grafted = True
        return anchor

    monkeypatch.setattr(
        zotero_authorization_loader_module.DirectoryAnchor,
        "open",
        open_then_graft,
    )

    with pytest.raises(ZoteroAuthorizationBindingError) as exc_info:
        zotero_authorization_loader_module.preflight_authorization_schema_versions(
            authorization_path
        )

    assert exc_info.value.code == "authorization_tampered"


@pytest.mark.parametrize("operation", ["verify", "reconcile"])
def test_authorization_preflight_rejects_skill_root_replacement_before_lock_mutation(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorization_path = _authorize(candidate_path, provider).authorization_path
    run_dir = authorization_path.parent.parent
    skill_root = run_dir.parent.parent.parent
    replacement = tmp_path / "replacement-skill-root"
    detached = tmp_path / "detached-skill-root"
    shutil.copytree(
        skill_root,
        replacement,
        ignore=shutil.ignore_patterns(".zotero-parent-locks", ".run.lock"),
    )
    module = (
        zotero_verification_module
        if operation == "verify"
        else zotero_reconciliation_module
    )
    original_parent_lock = module.locked_zotero_parent
    swapped = False

    @contextmanager
    def replace_root_before_parent_lock(path: Path, parent_key: str, **kwargs):
        nonlocal swapped
        if not swapped:
            skill_root.rename(detached)
            replacement.rename(skill_root)
            swapped = True
        with original_parent_lock(path, parent_key, **kwargs) as locked:
            yield locked

    monkeypatch.setattr(
        module,
        "locked_zotero_parent",
        replace_root_before_parent_lock,
    )
    error_type = (
        zotero_verification_module.ZoteroVerificationError
        if operation == "verify"
        else zotero_reconciliation_module.ZoteroReconciliationError
    )

    with pytest.raises(error_type) as exc_info:
        if operation == "verify":
            zotero_verification_module.verify_zotero_authorization(
                authorization_path,
                note_key="NOTE1",
                provider=provider,
            )
        else:
            zotero_reconciliation_module.reconcile_zotero_authorization(
                authorization_path,
                provider=provider,
            )

    assert exc_info.value.code == "run_directory_changed"
    assert not (skill_root / ".zotero-parent-locks").exists()
    assert not any(skill_root.rglob(".run.lock"))
    assert not (run_dir / "verifications").exists()
    assert not (run_dir / "reconciliations").exists()


@pytest.mark.parametrize("operation", ["verify", "reconcile"])
def test_authorization_preflight_rejects_run_replacement_before_run_lock_creation(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorization_path = _authorize(candidate_path, provider).authorization_path
    run_dir = authorization_path.parent.parent
    replacement = tmp_path / "replacement-authorization-run"
    detached = tmp_path / "detached-authorization-run"
    shutil.copytree(
        run_dir,
        replacement,
        ignore=shutil.ignore_patterns(".run.lock"),
    )
    module = (
        zotero_verification_module
        if operation == "verify"
        else zotero_reconciliation_module
    )
    original_parent_lock = module.locked_zotero_parent
    swapped = False

    @contextmanager
    def replace_run_before_parent_lock(path: Path, parent_key: str, **kwargs):
        nonlocal swapped
        if not swapped:
            run_dir.rename(detached)
            replacement.rename(run_dir)
            swapped = True
        with original_parent_lock(path, parent_key, **kwargs) as locked:
            yield locked

    monkeypatch.setattr(
        module,
        "locked_zotero_parent",
        replace_run_before_parent_lock,
    )
    error_type = (
        zotero_verification_module.ZoteroVerificationError
        if operation == "verify"
        else zotero_reconciliation_module.ZoteroReconciliationError
    )

    with pytest.raises(error_type) as exc_info:
        if operation == "verify":
            zotero_verification_module.verify_zotero_authorization(
                authorization_path,
                note_key="NOTE1",
                provider=provider,
            )
        else:
            zotero_reconciliation_module.reconcile_zotero_authorization(
                authorization_path,
                provider=provider,
            )

    assert exc_info.value.code == "run_directory_changed"
    assert not (run_dir / ".run.lock").exists()
    assert not (run_dir / "verifications").exists()
    assert not (run_dir / "reconciliations").exists()


@pytest.mark.parametrize("operation", ["verify", "reconcile"])
def test_authorization_preflight_rejects_manifest_swap_before_parent_lock_creation(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    authorization_path = _authorize(candidate_path, provider).authorization_path
    run_dir = authorization_path.parent.parent
    run_manifest = run_dir / "run.json"
    module = (
        zotero_verification_module
        if operation == "verify"
        else zotero_reconciliation_module
    )
    original_parent_lock = module.locked_zotero_parent
    swap_count = 0

    @contextmanager
    def replace_manifest_before_parent_lock(path: Path, parent_key: str, **kwargs):
        nonlocal swap_count
        if swap_count < 2:
            payload = json.loads(run_manifest.read_bytes())
            swap_count += 1
            payload["created_at"] = f"2026-07-10T09:3{swap_count}:00Z"
            run_manifest.write_bytes(canonical_json_bytes(payload))
        with original_parent_lock(path, parent_key, **kwargs) as locked:
            yield locked

    monkeypatch.setattr(
        module,
        "locked_zotero_parent",
        replace_manifest_before_parent_lock,
    )
    error_type = (
        zotero_verification_module.ZoteroVerificationError
        if operation == "verify"
        else zotero_reconciliation_module.ZoteroReconciliationError
    )

    with pytest.raises(error_type) as exc_info:
        if operation == "verify":
            zotero_verification_module.verify_zotero_authorization(
                authorization_path,
                note_key="NOTE1",
                provider=provider,
            )
        else:
            zotero_reconciliation_module.reconcile_zotero_authorization(
                authorization_path,
                provider=provider,
            )

    assert exc_info.value.code == "run_manifest_changed"
    assert swap_count == 2
    assert not (run_dir / "verifications").exists()
    assert not (run_dir / "reconciliations").exists()
