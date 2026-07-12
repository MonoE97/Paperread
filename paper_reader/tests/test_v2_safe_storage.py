from __future__ import annotations

import json
import fcntl
from pathlib import Path

import pytest

import paper_reader.run_lock as run_lock_module
import paper_reader.storage as storage_module
import paper_reader.v2_loader as loader_module
from paper_reader.run_lock import locked_v2_run
from paper_reader.storage import (
    UnsafeStoragePathError,
    atomic_publish_tree,
    atomic_write_bytes,
    create_anchored_directory,
    publish_bytes_no_replace,
    snapshot_anchored_tree,
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

    def read_then_swap(anchor, path):
        nonlocal injected
        raw = original_read(anchor, path)
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

    def read_then_swap(anchor, path):
        nonlocal injected
        raw = original_read(anchor, path)
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
        if not swapped:
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
        if not injected:
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
