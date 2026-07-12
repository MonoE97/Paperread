from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import stat
from contextlib import contextmanager
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


def _filesystem_snapshot(root: Path) -> dict[str, tuple[int, int, int, int, str]]:
    snapshot: dict[str, tuple[int, int, int, int, str]] = {}
    for current_root, directories, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in (".", *sorted(directories), *sorted(filenames)):
            path = current if name == "." else current / name
            relative = "." if path == root else path.relative_to(root).as_posix()
            if relative in snapshot:
                continue
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode):
                content = f"symlink:{os.readlink(path)}"
            elif stat.S_ISREG(metadata.st_mode):
                content = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                content = "directory" if stat.S_ISDIR(metadata.st_mode) else "other"
            snapshot[relative] = (
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_nlink,
                content,
            )
    return snapshot


def _install_unsafe_artifact_layout(
    *,
    run_dir: Path,
    outside: Path,
    root_name: str,
    parent_parts: tuple[str, ...],
    stem: str,
    case: str,
) -> None:
    root = run_dir / root_name
    if case == "root_symlink":
        root.symlink_to(outside, target_is_directory=True)
        return
    root.mkdir()
    parent = root
    for index, part in enumerate(parent_parts):
        candidate = parent / part
        if case == "intermediate_symlink" and index == len(parent_parts) - 1:
            target = outside / "intermediate"
            target.mkdir()
            candidate.symlink_to(target, target_is_directory=True)
            return
        candidate.mkdir()
        parent = candidate
    sidecar = parent / stem
    main = parent / f"{stem}.json"
    if case == "sidecar_symlink":
        target = outside / "sidecar"
        target.mkdir()
        sidecar.symlink_to(target, target_is_directory=True)
    elif case == "main_symlink":
        target = outside / "main.json"
        target.write_text("outside-main", encoding="utf-8")
        main.symlink_to(target)
    elif case == "main_hardlink":
        target = outside / "main.json"
        target.write_text("outside-main", encoding="utf-8")
        os.link(target, main)
    else:
        raise AssertionError(case)


def _inject_root_swap_at_anchor_recheck(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_dir: Path,
    root_name: str,
    outside: Path,
    provider_calls: list[str],
) -> tuple[Path, dict[str, object]]:
    import paper_reader.zotero_artifact_paths as artifact_paths

    detached = run_dir / f".{root_name}.detached"
    state: dict[str, object] = {"triggered": False, "calls_at_swap": ()}
    original = getattr(artifact_paths, "_validate_anchor_identity", None)

    def swap_then_validate(anchor) -> None:
        if not state["triggered"]:
            root = run_dir / root_name
            root.rename(detached)
            root.symlink_to(outside, target_is_directory=True)
            state["triggered"] = True
            state["calls_at_swap"] = tuple(provider_calls)
        if original is not None:
            original(anchor)

    monkeypatch.setattr(
        artifact_paths,
        "_validate_anchor_identity",
        swap_then_validate,
        raising=False,
    )
    return detached, state


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


def _install_authorization_clock(
    module,
    monkeypatch: pytest.MonkeyPatch,
    initial: datetime,
) -> dict[str, datetime]:
    clock = {"now": initial}

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = clock["now"]
            return instant if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(module, "datetime", FakeDateTime)
    return clock


def test_direct_authorization_binds_exact_envelope_and_returns_token_only_once(
    tmp_path: Path,
) -> None:
    candidate_path, provider = _candidate(tmp_path)
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))

    authorized = _authorize(candidate_path, provider)

    authorization_path = authorized.authorization_path
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
        "candidate.json",
        "children.json",
        "content.html",
        "parent.json",
        "record.json",
    ]
    run_dir = candidate_path.parent.parent.parent
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    auth_refs = [item for item in run["artifacts"] if item["role"] == "write_authorization"]
    assert len(auth_refs) == 1
    assert (run_dir / auth_refs[0]["path"]).read_bytes() == authorization_path.read_bytes()


def test_authorization_main_artifact_uses_required_topology(tmp_path: Path) -> None:
    candidate_path, provider = _candidate(tmp_path)

    authorized = _authorize(candidate_path, provider)

    expected = (
        authorized.run_dir
        / "authorizations"
        / f"{authorized.authorization.authorization_id}.json"
    )
    assert authorized.authorization_path == expected
    assert expected.is_file()
    assert authorized.authorization_dir == expected.with_suffix("")


@pytest.mark.parametrize("extra_path", ["extra.bin", "unreferenced/extra.bin"])
def test_authorize_rejects_unreferenced_candidate_tree_members_before_provider(
    extra_path: str,
    tmp_path: Path,
) -> None:
    candidate_path, _provider = _candidate(tmp_path)
    run_dir = candidate_path.parent.parent.parent
    extra = candidate_path.parent / extra_path
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"unreferenced candidate bytes")
    run_before = (run_dir / "run.json").read_bytes()

    class ProviderSpy:
        calls = 0

        def get_parent(self, _item_key: str):
            self.calls += 1
            raise AssertionError("unreferenced candidate member reached provider")

        def get_children(self, _parent_key: str):
            self.calls += 1
            raise AssertionError("unreferenced candidate member reached provider")

    provider = ProviderSpy()

    with pytest.raises(Exception) as exc_info:
        _authorize(candidate_path, provider)

    assert getattr(exc_info.value, "code", None) == "candidate_tampered"
    assert provider.calls == 0
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "authorizations").exists()


@pytest.mark.parametrize(
    "case",
    ["root_symlink", "sidecar_symlink", "main_symlink", "main_hardlink"],
)
def test_authorize_rejects_unsafe_deterministic_paths_before_provider_or_publication(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, _provider = _candidate(tmp_path)
    run_dir = candidate_path.parent.parent.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    authorization_id = "authorization_fixed"
    original_new_random_id = module.new_random_id

    def deterministic_id(prefix: str) -> str:
        if prefix == "authorization":
            return authorization_id
        return original_new_random_id(prefix)

    monkeypatch.setattr(module, "new_random_id", deterministic_id)
    _install_unsafe_artifact_layout(
        run_dir=run_dir,
        outside=outside,
        root_name="authorizations",
        parent_parts=(),
        stem=authorization_id,
        case=case,
    )
    run_before = _filesystem_snapshot(run_dir)
    outside_before = _filesystem_snapshot(outside)

    class ProviderSpy:
        calls = 0

        def get_parent(self, _item_key: str):
            self.calls += 1
            raise AssertionError("unsafe authorization path reached provider")

        def get_children(self, _parent_key: str):
            self.calls += 1
            raise AssertionError("unsafe authorization path reached provider")

    provider = ProviderSpy()

    with pytest.raises(Exception) as exc_info:
        _authorize(candidate_path, provider)

    assert getattr(exc_info.value, "code", None) == "unsafe_artifact_path"
    assert provider.calls == 0
    assert _filesystem_snapshot(run_dir) == run_before
    assert _filesystem_snapshot(outside) == outside_before


def test_authorize_root_swap_before_sidecar_publication_cannot_escape_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, delegate = _candidate(tmp_path)
    run_dir = candidate_path.parent.parent.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    authorization_id = "authorization_race"
    original_new_random_id = module.new_random_id

    def deterministic_id(prefix: str) -> str:
        if prefix == "authorization":
            return authorization_id
        return original_new_random_id(prefix)

    monkeypatch.setattr(module, "new_random_id", deterministic_id)
    provider_calls: list[str] = []

    class ProviderSpy:
        def get_parent(self, item_key: str):
            provider_calls.append("parent")
            return delegate.get_parent(item_key)

        def get_children(self, parent_key: str):
            provider_calls.append("children")
            return delegate.get_children(parent_key)

    detached, state = _inject_root_swap_at_anchor_recheck(
        monkeypatch,
        run_dir=run_dir,
        root_name="authorizations",
        outside=outside,
        provider_calls=provider_calls,
    )
    run_before = (run_dir / "run.json").read_bytes()
    outside_before = _filesystem_snapshot(outside)

    with pytest.raises(Exception) as exc_info:
        _authorize(candidate_path, ProviderSpy())

    assert getattr(exc_info.value, "code", None) == "unsafe_artifact_path"
    assert state == {
        "triggered": True,
        "calls_at_swap": ("parent", "children"),
    }
    assert provider_calls == ["parent", "children"]
    assert (run_dir / "run.json").read_bytes() == run_before
    assert _filesystem_snapshot(outside) == outside_before
    assert detached.is_dir()
    assert not (detached / authorization_id).exists()
    assert not os.path.lexists(detached / f"{authorization_id}.json")
    assert not (outside / authorization_id).exists()
    assert not os.path.lexists(outside / f"{authorization_id}.json")
    assert not tuple(run_dir.glob(".*.staging"))


def test_authorize_rejects_skill_root_replacement_between_preflight_and_parent_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, provider = _candidate(tmp_path)
    run_dir = candidate_path.parent.parent.parent
    skill_root = run_dir.parent.parent.parent
    replacement = tmp_path / "replacement-skill"
    detached = tmp_path / "detached-skill"
    shutil.copytree(skill_root, replacement)
    original_lock = module.locked_zotero_parent
    swapped = False

    @contextmanager
    def swap_before_lock(run_path: Path, parent_key: str, **kwargs):
        nonlocal swapped
        if not swapped:
            skill_root.rename(detached)
            replacement.rename(skill_root)
            swapped = True
        with original_lock(run_path, parent_key, **kwargs) as locked:
            yield locked

    monkeypatch.setattr(module, "locked_zotero_parent", swap_before_lock)

    with pytest.raises(Exception) as exc_info:
        _authorize(candidate_path, provider)

    assert getattr(exc_info.value, "code", None) == "run_directory_changed"
    assert swapped is True
    assert not (skill_root / ".zotero-authorization-reservations").exists()
    assert not (detached / ".zotero-authorization-reservations").exists()


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
    ("external_claim_id", "write_attempt_id"),
    [
        ("bad/claim", "attempt_batch_001"),
        ("claim_batch_001", "../bad-attempt"),
    ],
)
def test_batch_authorization_rejects_invalid_identifiers_before_provider_or_parent_lock(
    external_claim_id: str,
    write_attempt_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, _provider = _candidate(tmp_path)
    run_dir = candidate_path.parent.parent.parent
    run_before = (run_dir / "run.json").read_bytes()

    class ProviderSpy:
        called = False

        def get_parent(self, _item_key: str):
            self.called = True
            raise AssertionError("invalid identity reached provider")

        def get_children(self, _parent_key: str):
            self.called = True
            raise AssertionError("invalid identity reached provider")

    provider = ProviderSpy()
    lock_entered = False

    @contextmanager
    def parent_lock_spy(_run_dir: Path, _parent_key: str):
        nonlocal lock_entered
        lock_entered = True
        raise AssertionError("invalid identity reached parent lock")
        yield

    monkeypatch.setattr(module, "locked_zotero_parent", parent_lock_spy)

    with pytest.raises(Exception) as exc_info:
        _authorize(
            candidate_path,
            provider,
            external_claim_id=external_claim_id,
            write_attempt_id=write_attempt_id,
        )

    assert getattr(exc_info.value, "code", None) == "invalid_external_identity"
    assert provider.called is False
    assert lock_entered is False
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "authorizations").exists()


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


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_authorization_rejects_candidate_alias_before_parent_lock(
    alias_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, provider = _candidate(tmp_path)
    alias = candidate_path.with_name(f"candidate-{alias_kind}.json")
    if alias_kind == "symlink":
        alias.symlink_to(candidate_path)
    else:
        os.link(candidate_path, alias)
    lock_entered = False

    @contextmanager
    def forbidden_parent_lock(*_args, **_kwargs):
        nonlocal lock_entered
        lock_entered = True
        raise AssertionError("candidate alias reached parent lock")
        yield

    monkeypatch.setattr(module, "locked_zotero_parent", forbidden_parent_lock)

    with pytest.raises(Exception) as exc_info:
        _authorize(alias, provider)

    assert getattr(exc_info.value, "code", None) == "candidate_unreadable"
    assert lock_entered is False


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


def test_production_ttl_starts_after_lock_wait_and_live_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, provider = _candidate(tmp_path)
    clock = _install_authorization_clock(module, monkeypatch, NOW)
    original_parent_lock = module.locked_zotero_parent

    @contextmanager
    def delayed_parent_lock(*args, **kwargs):
        clock["now"] += timedelta(seconds=120)
        with original_parent_lock(*args, **kwargs) as locked:
            yield locked

    class SlowProvider:
        def get_parent(self, item_key: str):
            return provider.get_parent(item_key)

        def get_children(self, parent_key: str):
            children = provider.get_children(parent_key)
            clock["now"] += timedelta(seconds=120)
            return children

    monkeypatch.setattr(module, "locked_zotero_parent", delayed_parent_lock)

    authorized = module.authorize_zotero_candidate(
        candidate_path,
        provider=SlowProvider(),
        now=None,
        ttl_seconds=1,
    )

    assert authorized.authorization.created_at == "2026-07-10T12:04:00Z"
    assert authorized.authorization.expires_at == "2026-07-10T12:04:01Z"
    assert authorized.authorization.live_preflight.captured_at == "2026-07-10T12:04:00Z"


def test_expired_authorization_commit_never_returns_plaintext_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, provider = _candidate(tmp_path)
    clock = _install_authorization_clock(module, monkeypatch, NOW)
    original_write = module.atomic_write_json

    def commit_then_expire(path: Path, value, **kwargs):
        result = original_write(path, value, **kwargs)
        if Path(path).name == "run.json":
            clock["now"] += timedelta(seconds=2)
        return result

    monkeypatch.setattr(module, "atomic_write_json", commit_then_expire)

    with pytest.raises(module.ZoteroAuthorizationError) as exc_info:
        module.authorize_zotero_candidate(
            candidate_path,
            provider=provider,
            now=None,
            ttl_seconds=1,
        )

    assert exc_info.value.code == "authorization_expired_before_return"
    run_dir = candidate_path.parent.parent.parent
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert [item["role"] for item in run["artifacts"]].count("write_authorization") == 1


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


def test_source_change_is_a_structured_zotero_authorization_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, provider = _candidate(tmp_path)
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    source_path = Path(candidate["source"]["attachment"]["resolved_path"])
    source_path.write_bytes(source_path.read_bytes() + b"\nsource changed")
    monkeypatch.setattr(module, "LocalApiZoteroReadProvider", lambda: provider)

    with pytest.raises(module.ZoteroAuthorizationError) as exc_info:
        _authorize(candidate_path, provider)

    assert exc_info.value.code == "source_changed"

    result = CliRunner().invoke(app, ["zotero", "authorize", str(candidate_path)])

    assert result.exit_code != 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    assert payload["command"] == "zotero authorize"
    assert payload["ok"] is False
    assert payload["code"] == "source_changed"
    assert result.stderr.strip()
    assert not (candidate_path.parent.parent.parent / "authorizations").exists()


def test_bound_authorization_schema_is_rejected_before_parent_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, provider = _candidate(tmp_path)
    authorized = _authorize(candidate_path, provider, ttl_seconds=1)
    authorized.authorization_path.write_bytes(
        b'{"schema_version":"paper_reader.write-authorization.v1"}'
    )
    run_dir = candidate_path.parent.parent.parent
    run_before = (run_dir / "run.json").read_bytes()

    @contextmanager
    def forbidden_parent_lock(*_args, **_kwargs):
        raise AssertionError("unsupported bound authorization reached parent lock")
        yield

    monkeypatch.setattr(module, "locked_zotero_parent", forbidden_parent_lock)

    with pytest.raises(Exception) as exc_info:
        _authorize(candidate_path, provider, now=NOW + timedelta(seconds=2))

    assert getattr(exc_info.value, "code", None) == "unsupported_run_schema"
    assert (run_dir / "run.json").read_bytes() == run_before


def test_orphan_authorization_schema_is_rejected_before_parent_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, provider = _candidate(tmp_path)
    run_dir = candidate_path.parent.parent.parent
    original_write = module.atomic_write_json
    failed = False

    def fail_binding_once(path: Path, value, **kwargs):
        nonlocal failed
        if Path(path).name == "run.json" and not failed:
            failed = True
            raise OSError("injected authorization run binding failure")
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(module, "atomic_write_json", fail_binding_once)
    with pytest.raises(Exception) as first_error:
        _authorize(candidate_path, provider, ttl_seconds=1)
    assert getattr(first_error.value, "code", None) == "authorization_status_update_failed"
    orphan_path = next((run_dir / "authorizations").glob("*.json"))
    orphan_path.write_bytes(
        b'{"schema_version":"paper_reader.write-authorization.v1"}'
    )
    run_before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(module, "atomic_write_json", original_write)

    @contextmanager
    def forbidden_parent_lock(*_args, **_kwargs):
        raise AssertionError("unsupported orphan authorization reached parent lock")
        yield

    monkeypatch.setattr(module, "locked_zotero_parent", forbidden_parent_lock)

    with pytest.raises(Exception) as exc_info:
        _authorize(candidate_path, provider, now=NOW + timedelta(seconds=2))

    assert getattr(exc_info.value, "code", None) == "unsupported_run_schema"
    assert (run_dir / "run.json").read_bytes() == run_before


def test_authorization_publication_failure_does_not_leak_exception_text_through_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, provider = _candidate(tmp_path)
    secret = "secret-publication-path-and-token"

    def fail_publication(*_args, **_kwargs):
        raise OSError(secret)

    monkeypatch.setattr(module, "LocalApiZoteroReadProvider", lambda: provider)
    monkeypatch.setattr(module, "anchored_artifact_publication", fail_publication)

    result = CliRunner().invoke(app, ["zotero", "authorize", str(candidate_path)])

    assert result.exit_code != 0
    assert secret not in result.stdout
    assert secret not in result.stderr
    payload = json.loads(result.stdout)
    PaperReaderCommandResult.model_validate(payload)
    assert payload["code"] == "authorization_publication_failed"


def test_authorization_read_failure_does_not_leak_provider_exception_through_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, _provider = _candidate(tmp_path)
    secret = "secret-provider-response-and-token"

    class FailingProvider:
        def get_parent(self, _parent_key: str):
            raise OSError(secret)

        def get_children(self, _parent_key: str):
            raise AssertionError("get_children must not run after parent failure")

    monkeypatch.setattr(module, "LocalApiZoteroReadProvider", FailingProvider)

    result = CliRunner().invoke(app, ["zotero", "authorize", str(candidate_path)])

    assert result.exit_code != 0
    assert secret not in result.stdout
    assert secret not in result.stderr
    payload = json.loads(result.stdout)
    PaperReaderCommandResult.model_validate(payload)
    assert payload["code"] == "zotero_read_failed"


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


def test_manifest_change_repreflights_once_before_provider_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, provider = _candidate(tmp_path)
    original_parent_lock = module._locked_parent_for_preflight
    attempts = 0
    provider_calls: list[str] = []

    @contextmanager
    def change_once(inspected):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise module.ZoteroAuthorizationError(
                "run_manifest_changed",
                "injected optimistic manifest race",
            )
        with original_parent_lock(inspected) as locked:
            yield locked

    class CountingProvider:
        def get_parent(self, item_key: str):
            provider_calls.append("parent")
            return provider.get_parent(item_key)

        def get_children(self, parent_key: str):
            provider_calls.append("children")
            return provider.get_children(parent_key)

    monkeypatch.setattr(module, "_locked_parent_for_preflight", change_once)

    authorized = module.authorize_zotero_candidate(
        candidate_path,
        provider=CountingProvider(),
        now=NOW,
    )

    assert authorized.authorization.schema_version == "paper_reader.write-authorization.v2"
    assert attempts == 2
    assert provider_calls == ["parent", "children"]


def test_manifest_change_retry_is_bounded_to_one_repreflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, _provider = _candidate(tmp_path)
    attempts = 0

    @contextmanager
    def always_changed(_inspected):
        nonlocal attempts
        attempts += 1
        raise module.ZoteroAuthorizationError(
            "run_manifest_changed",
            "injected repeated optimistic manifest race",
        )
        yield

    class NetworkForbiddenProvider:
        def get_parent(self, _item_key: str):
            raise AssertionError("manifest retry reached provider read")

        def get_children(self, _parent_key: str):
            raise AssertionError("manifest retry reached provider read")

    monkeypatch.setattr(module, "_locked_parent_for_preflight", always_changed)

    with pytest.raises(module.ZoteroAuthorizationError) as exc_info:
        module.authorize_zotero_candidate(
            candidate_path,
            provider=NetworkForbiddenProvider(),
            now=NOW,
        )

    assert exc_info.value.code == "run_manifest_changed"
    assert attempts == 2


def test_manifest_change_retry_rejects_skill_root_identity_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    candidate_path, provider = _candidate(tmp_path)
    run_dir = candidate_path.parent.parent.parent
    skill_root = run_dir.parent.parent.parent
    replacement = tmp_path / "replacement-retry-skill-root"
    detached = tmp_path / "detached-retry-skill-root"
    shutil.copytree(skill_root, replacement)
    original_parent_lock = module._locked_parent_for_preflight
    attempts = 0
    provider_calls = 0

    @contextmanager
    def change_then_replace_root(inspected):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            skill_root.rename(detached)
            replacement.rename(skill_root)
            raise module.ZoteroAuthorizationError(
                "run_manifest_changed",
                "injected manifest race followed by root replacement",
            )
        with original_parent_lock(inspected) as locked:
            yield locked

    class CountingProvider:
        def get_parent(self, item_key: str):
            nonlocal provider_calls
            provider_calls += 1
            return provider.get_parent(item_key)

        def get_children(self, parent_key: str):
            nonlocal provider_calls
            provider_calls += 1
            return provider.get_children(parent_key)

    monkeypatch.setattr(
        module,
        "_locked_parent_for_preflight",
        change_then_replace_root,
    )

    with pytest.raises(module.ZoteroAuthorizationError) as exc_info:
        module.authorize_zotero_candidate(
            candidate_path,
            provider=CountingProvider(),
            now=NOW,
        )

    assert exc_info.value.code == "run_directory_changed"
    assert attempts == 1
    assert provider_calls == 0
    assert not (skill_root / ".zotero-authorization-reservations").exists()
    assert not (detached / ".zotero-authorization-reservations").exists()


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

    def fail_once(path: Path, value, **kwargs):
        nonlocal failed
        if Path(path).name == "run.json" and not failed:
            failed = True
            raise OSError("injected authorization run binding failure")
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(module, "atomic_write_json", fail_once)

    with pytest.raises(Exception) as fault_error:
        _authorize(candidate_path, provider)

    assert getattr(fault_error.value, "code", None) == "authorization_status_update_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    orphan_mains = tuple((run_dir / "authorizations").glob("*.json"))
    orphan_sidecars = tuple(
        path for path in (run_dir / "authorizations").iterdir() if path.is_dir()
    )
    assert len(orphan_mains) == 1
    assert len(orphan_sidecars) == 1

    with pytest.raises(Exception) as recovery_error:
        _authorize(candidate_path, provider)

    assert (
        getattr(recovery_error.value, "code", None)
        == "authorization_recovered_token_unavailable"
    )
    assert tuple((run_dir / "authorizations").glob("*.json")) == orphan_mains
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    bound = [item for item in run["artifacts"] if item["role"] == "write_authorization"]
    assert len(bound) == 1
    assert run_dir / bound[0]["path"] == orphan_mains[0]
