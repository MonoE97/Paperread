from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

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


@dataclass(frozen=True)
class ReadyWriteRun:
    run_dir: Path
    built: BuiltWorkerFixture


def _ready_write_run(tmp_path: Path) -> ReadyWriteRun:
    built = _zotero_fixture(tmp_path / "single")
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
    nonce = "nonce_" + "n" * 37
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


def _make_verification(
    ready: ReadyWriteRun,
    authorization_path: Path,
    authorization: dict[str, object],
    *,
    note_key: str = "NOTE1",
    failed_check: str | None = None,
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
            "tags": [{"tag": tag} for tag in authorization["tags"]],
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
                "tags": [
                    {"tag": tag}
                    for index, tag in enumerate(authorization["tags"])
                    if verification_failure != "tag_set" or index > 0
                ],
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
    if len(note_keys) == 1:
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
    if len(note_keys) == 1:
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
