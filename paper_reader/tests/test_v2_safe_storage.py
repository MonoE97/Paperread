from __future__ import annotations

import json
import fcntl
import hashlib
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import paper_reader.run_lock as run_lock_module
import paper_reader.review_package as review_package_module
import paper_reader.storage as storage_module
import paper_reader.v2_loader as loader_module
import paper_reader.candidate_integrity as candidate_integrity_module
from paper_reader.candidate_integrity import verify_artifact_ref
from paper_reader.contracts import ArtifactRef
from paper_reader.run_lock import locked_v2_run
from paper_reader.run_size import RunSizeLimitError
from paper_reader.storage import (
    UnsafeStoragePathError,
    atomic_publish_tree,
    atomic_write_bytes,
    create_anchored_directory,
    publish_bytes_no_replace,
    read_anchored_bytes,
    snapshot_anchored_tree,
    snapshot_directory_fd,
    tree_snapshot_from_bytes,
)
from paper_reader.v2_loader import DirectoryAnchor, RunLoadError, load_v2_run


def _valid_run_payload() -> dict[str, object]:
    return {
        "schema_version": "paper_reader.run.v2",
        "run_id": "run_safe_storage",
        "created_at": "2026-07-12T08:00:00Z",
        "source": {
            "source_type": "local_pdf",
            "requested_path": "/tmp/paper.pdf",
            "resolved_path": "/tmp/paper.pdf",
            "sha256": "a" * 64,
            "size_bytes": 10,
            "device": 1,
            "inode": 2,
        },
        "target": None,
        "status": "initialized",
        "artifacts": [],
        "gate": {
            "status": "not_evaluated",
            "evaluated_at": None,
            "checks": [],
            "blockers": [],
        },
        "live_preflight": None,
    }


def _write_valid_run(run_dir: Path) -> Path:
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    manifest.write_text(json.dumps(_valid_run_payload()), encoding="utf-8")
    return manifest


def test_run_loader_rejects_same_size_manifest_mutation_with_restored_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    manifest = _write_valid_run(run_dir)
    original_read = loader_module.os.read
    changed = False

    def mutate_after_read(descriptor: int, requested: int) -> bytes:
        nonlocal changed
        chunk = original_read(descriptor, requested)
        if chunk and not changed:
            metadata = manifest.stat()
            raw = manifest.read_bytes()
            replacement = raw.replace(b"run_safe_storage", b"run_evil_storage", 1)
            assert len(replacement) == len(raw)
            manifest.write_bytes(replacement)
            os.utime(
                manifest,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
            )
            changed = True
        return chunk

    monkeypatch.setattr(loader_module.os, "read", mutate_after_read)

    with pytest.raises(RunLoadError) as exc_info:
        load_v2_run(run_dir)

    assert changed is True
    assert exc_info.value.code == "run_manifest_unsafe"


def test_anchored_read_rejects_declared_size_mismatch_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    artifact = run_dir / "artifact.json"
    artifact.write_bytes(b"oversized")

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("size mismatch reached os.read")

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as anchor:
        monkeypatch.setattr(storage_module.os, "read", forbidden_read)
        with pytest.raises(UnsafeStoragePathError, match="size"):
            read_anchored_bytes(
                anchor,
                artifact,
                expected_size=4,
                max_bytes=4,
            )


def test_anchored_read_enforces_limit_during_chunk_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    artifact = run_dir / "artifact.json"
    artifact.write_bytes(b"1234")
    calls = 0

    def growing_read(_descriptor: int, requested: int) -> bytes:
        nonlocal calls
        calls += 1
        assert requested <= 5
        return b"12345"

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as anchor:
        monkeypatch.setattr(storage_module.os, "read", growing_read)
        with pytest.raises(UnsafeStoragePathError, match="limit"):
            read_anchored_bytes(anchor, artifact, max_bytes=4)

    assert calls == 1


def test_tree_snapshot_rejects_upfront_oversized_file_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "oversized.json").write_bytes(b"12345")

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("oversized snapshot member reached os.read")

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as anchor:
        monkeypatch.setattr(storage_module.os, "read", forbidden_read)
        with pytest.raises(UnsafeStoragePathError, match="file limit"):
            snapshot_anchored_tree(
                anchor,
                max_file_bytes=4,
                max_total_bytes=16,
                max_members=8,
                max_depth=2,
            )


def test_tree_snapshot_rejects_upfront_total_size_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "artifact.json").write_bytes(b"123456")

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("oversized snapshot tree reached os.read")

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as anchor:
        monkeypatch.setattr(storage_module.os, "read", forbidden_read)
        with pytest.raises(UnsafeStoragePathError, match="total byte limit"):
            snapshot_anchored_tree(
                anchor,
                max_file_bytes=8,
                max_total_bytes=5,
                max_members=8,
                max_depth=2,
            )


def test_tree_snapshot_enforces_limits_during_chunk_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "artifact.json").write_bytes(b"1234")
    calls = 0

    def growing_read(_descriptor: int, requested: int) -> bytes:
        nonlocal calls
        calls += 1
        assert requested <= 5
        return b"12345"

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as anchor:
        monkeypatch.setattr(storage_module.os, "read", growing_read)
        with pytest.raises(UnsafeStoragePathError, match="file limit"):
            snapshot_anchored_tree(
                anchor,
                max_file_bytes=4,
                max_total_bytes=16,
                max_members=8,
                max_depth=2,
            )

    assert calls == 1


def test_tree_snapshot_rejects_member_limit_before_reading_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "first.json").write_bytes(b"1")
    (run_dir / "second.json").write_bytes(b"2")

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("snapshot member limit reached os.read")

    descriptor = storage_module.os.open(
        run_dir,
        storage_module._DIRECTORY_OPEN_FLAGS,
    )
    try:
        monkeypatch.setattr(storage_module.os, "read", forbidden_read)
        with pytest.raises(UnsafeStoragePathError, match="member limit"):
            snapshot_directory_fd(
                descriptor,
                max_file_bytes=4,
                max_total_bytes=16,
                max_members=1,
                max_depth=2,
            )
    finally:
        storage_module.os.close(descriptor)


def test_tree_snapshot_rejects_depth_limit_before_reading_deep_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    nested = run_dir / "nested"
    nested.mkdir(parents=True)
    (nested / "artifact.json").write_bytes(b"1")

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("snapshot depth limit reached os.read")

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as anchor:
        monkeypatch.setattr(storage_module.os, "read", forbidden_read)
        with pytest.raises(UnsafeStoragePathError, match="depth limit"):
            snapshot_anchored_tree(
                anchor,
                max_file_bytes=4,
                max_total_bytes=16,
                max_members=8,
                max_depth=1,
            )


def test_artifact_ref_uses_exact_size_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    artifact_path = run_dir / "artifact.json"
    artifact_path.write_bytes(b"oversized")
    artifact = ArtifactRef(
        role="summary_snapshot",
        path="artifact.json",
        sha256="a" * 64,
        size_bytes=4,
        media_type="application/json",
    )

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("ArtifactRef size mismatch reached os.read")

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as anchor:
        monkeypatch.setattr(storage_module.os, "read", forbidden_read)
        with pytest.raises(ValueError) as exc_info:
            verify_artifact_ref(run_dir, artifact, anchor=anchor)

    assert getattr(exc_info.value, "code", None) == "sealed_artifact_tampered"


def test_artifact_ref_rejects_declared_size_above_resource_cap_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from paper_reader.resource_policy import V2_RESOURCE_POLICY

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    artifact_path = run_dir / "artifact.json"
    artifact_path.write_bytes(b"12345678")
    artifact = ArtifactRef(
        role="summary_snapshot",
        path="artifact.json",
        sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        size_bytes=8,
        media_type="application/json",
    )
    monkeypatch.setattr(
        candidate_integrity_module,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, run_max_bytes=4),
        raising=False,
    )

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("oversized ArtifactRef declaration reached os.read")

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as anchor:
        monkeypatch.setattr(storage_module.os, "read", forbidden_read)
        with pytest.raises(ValueError) as exc_info:
            candidate_integrity_module.verify_artifact_ref(
                run_dir,
                artifact,
                anchor=anchor,
            )

    assert getattr(exc_info.value, "code", None) == "sealed_artifact_tampered"


def test_review_schema_preflight_passes_explicit_resource_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader.resource_policy import V2_RESOURCE_POLICY

    run_dir = tmp_path / "run"
    _write_valid_run(run_dir)
    summary_path = run_dir / "summary.json"
    summary_path.write_bytes(b"{}")
    original_read = review_package_module.read_anchored_bytes
    observed = False

    def bounded_read(anchor, path, **kwargs):
        nonlocal observed
        if Path(path) == summary_path:
            observed = True
            assert (
                kwargs["max_bytes"]
                == V2_RESOURCE_POLICY.structured_artifact_max_bytes
            )
        return original_read(anchor, path, **kwargs)

    monkeypatch.setattr(
        review_package_module,
        "read_anchored_bytes",
        bounded_read,
    )

    with pytest.raises(RunLoadError) as exc_info:
        review_package_module._preflight_review_schema_versions(run_dir)

    assert exc_info.value.code == "unsupported_run_schema"
    assert observed is True


def test_run_manifest_rejects_preexisting_oversize_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    manifest = _write_valid_run(run_dir)
    monkeypatch.setattr(loader_module, "MAX_RUN_MANIFEST_BYTES", 16, raising=False)

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("oversized run manifest reached os.read")

    monkeypatch.setattr(loader_module.os, "read", forbidden_read)

    with pytest.raises(RunLoadError) as exc_info:
        load_v2_run(run_dir)

    assert exc_info.value.code == "run_manifest_too_large"
    assert exc_info.value.manifest_path == manifest


def test_run_manifest_enforces_limit_during_chunk_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    manifest = _write_valid_run(run_dir)
    monkeypatch.setattr(
        loader_module,
        "MAX_RUN_MANIFEST_BYTES",
        manifest.stat().st_size,
        raising=False,
    )
    calls = 0

    def growing_read(_descriptor: int, requested: int) -> bytes:
        nonlocal calls
        calls += 1
        assert requested <= manifest.stat().st_size + 1
        return b"x" * (manifest.stat().st_size + 1)

    monkeypatch.setattr(loader_module.os, "read", growing_read)

    with pytest.raises(RunLoadError) as exc_info:
        load_v2_run(run_dir)

    assert exc_info.value.code == "run_manifest_too_large"
    assert calls == 1


def test_load_v2_run_rejects_symlinked_manifest_without_following_it(
    tmp_path: Path,
) -> None:
    outside_manifest = tmp_path / "outside-run.json"
    outside_manifest.write_text(json.dumps(_valid_run_payload()), encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").symlink_to(outside_manifest)

    with pytest.raises(RunLoadError) as exc_info:
        load_v2_run(run_dir)

    assert exc_info.value.code == "run_manifest_unsafe"
    assert exc_info.value.manifest_path == run_dir / "run.json"


def test_locked_v2_run_rejects_run_directory_replacement_during_lock_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    _write_valid_run(run_dir)
    detached = tmp_path / "detached-run"
    original_flock = run_lock_module.fcntl.flock
    replaced = False

    def replace_then_flock(descriptor: int, operation: int) -> None:
        nonlocal replaced
        if operation == fcntl.LOCK_EX and not replaced:
            replaced = True
            run_dir.rename(detached)
            _write_valid_run(run_dir)
        original_flock(descriptor, operation)

    monkeypatch.setattr(run_lock_module.fcntl, "flock", replace_then_flock)

    with pytest.raises(ValueError) as exc_info:
        with locked_v2_run(run_dir):
            pytest.fail("replacement run reached the locked critical section")

    assert getattr(exc_info.value, "code", None) == "run_directory_changed"
    assert not (run_dir / ".run.lock").exists()
    assert (detached / ".run.lock").is_file()


def test_locked_v2_run_rejects_run_directory_replacement_at_context_exit(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    _write_valid_run(run_dir)
    detached = tmp_path / "detached-run"

    with pytest.raises(run_lock_module.RunLockError) as exc_info:
        with locked_v2_run(run_dir):
            run_dir.rename(detached)
            _write_valid_run(run_dir)

    assert exc_info.value.code == "run_directory_changed"
    assert (detached / ".run.lock").is_file()
    assert (run_dir / "run.json").is_file()


def test_locked_v2_run_rejects_named_lock_replacement_at_context_exit(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    _write_valid_run(run_dir)
    detached_lock = run_dir / ".run.lock.detached"

    with pytest.raises(run_lock_module.RunLockError) as exc_info:
        with locked_v2_run(run_dir):
            lock_path = run_dir / ".run.lock"
            lock_path.rename(detached_lock)
            lock_path.write_bytes(b"")

    assert exc_info.value.code == "run_lock_changed"
    assert detached_lock.is_file()
    assert (run_dir / ".run.lock").is_file()


def test_load_v2_run_rejects_directory_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    _write_valid_run(run_dir)
    detached = tmp_path / "detached-run"
    original_loads = loader_module.json.loads
    replaced = False

    def replace_after_parse(payload):
        nonlocal replaced
        parsed = original_loads(payload)
        if not replaced:
            replaced = True
            run_dir.rename(detached)
            _write_valid_run(run_dir)
        return parsed

    monkeypatch.setattr(loader_module.json, "loads", replace_after_parse)

    with pytest.raises(RunLoadError) as exc_info:
        load_v2_run(run_dir)

    assert exc_info.value.code == "run_directory_changed"
    assert (detached / "run.json").is_file()
    assert (run_dir / "run.json").is_file()


def test_directory_lock_survives_named_lock_replacement(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_valid_run(run_dir)
    detached_lock = run_dir / ".run.lock.detached"

    with pytest.raises(run_lock_module.RunLockError) as exc_info:
        with locked_v2_run(run_dir):
            lock_path = run_dir / ".run.lock"
            lock_path.rename(detached_lock)
            lock_path.write_bytes(b"")
            competing_directory_fd = storage_module.os.open(
                run_dir,
                storage_module.os.O_RDONLY
                | getattr(storage_module.os, "O_DIRECTORY", 0),
            )
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(
                        competing_directory_fd,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
            finally:
                storage_module.os.close(competing_directory_fd)

    assert exc_info.value.code == "run_lock_changed"


@pytest.mark.parametrize("fault_point", ["stat", "fsync"])
def test_anchored_staging_creation_cleans_empty_directory_on_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    staging = run_dir / ".stage"
    original_stat = storage_module.os.stat
    original_fsync = storage_module.os.fsync
    injected = False

    def fail_stat(path, *args, **kwargs):
        nonlocal injected
        if path == staging.name and kwargs.get("dir_fd") is not None and not injected:
            injected = True
            raise OSError("injected staging stat failure")
        return original_stat(path, *args, **kwargs)

    def fail_fsync(descriptor: int):
        nonlocal injected
        if not injected:
            injected = True
            raise OSError("injected staging fsync failure")
        return original_fsync(descriptor)

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=run_dir / "run.json",
    ) as anchor:
        if fault_point == "stat":
            monkeypatch.setattr(storage_module.os, "stat", fail_stat)
        else:
            monkeypatch.setattr(storage_module.os, "fsync", fail_fsync)
        with pytest.raises(OSError):
            create_anchored_directory(anchor, staging)

    assert injected is True
    assert not staging.exists()


def test_anchored_atomic_write_preserves_unknown_replacement_after_temp_name_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    manifest.write_bytes(b"previous")
    original_replace = storage_module.os.replace
    injected = False

    def swap_temp_then_replace(source, destination, *args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            source_parent_fd = kwargs["src_dir_fd"]
            detached = f"{source}.detached"
            storage_module.os.rename(
                source,
                detached,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=source_parent_fd,
            )
            attacker_fd = storage_module.os.open(
                source,
                storage_module.os.O_WRONLY
                | storage_module.os.O_CREAT
                | storage_module.os.O_EXCL,
                0o644,
                dir_fd=source_parent_fd,
            )
            try:
                storage_module.os.write(attacker_fd, b"attacker")
                storage_module.os.fsync(attacker_fd)
            finally:
                storage_module.os.close(attacker_fd)
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "replace", swap_temp_then_replace)

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=manifest,
    ) as anchor:
        with pytest.raises(UnsafeStoragePathError):
            atomic_write_bytes(manifest, b"expected", anchor=anchor)

    assert injected is True
    assert manifest.read_bytes() == b"attacker"


def test_identity_bound_atomic_write_preserves_last_moment_replacement_as_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    manifest.write_bytes(b"expected-current")
    original_exchange = getattr(storage_module, "_native_exchangeat", None)
    injected = False
    retired_name: str | None = None

    def replace_then_exchange(
        parent_fd: int,
        first_name: str,
        second_name: str,
        **kwargs,
    ) -> None:
        nonlocal injected, retired_name
        if not injected:
            replacement = run_dir / ".external-run.json"
            replacement.write_bytes(b"external")
            os.replace(replacement, manifest)
            injected = True
            retired_name = first_name
        assert original_exchange is not None
        original_exchange(
            parent_fd,
            first_name,
            second_name,
            **kwargs,
        )

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=manifest,
    ) as anchor, storage_module.open_anchored_regular_file(
        anchor,
        manifest,
        expected_size=len(b"expected-current"),
    ) as expected_current:
        monkeypatch.setattr(
            storage_module,
            "_native_exchangeat",
            replace_then_exchange,
            raising=False,
        )
        with pytest.raises(UnsafeStoragePathError):
            atomic_write_bytes(
                manifest,
                b"restored",
                anchor=anchor,
                hold_open=True,
                expected_current=expected_current,
            )

    assert injected is True
    assert retired_name is not None
    assert manifest.read_bytes() == b"restored"
    assert (run_dir / retired_name).read_bytes() == b"external"


def test_open_anchored_regular_file_rejects_oversized_replacement_before_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    expected = b"expected-current"
    manifest.write_bytes(expected)

    with DirectoryAnchor.open(run_dir, manifest_path=manifest) as anchor:
        replacement = run_dir / ".oversized-run.json"
        with replacement.open("wb") as handle:
            handle.seek(
                storage_module.V2_RESOURCE_POLICY.structured_artifact_max_bytes
            )
            handle.write(b"\0")
        os.replace(replacement, manifest)

        def forbidden_hash(*_args, **_kwargs):
            pytest.fail("oversized held file reached descriptor hashing")

        monkeypatch.setattr(
            storage_module,
            "_sha256_descriptor",
            forbidden_hash,
        )

        with pytest.raises(storage_module.UnexpectedStorageSizeError):
            storage_module.open_anchored_regular_file(
                anchor,
                manifest,
                expected_size=len(expected),
            )


def test_identity_bound_atomic_write_preserves_last_moment_rewrite_as_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    manifest.write_bytes(b"expected-current")
    original_exchange = storage_module._native_exchangeat
    injected = False
    retired_name: str | None = None

    def rewrite_then_exchange(
        parent_fd: int,
        first_name: str,
        second_name: str,
        **kwargs,
    ) -> None:
        nonlocal injected, retired_name
        if not injected:
            before = manifest.stat()
            time.sleep(0.01)
            with manifest.open("r+b") as handle:
                handle.write(b"attacker-current")
                handle.flush()
                os.fsync(handle.fileno())
            os.utime(
                manifest,
                ns=(before.st_atime_ns, before.st_mtime_ns),
            )
            after = manifest.stat()
            assert after.st_size == before.st_size
            assert after.st_mtime_ns == before.st_mtime_ns
            assert after.st_ctime_ns != before.st_ctime_ns
            injected = True
            retired_name = first_name
        original_exchange(
            parent_fd,
            first_name,
            second_name,
            **kwargs,
        )

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=manifest,
    ) as anchor, storage_module.open_anchored_regular_file(
        anchor,
        manifest,
        expected_size=len(b"expected-current"),
    ) as expected_current:
        monkeypatch.setattr(
            storage_module,
            "_native_exchangeat",
            rewrite_then_exchange,
        )
        with pytest.raises(UnsafeStoragePathError):
            atomic_write_bytes(
                manifest,
                b"restored",
                anchor=anchor,
                hold_open=True,
                expected_current=expected_current,
            )

    assert injected is True
    assert retired_name is not None
    assert manifest.read_bytes() == b"restored"
    assert (run_dir / retired_name).read_bytes() == b"attacker-current"


def test_identity_bound_atomic_write_never_reexchanges_replaced_retired_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    manifest.write_bytes(b"expected-current")
    original_exchange = storage_module._native_exchangeat
    exchange_calls = 0
    retired_name: str | None = None
    late_replacement_created = False

    def replace_around_exchange(
        parent_fd: int,
        first_name: str,
        second_name: str,
        **kwargs,
    ) -> None:
        nonlocal exchange_calls, retired_name, late_replacement_created
        exchange_calls += 1
        if exchange_calls == 1:
            replacement = run_dir / ".external-run.json"
            replacement.write_bytes(b"external-current")
            os.replace(replacement, manifest)
            retired_name = first_name
        else:
            os.rename(
                first_name,
                f"{first_name}.detached-external",
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            external_fd = os.open(
                first_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
                dir_fd=parent_fd,
            )
            try:
                os.write(external_fd, b"late-external")
            finally:
                os.close(external_fd)
            late_replacement_created = True
        original_exchange(parent_fd, first_name, second_name, **kwargs)

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=manifest,
    ) as anchor, storage_module.open_anchored_regular_file(
        anchor,
        manifest,
        expected_size=len(b"expected-current"),
    ) as expected_current:
        monkeypatch.setattr(storage_module, "_native_exchangeat", replace_around_exchange)
        with pytest.raises(UnsafeStoragePathError):
            atomic_write_bytes(
                manifest,
                b"updated",
                anchor=anchor,
                expected_current=expected_current,
            )

    assert exchange_calls == 1
    assert late_replacement_created is False
    assert retired_name is not None
    assert manifest.read_bytes() == b"updated"
    assert (run_dir / retired_name).read_bytes() == b"external-current"


def test_identity_bound_atomic_write_never_reopens_the_retired_inode_for_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    manifest.write_bytes(b"expected-current")
    expected_bytes = b"expected-current"
    original_open = storage_module.os.open

    def reject_retired_writable_open(path, flags, *args, **kwargs):
        if (
            manifest.read_bytes() == b"updated"
            and isinstance(path, str)
            and path.startswith(".run.json.")
            and path.endswith(".tmp")
            and flags & os.O_WRONLY
            and kwargs.get("dir_fd") is not None
        ):
            raise AssertionError("retired CAS inode was reopened for mutation")
        return original_open(path, flags, *args, **kwargs)

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=manifest,
    ) as anchor, storage_module.open_anchored_regular_file(
        anchor,
        manifest,
        expected_size=len(expected_bytes),
    ) as expected_current:
        monkeypatch.setattr(
            storage_module.os,
            "open",
            reject_retired_writable_open,
        )
        atomic_write_bytes(
            manifest,
            b"updated",
            anchor=anchor,
            expected_current=expected_current,
        )

    assert manifest.read_bytes() == b"updated"
    retired = tuple(run_dir.glob(".run.json.*.tmp"))
    assert len(retired) == 1
    assert retired[0].read_bytes() == expected_bytes


def test_identity_bound_atomic_write_never_truncates_a_new_retired_inode_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    expected_bytes = b"expected-current"
    manifest.write_bytes(expected_bytes)
    outside = tmp_path / "outside-hardlink.json"
    original_ftruncate = storage_module.os.ftruncate
    injected = False

    def hardlink_after_identity_check(descriptor: int, length: int) -> None:
        nonlocal injected
        if not injected:
            retired = next(run_dir.glob(".run.json.*.tmp"))
            os.link(retired, outside)
            injected = True
        original_ftruncate(descriptor, length)

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=manifest,
    ) as anchor, storage_module.open_anchored_regular_file(
        anchor,
        manifest,
        expected_size=len(expected_bytes),
    ) as expected_current:
        monkeypatch.setattr(
            storage_module.os,
            "ftruncate",
            hardlink_after_identity_check,
        )
        atomic_write_bytes(
            manifest,
            b"updated",
            anchor=anchor,
            expected_current=expected_current,
        )

    assert manifest.read_bytes() == b"updated"
    retired = tuple(run_dir.glob(".run.json.*.tmp"))
    assert len(retired) == 1
    assert retired[0].read_bytes() == expected_bytes
    if injected:
        assert outside.read_bytes() == expected_bytes


def test_compare_and_swap_never_rolls_back_after_post_exchange_guard_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    old_bytes = b"expected-current"
    new_bytes = b"updated-current"
    manifest.write_bytes(old_bytes)
    sidecar = run_dir / "artifact.json"
    sidecar.write_bytes(b"sealed-artifact")
    original_atomic_write = storage_module.atomic_write_bytes
    writes = 0

    def count_atomic_writes(*args, **kwargs):
        nonlocal writes
        writes += 1
        return original_atomic_write(*args, **kwargs)

    class DriftAfterExchangeGuard:
        def __init__(self) -> None:
            self.verifications = 0

        def verify(self) -> None:
            self.verifications += 1
            if self.verifications == 2:
                sidecar.write_bytes(b"drifted-after-exchange")
                raise UnsafeStoragePathError("finalization artifact changed")

    monkeypatch.setattr(storage_module, "atomic_write_bytes", count_atomic_writes)
    with DirectoryAnchor.open(run_dir, manifest_path=manifest) as anchor:
        with pytest.raises(UnsafeStoragePathError):
            storage_module.compare_and_swap_bytes(
                manifest,
                new_bytes,
                anchor=anchor,
                expected_current_bytes=old_bytes,
                finalization_guards=(DriftAfterExchangeGuard(),),
            )

    assert writes == 1
    assert manifest.read_bytes() == new_bytes
    retired = tuple(run_dir.glob(".run.json.*.tmp"))
    assert len(retired) == 1
    assert retired[0].read_bytes() == old_bytes
    assert sidecar.read_bytes() == b"drifted-after-exchange"


def test_cas_update_run_counts_the_retained_manifest_before_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    old_bytes = b'{"status":"old"}'
    manifest.write_bytes(old_bytes)
    filler = run_dir / "immutable-artifact.bin"
    filler.write_bytes(b"x" * 32)
    updated_run = {"status": "updated", "payload": "y" * 32}
    updated_bytes = storage_module.canonical_json_bytes(updated_run)
    projected = len(old_bytes) + filler.stat().st_size + len(updated_bytes)
    monkeypatch.setattr(
        storage_module,
        "V2_RESOURCE_POLICY",
        replace(storage_module.V2_RESOURCE_POLICY, run_max_bytes=projected - 1),
    )

    with DirectoryAnchor.open(run_dir, manifest_path=manifest) as anchor:
        loaded = SimpleNamespace(
            manifest_path=manifest,
            manifest_bytes=old_bytes,
            run_directory_anchor=anchor,
        )
        with pytest.raises(RunSizeLimitError):
            storage_module.cas_update_run(loaded, updated_run)

    assert manifest.read_bytes() == old_bytes
    assert not tuple(run_dir.glob(".run.json.*.tmp"))


def test_identity_bound_atomic_write_has_no_stat_then_exchange_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    manifest.write_bytes(b"expected-current")
    original_exchange = storage_module._native_exchangeat
    original_stat = storage_module.os.stat
    temp_stat_calls = 0
    injected = False

    def replace_destination_then_exchange(
        parent_fd: int,
        first_name: str,
        second_name: str,
        **kwargs,
    ) -> None:
        if not (run_dir / ".initial-exchange-done").exists():
            marker = run_dir / ".initial-exchange-done"
            marker.write_bytes(b"marker")
            replacement = run_dir / ".external-run.json"
            replacement.write_bytes(b"external-current")
            os.replace(replacement, manifest)
        original_exchange(parent_fd, first_name, second_name, **kwargs)

    def race_second_temp_stat(path, *args, **kwargs):
        nonlocal temp_stat_calls, injected
        result = original_stat(path, *args, **kwargs)
        if (
            isinstance(path, str)
            and path.startswith(".run.json.")
            and path.endswith(".tmp")
            and kwargs.get("dir_fd") is not None
        ):
            temp_stat_calls += 1
            if temp_stat_calls == 3:
                injected = True
                os.rename(
                    path,
                    f"{path}.detached-external",
                    src_dir_fd=kwargs["dir_fd"],
                    dst_dir_fd=kwargs["dir_fd"],
                )
                external_fd = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                    dir_fd=kwargs["dir_fd"],
                )
                try:
                    os.write(external_fd, b"late-external")
                finally:
                    os.close(external_fd)
        return result

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=manifest,
    ) as anchor, storage_module.open_anchored_regular_file(
        anchor,
        manifest,
        expected_size=len(b"expected-current"),
    ) as expected_current:
        monkeypatch.setattr(storage_module, "_native_exchangeat", replace_destination_then_exchange)
        monkeypatch.setattr(storage_module.os, "stat", race_second_temp_stat)
        with pytest.raises(UnsafeStoragePathError):
            atomic_write_bytes(
                manifest,
                b"updated",
                anchor=anchor,
                expected_current=expected_current,
            )

    assert injected is False
    assert temp_stat_calls == 2
    assert manifest.read_bytes() == b"updated"
    retired = [
        path
        for path in run_dir.glob(".run.json.*.tmp")
        if path.read_bytes() == b"external-current"
    ]
    assert len(retired) == 1


def test_anchored_no_replace_preserves_unknown_replacement_after_temp_name_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "note.md"
    original_rename = storage_module._native_renameat_tree_no_replace
    injected = False

    def swap_temp_then_rename(
        source_parent_fd,
        source_name,
        destination_parent_fd,
        destination_name,
        **kwargs,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            storage_module.os.rename(
                source_name,
                f"{source_name}.detached",
                src_dir_fd=source_parent_fd,
                dst_dir_fd=source_parent_fd,
            )
            attacker_fd = storage_module.os.open(
                source_name,
                storage_module.os.O_WRONLY
                | storage_module.os.O_CREAT
                | storage_module.os.O_EXCL,
                0o644,
                dir_fd=source_parent_fd,
            )
            try:
                storage_module.os.write(attacker_fd, b"attacker")
                storage_module.os.fsync(attacker_fd)
            finally:
                storage_module.os.close(attacker_fd)
        original_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            **kwargs,
        )

    monkeypatch.setattr(
        storage_module,
        "_native_renameat_tree_no_replace",
        swap_temp_then_rename,
    )

    with DirectoryAnchor.open(
        parent,
        manifest_path=target,
    ) as anchor:
        with pytest.raises(UnsafeStoragePathError):
            publish_bytes_no_replace(b"expected", target, anchor=anchor)

    assert injected is True
    assert target.read_bytes() == b"attacker"


@pytest.mark.parametrize("writer_kind", ["atomic_write", "no_replace"])
def test_anchored_file_writer_cleanup_preserves_primary_error_and_closes_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_kind: str,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / ("run.json" if writer_kind == "atomic_write" else "note.md")
    if writer_kind == "atomic_write":
        target.write_bytes(b"previous")

    with DirectoryAnchor.open(parent, manifest_path=target) as anchor:
        original_open_parent = storage_module._open_anchored_parent
        original_open = storage_module.os.open
        original_close = storage_module.os.close
        original_stat = storage_module.os.stat
        parent_fd: int | None = None
        temporary_fd: int | None = None
        cleanup_started = False
        closed_fds: set[int] = set()

        def capture_parent(*args, **kwargs):
            nonlocal parent_fd
            parent_fd, name = original_open_parent(*args, **kwargs)
            return parent_fd, name

        def capture_open(path, flags, *args, **kwargs):
            nonlocal temporary_fd
            descriptor = original_open(path, flags, *args, **kwargs)
            if isinstance(path, str) and path.startswith(".") and path.endswith(".tmp"):
                temporary_fd = descriptor
            return descriptor

        def record_close(descriptor: int) -> None:
            closed_fds.add(descriptor)
            original_close(descriptor)

        def fail_cleanup_stat(path, *args, **kwargs):
            if (
                cleanup_started
                and isinstance(path, str)
                and path.startswith(".")
                and path.endswith(".tmp")
            ):
                raise PermissionError("injected cleanup stat failure")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(storage_module, "_open_anchored_parent", capture_parent)
        monkeypatch.setattr(storage_module.os, "open", capture_open)
        monkeypatch.setattr(storage_module.os, "close", record_close)
        monkeypatch.setattr(storage_module.os, "stat", fail_cleanup_stat)

        if writer_kind == "atomic_write":

            def fail_primary(*args, **kwargs):
                nonlocal cleanup_started
                cleanup_started = True
                raise RuntimeError("injected primary write failure")

            monkeypatch.setattr(storage_module.os, "replace", fail_primary)
            invoke = lambda: atomic_write_bytes(target, b"expected", anchor=anchor)
        else:

            def fail_primary(*args, **kwargs):
                nonlocal cleanup_started
                cleanup_started = True
                raise RuntimeError("injected primary write failure")

            monkeypatch.setattr(
                storage_module,
                "_native_renameat_tree_no_replace",
                fail_primary,
            )
            invoke = lambda: publish_bytes_no_replace(b"expected", target, anchor=anchor)

        with pytest.raises(RuntimeError, match="injected primary write failure"):
            invoke()

        assert parent_fd is not None
        assert temporary_fd is not None
        assert parent_fd in closed_fds
        assert temporary_fd in closed_fds


def test_anchored_atomic_write_detects_name_swap_after_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "run.json"
    manifest.write_bytes(b"previous")
    detached = run_dir / "run.detached.json"
    original_read = storage_module._read_anchored_regular_file
    injected = False

    def read_then_swap(anchor, path, **kwargs):
        nonlocal injected
        raw = original_read(anchor, path, **kwargs)
        if Path(path) == manifest and raw == b"expected" and not injected:
            injected = True
            manifest.rename(detached)
            manifest.write_bytes(b"attacker")
        return raw

    monkeypatch.setattr(storage_module, "_read_anchored_regular_file", read_then_swap)

    with DirectoryAnchor.open(
        run_dir,
        manifest_path=manifest,
    ) as anchor:
        with pytest.raises(UnsafeStoragePathError):
            atomic_write_bytes(manifest, b"expected", anchor=anchor)

    assert injected is True
    assert manifest.read_bytes() == b"attacker"
    assert detached.read_bytes() == b"expected"


def test_anchored_no_replace_detects_name_swap_after_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "note.md"
    detached = parent / "note.detached.md"
    original_read = storage_module._read_anchored_regular_file
    injected = False

    def read_then_swap(anchor, path, **kwargs):
        nonlocal injected
        raw = original_read(anchor, path, **kwargs)
        if Path(path) == target and not injected:
            injected = True
            target.rename(detached)
            target.write_bytes(b"attacker")
        return raw

    monkeypatch.setattr(storage_module, "_read_anchored_regular_file", read_then_swap)

    with DirectoryAnchor.open(
        parent,
        manifest_path=target,
    ) as anchor:
        with pytest.raises(UnsafeStoragePathError):
            publish_bytes_no_replace(b"expected", target, anchor=anchor)

    assert injected is True
    assert target.read_bytes() == b"attacker"
    assert detached.read_bytes() == b"expected"


def test_anchored_tree_publication_never_follows_replaced_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_path = tmp_path / "anchor"
    anchor_path.mkdir()
    staging = anchor_path / ".candidate.staging"
    staging.mkdir()
    (staging / "candidate.json").write_text("sealed", encoding="utf-8")
    destination = anchor_path / "candidates" / "candidate-id"
    detached = tmp_path / "detached-anchor"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_rename = storage_module._native_renameat_tree_no_replace
    swapped = False

    def swap_then_rename(*args, **kwargs) -> None:
        nonlocal swapped
        source_name = args[1]
        if not swapped and source_name == staging.name:
            swapped = True
            anchor_path.rename(detached)
            anchor_path.symlink_to(outside, target_is_directory=True)
        original_rename(*args, **kwargs)

    monkeypatch.setattr(
        storage_module,
        "_native_renameat_tree_no_replace",
        swap_then_rename,
    )

    with DirectoryAnchor.open(
        anchor_path,
        manifest_path=anchor_path / "run.json",
    ) as anchor:
        with pytest.raises(UnsafeStoragePathError):
            atomic_publish_tree(staging, destination, anchor=anchor)

    assert swapped is True
    assert not (outside / "candidates" / "candidate-id").exists()
    assert (detached / "candidates" / "candidate-id" / "candidate.json").read_text() == "sealed"


def test_anchored_tree_publication_rejects_replaced_child_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_path = tmp_path / "anchor"
    anchor_path.mkdir()
    staging = anchor_path / ".candidate.staging"
    staging.mkdir()
    (staging / "candidate.json").write_text("sealed", encoding="utf-8")
    candidates = anchor_path / "candidates"
    candidates.mkdir()
    detached_candidates = anchor_path / "candidates-detached"
    destination = candidates / "candidate-id"
    original_rename = storage_module._native_renameat_tree_no_replace

    def replace_child_parent_then_rename(*args, **kwargs) -> None:
        candidates.rename(detached_candidates)
        candidates.mkdir()
        original_rename(*args, **kwargs)

    monkeypatch.setattr(
        storage_module,
        "_native_renameat_tree_no_replace",
        replace_child_parent_then_rename,
    )

    with DirectoryAnchor.open(
        anchor_path,
        manifest_path=anchor_path / "run.json",
    ) as anchor:
        with pytest.raises(UnsafeStoragePathError):
            atomic_publish_tree(staging, destination, anchor=anchor)

    assert not destination.exists()
    assert (
        detached_candidates / "candidate-id" / "candidate.json"
    ).read_text() == "sealed"


def test_anchored_tree_publication_fsyncs_source_and_destination_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_path = tmp_path / "anchor"
    anchor_path.mkdir()
    staging = anchor_path / ".candidate.staging"
    staging.mkdir()
    (staging / "candidate.json").write_text("sealed", encoding="utf-8")
    candidates = anchor_path / "candidates"
    candidates.mkdir()
    destination = candidates / "candidate-id"
    expected_parent_inodes = {anchor_path.stat().st_ino, candidates.stat().st_ino}
    observed_inodes: set[int] = set()
    original_fsync = storage_module.os.fsync

    def record_fsync(descriptor: int) -> None:
        metadata = storage_module.os.fstat(descriptor)
        if metadata.st_ino in expected_parent_inodes:
            observed_inodes.add(metadata.st_ino)
        original_fsync(descriptor)

    monkeypatch.setattr(storage_module.os, "fsync", record_fsync)

    with DirectoryAnchor.open(
        anchor_path,
        manifest_path=anchor_path / "run.json",
    ) as anchor:
        atomic_publish_tree(staging, destination, anchor=anchor)

    assert observed_inodes == expected_parent_inodes


def test_held_tree_publication_rejects_unsafe_file_before_commit(
    tmp_path: Path,
) -> None:
    anchor_path = tmp_path / "anchor"
    anchor_path.mkdir()
    staging = anchor_path / ".candidate.staging"
    staging.mkdir()
    (staging / "run.json").write_text("sealed", encoding="utf-8")
    destination = anchor_path / "candidate-id"

    with DirectoryAnchor.open(
        anchor_path,
        manifest_path=anchor_path / "run.json",
    ) as anchor:
        with pytest.raises(ValueError):
            atomic_publish_tree(
                staging,
                destination,
                anchor=anchor,
                hold_open_relative_file="../run.json",
            )

    assert staging.is_dir()
    assert not destination.exists()


def test_anchored_tree_publication_rejects_changed_closed_set(
    tmp_path: Path,
) -> None:
    anchor_path = tmp_path / "anchor"
    anchor_path.mkdir()
    staging = anchor_path / ".candidate.staging"
    staging.mkdir()
    candidate = staging / "candidate.json"
    candidate.write_bytes(b"expected")
    destination = anchor_path / "candidates" / "candidate-id"

    with DirectoryAnchor.open(
        anchor_path,
        manifest_path=anchor_path / "run.json",
    ) as anchor, DirectoryAnchor.open(
        staging,
        manifest_path=staging / "candidate.json",
    ) as staging_anchor:
        sealed = snapshot_anchored_tree(staging_anchor)
        candidate.rename(staging / "candidate.detached.json")
        candidate.write_bytes(b"attacker")

        with pytest.raises(UnsafeStoragePathError):
            atomic_publish_tree(
                staging,
                destination,
                anchor=anchor,
                expected_staging_anchor=staging_anchor,
                expected_tree_snapshot=sealed,
            )

    assert not destination.exists()


def test_anchored_tree_publication_preserves_unknown_child_swap_during_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_path = tmp_path / "anchor"
    anchor_path.mkdir()
    staging = anchor_path / ".candidate.staging"
    staging.mkdir()
    (staging / "candidate.json").write_bytes(b"expected")
    destination = anchor_path / "candidates" / "candidate-id"
    original_rename = storage_module._native_renameat_tree_no_replace
    injected = False

    def swap_child_then_rename(
        source_parent_fd,
        source_name,
        destination_parent_fd,
        destination_name,
        **kwargs,
    ) -> None:
        nonlocal injected
        if not injected and source_name == staging.name:
            injected = True
            staging_fd = storage_module.os.open(
                source_name,
                storage_module.os.O_RDONLY
                | getattr(storage_module.os, "O_DIRECTORY", 0),
                dir_fd=source_parent_fd,
            )
            try:
                storage_module.os.rename(
                    "candidate.json",
                    "candidate.detached.json",
                    src_dir_fd=staging_fd,
                    dst_dir_fd=staging_fd,
                )
                attacker_fd = storage_module.os.open(
                    "candidate.json",
                    storage_module.os.O_WRONLY
                    | storage_module.os.O_CREAT
                    | storage_module.os.O_EXCL,
                    0o644,
                    dir_fd=staging_fd,
                )
                try:
                    storage_module.os.write(attacker_fd, b"attacker")
                finally:
                    storage_module.os.close(attacker_fd)
            finally:
                storage_module.os.close(staging_fd)
        original_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            **kwargs,
        )

    monkeypatch.setattr(
        storage_module,
        "_native_renameat_tree_no_replace",
        swap_child_then_rename,
    )

    with DirectoryAnchor.open(
        anchor_path,
        manifest_path=anchor_path / "run.json",
    ) as anchor, DirectoryAnchor.open(
        staging,
        manifest_path=staging / "candidate.json",
    ) as staging_anchor:
        with pytest.raises(UnsafeStoragePathError):
            atomic_publish_tree(
                staging,
                destination,
                anchor=anchor,
                expected_staging_anchor=staging_anchor,
                expected_tree_snapshot=tree_snapshot_from_bytes(
                    {"candidate.json": b"expected"}
                ),
            )

    assert injected is True
    assert (destination / "candidate.json").read_bytes() == b"attacker"
    assert (destination / "candidate.detached.json").read_bytes() == b"expected"
