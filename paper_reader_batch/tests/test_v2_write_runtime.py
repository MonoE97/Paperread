from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

import paper_reader_batch.v2_write as write_module
import paper_reader_batch.v2_journal as journal_module
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_contracts import WriteResult
from paper_reader_batch.v2_json import canonical_json_bytes, sha256_bytes
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_run import initialize_run
from paper_reader_batch.v2_worker import claim_worker, finish_worker

from test_v2_artifact_closure import (
    BuiltWorkerFixture,
    _foreign_ref,
    _json,
    _outer_ref,
    _zotero_fixture,
)


REQUEST_INIT = "11111111-1111-4111-8111-111111111111"
REQUEST_WORKER_CLAIM = "22222222-2222-4222-8222-222222222222"
REQUEST_WORKER_FINISH = "33333333-3333-4333-8333-333333333333"
REQUEST_WRITE_CLAIM = "44444444-4444-4444-8444-444444444444"
REQUEST_WRITE_RENEW = "55555555-5555-4555-8555-555555555555"
REQUEST_WRITE_RELEASE = "66666666-6666-4666-8666-666666666666"
REQUEST_WRITE_BEGIN = "77777777-7777-4777-8777-777777777777"
REQUEST_WRITE_BEGIN_OTHER = "88888888-8888-4888-8888-888888888888"
REQUEST_WRITE_COMMIT = "99999999-9999-4999-8999-999999999999"
REQUEST_WRITE_UNCERTAIN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REQUEST_WRITE_RECONCILE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
REQUEST_WRITE_RETRY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
REQUEST_WRITE_CLAIM_RETRY = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
REQUEST_WRITE_BEGIN_RETRY = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
_DEFAULT_TAGS = object()


@dataclass(frozen=True)
class ReadyWriteRun:
    run_dir: Path
    built: BuiltWorkerFixture


def _guard_secret_read(
    monkeypatch: pytest.MonkeyPatch,
    secret_path: Path,
) -> list[Path]:
    observed: list[Path] = []
    original_json = write_module.read_json_bytes
    original_bytes = write_module.read_bytes

    def guarded(path: Path, *args, **kwargs):
        normalized = Path(path)
        if normalized == secret_path:
            observed.append(normalized)
            raise AssertionError("external secret path was read")
        return original_json(path, *args, **kwargs)

    def guarded_bytes(path: Path, *args, **kwargs):
        normalized = Path(path)
        if normalized == secret_path:
            observed.append(normalized)
            raise AssertionError("external secret path was read")
        return original_bytes(path, *args, **kwargs)

    monkeypatch.setattr(write_module, "read_json_bytes", guarded)
    monkeypatch.setattr(write_module, "read_bytes", guarded_bytes)
    return observed


def _tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def _replace_file_with_same_bytes(path: Path, backup_root: Path) -> None:
    backup = backup_root / f"{path.name}.original"
    path.rename(backup)
    shutil.copy2(backup, path)


def _replace_directory_with_same_bytes(path: Path, backup_root: Path) -> None:
    backup = backup_root / f"{path.name}.original"
    path.rename(backup)
    shutil.copytree(backup, path, copy_function=shutil.copy2)


def _crash_on_file_fsync(message: str):
    def fault(stage: str) -> None:
        if stage == "after_file_fsync":
            raise RuntimeError(message)

    return fault


def _ready_write_run(tmp_path: Path) -> ReadyWriteRun:
    built = _zotero_fixture(tmp_path / "single")
    (built.run_dir / ".run.lock").touch()
    skill_root = tmp_path / "batch-skill"
    skill_root.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(built.manifest))
    run_dir = tmp_path / "batch-run"
    initialize_run(
        manifest_path,
        request_id=REQUEST_INIT,
        skill_root=skill_root,
        output=run_dir,
        initialized_at="2026-07-10T00:00:00Z",
    )
    assignment = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=REQUEST_WORKER_CLAIM,
        limit=1,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    result = built.result.model_copy(
        update={
            "manifest_sha256": built.result.manifest_sha256,
            "claim_id": assignment["claim_id"],
            "attempt_id": assignment["attempt_id"],
            "attempt_number": assignment["attempt_number"],
            "lease_token_sha256": sha256_bytes(assignment["lease_token"].encode()),
        }
    )
    result_path = tmp_path / "worker-result.json"
    result_path.write_bytes(canonical_json_bytes(result))
    finish_worker(
        run_dir,
        "001",
        worker_id="worker-1",
        claim_id=assignment["claim_id"],
        lease_token=assignment["lease_token"],
        attempt_id=assignment["attempt_id"],
        result_path=result_path,
        request_id=REQUEST_WORKER_FINISH,
        now="2026-07-10T00:00:02Z",
    )
    return ReadyWriteRun(run_dir=run_dir, built=built)


def _artifact_ref(run_dir: Path, path: Path, role: str, media_type: str) -> dict[str, object]:
    return _foreign_ref(run_dir, path, role, media_type)


def _make_authorization(
    ready: ReadyWriteRun,
    claimed: dict[str, object],
    *,
    created_at: str = "2026-07-10T00:00:05Z",
    expires_at: str = "2026-07-10T00:05:05Z",
    ttl_seconds: int = 300,
    nonce: str = "nonce_" + "n" * 37,
) -> tuple[Path, dict[str, object]]:
    candidate_path = ready.built.candidate_path
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    paper_run_dir = ready.built.run_dir
    authorization_id = f"authorization_{str(claimed['write_attempt_id']).replace('-', '_')}"
    authorization_dir = paper_run_dir / "authorizations" / authorization_id
    authorization_dir.mkdir(parents=True)
    candidate_raw = candidate_path.read_bytes()
    note_html = (candidate_path.parent / "note.html").read_bytes()
    parent_raw = (candidate_path.parent / "parent.json").read_bytes()
    children_raw = (candidate_path.parent / "children.json").read_bytes()
    files = {
        "candidate.json": candidate_raw,
        "content.html": note_html,
        "parent.json": parent_raw,
        "children.json": children_raw,
    }
    for name, raw in files.items():
        (authorization_dir / name).write_bytes(raw)
    refs = {
        "candidate.json": _artifact_ref(
            paper_run_dir,
            authorization_dir / "candidate.json",
            "candidate_snapshot",
            "application/json",
        ),
        "content.html": _artifact_ref(
            paper_run_dir,
            authorization_dir / "content.html",
            "authorized_content_html",
            "text/html",
        ),
        "parent.json": _artifact_ref(
            paper_run_dir,
            authorization_dir / "parent.json",
            "zotero_parent_snapshot",
            "application/json",
        ),
        "children.json": _artifact_ref(
            paper_run_dir,
            authorization_dir / "children.json",
            "zotero_children_snapshot",
            "application/json",
        ),
    }
    live_preflight = {
        "preflight_id": f"preflight_{authorization_id}",
        "captured_at": created_at,
        "parent_key": candidate["target"]["parent_key"],
        "parent_fingerprint": candidate["target"]["parent_fingerprint"],
        "requested_note_title": candidate["note_title"],
        "title_available": True,
        "matching_note_keys": [],
        "parent_snapshot": refs["parent.json"],
        "children_snapshot": refs["children.json"],
    }
    gate = {
        "status": "write_ready",
        "evaluated_at": created_at,
        "checks": [
            "candidate_integrity",
            "source_identity",
            "parent_fingerprint",
            "live_title_availability",
            "canonical_html_binding",
            "authorization_ttl",
        ],
        "blockers": [],
    }
    authorization = {
        "schema_version": "paper_reader.write-authorization.v2",
        "authorization_id": authorization_id,
        "run_id": candidate["run_id"],
        "created_at": created_at,
        "expires_at": expires_at,
        "ttl_seconds": ttl_seconds,
        "candidate": refs["candidate.json"],
        "candidate_digest": sha256_bytes(candidate_raw),
        "target": candidate["target"],
        "note_title": candidate["note_title"],
        "tags": candidate["tags"],
        "content_html": note_html.decode("utf-8"),
        "content_sha256": candidate["content_sha256"],
        "content_length": candidate["content_length"],
        "minimum_content_length": candidate["content_length"],
        "required_headings": ["30 秒结论"],
        "forbidden_headings": ["待补充"],
        "nonce": nonce,
        "token_sha256": hashlib.sha256(b"one-time-token").hexdigest(),
        "external_claim_id": claimed["claim_id"],
        "write_attempt_id": claimed["write_attempt_id"],
        "mcp_envelope": {
            "action": "create",
            "parentKey": candidate["target"]["parent_key"],
            "content": note_html.decode("utf-8"),
            "tags": candidate["tags"],
        },
        "artifacts": list(refs.values()),
        "live_preflight": live_preflight,
        "gate": gate,
    }
    authorization_raw = canonical_json_bytes(authorization)
    (authorization_dir / "record.json").write_bytes(authorization_raw)
    authorization_path = paper_run_dir / "authorizations" / f"{authorization_id}.json"
    authorization_path.write_bytes(authorization_raw)
    paper_run = json.loads(ready.built.run_path.read_text(encoding="utf-8"))
    paper_run["artifacts"].append(
        _artifact_ref(
            paper_run_dir,
            authorization_path,
            "write_authorization",
            "application/json",
        )
    )
    paper_run["live_preflight"] = live_preflight
    paper_run["gate"] = gate
    _json(ready.built.run_path, paper_run)
    return authorization_path, authorization


def _rewrite_authorization(
    ready: ReadyWriteRun,
    authorization_path: Path,
    authorization: dict[str, object],
) -> None:
    raw = canonical_json_bytes(authorization)
    authorization_path.write_bytes(raw)
    (authorization_path.with_suffix("") / "record.json").write_bytes(raw)
    paper_run = json.loads(ready.built.run_path.read_text(encoding="utf-8"))
    for ref in paper_run["artifacts"]:
        if ref["role"] == "write_authorization" and Path(ref["path"]).name == authorization_path.name:
            ref["sha256"] = sha256_bytes(raw)
            ref["size_bytes"] = len(raw)
    _json(ready.built.run_path, paper_run)


def _make_verification(
    ready: ReadyWriteRun,
    authorization_path: Path,
    authorization: dict[str, object],
    *,
    note_key: str = "NOTE1",
    failed_check: str | None = None,
    snapshot_tags: object = _DEFAULT_TAGS,
) -> Path:
    paper_run_dir = ready.built.run_dir
    authorization_id = str(authorization["authorization_id"])
    verification_dir = paper_run_dir / "verifications" / authorization_id / note_key
    verification_dir.mkdir(parents=True)
    note_snapshot = {
        "key": note_key,
        "data": {
            "key": note_key,
            "itemType": "note",
            "parentItem": authorization["target"]["parent_key"],
            "note": authorization["content_html"],
            "tags": (
                [{"tag": tag} for tag in authorization["tags"]]
                if snapshot_tags is _DEFAULT_TAGS
                else snapshot_tags
            ),
        },
    }
    check_names = [
        "note_key",
        "item_type",
        "parent_key",
        "note_title",
        "tag_set",
        "required_headings",
        "forbidden_headings",
        "minimum_content_length",
        "content_length",
        "content_sha256",
    ]
    checks = [
        {
            "name": name,
            "passed": name != failed_check,
            "expected": None,
            "actual": None,
            "message": "injected failure" if name == failed_check else None,
        }
        for name in check_names
    ]
    checks_payload = {
        "format": "paper_reader.verification-checks.v2-internal",
        "authorization_digest": sha256_bytes(authorization_path.read_bytes()),
        "note_key": note_key,
        "checks": checks,
    }
    files = {
        "authorization.json": authorization_path.read_bytes(),
        "note.json": canonical_json_bytes(note_snapshot),
        "checks.json": canonical_json_bytes(checks_payload),
    }
    for name, raw in files.items():
        (verification_dir / name).write_bytes(raw)
    refs = {
        "authorization.json": _artifact_ref(
            paper_run_dir,
            verification_dir / "authorization.json",
            "authorization_snapshot",
            "application/json",
        ),
        "note.json": _artifact_ref(
            paper_run_dir,
            verification_dir / "note.json",
            "zotero_note_readback",
            "application/json",
        ),
        "checks.json": _artifact_ref(
            paper_run_dir,
            verification_dir / "checks.json",
            "verification_checks",
            "application/json",
        ),
    }
    verified = failed_check is None
    gate = {
        "status": "passed" if verified else "blocked",
        "evaluated_at": "2026-07-10T00:00:07Z",
        "checks": check_names,
        "blockers": []
        if verified
        else [{"code": f"verification_{failed_check}", "message": "injected failure", "artifact_path": None}],
    }
    verification = {
        "schema_version": "paper_reader.verification.v2",
        "verification_id": f"verification_{note_key}",
        "run_id": authorization["run_id"],
        "created_at": "2026-07-10T00:00:07Z",
        "authorization": refs["authorization.json"],
        "authorization_digest": sha256_bytes(authorization_path.read_bytes()),
        "target": authorization["target"],
        "note_key": note_key,
        "verified": verified,
        "content_sha256": authorization["content_sha256"],
        "content_length": authorization["content_length"],
        "checks": checks,
        "note_snapshot": refs["note.json"],
        "checks_snapshot": refs["checks.json"],
        "artifacts": list(refs.values()),
        "gate": gate,
    }
    verification_raw = canonical_json_bytes(verification)
    (verification_dir / "record.json").write_bytes(verification_raw)
    verification_path = verification_dir.parent / f"{note_key}.json"
    verification_path.write_bytes(verification_raw)
    paper_run = json.loads(ready.built.run_path.read_text(encoding="utf-8"))
    paper_run["artifacts"].append(
        _artifact_ref(
            paper_run_dir,
            verification_path,
            "zotero_verification",
            "application/json",
        )
    )
    paper_run["status"] = "published" if verified else "blocked"
    paper_run["gate"] = gate
    _json(ready.built.run_path, paper_run)
    return verification_path


def _make_write_result(
    ready: ReadyWriteRun,
    claimed: dict[str, object],
    authorization_path: Path,
    authorization: dict[str, object],
    verification_path: Path,
) -> Path:
    view = load_run_view(ready.run_dir)
    item = view.state.items[0]
    result = WriteResult(
        schema_version="paper_reader_batch.write-result.v2",
        manifest_sha256=view.manifest_sha256,
        item_id="001",
        writer_id="writer-1",
        claim_id=str(claimed["claim_id"]),
        write_attempt_id=str(claimed["write_attempt_id"]),
        lease_token_sha256=sha256_bytes(str(claimed["lease_token"]).encode()),
        started_event_sha256=str(item.write_started_event_sha256),
        candidate_sha256=str(item.candidate_sha256),
        authorization_sha256=sha256_bytes(authorization_path.read_bytes()),
        authorization_nonce_sha256=sha256_bytes(str(authorization["nonce"]).encode()),
        external_claim_id=str(claimed["claim_id"]),
        note_key="NOTE1",
        parent_key=str(authorization["target"]["parent_key"]),
        canonical_html_sha256=str(authorization["content_sha256"]),
        verification=_outer_ref(
            verification_path,
            "paper_reader.verification.v2",
            "verification_NOTE1",
        ),
    )
    path = ready.run_dir.parent / "write-result.json"
    path.write_bytes(canonical_json_bytes(result))
    return path


def _make_reconciliation_not_found(
    ready: ReadyWriteRun,
    authorization_path: Path,
    authorization: dict[str, object],
) -> Path:
    paper_run_dir = ready.built.run_dir
    authorization_id = str(authorization["authorization_id"])
    reconciliation_dir = paper_run_dir / "reconciliations" / authorization_id
    reconciliation_dir.mkdir(parents=True)
    children_raw = canonical_json_bytes([])
    (reconciliation_dir / "authorization.json").write_bytes(authorization_path.read_bytes())
    (reconciliation_dir / "children.json").write_bytes(children_raw)
    refs = {
        "authorization.json": _artifact_ref(
            paper_run_dir,
            reconciliation_dir / "authorization.json",
            "authorization_snapshot",
            "application/json",
        ),
        "children.json": _artifact_ref(
            paper_run_dir,
            reconciliation_dir / "children.json",
            "zotero_children_snapshot",
            "application/json",
        ),
    }
    gate = {
        "status": "blocked",
        "evaluated_at": "2026-07-10T00:00:09Z",
        "checks": ["exact_parent_title_hash_locator"],
        "blockers": [
            {
                "code": "reconciliation_not_found",
                "message": "no exact note was found",
                "artifact_path": None,
            }
        ],
    }
    reconciliation = {
        "schema_version": "paper_reader.reconciliation.v2",
        "reconciliation_id": f"reconciliation_{authorization_id}",
        "run_id": authorization["run_id"],
        "created_at": "2026-07-10T00:00:09Z",
        "authorization": refs["authorization.json"],
        "authorization_digest": sha256_bytes(authorization_path.read_bytes()),
        "target": authorization["target"],
        "outcome": "not_found",
        "match_count": 0,
        "matched_note_keys": [],
        "children_snapshot": refs["children.json"],
        "verification": None,
        "retry_confirmation_required": True,
        "artifacts": list(refs.values()),
        "gate": gate,
    }
    raw = canonical_json_bytes(reconciliation)
    (reconciliation_dir / "record.json").write_bytes(raw)
    path = reconciliation_dir.parent / f"{authorization_id}.json"
    path.write_bytes(raw)
    paper_run = json.loads(ready.built.run_path.read_text(encoding="utf-8"))
    paper_run["artifacts"].append(
        _artifact_ref(
            paper_run_dir,
            path,
            "zotero_reconciliation",
            "application/json",
        )
    )
    paper_run["status"] = "blocked"
    paper_run["gate"] = gate
    _json(ready.built.run_path, paper_run)
    return path


def _make_reconciliation_matches(
    ready: ReadyWriteRun,
    authorization_path: Path,
    authorization: dict[str, object],
    *,
    note_keys: tuple[str, ...],
    verification_failure: str | None = None,
    omit_verification_outcome: str | None = None,
    snapshot_tags: object = _DEFAULT_TAGS,
) -> Path:
    assert note_keys
    paper_run_dir = ready.built.run_dir
    authorization_id = str(authorization["authorization_id"])
    reconciliation_dir = paper_run_dir / "reconciliations" / authorization_id
    reconciliation_dir.mkdir(parents=True)
    children = [
        {
            "key": note_key,
            "data": {
                "key": note_key,
                "itemType": "note",
                "parentItem": authorization["target"]["parent_key"],
                "note": authorization["content_html"],
                "tags": (
                    [
                        {"tag": tag}
                        for index, tag in enumerate(authorization["tags"])
                        if verification_failure != "tag_set" or index > 0
                    ]
                    if snapshot_tags is _DEFAULT_TAGS
                    else snapshot_tags
                ),
            },
        }
        for note_key in note_keys
    ]
    files: dict[str, bytes] = {
        "authorization.json": authorization_path.read_bytes(),
        "children.json": canonical_json_bytes(children),
    }
    specs = {
        "authorization.json": ("authorization_snapshot", "application/json"),
        "children.json": ("zotero_children_snapshot", "application/json"),
    }
    check_names = [
        "note_key",
        "item_type",
        "parent_key",
        "note_title",
        "tag_set",
        "required_headings",
        "forbidden_headings",
        "minimum_content_length",
        "content_length",
        "content_sha256",
    ]
    checks = [
        {
            "name": name,
            "passed": name != verification_failure,
            "expected": None,
            "actual": None,
            "message": "injected verification failure" if name == verification_failure else None,
        }
        for name in check_names
    ]
    if len(note_keys) == 1 and omit_verification_outcome is None:
        files["note.json"] = canonical_json_bytes(children[0])
        files["checks.json"] = canonical_json_bytes(
            {
                "format": "paper_reader.verification-checks.v2-internal",
                "authorization_digest": sha256_bytes(authorization_path.read_bytes()),
                "note_key": note_keys[0],
                "checks": checks,
            }
        )
        specs |= {
            "note.json": ("zotero_note_readback", "application/json"),
            "checks.json": ("verification_checks", "application/json"),
        }
    for name, raw in files.items():
        (reconciliation_dir / name).write_bytes(raw)
    refs = {
        name: _artifact_ref(
            paper_run_dir,
            reconciliation_dir / name,
            role,
            media_type,
        )
        for name, (role, media_type) in specs.items()
    }
    if len(note_keys) == 1 and omit_verification_outcome is None:
        strong_pass = verification_failure is None
        gate = {
            "status": "passed" if strong_pass else "blocked",
            "evaluated_at": "2026-07-10T00:00:09Z",
            "checks": check_names,
            "blockers": []
            if strong_pass
            else [
                {
                    "code": f"verification_{verification_failure}",
                    "message": "injected verification failure",
                    "artifact_path": None,
                }
            ],
        }
        verification = {
            "schema_version": "paper_reader.verification.v2",
            "verification_id": f"verification_{note_keys[0]}",
            "run_id": authorization["run_id"],
            "created_at": "2026-07-10T00:00:09Z",
            "authorization": refs["authorization.json"],
            "authorization_digest": sha256_bytes(authorization_path.read_bytes()),
            "target": authorization["target"],
            "note_key": note_keys[0],
            "verified": strong_pass,
            "content_sha256": authorization["content_sha256"],
            "content_length": authorization["content_length"],
            "checks": checks,
            "note_snapshot": refs["note.json"],
            "checks_snapshot": refs["checks.json"],
            "artifacts": [refs["authorization.json"], refs["note.json"], refs["checks.json"]],
            "gate": gate,
        }
        verification_raw = canonical_json_bytes(verification)
        (reconciliation_dir / "verification.json").write_bytes(verification_raw)
        refs["verification.json"] = _artifact_ref(
            paper_run_dir,
            reconciliation_dir / "verification.json",
            "reconciliation_verification",
            "application/json",
        )
        outcome = "verified" if strong_pass else "blocked"
        verification_ref = refs["verification.json"]
    elif len(note_keys) == 1:
        assert omit_verification_outcome in {"verified", "blocked"}
        outcome = omit_verification_outcome
        verification_ref = None
        gate = {
            "status": "passed" if outcome == "verified" else "blocked",
            "evaluated_at": "2026-07-10T00:00:09Z",
            "checks": ["exact_parent_title_hash_locator"],
            "blockers": []
            if outcome == "verified"
            else [
                {
                    "code": "verification_missing",
                    "message": "strong verification was omitted",
                    "artifact_path": None,
                }
            ],
        }
    else:
        gate = {
            "status": "blocked",
            "evaluated_at": "2026-07-10T00:00:09Z",
            "checks": ["exact_parent_title_hash_locator"],
            "blockers": [
                {
                    "code": "reconciliation_ambiguous",
                    "message": "multiple exact notes were found",
                    "artifact_path": None,
                }
            ],
        }
        outcome = "ambiguous"
        verification_ref = None
    reconciliation = {
        "schema_version": "paper_reader.reconciliation.v2",
        "reconciliation_id": f"reconciliation_{authorization_id}",
        "run_id": authorization["run_id"],
        "created_at": "2026-07-10T00:00:09Z",
        "authorization": refs["authorization.json"],
        "authorization_digest": sha256_bytes(authorization_path.read_bytes()),
        "target": authorization["target"],
        "outcome": outcome,
        "match_count": len(note_keys),
        "matched_note_keys": list(note_keys),
        "children_snapshot": refs["children.json"],
        "verification": verification_ref,
        "retry_confirmation_required": False,
        "artifacts": list(refs.values()),
        "gate": gate,
    }
    raw = canonical_json_bytes(reconciliation)
    (reconciliation_dir / "record.json").write_bytes(raw)
    path = reconciliation_dir.parent / f"{authorization_id}.json"
    path.write_bytes(raw)
    paper_run = json.loads(ready.built.run_path.read_text(encoding="utf-8"))
    paper_run["artifacts"].append(
        _artifact_ref(
            paper_run_dir,
            path,
            "zotero_reconciliation",
            "application/json",
        )
    )
    paper_run["status"] = "published" if outcome == "verified" else "blocked"
    paper_run["gate"] = gate
    _json(ready.built.run_path, paper_run)
    return path


def test_v2_write_runtime_module_exists() -> None:
    assert importlib.util.find_spec("paper_reader_batch.v2_write") is not None


def test_claim_write_returns_one_candidate_and_replays_exact_secret(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import claim_write

    ready = _ready_write_run(tmp_path)

    first = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    )
    replay = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.result == first.result
    assert first.result["item_id"] == "001"
    assert first.result["writer_id"] == "writer-1"
    assert first.result["candidate_sha256"] == ready.built.result.candidate.sha256
    assert first.result["attempt_number"] == 1
    assert first.result["expires_at"] == "2026-07-10T00:02:03.000000Z"
    assert len(first.result["claim_id"]) == 36
    assert len(first.result["write_attempt_id"]) == 36
    assert len(first.result["lease_token"]) >= 32


def test_preview_write_returns_exact_immutable_candidate_without_authorization(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import claim_write, preview_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result

    preview = preview_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        now="2026-07-10T00:00:04Z",
    )

    candidate = ready.built.candidate_path.read_text(encoding="utf-8")
    note_md = (ready.built.candidate_path.parent / "note.md").read_text(encoding="utf-8")
    note_html = (ready.built.candidate_path.parent / "note.html").read_text(encoding="utf-8")
    assert preview["candidate_json"] == candidate
    assert preview["note_markdown"] == note_md
    assert preview["note_html"] == note_html
    assert preview["candidate_sha256"] == ready.built.result.candidate.sha256
    assert preview["target"]["parent_key"] == "PARENT1"
    assert preview["authorization_present_for_attempt"] is False


def test_authorization_presence_rejects_escaping_ref_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = _ready_write_run(tmp_path)
    secret = tmp_path / "external-secret.json"
    secret.write_bytes(canonical_json_bytes({"secret": True}))
    run = json.loads(ready.built.run_path.read_text(encoding="utf-8"))
    run["artifacts"].append(
        {
            "role": "write_authorization",
            "path": str(secret),
            "sha256": sha256_bytes(secret.read_bytes()),
            "size_bytes": secret.stat().st_size,
            "media_type": "application/json",
        }
    )
    _json(ready.built.run_path, run)
    observed = _guard_secret_read(monkeypatch, secret)

    with pytest.raises(BatchRuntimeError) as exc_info:
        write_module._authorization_present_for_attempt(
            ready.built.result,
            claim_id="claim",
            write_attempt_id="attempt",
        )

    assert exc_info.value.code == "authorization_tampered"
    assert observed == []


def test_begin_rejects_wrong_internal_authorization_topology_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    secret = ready.built.run_dir / "wrong-place" / "authorization.json"
    secret.parent.mkdir()
    secret.write_bytes(canonical_json_bytes({"secret": True}))
    observed = _guard_secret_read(monkeypatch, secret)

    with pytest.raises(BatchRuntimeError) as exc_info:
        begin_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            authorization_path=secret,
            request_id=REQUEST_WRITE_BEGIN,
            now="2026-07-10T00:00:06Z",
        )

    assert exc_info.value.code == "authorization_tampered"
    assert observed == []


def test_started_authorization_lookup_rejects_wrong_topology_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = _ready_write_run(tmp_path)
    secret = ready.built.run_dir / "wrong-place" / "authorization.json"
    secret.parent.mkdir()
    secret.write_bytes(canonical_json_bytes({"secret": True}))
    digest = sha256_bytes(secret.read_bytes())
    run = json.loads(ready.built.run_path.read_text(encoding="utf-8"))
    run["artifacts"].append(
        {
            "role": "write_authorization",
            "path": secret.relative_to(ready.built.run_dir).as_posix(),
            "sha256": digest,
            "size_bytes": secret.stat().st_size,
            "media_type": "application/json",
        }
    )
    _json(ready.built.run_path, run)
    observed = _guard_secret_read(monkeypatch, secret)

    with pytest.raises(BatchRuntimeError) as exc_info:
        write_module._authorization_path_for_digest(
            load_run_view(ready.run_dir),
            item_id="001",
            authorization_sha256=digest,
        )

    assert exc_info.value.code == "authorization_tampered"
    assert observed == []


@pytest.mark.parametrize("artifact_kind", ["verification", "reconciliation"])
def test_readback_rejects_external_path_before_read(
    artifact_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader_batch.v2_write import claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(ready, claimed)
    view = load_run_view(ready.run_dir)
    _path, authorization_raw, authorization, _candidate = write_module._load_authorization(
        view,
        item_id="001",
        authorization_path=authorization_path,
        claim_id=str(claimed["claim_id"]),
        write_attempt_id=str(claimed["write_attempt_id"]),
    )
    secret = tmp_path / f"external-{artifact_kind}.json"
    secret.write_bytes(canonical_json_bytes({"secret": True}))
    observed = _guard_secret_read(monkeypatch, secret)

    with pytest.raises(BatchRuntimeError) as exc_info:
        if artifact_kind == "verification":
            write_module._load_verification(
                view,
                item_id="001",
                verification_ref=_outer_ref(
                    secret,
                    "paper_reader.verification.v2",
                    "verification_SECRET",
                ),
                authorization_raw=authorization_raw,
                authorization=authorization,
            )
        else:
            write_module._load_reconciliation_readback(
                view,
                item_id="001",
                readback_path=secret,
                authorization_raw=authorization_raw,
                authorization=authorization,
            )

    assert exc_info.value.code == f"{artifact_kind}_tampered"
    assert observed == []


def test_renew_and_release_write_bind_the_exact_claim_attempt_and_token(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import claim_write, release_write, renew_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result

    renewed = renew_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        request_id=REQUEST_WRITE_RENEW,
        now="2026-07-10T00:00:04Z",
    )
    replay = renew_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        request_id=REQUEST_WRITE_RENEW,
        now="2026-07-10T00:00:04Z",
    )
    assert renewed.result["expires_at"] == "2026-07-10T00:02:04.000000Z"
    assert replay.replayed is True
    assert replay.result == renewed.result

    released = release_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        request_id=REQUEST_WRITE_RELEASE,
        now="2026-07-10T00:00:05Z",
    )
    assert released.result["status"] == "queued"
    state_item = load_run_view(ready.run_dir).state.items[0]
    assert state_item.write_status == "queued"
    assert state_item.write_lease is None


def test_started_write_can_renew_for_mcp_readback_but_cannot_release(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import (
        begin_write,
        claim_write,
        release_write,
        renew_write,
    )

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(ready, claimed)
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

    renewed = renew_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        request_id=REQUEST_WRITE_RENEW,
        now="2026-07-10T00:00:07Z",
    )
    assert renewed.result["status"] == "lease_extended"
    assert load_run_view(ready.run_dir).state.items[0].write_status == "started"

    with pytest.raises(BatchRuntimeError) as exc_info:
        release_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            request_id=REQUEST_WRITE_RELEASE,
            now="2026-07-10T00:00:08Z",
        )
    assert exc_info.value.code == "write_lease_inactive"


def test_begin_write_commits_started_before_returning_one_exact_envelope(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, authorization = _make_authorization(ready, claimed)

    first = begin_write(
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
    replay = begin_write(
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

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.result == first.result
    assert first.result["mcp_envelope"] == authorization["mcp_envelope"]
    assert first.result["delivery_rule"] == "send_only_when_command_result.replayed_is_false"
    view = load_run_view(ready.run_dir)
    item = view.state.items[0]
    assert item.write_status == "started"
    assert item.write_started_event_sha256 == view.events[-1].event_sha256
    assert item.authorization_sha256 == sha256_bytes(authorization_path.read_bytes())
    assert item.authorization_nonce_sha256 == sha256_bytes(str(authorization["nonce"]).encode())

    with pytest.raises(BatchRuntimeError) as exc_info:
        begin_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            authorization_path=authorization_path,
            request_id=REQUEST_WRITE_BEGIN_OTHER,
            now="2026-07-10T00:00:07Z",
        )
    assert exc_info.value.code == "write_lease_inactive"


def test_begin_same_request_changed_identity_conflicts_without_mutation(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(ready, claimed)
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
    before = _tree_snapshot(ready.run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        begin_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id="11111111-2222-4333-8444-555555555555",
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            authorization_path=authorization_path,
            request_id=REQUEST_WRITE_BEGIN,
            now="2026-07-10T00:00:06Z",
        )

    assert exc_info.value.code == "idempotency_conflict"
    assert _tree_snapshot(ready.run_dir) == before


def test_begin_rejects_unbound_empty_tombstone_in_authorization_sidecar(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, authorization = _make_authorization(ready, claimed)
    sidecar = authorization_path.with_suffix("")
    tombstone = sidecar / f".extra.{'a' * 32}.deleting"
    tombstone.write_bytes(b"")

    with pytest.raises(BatchRuntimeError) as exc_info:
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

    assert exc_info.value.code == "authorization_tampered"
    assert tombstone.exists()
    assert authorization["authorization_id"] in str(sidecar)


def test_begin_rejects_authorization_with_less_than_thirty_seconds_remaining(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(
        ready,
        claimed,
        expires_at="2026-07-10T00:00:35Z",
        ttl_seconds=30,
    )
    events_before = tuple((ready.run_dir / "events").iterdir())

    with pytest.raises(BatchRuntimeError) as exc_info:
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

    assert exc_info.value.code == "authorization_expiring"
    assert tuple((ready.run_dir / "events").iterdir()) == events_before


def test_begin_accepts_future_single_authorization_extra_field(tmp_path: Path) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, authorization = _make_authorization(ready, claimed)
    authorization["future_single_owned_metadata"] = {"revision": 3}
    _rewrite_authorization(ready, authorization_path, authorization)

    started = begin_write(
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

    assert started.result["mcp_envelope"] == authorization["mcp_envelope"]


@pytest.mark.parametrize(
    ("field_path", "invalid"),
    [
        (("ttl_seconds",), True),
        (("content_length",), 1.0),
        (("live_preflight", "title_available"), 1),
        (("external_claim_id",), ""),
        (("candidate", "size_bytes"), False),
    ],
)
def test_begin_rejects_ambiguous_or_falsey_consumed_authorization_types(
    field_path: tuple[str, ...],
    invalid: object,
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, authorization = _make_authorization(ready, claimed)
    target: dict[str, object] = authorization
    for field in field_path[:-1]:
        nested = target[field]
        assert isinstance(nested, dict)
        target = nested
    target[field_path[-1]] = invalid
    _rewrite_authorization(ready, authorization_path, authorization)

    with pytest.raises(BatchRuntimeError) as exc_info:
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

    assert exc_info.value.code == "authorization_tampered"


def test_begin_crash_after_started_event_replays_envelope_as_do_not_send_again(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, authorization = _make_authorization(ready, claimed)

    def crash_after_event(stage: str) -> None:
        if stage == "after_event":
            raise RuntimeError("injected process crash after write.started")

    with pytest.raises(RuntimeError, match="injected process crash"):
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
            fault=crash_after_event,
        )

    replay = begin_write(
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

    assert replay.replayed is True
    assert replay.result["mcp_envelope"] == authorization["mcp_envelope"]
    assert replay.result["delivery_rule"] == "send_only_when_command_result.replayed_is_false"
    view = load_run_view(ready.run_dir)
    assert sum(event.data.kind == "write.started" for event in view.events) == 1
    assert view.state.items[0].write_status == "started"


def test_replay_rejects_tampered_started_authorization_closure(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(ready, claimed)
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
    authorization_path.write_bytes(b"{}")

    with pytest.raises(BatchRuntimeError) as exc_info:
        load_run_view(ready.run_dir)

    assert exc_info.value.code == "journal_corrupt"


@pytest.mark.parametrize(
    "failed_check",
    [
        "note_key",
        "item_type",
        "parent_key",
        "note_title",
        "tag_set",
        "required_headings",
        "forbidden_headings",
        "minimum_content_length",
        "content_length",
        "content_sha256",
    ],
)
def test_commit_rejects_each_missing_strong_verification_check(
    tmp_path: Path,
    failed_check: str,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write, commit_write

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
    verification_path = _make_verification(
        ready,
        authorization_path,
        authorization,
        failed_check=failed_check,
    )
    result_path = _make_write_result(
        ready,
        claimed,
        authorization_path,
        authorization,
        verification_path,
    )
    events_before = tuple((ready.run_dir / "events").iterdir())

    with pytest.raises(BatchRuntimeError) as exc_info:
        commit_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            result_path=result_path,
            request_id=REQUEST_WRITE_COMMIT,
            now="2026-07-10T00:00:08Z",
        )

    assert exc_info.value.code == "verification_failed"
    assert tuple((ready.run_dir / "events").iterdir()) == events_before


def test_commit_write_accepts_only_full_passed_verification_and_publishes_result(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write, commit_write

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
    verification_path = _make_verification(ready, authorization_path, authorization)
    result_path = _make_write_result(
        ready,
        claimed,
        authorization_path,
        authorization,
        verification_path,
    )

    committed = commit_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        result_path=result_path,
        request_id=REQUEST_WRITE_COMMIT,
        now="2026-07-10T00:00:08Z",
    )

    assert committed.result["status"] == "written"
    assert committed.result["note_key"] == "NOTE1"
    item = load_run_view(ready.run_dir).state.items[0]
    assert item.write_status == "written"
    durable = ready.run_dir / "results" / "write" / f"{item.write_result_sha256}.json"
    assert durable.read_bytes() == result_path.read_bytes()
    events_before = tuple(
        path.read_bytes() for path in sorted((ready.run_dir / "events").glob("*.json"))
    )
    state_before = (ready.run_dir / "state.json").read_bytes()
    result_path.rename(tmp_path / "moved-write-result.json")

    replayed = commit_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        result_path=result_path,
        request_id=REQUEST_WRITE_COMMIT,
        now="2026-07-10T00:00:08Z",
    )
    assert replayed.replayed is True
    assert replayed.result == committed.result
    assert tuple(
        path.read_bytes() for path in sorted((ready.run_dir / "events").glob("*.json"))
    ) == events_before
    assert (ready.run_dir / "state.json").read_bytes() == state_before


def test_commit_crash_after_result_leaves_ignorable_orphan_then_commits_once(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write, commit_write

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
    verification_path = _make_verification(ready, authorization_path, authorization)
    result_path = _make_write_result(
        ready,
        claimed,
        authorization_path,
        authorization,
        verification_path,
    )

    def crash_after_result(stage: str) -> None:
        if stage == "after_result":
            raise RuntimeError("injected crash after durable result")

    with pytest.raises(RuntimeError, match="durable result"):
        commit_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            result_path=result_path,
            request_id=REQUEST_WRITE_COMMIT,
            now="2026-07-10T00:00:08Z",
            fault=crash_after_result,
        )
    before_retry = load_run_view(ready.run_dir)
    assert before_retry.state.items[0].write_status == "started"
    assert sum(event.data.kind == "write.written" for event in before_retry.events) == 0
    assert len(list((ready.run_dir / "results" / "write").iterdir())) == 1

    committed = commit_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        result_path=result_path,
        request_id=REQUEST_WRITE_COMMIT,
        now="2026-07-10T00:00:08Z",
    )
    assert committed.replayed is False
    final = load_run_view(ready.run_dir)
    assert final.state.items[0].write_status == "written"
    assert sum(event.data.kind == "write.written" for event in final.events) == 1


def test_mark_uncertain_is_started_only_and_never_requeues_the_attempt(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write, mark_write_uncertain

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(ready, claimed)
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

    first = mark_write_uncertain(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        reason="MCP response was lost after dispatch",
        request_id=REQUEST_WRITE_UNCERTAIN,
        now="2026-07-10T00:00:08Z",
    )
    replay = mark_write_uncertain(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        reason="MCP response was lost after dispatch",
        request_id=REQUEST_WRITE_UNCERTAIN,
        now="2026-07-10T00:00:08Z",
    )

    assert first.result["status"] == "uncertain"
    assert replay.replayed is True
    item = load_run_view(ready.run_dir).state.items[0]
    assert item.write_status == "uncertain"
    assert item.write_lease is None
    assert item.write_last_attempt_id == claimed["write_attempt_id"]


def test_zero_match_reconcile_requires_ack_then_new_attempt_rejects_old_authorization(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import (
        begin_write,
        claim_write,
        mark_write_uncertain,
        reconcile_write,
        retry_write,
    )

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
    mark_write_uncertain(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        reason="lost response",
        request_id=REQUEST_WRITE_UNCERTAIN,
        now="2026-07-10T00:00:08Z",
    )
    reconciliation_path = _make_reconciliation_not_found(
        ready,
        authorization_path,
        authorization,
    )

    reconciled = reconcile_write(
        ready.run_dir,
        "001",
        readback_path=reconciliation_path,
        request_id=REQUEST_WRITE_RECONCILE,
        now="2026-07-10T00:00:10Z",
    )
    assert reconciled.result["outcome"] == "not_found"
    assert load_run_view(ready.run_dir).state.items[0].write_status == "retry_confirmation_required"
    replayed = reconcile_write(
        ready.run_dir,
        "001",
        readback_path=reconciliation_path,
        request_id=REQUEST_WRITE_RECONCILE,
        now="2026-07-10T00:00:10Z",
    )
    assert replayed.replayed is True
    assert replayed.result == reconciled.result

    with pytest.raises(BatchRuntimeError) as no_ack:
        retry_write(
            ready.run_dir,
            "001",
            acknowledge_no_match=False,
            request_id=REQUEST_WRITE_RETRY,
            now="2026-07-10T00:00:11Z",
        )
    assert no_ack.value.code == "acknowledgement_required"

    retried = retry_write(
        ready.run_dir,
        "001",
        acknowledge_no_match=True,
        request_id=REQUEST_WRITE_RETRY,
        now="2026-07-10T00:00:11Z",
    )
    assert retried.result["status"] == "queued"
    with pytest.raises(BatchRuntimeError) as conflicting_ack:
        retry_write(
            ready.run_dir,
            "001",
            acknowledge_no_match=False,
            request_id=REQUEST_WRITE_RETRY,
            now="2026-07-10T00:00:11Z",
        )
    assert conflicting_ack.value.code == "idempotency_conflict"
    second = claim_write(
        ready.run_dir,
        writer_id="writer-2",
        request_id=REQUEST_WRITE_CLAIM_RETRY,
        now="2026-07-10T00:00:12Z",
    ).result
    assert second["attempt_number"] == 2
    assert second["write_attempt_id"] != claimed["write_attempt_id"]
    assert second["claim_id"] != claimed["claim_id"]

    with pytest.raises(BatchRuntimeError) as stale:
        begin_write(
            ready.run_dir,
            "001",
            writer_id="writer-2",
            claim_id=second["claim_id"],
            lease_token=second["lease_token"],
            write_attempt_id=second["write_attempt_id"],
            authorization_path=authorization_path,
            request_id=REQUEST_WRITE_BEGIN_RETRY,
            now="2026-07-10T00:00:13Z",
        )
    assert stale.value.code == "authorization_identity_mismatch"


@pytest.mark.parametrize(
    ("note_keys", "verification_failure", "expected_outcome", "expected_status"),
    [
        (("NOTE1",), None, "verified", "written"),
        (("NOTE1",), "tag_set", "blocked", "blocked"),
        (("NOTE1", "NOTE2"), None, "ambiguous", "blocked"),
    ],
)
def test_reconcile_unique_requires_full_verify_and_many_blocks_without_selection(
    tmp_path: Path,
    note_keys: tuple[str, ...],
    verification_failure: str | None,
    expected_outcome: str,
    expected_status: str,
) -> None:
    from paper_reader_batch.v2_write import (
        begin_write,
        claim_write,
        mark_write_uncertain,
        reconcile_write,
    )

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
    mark_write_uncertain(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        reason="lost response",
        request_id=REQUEST_WRITE_UNCERTAIN,
        now="2026-07-10T00:00:08Z",
    )
    reconciliation_path = _make_reconciliation_matches(
        ready,
        authorization_path,
        authorization,
        note_keys=note_keys,
        verification_failure=verification_failure,
    )

    outcome = reconcile_write(
        ready.run_dir,
        "001",
        readback_path=reconciliation_path,
        request_id=REQUEST_WRITE_RECONCILE,
        now="2026-07-10T00:00:10Z",
    )

    assert outcome.result["outcome"] == expected_outcome
    item = load_run_view(ready.run_dir).state.items[0]
    assert item.write_status == expected_status
    durable = ready.run_dir / "results" / "reconcile" / f"{item.reconciliation_sha256}.json"
    assert durable.is_file()


@pytest.mark.parametrize("outcome", ["verified", "blocked"])
def test_unique_reconciliation_requires_embedded_strong_verification(
    outcome: str,
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import (
        begin_write,
        claim_write,
        mark_write_uncertain,
        reconcile_write,
    )

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
    mark_write_uncertain(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        reason="lost response",
        request_id=REQUEST_WRITE_UNCERTAIN,
        now="2026-07-10T00:00:08Z",
    )
    reconciliation_path = _make_reconciliation_matches(
        ready,
        authorization_path,
        authorization,
        note_keys=("NOTE1",),
        omit_verification_outcome=outcome,
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        reconcile_write(
            ready.run_dir,
            "001",
            readback_path=reconciliation_path,
            request_id=REQUEST_WRITE_RECONCILE,
            now="2026-07-10T00:00:10Z",
        )

    assert exc_info.value.code == "reconciliation_tampered"


@pytest.mark.parametrize(
    ("top_key", "data_key"),
    [("bad/key", "bad/key"), (None, ""), ("NOTE1", "NOTE2")],
)
def test_exact_reconciliation_match_rejects_invalid_missing_or_mismatched_key(
    top_key: str | None,
    data_key: str,
) -> None:
    note_html = "<h1>[Codex Summary] Paper</h1><p>exact body</p>"
    authorization = write_module._OpaqueRecord(
        {
            "target": {"parent_key": "PARENT1"},
            "note_title": "[Codex Summary] Paper",
            "content_sha256": sha256_bytes(note_html.encode("utf-8")),
        }
    )
    child = {
        "data": {
            "key": data_key,
            "itemType": "note",
            "parentItem": "PARENT1",
            "note": note_html,
        }
    }
    if top_key is not None:
        child["key"] = top_key

    with pytest.raises(BatchRuntimeError) as exc_info:
        write_module._reconciliation_exact_match_key(child, authorization)

    assert exc_info.value.code == "reconciliation_tampered"


@pytest.mark.parametrize(
    ("top_key", "data_key"),
    [("NOTE1", ""), (None, "NOTE1"), ("NOTE1", "NOTE1")],
)
def test_exact_reconciliation_match_accepts_single_locator_key_shapes(
    top_key: str | None,
    data_key: str,
) -> None:
    note_html = "<h1>[Codex Summary] Paper</h1><p>exact body</p>"
    authorization = write_module._OpaqueRecord(
        {
            "target": {"parent_key": "PARENT1"},
            "note_title": "[Codex Summary] Paper",
            "content_sha256": sha256_bytes(note_html.encode("utf-8")),
        }
    )
    child = {
        "data": {
            "key": data_key,
            "itemType": "note",
            "parentItem": "PARENT1",
            "note": note_html,
        }
    }
    if top_key is not None:
        child["key"] = top_key

    assert write_module._reconciliation_exact_match_key(child, authorization) == "NOTE1"


def test_consumed_heading_parser_matches_single_visible_text_semantics() -> None:
    parser = write_module._HeadingParser()
    parser.feed(
        "<h1> A  &amp;amp;\n B </h1>"
        "<h2> 30   秒结论 </h2>"
        "<h3>not a required heading</h3>"
    )

    assert parser.title == "A &amp; B"
    assert parser.headings == ["30 秒结论"]


def test_begin_accepts_parent_snapshot_with_equivalent_visible_html_title(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, authorization = _make_authorization(ready, claimed)
    parent_path = authorization_path.with_suffix("") / "parent.json"
    parent = json.loads(parent_path.read_text(encoding="utf-8"))
    parent["data"]["title"] = "<b>A Useful Paper &amp; Result</b>"
    parent_raw = canonical_json_bytes(parent)
    parent_path.write_bytes(parent_raw)
    parent_ref = next(
        ref
        for ref in authorization["artifacts"]
        if ref["role"] == "zotero_parent_snapshot"
    )
    parent_ref["sha256"] = sha256_bytes(parent_raw)
    parent_ref["size_bytes"] = len(parent_raw)
    authorization["live_preflight"]["parent_snapshot"] = dict(parent_ref)
    _rewrite_authorization(ready, authorization_path, authorization)

    result = begin_write(
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

    assert result.result["authorization_sha256"] == sha256_bytes(
        authorization_path.read_bytes()
    )


def test_commit_rejects_non_array_snapshot_tags_with_structured_error(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write, commit_write

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
    verification_path = _make_verification(
        ready,
        authorization_path,
        authorization,
        snapshot_tags=7,
    )
    result_path = _make_write_result(
        ready,
        claimed,
        authorization_path,
        authorization,
        verification_path,
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        commit_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            result_path=result_path,
            request_id=REQUEST_WRITE_COMMIT,
            now="2026-07-10T00:00:08Z",
        )

    assert exc_info.value.code == "verification_failed"


def test_reconcile_rejects_non_array_snapshot_tags_with_structured_error(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import (
        begin_write,
        claim_write,
        mark_write_uncertain,
        reconcile_write,
    )

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
    mark_write_uncertain(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        reason="lost response",
        request_id=REQUEST_WRITE_UNCERTAIN,
        now="2026-07-10T00:00:08Z",
    )
    reconciliation_path = _make_reconciliation_matches(
        ready,
        authorization_path,
        authorization,
        note_keys=("NOTE1",),
        snapshot_tags=7,
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        reconcile_write(
            ready.run_dir,
            "001",
            readback_path=reconciliation_path,
            request_id=REQUEST_WRITE_RECONCILE,
            now="2026-07-10T00:00:10Z",
        )

    assert exc_info.value.code == "reconciliation_tampered"


def test_commit_replay_changed_result_path_conflicts_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write, commit_write

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
    verification_path = _make_verification(ready, authorization_path, authorization)
    result_path = _make_write_result(
        ready,
        claimed,
        authorization_path,
        authorization,
        verification_path,
    )
    commit_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        result_path=result_path,
        request_id=REQUEST_WRITE_COMMIT,
        now="2026-07-10T00:00:08Z",
    )
    secret = tmp_path / "external-secret-result.json"
    secret.write_bytes(canonical_json_bytes({"secret": True}))
    observed = _guard_secret_read(monkeypatch, secret)
    before = _tree_snapshot(ready.run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        commit_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            result_path=secret,
            request_id=REQUEST_WRITE_COMMIT,
            now="2026-07-10T00:00:08Z",
        )

    assert exc_info.value.code == "idempotency_conflict"
    assert observed == []
    assert _tree_snapshot(ready.run_dir) == before


def test_reconcile_replay_changed_readback_path_conflicts_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader_batch.v2_write import (
        begin_write,
        claim_write,
        mark_write_uncertain,
        reconcile_write,
    )

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
    mark_write_uncertain(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        reason="lost response",
        request_id=REQUEST_WRITE_UNCERTAIN,
        now="2026-07-10T00:00:08Z",
    )
    reconciliation_path = _make_reconciliation_not_found(
        ready,
        authorization_path,
        authorization,
    )
    reconcile_write(
        ready.run_dir,
        "001",
        readback_path=reconciliation_path,
        request_id=REQUEST_WRITE_RECONCILE,
        now="2026-07-10T00:00:10Z",
    )
    secret = tmp_path / "external-secret-reconciliation.json"
    secret.write_bytes(canonical_json_bytes({"secret": True}))
    observed = _guard_secret_read(monkeypatch, secret)
    before = _tree_snapshot(ready.run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        reconcile_write(
            ready.run_dir,
            "001",
            readback_path=secret,
            request_id=REQUEST_WRITE_RECONCILE,
            now="2026-07-10T00:00:10Z",
        )

    assert exc_info.value.code == "idempotency_conflict"
    assert observed == []
    assert _tree_snapshot(ready.run_dir) == before


@pytest.mark.parametrize("fault_stage", ["after_pre_recovery_validation", "before_event"])
def test_begin_revalidates_authorization_until_event_publication(
    tmp_path: Path,
    fault_stage: str,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(ready, claimed)
    events_before = _tree_snapshot(ready.run_dir / "events")
    state_before = (ready.run_dir / "state.json").read_bytes()
    mutated = False

    def mutate_authorization(stage: str) -> None:
        nonlocal mutated
        if stage == fault_stage and not mutated:
            mutated = True
            authorization_path.write_bytes(b"{}")

    with pytest.raises(BatchRuntimeError):
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
            fault=mutate_authorization,
        )

    assert mutated is True
    assert _tree_snapshot(ready.run_dir / "events") == events_before
    assert (ready.run_dir / "state.json").read_bytes() == state_before
    assert load_run_view(ready.run_dir).state.items[0].write_status == "claimed"


def test_begin_real_clock_rejects_authorization_that_loses_final_thirty_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(
        ready,
        claimed,
        created_at="2026-07-10T00:00:05Z",
        expires_at="2026-07-10T00:00:37Z",
        ttl_seconds=32,
    )
    events_before = _tree_snapshot(ready.run_dir / "events")
    state_before = (ready.run_dir / "state.json").read_bytes()
    advanced = False

    def current_time() -> str:
        return (
            "2026-07-10T00:00:08.000000Z"
            if advanced
            else "2026-07-10T00:00:06.000000Z"
        )

    def advance_after_preflight(stage: str) -> None:
        nonlocal advanced
        if stage == "after_pre_recovery_validation":
            advanced = True

    monkeypatch.setattr(journal_module, "utc_now", current_time)

    with pytest.raises(BatchRuntimeError) as exc_info:
        begin_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            authorization_path=authorization_path,
            request_id=REQUEST_WRITE_BEGIN,
            fault=advance_after_preflight,
        )

    assert exc_info.value.code == "authorization_expiring"
    assert _tree_snapshot(ready.run_dir / "events") == events_before
    assert (ready.run_dir / "state.json").read_bytes() == state_before
    assert load_run_view(ready.run_dir).state.items[0].write_status == "claimed"


def test_expired_fsynced_begin_is_aborted_and_write_claim_remains_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write, renew_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(
        ready,
        claimed,
        created_at="2026-07-10T00:00:05Z",
        expires_at="2026-07-10T00:00:37Z",
        ttl_seconds=32,
    )
    advanced = False

    def current_time() -> str:
        return (
            "2026-07-10T00:00:08.000000Z"
            if advanced
            else "2026-07-10T00:00:06.000000Z"
        )

    def advance_after_event_fsync(stage: str) -> None:
        nonlocal advanced
        if stage == "after_writing_fsync":
            advanced = True

    monkeypatch.setattr(journal_module, "utc_now", current_time)
    arguments = {
        "run_dir": ready.run_dir,
        "item_id": "001",
        "writer_id": "writer-1",
        "claim_id": claimed["claim_id"],
        "lease_token": claimed["lease_token"],
        "write_attempt_id": claimed["write_attempt_id"],
        "authorization_path": authorization_path,
        "request_id": REQUEST_WRITE_BEGIN,
    }

    with pytest.raises(BatchRuntimeError) as first_error:
        begin_write(**arguments, fault=advance_after_event_fsync)
    assert first_error.value.code == "authorization_expiring"
    view = load_run_view(ready.run_dir)
    assert view.pending_event is None
    assert len(view.aborted_events) == 1
    assert view.state.items[0].write_status == "claimed"

    with pytest.raises(BatchRuntimeError) as replay_error:
        begin_write(**arguments)

    assert replay_error.value.code == "request_aborted"

    renewed = renew_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        request_id=REQUEST_WRITE_RENEW,
    )
    assert renewed.result["item_id"] == "001"


def test_postrename_clock_advance_cannot_reverse_committed_begin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader_batch.v2_run import recover_run
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(
        ready,
        claimed,
        created_at="2026-07-10T00:00:05Z",
        expires_at="2026-07-10T00:00:37Z",
        ttl_seconds=32,
    )
    advanced = False

    def current_time() -> str:
        return (
            "2026-07-10T00:00:08.000000Z"
            if advanced
            else "2026-07-10T00:00:06.000000Z"
        )

    def advance_after_commit(stage: str) -> None:
        nonlocal advanced
        if stage == "after_rename":
            advanced = True

    monkeypatch.setattr(journal_module, "utc_now", current_time)
    outcome = begin_write(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        authorization_path=authorization_path,
        request_id=REQUEST_WRITE_BEGIN,
        fault=advance_after_commit,
    )

    assert outcome.replayed is False
    committed = load_run_view(ready.run_dir)
    assert committed.pending_event is None
    assert committed.state.items[0].write_status == "started"

    recover_run(
        ready.run_dir,
        request_id="12121212-1212-4212-8212-121212121212",
        now="2026-07-10T00:02:04Z",
    )
    assert load_run_view(ready.run_dir).state.items[0].write_status == "uncertain"


@pytest.mark.parametrize(
    "fault_stage",
    ["after_pre_recovery_validation", "after_result", "before_event"],
)
def test_commit_revalidates_verification_until_event_publication(
    tmp_path: Path,
    fault_stage: str,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write, commit_write

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
    verification_path = _make_verification(ready, authorization_path, authorization)
    result_path = _make_write_result(
        ready,
        claimed,
        authorization_path,
        authorization,
        verification_path,
    )
    events_before = _tree_snapshot(ready.run_dir / "events")
    state_before = (ready.run_dir / "state.json").read_bytes()
    mutated = False

    def mutate_verification(stage: str) -> None:
        nonlocal mutated
        if stage == fault_stage and not mutated:
            mutated = True
            verification_path.write_bytes(b"{}")

    with pytest.raises(BatchRuntimeError):
        commit_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            result_path=result_path,
            request_id=REQUEST_WRITE_COMMIT,
            now="2026-07-10T00:00:08Z",
            fault=mutate_verification,
        )

    assert mutated is True
    assert _tree_snapshot(ready.run_dir / "events") == events_before
    assert (ready.run_dir / "state.json").read_bytes() == state_before
    assert load_run_view(ready.run_dir).state.items[0].write_status == "started"


@pytest.mark.parametrize(
    "fault_stage",
    ["after_pre_recovery_validation", "after_result", "before_event"],
)
def test_reconcile_revalidates_readback_until_event_publication(
    tmp_path: Path,
    fault_stage: str,
) -> None:
    from paper_reader_batch.v2_write import (
        begin_write,
        claim_write,
        mark_write_uncertain,
        reconcile_write,
    )

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
    mark_write_uncertain(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        reason="lost response",
        request_id=REQUEST_WRITE_UNCERTAIN,
        now="2026-07-10T00:00:08Z",
    )
    reconciliation_path = _make_reconciliation_not_found(
        ready,
        authorization_path,
        authorization,
    )
    events_before = _tree_snapshot(ready.run_dir / "events")
    state_before = (ready.run_dir / "state.json").read_bytes()
    mutated = False

    def mutate_readback(stage: str) -> None:
        nonlocal mutated
        if stage == fault_stage and not mutated:
            mutated = True
            reconciliation_path.write_bytes(b"{}")

    with pytest.raises(BatchRuntimeError):
        reconcile_write(
            ready.run_dir,
            "001",
            readback_path=reconciliation_path,
            request_id=REQUEST_WRITE_RECONCILE,
            now="2026-07-10T00:00:10Z",
            fault=mutate_readback,
        )

    assert mutated is True
    assert _tree_snapshot(ready.run_dir / "events") == events_before
    assert (ready.run_dir / "state.json").read_bytes() == state_before
    assert load_run_view(ready.run_dir).state.items[0].write_status == "uncertain"


@pytest.mark.parametrize(
    ("fault_stage", "swap_target"),
    [
        ("before_event", "authorization_main"),
        ("before_event", "authorization_sidecar"),
        ("before_event", "run_json"),
        ("before_event", "single_lock"),
        ("after_pending_rename", "authorization_main"),
    ],
)
def test_begin_held_closure_rejects_same_byte_inode_replacement(
    tmp_path: Path,
    fault_stage: str,
    swap_target: str,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(ready, claimed)
    targets = {
        "authorization_main": authorization_path,
        "authorization_sidecar": authorization_path.with_suffix(""),
        "run_json": ready.built.run_path,
        "single_lock": ready.built.run_dir / ".run.lock",
    }
    target = targets[swap_target]
    backup_root = tmp_path / f"swap-{swap_target}"
    backup_root.mkdir()
    event_bytes_before = tuple(
        path.read_bytes()
        for path in sorted((ready.run_dir / "events").glob("*.json"))
        if not path.name.startswith(".")
    )
    state_before = (ready.run_dir / "state.json").read_bytes()
    mutated = False

    def replace_same_bytes(stage: str) -> None:
        nonlocal mutated
        if stage != fault_stage or mutated:
            return
        mutated = True
        if target.is_dir():
            _replace_directory_with_same_bytes(target, backup_root)
        else:
            _replace_file_with_same_bytes(target, backup_root)

    with pytest.raises(BatchRuntimeError):
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
            fault=replace_same_bytes,
        )

    assert mutated is True
    event_bytes_after = tuple(
        path.read_bytes()
        for path in sorted((ready.run_dir / "events").glob("*.json"))
        if not path.name.startswith(".")
    )
    if fault_stage == "after_pending_rename":
        assert event_bytes_after[:-1] == event_bytes_before
        assert json.loads(event_bytes_after[-1])["data"]["kind"] == "request.aborted"
    else:
        assert event_bytes_after == event_bytes_before
    assert (ready.run_dir / "state.json").read_bytes() == state_before
    assert len(load_run_view(ready.run_dir).aborted_events) == (
        1 if fault_stage == "after_pending_rename" else 0
    )


@pytest.mark.parametrize("swap_target", ["result", "verification"])
def test_commit_held_closure_rejects_same_byte_inode_replacement(
    tmp_path: Path,
    swap_target: str,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write, commit_write

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
    verification_path = _make_verification(ready, authorization_path, authorization)
    result_path = _make_write_result(
        ready,
        claimed,
        authorization_path,
        authorization,
        verification_path,
    )
    target = result_path if swap_target == "result" else verification_path
    backup_root = tmp_path / f"swap-commit-{swap_target}"
    backup_root.mkdir()
    event_bytes_before = tuple(
        path.read_bytes() for path in sorted((ready.run_dir / "events").glob("*.json"))
    )
    state_before = (ready.run_dir / "state.json").read_bytes()
    mutated = False

    def replace_same_bytes(stage: str) -> None:
        nonlocal mutated
        if stage == "after_result" and not mutated:
            mutated = True
            _replace_file_with_same_bytes(target, backup_root)

    with pytest.raises(BatchRuntimeError):
        commit_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            result_path=result_path,
            request_id=REQUEST_WRITE_COMMIT,
            now="2026-07-10T00:00:08Z",
            fault=replace_same_bytes,
        )

    assert mutated is True
    assert tuple(
        path.read_bytes() for path in sorted((ready.run_dir / "events").glob("*.json"))
    ) == event_bytes_before
    assert (ready.run_dir / "state.json").read_bytes() == state_before


def test_reconcile_held_closure_rejects_same_byte_children_replacement(
    tmp_path: Path,
) -> None:
    from paper_reader_batch.v2_write import (
        begin_write,
        claim_write,
        mark_write_uncertain,
        reconcile_write,
    )

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
    mark_write_uncertain(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        reason="lost response",
        request_id=REQUEST_WRITE_UNCERTAIN,
        now="2026-07-10T00:00:08Z",
    )
    reconciliation_path = _make_reconciliation_not_found(
        ready,
        authorization_path,
        authorization,
    )
    children_path = reconciliation_path.with_suffix("") / "children.json"
    backup_root = tmp_path / "swap-reconciliation-children"
    backup_root.mkdir()
    event_bytes_before = tuple(
        path.read_bytes() for path in sorted((ready.run_dir / "events").glob("*.json"))
    )
    state_before = (ready.run_dir / "state.json").read_bytes()
    mutated = False

    def replace_same_bytes(stage: str) -> None:
        nonlocal mutated
        if stage == "after_result" and not mutated:
            mutated = True
            _replace_file_with_same_bytes(children_path, backup_root)

    with pytest.raises(BatchRuntimeError):
        reconcile_write(
            ready.run_dir,
            "001",
            readback_path=reconciliation_path,
            request_id=REQUEST_WRITE_RECONCILE,
            now="2026-07-10T00:00:10Z",
            fault=replace_same_bytes,
        )

    assert mutated is True
    assert tuple(
        path.read_bytes() for path in sorted((ready.run_dir / "events").glob("*.json"))
    ) == event_bytes_before
    assert (ready.run_dir / "state.json").read_bytes() == state_before


def test_pending_begin_recovery_holds_external_closure_through_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write

    ready = _ready_write_run(tmp_path)
    claimed = claim_write(
        ready.run_dir,
        writer_id="writer-1",
        request_id=REQUEST_WRITE_CLAIM,
        now="2026-07-10T00:00:03Z",
    ).result
    authorization_path, _authorization = _make_authorization(ready, claimed)

    def crash_with_pending_event(stage: str) -> None:
        if stage == "after_file_fsync":
            raise RuntimeError("injected crash with pending write.started")

    with pytest.raises(RuntimeError, match="pending write.started"):
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
            fault=crash_with_pending_event,
        )
    pending = load_run_view(ready.run_dir)
    assert pending.pending_event is not None
    assert sum(event.data.kind == "write.started" for event in pending.events) == 0

    original_promote = journal_module.promote_bytes_no_replace
    mutated = False

    def mutate_then_promote(staging: Path, target: Path, expected: bytes, *, guard=None) -> None:
        nonlocal mutated
        if not mutated:
            mutated = True
            backup_root = tmp_path / "pending-promotion-swap"
            backup_root.mkdir()
            _replace_directory_with_same_bytes(
                authorization_path.with_suffix(""),
                backup_root,
            )
        original_promote(staging, target, expected, guard=guard)

    monkeypatch.setattr(journal_module, "promote_bytes_no_replace", mutate_then_promote)
    with pytest.raises(BatchRuntimeError) as exc_info:
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

    assert exc_info.value.code == "storage_path_changed"
    assert mutated is True
    after = load_run_view(ready.run_dir)
    assert after.pending_event is None
    assert len(after.aborted_events) == 1
    assert sum(event.data.kind == "write.started" for event in after.events) == 0
    assert after.state.items[0].write_status == "claimed"


def test_pending_commit_recovery_holds_verification_through_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader_batch.v2_write import begin_write, claim_write, commit_write

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
    verification_path = _make_verification(ready, authorization_path, authorization)
    result_path = _make_write_result(
        ready,
        claimed,
        authorization_path,
        authorization,
        verification_path,
    )
    with pytest.raises(RuntimeError, match="pending write.written"):
        commit_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            result_path=result_path,
            request_id=REQUEST_WRITE_COMMIT,
            now="2026-07-10T00:00:08Z",
            fault=_crash_on_file_fsync("pending write.written"),
        )
    original_promote = journal_module.promote_bytes_no_replace
    mutated = False

    def mutate_then_promote(staging: Path, target: Path, expected: bytes, *, guard=None) -> None:
        nonlocal mutated
        if not mutated:
            mutated = True
            backup_root = tmp_path / "pending-commit-swap"
            backup_root.mkdir()
            _replace_file_with_same_bytes(verification_path, backup_root)
        original_promote(staging, target, expected, guard=guard)

    monkeypatch.setattr(journal_module, "promote_bytes_no_replace", mutate_then_promote)
    with pytest.raises(BatchRuntimeError) as exc_info:
        commit_write(
            ready.run_dir,
            "001",
            writer_id="writer-1",
            claim_id=claimed["claim_id"],
            lease_token=claimed["lease_token"],
            write_attempt_id=claimed["write_attempt_id"],
            result_path=result_path,
            request_id=REQUEST_WRITE_COMMIT,
            now="2026-07-10T00:00:08Z",
        )

    assert mutated is True
    assert exc_info.value.code == "storage_path_changed"
    after = load_run_view(ready.run_dir)
    assert after.pending_event is None
    assert len(after.aborted_events) == 1
    assert sum(event.data.kind == "write.written" for event in after.events) == 0
    assert after.state.items[0].write_status == "started"


def test_pending_reconcile_recovery_holds_readback_through_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader_batch.v2_write import (
        begin_write,
        claim_write,
        mark_write_uncertain,
        reconcile_write,
    )

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
    mark_write_uncertain(
        ready.run_dir,
        "001",
        writer_id="writer-1",
        claim_id=claimed["claim_id"],
        lease_token=claimed["lease_token"],
        write_attempt_id=claimed["write_attempt_id"],
        reason="lost response",
        request_id=REQUEST_WRITE_UNCERTAIN,
        now="2026-07-10T00:00:08Z",
    )
    reconciliation_path = _make_reconciliation_not_found(
        ready,
        authorization_path,
        authorization,
    )
    with pytest.raises(RuntimeError, match="pending write.reconciled"):
        reconcile_write(
            ready.run_dir,
            "001",
            readback_path=reconciliation_path,
            request_id=REQUEST_WRITE_RECONCILE,
            now="2026-07-10T00:00:10Z",
            fault=_crash_on_file_fsync("pending write.reconciled"),
        )
    children_path = reconciliation_path.with_suffix("") / "children.json"
    original_promote = journal_module.promote_bytes_no_replace
    mutated = False

    def mutate_then_promote(staging: Path, target: Path, expected: bytes, *, guard=None) -> None:
        nonlocal mutated
        if not mutated:
            mutated = True
            backup_root = tmp_path / "pending-reconcile-swap"
            backup_root.mkdir()
            _replace_file_with_same_bytes(children_path, backup_root)
        assert guard is not None
        guard()
        original_promote(staging, target, expected, guard=guard)

    monkeypatch.setattr(journal_module, "promote_bytes_no_replace", mutate_then_promote)
    with pytest.raises(BatchRuntimeError) as exc_info:
        reconcile_write(
            ready.run_dir,
            "001",
            readback_path=reconciliation_path,
            request_id=REQUEST_WRITE_RECONCILE,
            now="2026-07-10T00:00:10Z",
        )

    assert mutated is True
    assert exc_info.value.code == "storage_path_changed"
    after = load_run_view(ready.run_dir)
    assert after.pending_event is None
    assert len(after.aborted_events) == 1
    assert sum(event.data.kind == "write.reconciled" for event in after.events) == 0
    assert after.state.items[0].write_status == "uncertain"
