from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paper_reader.contracts import PaperReaderCommandResult, PaperReaderWriteAuthorization
from paper_reader.note import FORBIDDEN_RENDERED_HEADINGS, REQUIRED_SECTIONS
from paper_reader.public_cli import app

from test_v2_local_publication import _built_candidate as _built_local_candidate
from test_v2_zotero_candidate import (
    InMemoryZoteroProvider,
    _build,
    _note,
    _sealed_zotero_run,
    _parent,
)


NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def _module():
    module_name = "paper_reader.zotero_authorization"
    assert importlib.util.find_spec(module_name) is not None, "Zotero authorization module is missing"
    return importlib.import_module(module_name)


def _candidate(tmp_path: Path) -> tuple[Path, InMemoryZoteroProvider]:
    run_dir = _sealed_zotero_run(tmp_path)
    provider = InMemoryZoteroProvider()
    built = _build(run_dir, provider)
    return built.candidate_dir / "candidate.json", provider


def _authorize(
    candidate_path: Path,
    provider,
    *,
    now: datetime = NOW,
    ttl_seconds: int = 300,
    external_claim_id: str | None = None,
    write_attempt_id: str | None = None,
):
    return _module().authorize_zotero_candidate(
        candidate_path,
        provider=provider,
        now=now,
        ttl_seconds=ttl_seconds,
        external_claim_id=external_claim_id,
        write_attempt_id=write_attempt_id,
    )


def test_direct_authorization_binds_exact_envelope_and_returns_token_only_once(
    tmp_path: Path,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))

    authorized = _authorize(candidate_path, provider)

    authorization_path = authorized.authorization_dir / "authorization.json"
    raw_payload = json.loads(authorization_path.read_text(encoding="utf-8"))
    authorization = PaperReaderWriteAuthorization.model_validate_json(
        authorization_path.read_bytes()
    )
    assert "token" not in raw_payload
    assert authorization.token_sha256 == hashlib.sha256(
        authorized.write_token.encode("utf-8")
    ).hexdigest()
    assert len(authorized.write_token) >= 43
    assert authorization.external_claim_id.startswith("direct_")
    assert authorization.write_attempt_id.startswith("direct_")
    assert authorization.external_claim_id != authorization.write_attempt_id
    assert authorization.ttl_seconds == 300
    assert authorization.created_at == "2026-07-10T12:00:00Z"
    assert authorization.expires_at == "2026-07-10T12:05:00Z"
    assert authorization.note_title == candidate["note_title"]
    assert authorization.tags == tuple(candidate["tags"])
    assert authorization.required_headings == tuple(REQUIRED_SECTIONS)
    assert authorization.forbidden_headings == tuple(FORBIDDEN_RENDERED_HEADINGS)
    assert authorization.minimum_content_length == candidate["content_length"]
    exact_html = (candidate_path.parent / "note.html").read_text(encoding="utf-8")
    assert authorization.content_html == exact_html
    assert authorization.mcp_envelope.model_dump(mode="json") == {
        "action": "create",
        "parentKey": candidate["target"]["parent_key"],
        "content": exact_html,
        "tags": candidate["tags"],
    }
    assert authorization.live_preflight.parent_snapshot in authorization.artifacts
    assert authorization.live_preflight.children_snapshot in authorization.artifacts
    assert sorted(path.name for path in authorized.authorization_dir.iterdir()) == [
        "authorization.json",
        "candidate.json",
        "children.json",
        "content.html",
        "parent.json",
    ]
    run_dir = candidate_path.parent.parent.parent
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    auth_refs = [item for item in run["artifacts"] if item["role"] == "write_authorization"]
    assert len(auth_refs) == 1
    assert (run_dir / auth_refs[0]["path"]).read_bytes() == authorization_path.read_bytes()


def test_batch_authorization_preserves_both_external_identities(tmp_path: Path) -> None:
    candidate_path, provider = _candidate(tmp_path)

    authorized = _authorize(
        candidate_path,
        provider,
        ttl_seconds=90,
        external_claim_id="claim_batch_001",
        write_attempt_id="attempt_batch_001",
    )

    authorization = authorized.authorization
    assert authorization.external_claim_id == "claim_batch_001"
    assert authorization.write_attempt_id == "attempt_batch_001"
    assert authorization.ttl_seconds == 90
    assert authorization.expires_at == "2026-07-10T12:01:30Z"


@pytest.mark.parametrize(
    ("external_claim_id", "write_attempt_id", "ttl_seconds", "expected_code"),
    [
        ("claim_only", None, 300, "invalid_identity_options"),
        (None, "attempt_only", 300, "invalid_identity_options"),
        (None, None, 301, "invalid_authorization_ttl"),
        (None, None, 0, "invalid_authorization_ttl"),
    ],
)
def test_authorization_rejects_invalid_options_before_mutation(
    external_claim_id: str | None,
    write_attempt_id: str | None,
    ttl_seconds: int,
    expected_code: str,
    tmp_path: Path,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    run_dir = candidate_path.parent.parent.parent
    before = (run_dir / "run.json").read_bytes()

    with pytest.raises(Exception) as exc_info:
        _authorize(
            candidate_path,
            provider,
            ttl_seconds=ttl_seconds,
            external_claim_id=external_claim_id,
            write_attempt_id=write_attempt_id,
        )

    assert getattr(exc_info.value, "code", None) == expected_code
    assert (run_dir / "run.json").read_bytes() == before
    assert not (run_dir / "authorizations").exists()


def test_authorization_rejects_local_candidate_before_any_provider_read(tmp_path: Path) -> None:
    _run_dir, candidate_path = _built_local_candidate(tmp_path)

    class NetworkForbiddenProvider:
        def get_parent(self, _item_key: str):
            raise AssertionError("local candidate reached Zotero provider")

        def get_children(self, _parent_key: str):
            raise AssertionError("local candidate reached Zotero provider")

        def get_note(self, _note_key: str):
            raise AssertionError("local candidate reached Zotero provider")

    with pytest.raises(Exception) as exc_info:
        _authorize(candidate_path, NetworkForbiddenProvider())

    assert getattr(exc_info.value, "code", None) == "local_candidate_forbidden"


def test_authorization_detects_same_title_suffix_race_as_stale_candidate(
    tmp_path: Path,
) -> None:
    candidate_path, _provider = _candidate(tmp_path)
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    raced_provider = InMemoryZoteroProvider(
        children=[_note("RACE1", candidate["note_title"])]
    )
    run_dir = candidate_path.parent.parent.parent
    before = (run_dir / "run.json").read_bytes()

    with pytest.raises(Exception) as exc_info:
        _authorize(candidate_path, raced_provider)

    assert getattr(exc_info.value, "code", None) == "stale_candidate"
    assert (run_dir / "run.json").read_bytes() == before
    assert not (run_dir / "authorizations").exists()


def test_only_one_unexpired_authorization_can_bind_a_candidate_then_expiry_allows_new(
    tmp_path: Path,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    first = _authorize(candidate_path, provider, ttl_seconds=60)

    with pytest.raises(Exception) as exc_info:
        _authorize(candidate_path, provider, now=NOW + timedelta(seconds=30))

    assert getattr(exc_info.value, "code", None) == "authorization_active"

    second = _authorize(candidate_path, provider, now=NOW + timedelta(seconds=61))

    assert second.authorization.authorization_id != first.authorization.authorization_id
    assert second.authorization.nonce != first.authorization.nonce
    assert second.authorization.token_sha256 != first.authorization.token_sha256
    assert second.write_token != first.write_token


def test_unexpired_authorization_blocks_second_candidate_for_same_parent_and_title(
    tmp_path: Path,
) -> None:
    run_dir = _sealed_zotero_run(tmp_path)
    provider = InMemoryZoteroProvider()
    first_candidate = _build(run_dir, provider)
    second_candidate = _build(run_dir, provider)
    assert first_candidate.candidate.note_title == second_candidate.candidate.note_title
    _authorize(first_candidate.candidate_dir / "candidate.json", provider, ttl_seconds=60)

    with pytest.raises(Exception) as exc_info:
        _authorize(
            second_candidate.candidate_dir / "candidate.json",
            provider,
            now=NOW + timedelta(seconds=30),
            ttl_seconds=60,
        )

    assert getattr(exc_info.value, "code", None) == "authorization_active"
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert [item["role"] for item in run["artifacts"]].count("write_authorization") == 1


def test_authorize_cli_returns_one_command_result_with_plaintext_token_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, provider = _candidate(tmp_path)
    monkeypatch.setattr(module, "LocalApiZoteroReadProvider", lambda: provider)

    result = CliRunner().invoke(app, ["zotero", "authorize", str(candidate_path)])

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    assert payload["code"] == "authorized"
    assert payload["command"] == "zotero authorize"
    assert payload["data"]["write_token"]
    assert payload["data"]["external_claim_id"].startswith("direct_")
    assert payload["data"]["write_attempt_id"].startswith("direct_")
    authorization_path = Path(payload["data"]["authorization_path"])
    persisted = authorization_path.read_text(encoding="utf-8")
    assert payload["data"]["write_token"] not in persisted
    assert payload["data"]["mcp_envelope"]["action"] == "create"


def test_concurrent_authorize_allows_one_active_authorization(tmp_path: Path) -> None:
    candidate_path, provider = _candidate(tmp_path)

    def authorize_once():
        try:
            return _authorize(candidate_path, provider, ttl_seconds=60)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: authorize_once(), range(2)))

    successes = [item for item in outcomes if hasattr(item, "authorization")]
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert getattr(failures[0], "code", None) == "authorization_active"
    run_dir = candidate_path.parent.parent.parent
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert [item["role"] for item in run["artifacts"]].count("write_authorization") == 1


def test_authorization_rehashes_candidate_and_rechecks_parent_before_live_grant(
    tmp_path: Path,
) -> None:
    tamper_dir = tmp_path / "tamper"
    tamper_dir.mkdir()
    candidate_path, provider = _candidate(tamper_dir)
    (candidate_path.parent / "note.html").write_bytes(b"tampered candidate html")

    with pytest.raises(Exception) as tamper_error:
        _authorize(candidate_path, provider)

    assert getattr(tamper_error.value, "code", None) == "candidate_tampered"

    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    candidate_path, _provider = _candidate(parent_dir)

    with pytest.raises(Exception) as parent_error:
        _authorize(candidate_path, InMemoryZoteroProvider(parent=_parent(version=18)))

    assert getattr(parent_error.value, "code", None) == "stale_candidate"


def test_authorization_faults_and_size_gate_do_not_create_false_bound_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    from paper_reader.resource_policy import V2_RESOURCE_POLICY

    size_dir = tmp_path / "size"
    size_dir.mkdir()
    candidate_path, provider = _candidate(size_dir)
    run_dir = candidate_path.parent.parent.parent
    run_before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(
        module,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, run_max_bytes=1),
    )

    with pytest.raises(Exception) as size_error:
        _authorize(candidate_path, provider)

    assert getattr(size_error.value, "code", None) == "run_size_limit_exceeded"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "authorizations").exists()

    monkeypatch.setattr(module, "V2_RESOURCE_POLICY", V2_RESOURCE_POLICY)
    fault_dir = tmp_path / "fault"
    fault_dir.mkdir()
    candidate_path, provider = _candidate(fault_dir)
    run_dir = candidate_path.parent.parent.parent
    run_before = (run_dir / "run.json").read_bytes()
    original_write = module.atomic_write_json
    failed = False

    def fail_once(path: Path, value):
        nonlocal failed
        if Path(path).name == "run.json" and not failed:
            failed = True
            raise OSError("injected authorization run binding failure")
        return original_write(path, value)

    monkeypatch.setattr(module, "atomic_write_json", fail_once)

    with pytest.raises(Exception) as fault_error:
        _authorize(candidate_path, provider)

    assert getattr(fault_error.value, "code", None) == "authorization_status_update_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    orphan_dirs = tuple((run_dir / "authorizations").iterdir())
    assert len(orphan_dirs) == 1

    retry = _authorize(candidate_path, provider)

    assert retry.authorization_dir not in orphan_dirs
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    bound = [item for item in run["artifacts"] if item["role"] == "write_authorization"]
    assert len(bound) == 1
    assert run_dir / bound[0]["path"] == retry.authorization_dir / "authorization.json"
