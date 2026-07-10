from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _storage_module():
    assert importlib.util.find_spec("paper_reader.storage") is not None, "V2 storage module is missing"
    return importlib.import_module("paper_reader.storage")


def test_canonical_json_and_sha256_are_stable_and_reject_nan() -> None:
    storage = _storage_module()
    payload = {"z": "材料", "a": [2, {"b": True}]}

    encoded = storage.canonical_json_bytes(payload)

    assert encoded == '{"a":[2,{"b":true}],"z":"材料"}'.encode()
    assert storage.canonical_json_sha256(payload) == hashlib.sha256(encoded).hexdigest()
    with pytest.raises(ValueError):
        storage.canonical_json_bytes({"bad": float("nan")})


def test_rfc3339_utc_normalizes_aware_times_and_rejects_naive_times() -> None:
    storage = _storage_module()
    value = datetime(2026, 7, 10, 17, 30, 4, 120000, tzinfo=timezone(timedelta(hours=8)))

    assert storage.rfc3339_utc(value) == "2026-07-10T09:30:04.12Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        storage.rfc3339_utc(datetime(2026, 7, 10, 9, 30, 4))


def test_uuid_random_ids_and_tokens_are_random_and_well_formed() -> None:
    storage = _storage_module()

    first_uuid = storage.new_uuid()
    second_uuid = storage.new_uuid()

    assert uuid.UUID(first_uuid).version == 4
    assert first_uuid != second_uuid
    assert storage.new_random_id("evidence").startswith("evidence_")
    assert storage.random_token() != storage.random_token()


@pytest.mark.parametrize(
    "value",
    ["", ".", "../note.md", "evidence/../note.md", "/tmp/note.md", "C:\\note.md", "a//b"],
)
def test_safe_relative_artifact_path_rejects_ambiguous_or_escaping_paths(value: str) -> None:
    storage = _storage_module()

    with pytest.raises(ValueError):
        storage.safe_relative_artifact_path(value)


def test_resolve_artifact_path_rejects_symlink_escape(tmp_path: Path) -> None:
    storage = _storage_module()
    root = tmp_path / "run"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    assert storage.resolve_artifact_path(root, "evidence/context.md") == root / "evidence/context.md"
    with pytest.raises(ValueError, match="escapes"):
        storage.resolve_artifact_path(root, "escape/context.md")


def test_resolved_artifact_path_cannot_be_retargeted_outside_root(tmp_path: Path) -> None:
    storage = _storage_module()
    root = tmp_path / "run"
    inside = root / "inside"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    (inside / "context.md").write_text("inside", encoding="utf-8")
    (outside / "context.md").write_text("outside", encoding="utf-8")
    link = root / "current"
    link.symlink_to(inside, target_is_directory=True)

    resolved = storage.resolve_artifact_path(root, "current/context.md")
    link.unlink()
    link.symlink_to(outside, target_is_directory=True)

    assert resolved == inside / "context.md"
    assert resolved.read_text(encoding="utf-8") == "inside"


def test_source_fingerprint_resolves_symlink_and_binds_bytes_and_inode(tmp_path: Path) -> None:
    storage = _storage_module()
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"paper-v2")
    alias = tmp_path / "paper-alias.pdf"
    alias.symlink_to(source)

    fingerprint = storage.fingerprint_source(alias)
    stat = source.stat()

    assert fingerprint.resolved_path == str(source.resolve())
    assert fingerprint.size_bytes == len(b"paper-v2")
    assert fingerprint.sha256 == hashlib.sha256(b"paper-v2").hexdigest()
    assert fingerprint.device == stat.st_dev
    assert fingerprint.inode == stat.st_ino
    assert storage.source_matches_fingerprint(source, fingerprint) is True
    source.write_bytes(b"changed")
    assert storage.source_matches_fingerprint(source, fingerprint) is False


def test_alias_detection_catches_resolved_symlink_and_hardlink_aliases(tmp_path: Path) -> None:
    storage = _storage_module()
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"paper")
    symlink = tmp_path / "symlink.pdf"
    symlink.symlink_to(source)
    hardlink = tmp_path / "hardlink.pdf"
    os.link(source, hardlink)

    assert storage.paths_alias(source, source.parent / "." / source.name) is True
    assert storage.paths_alias(source, symlink) is True
    assert storage.paths_alias(source, hardlink) is True
    assert storage.paths_alias(source, tmp_path / "different.pdf") is False
    with pytest.raises(ValueError, match="aliases source"):
        storage.assert_no_source_output_alias(source, hardlink)


def test_atomic_json_write_uses_canonical_bytes_and_replaces_as_one_file(tmp_path: Path) -> None:
    storage = _storage_module()
    target = tmp_path / "state.json"

    storage.atomic_write_json(target, {"seq": 1, "state": "ready"})
    assert target.read_bytes() == storage.canonical_json_bytes({"seq": 1, "state": "ready"})

    storage.atomic_write_json(target, {"seq": 2, "state": "done"})
    assert json.loads(target.read_text()) == {"seq": 2, "state": "done"}
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_tree_publish_does_not_replace_an_existing_tree(tmp_path: Path) -> None:
    storage = _storage_module()
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "evidence.json").write_text("v2")
    target = tmp_path / "evidence_1"

    storage.atomic_publish_tree(staging, target)

    assert not staging.exists()
    assert (target / "evidence.json").read_text() == "v2"
    second_staging = tmp_path / "staging-2"
    second_staging.mkdir()
    (second_staging / "evidence.json").write_text("replacement")
    with pytest.raises(FileExistsError):
        storage.atomic_publish_tree(second_staging, target)
    assert (target / "evidence.json").read_text() == "v2"


def test_atomic_tree_publish_rejects_destination_created_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage_module()
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "marker").write_text("candidate", encoding="utf-8")
    target = tmp_path / "published"
    original_fsync_tree = storage._fsync_tree

    def fsync_then_create_competing_target(root: Path) -> None:
        original_fsync_tree(root)
        target.mkdir()

    monkeypatch.setattr(storage, "_fsync_tree", fsync_then_create_competing_target)

    with pytest.raises(FileExistsError) as exc_info:
        storage.atomic_publish_tree(staging, target)

    assert getattr(exc_info.value, "code", None) == "publish_conflict"
    assert staging.is_dir()
    assert (staging / "marker").read_text(encoding="utf-8") == "candidate"
    assert target.is_dir()
    assert not list(target.iterdir())


def test_atomic_tree_publish_uses_native_no_replace_not_os_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage_module()
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "published"

    def forbidden_replace_capable_rename(*_args) -> None:
        pytest.fail("replace-capable os.rename fallback was called")

    monkeypatch.setattr(storage.os, "rename", forbidden_replace_capable_rename)

    storage.atomic_publish_tree(staging, target)

    assert target.is_dir()
    assert not staging.exists()


def test_atomic_tree_publish_fails_closed_when_native_primitive_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage_module()
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "published"
    monkeypatch.setattr(sys, "platform", "unsupported-test-platform")

    with pytest.raises(NotImplementedError, match="atomic no-replace"):
        storage.atomic_publish_tree(staging, target)

    assert staging.is_dir()
    assert not target.exists()


def test_atomic_tree_publish_allows_exactly_one_real_concurrent_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage_module()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "marker").write_text("first", encoding="utf-8")
    (second / "marker").write_text("second", encoding="utf-8")
    target = tmp_path / "published"
    barrier = threading.Barrier(2)
    original_fsync_tree = storage._fsync_tree

    def fsync_then_wait(root: Path) -> None:
        original_fsync_tree(root)
        barrier.wait(timeout=5)

    monkeypatch.setattr(storage, "_fsync_tree", fsync_then_wait)

    def publish(staging: Path):
        try:
            storage.atomic_publish_tree(staging, target)
            return None
        except Exception as exc:  # asserted below, preserving the real exception
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish, (first, second)))

    errors = [outcome for outcome in outcomes if outcome is not None]
    assert len(errors) == 1
    assert isinstance(errors[0], FileExistsError)
    assert getattr(errors[0], "code", None) == "publish_conflict"
    assert (target / "marker").read_text(encoding="utf-8") in {"first", "second"}
    assert sum(path.exists() for path in (first, second)) == 1


def test_file_publish_is_atomic_no_replace_and_does_not_hardlink_candidate(tmp_path: Path) -> None:
    storage = _storage_module()
    candidate = tmp_path / "candidate.md"
    candidate.write_bytes(b"immutable candidate")
    target = tmp_path / "published.md"

    storage.publish_file_no_replace(candidate, target)

    assert candidate.read_bytes() == target.read_bytes()
    assert not storage.paths_alias(candidate, target)
    replacement = tmp_path / "replacement.md"
    replacement.write_bytes(b"replacement")
    with pytest.raises(FileExistsError):
        storage.publish_file_no_replace(replacement, target)
    assert target.read_bytes() == b"immutable candidate"
