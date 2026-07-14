import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile

import pytest

import paper_reader_batch.v2_json as json_module
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    locked_file,
    open_directory_fd,
    publish_bytes_no_replace,
    read_bytes,
    read_json_bytes,
    read_locked_bytes,
)
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest


REQUEST_1 = "11111111-1111-4111-8111-111111111111"


def test_bounded_directory_listing_stops_at_first_entry_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumed = 0

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeScandir:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed
            consumed += 1
            if consumed > 3:
                raise AssertionError("listing consumed entries beyond max_entries + 1")
            return Entry(f"entry-{consumed}")

    monkeypatch.setattr(json_module.os, "scandir", lambda _fd: FakeScandir())

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module._bounded_sorted_names(
            123,
            max_entries=2,
            label="test directory",
        )

    assert exc_info.value.code == "resource_limit"
    assert consumed == 3


def test_held_exact_sibling_files_keep_both_named_inodes_bound(
    tmp_path: Path,
) -> None:
    markdown = tmp_path / "batch-report.md"
    report_json = tmp_path / "batch-report.json"
    markdown.write_bytes(b"markdown")
    report_json.write_bytes(b"json")
    markdown.chmod(0o600)
    report_json.chmod(0o600)
    detached = tmp_path / "detached-markdown"

    with pytest.raises(BatchRuntimeError) as exc_info:
        with json_module.held_exact_sibling_files(
            tmp_path,
            {
                markdown.name: b"markdown",
                report_json.name: b"json",
            },
        ):
            markdown.rename(detached)
            markdown.write_bytes(b"markdown")
            markdown.chmod(0o600)

    assert exc_info.value.code == "storage_path_changed"
    assert detached.read_bytes() == b"markdown"
    assert markdown.read_bytes() == b"markdown"


def test_anchored_relative_read_stays_on_held_directory_during_path_replacement(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "paper_analysis"
    run_dir.mkdir()
    (run_dir / "run.json").write_bytes(b"held run")
    detached_held = tmp_path / "paper_analysis.held"
    detached_replacement = tmp_path / "paper_analysis.replacement"

    with open_directory_fd(run_dir, create=False) as (descriptor, _bound):
        run_dir.rename(detached_held)
        run_dir.mkdir()
        (run_dir / "run.json").write_bytes(b"replacement run")

        observed = json_module.read_relative_bytes(
            descriptor,
            "run.json",
            code="child_artifact_mismatch",
        )

        run_dir.rename(detached_replacement)
        detached_held.rename(run_dir)

    assert observed == b"held run"


def test_relative_read_rejects_oversized_file_before_os_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "artifact.json").write_bytes(b"12345")

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("oversized relative artifact reached os.read")

    with open_directory_fd(run_dir, create=False) as (descriptor, _bound):
        monkeypatch.setattr(json_module.os, "read", forbidden_read)
        with pytest.raises(BatchRuntimeError) as exc_info:
            json_module.read_relative_bytes(
                descriptor,
                "artifact.json",
                max_bytes=4,
            )

    assert exc_info.value.code == "artifact_unreadable"


def test_low_level_read_rejects_fifo_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    os.mkfifo(run_dir / "artifact.fifo")

    with open_directory_fd(run_dir, create=False) as (descriptor, _bound):
        def forbidden_open(*_args, **_kwargs):
            raise AssertionError("FIFO reached os.open")

        monkeypatch.setattr(json_module.os, "open", forbidden_open)
        with pytest.raises(BatchRuntimeError) as exc_info:
            json_module._read_regular_single_link(
                descriptor,
                "artifact.fifo",
                code="artifact_unreadable",
            )

    assert exc_info.value.code == "unsafe_storage"


def test_json_read_uses_bounded_context_default_before_os_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(b"12345")

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("oversized JSON artifact reached os.read")

    monkeypatch.setattr(json_module, "MAX_JSON_ARTIFACT_BYTES", 4, raising=False)
    monkeypatch.setattr(json_module.os, "read", forbidden_read)
    with pytest.raises(BatchRuntimeError) as exc_info:
        read_json_bytes(artifact)

    assert exc_info.value.code == "artifact_unreadable"


def test_read_limit_is_enforced_during_chunk_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"1234")
    calls = 0

    def growing_read(_descriptor: int, requested: int) -> bytes:
        nonlocal calls
        calls += 1
        assert requested <= 5
        return b"12345"

    monkeypatch.setattr(json_module.os, "read", growing_read)
    with pytest.raises(BatchRuntimeError) as exc_info:
        read_bytes(artifact, max_bytes=4)

    assert exc_info.value.code == "artifact_unreadable"
    assert calls == 1


def test_locked_read_rejects_oversized_file_before_os_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "runtime.lock"
    artifact.write_bytes(b"12345")
    descriptor = os.open(artifact, os.O_RDONLY)

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("oversized locked file reached os.read")

    try:
        monkeypatch.setattr(json_module.os, "read", forbidden_read)
        with pytest.raises(BatchRuntimeError) as exc_info:
            read_locked_bytes(descriptor, max_bytes=4)
    finally:
        os.close(descriptor)

    assert exc_info.value.code == "unsafe_storage"


def _crash_after_rename(target: str) -> None:
    def crash(stage: str) -> None:
        if stage == "after_rename":
            os._exit(17)

    publish_bytes_no_replace(Path(target), b"exact bytes", fault=crash)


def _crash_replace_before_exchange(target: str) -> None:
    def crash(stage: str) -> None:
        if stage == "after_file_fsync":
            os._exit(23)

    json_module.replace_bytes_atomic(
        Path(target),
        b"new",
        transition_id="storage-crash",
        expected_current=b"old",
        fault=crash,
    )


def _crash_replace_owner_bootstrap(target: str, crash_stage: str) -> None:
    def crash(stage: str) -> None:
        if stage == crash_stage:
            os._exit(29)

    json_module.replace_bytes_atomic(
        Path(target),
        b"new",
        transition_id="storage-crash",
        expected_current=b"old",
        fault=crash,
    )


def _crash_during_transition_write(target: str, kind: str) -> None:
    original_write_all = json_module._write_all

    def write_prefix_then_crash(descriptor: int, data: bytes) -> None:
        selected = (
            (kind == "owner" and b"storage-transition-owner" in data)
            or (kind == "payload" and data == b"new")
            or (kind == "completion" and b"storage-transition-completion" in data)
        )
        if not selected:
            original_write_all(descriptor, data)
            return
        written = os.write(descriptor, data[: max(1, len(data) // 2)])
        assert written > 0
        os.fsync(descriptor)
        os._exit(37)

    json_module._write_all = write_prefix_then_crash
    json_module.replace_bytes_atomic(
        Path(target),
        b"new",
        transition_id="partial-write-crash",
        expected_current=b"old",
    )


def _hold_replaced_lock(lock_path: str, entered, release, queue) -> None:
    try:
        with locked_file(Path(lock_path), create=False):
            entered.set()
            release.wait(timeout=10)
    except Exception as exc:
        queue.put(("holder_error", getattr(exc, "code", type(exc).__name__)))
    else:
        queue.put(("holder_ok", None))


def _contend_replaced_lock(lock_path: str, ready, entered, queue) -> None:
    ready.set()
    try:
        with locked_file(Path(lock_path), create=False):
            entered.set()
    except Exception as exc:
        queue.put(("contender_error", getattr(exc, "code", type(exc).__name__)))
    else:
        queue.put(("contender_ok", None))


def _inputs(tmp_path: Path) -> Path:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nsafe\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    return paths


def test_receipt_root_and_output_parent_symlinks_fail_closed(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (skill_root / ".paper_reader_batch").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BatchRuntimeError, match="symlink|safe|component"):
        create_pdf_paths_manifest(
            paths,
            batch_title="unsafe receipt root",
            output=tmp_path / "manifest.json",
            request_id=REQUEST_1,
            skill_root=skill_root,
        )
    assert list(outside.iterdir()) == []

    safe_skill = tmp_path / "safe-skill"
    safe_skill.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(BatchRuntimeError, match="symlink|safe|component"):
        create_pdf_paths_manifest(
            paths,
            batch_title="unsafe output",
            output=output_link / "manifest.json",
            request_id="22222222-2222-4222-8222-222222222222",
            skill_root=safe_skill,
        )
    assert not (outside / "manifest.json").exists()


def test_no_replace_rejects_hardlinked_existing_target_even_with_exact_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(b"{}")
    target = tmp_path / "target.json"
    target.hardlink_to(source)

    with pytest.raises(BatchRuntimeError, match="single-link|conflict"):
        publish_bytes_no_replace(target, b"{}", allow_existing_exact=True)
    with pytest.raises(BatchRuntimeError, match="single-link|safe"):
        read_bytes(target)


def test_parent_swap_and_file_fsync_failure_do_not_report_publication(tmp_path: Path, monkeypatch) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "artifact.json"
    moved = tmp_path / "moved-parent"

    def swap_parent(stage: str) -> None:
        if stage == "before_parent_fsync":
            parent.rename(moved)
            parent.mkdir()

    with pytest.raises(BatchRuntimeError, match="changed|stable"):
        publish_bytes_no_replace(target, b"{}", fault=swap_parent)
    assert not target.exists()
    assert (moved / "artifact.json").read_bytes() == b"{}"

    fsync_parent = tmp_path / "fsync-parent"
    fsync_parent.mkdir()
    fsync_target = fsync_parent / "artifact.json"

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr("paper_reader_batch.v2_json.os.fsync", fail_fsync)
    with pytest.raises(BatchRuntimeError, match="fsync"):
        publish_bytes_no_replace(fsync_target, b"{}")
    assert not fsync_target.exists()


def test_lock_and_read_path_swaps_fail_closed(tmp_path: Path) -> None:
    lock = tmp_path / "runtime.lock"
    moved_lock = tmp_path / "runtime.lock.old"

    def swap_lock(stage: str) -> None:
        if stage == "after_flock":
            lock.rename(moved_lock)
            lock.write_bytes(b"replacement")

    with pytest.raises(BatchRuntimeError, match="changed"):
        with locked_file(lock, fault=swap_lock):
            raise AssertionError("swapped lock must not be yielded")

    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(b"old bytes")
    moved_artifact = tmp_path / "artifact.old"

    def swap_artifact(stage: str) -> None:
        if stage == "after_open":
            artifact.rename(moved_artifact)
            artifact.write_bytes(b"new bytes")

    with pytest.raises(BatchRuntimeError, match="changed"):
        read_bytes(artifact, fault=swap_artifact)


def test_lock_path_replacement_does_not_create_a_second_live_critical_section(tmp_path: Path) -> None:
    lock = tmp_path / "runtime.lock"
    lock.write_bytes(b"stable secret")
    context = multiprocessing.get_context("spawn")
    holder_entered = context.Event()
    release_holder = context.Event()
    contender_ready = context.Event()
    contender_entered = context.Event()
    queue = context.Queue()
    holder = context.Process(
        target=_hold_replaced_lock,
        args=(str(lock), holder_entered, release_holder, queue),
    )
    holder.start()
    assert holder_entered.wait(timeout=5)

    moved = tmp_path / "runtime.lock.old"
    lock.rename(moved)
    lock.write_bytes(b"replacement secret")
    contender = context.Process(
        target=_contend_replaced_lock,
        args=(str(lock), contender_ready, contender_entered, queue),
    )
    contender.start()
    assert contender_ready.wait(timeout=5)
    assert not contender_entered.wait(timeout=0.5)

    release_holder.set()
    holder.join(timeout=10)
    contender.join(timeout=10)
    assert holder.exitcode == 0
    assert contender.exitcode == 0
    outcomes = [queue.get(timeout=2), queue.get(timeout=2)]
    assert ("holder_error", "storage_path_changed") in outcomes
    assert ("contender_ok", None) in outcomes


def test_lock_parent_replacement_does_not_create_a_second_live_critical_section(tmp_path: Path) -> None:
    parent = tmp_path / "runtime"
    parent.mkdir()
    lock = parent / "runtime.lock"
    lock.write_bytes(b"stable secret")
    context = multiprocessing.get_context("spawn")
    holder_entered = context.Event()
    release_holder = context.Event()
    contender_ready = context.Event()
    contender_entered = context.Event()
    queue = context.Queue()
    holder = context.Process(
        target=_hold_replaced_lock,
        args=(str(lock), holder_entered, release_holder, queue),
    )
    holder.start()
    assert holder_entered.wait(timeout=5)

    moved_parent = tmp_path / "runtime.old"
    parent.rename(moved_parent)
    parent.mkdir()
    lock.write_bytes(b"replacement secret")
    contender = context.Process(
        target=_contend_replaced_lock,
        args=(str(lock), contender_ready, contender_entered, queue),
    )
    contender.start()
    assert contender_ready.wait(timeout=5)
    assert not contender_entered.wait(timeout=0.5)

    release_holder.set()
    holder.join(timeout=10)
    contender.join(timeout=10)
    assert holder.exitcode == 0
    assert contender.exitcode == 0
    outcomes = [queue.get(timeout=2), queue.get(timeout=2)]
    assert ("holder_error", "storage_path_changed") in outcomes
    assert ("contender_ok", None) in outcomes


def test_lock_ancestor_replacement_does_not_create_a_second_live_critical_section(
    tmp_path: Path,
) -> None:
    ancestor = tmp_path / "ancestor"
    parent = ancestor / "runtime"
    parent.mkdir(parents=True)
    lock = parent / "runtime.lock"
    lock.write_bytes(b"stable secret")
    context = multiprocessing.get_context("spawn")
    holder_entered = context.Event()
    release_holder = context.Event()
    contender_ready = context.Event()
    contender_entered = context.Event()
    queue = context.Queue()
    holder = context.Process(
        target=_hold_replaced_lock,
        args=(str(lock), holder_entered, release_holder, queue),
    )
    holder.start()
    assert holder_entered.wait(timeout=5)

    moved_ancestor = tmp_path / "ancestor.old"
    ancestor.rename(moved_ancestor)
    parent.mkdir(parents=True)
    lock.write_bytes(b"replacement secret")
    contender = context.Process(
        target=_contend_replaced_lock,
        args=(str(lock), contender_ready, contender_entered, queue),
    )
    contender.start()
    assert contender_ready.wait(timeout=5)
    assert not contender_entered.wait(timeout=0.5)

    release_holder.set()
    holder.join(timeout=10)
    contender.join(timeout=10)
    assert holder.exitcode == 0
    assert contender.exitcode == 0
    outcomes = [queue.get(timeout=2), queue.get(timeout=2)]
    assert ("holder_error", "storage_path_changed") in outcomes
    assert ("contender_ok", None) in outcomes


def test_inherited_lock_bundle_keeps_parent_guard_live_after_owner_context_exits(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("descriptor inheritance regression is POSIX-only")
    parent = tmp_path / "runtime"
    parent.mkdir()
    lock = parent / "runtime.lock"
    lock.write_bytes(b"stable secret")
    inherited: list[int] = []
    release_read, release_write = os.pipe()
    child: subprocess.Popen[bytes] | None = None
    try:
        with locked_file(
            lock,
            create=False,
            inherited_lock_descriptors=inherited,
        ) as descriptor:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os,sys; os.read(int(sys.argv[1]), 1)",
                    str(release_read),
                ],
                pass_fds=(release_read, descriptor, *inherited),
                close_fds=True,
            )
        os.close(release_read)
        release_read = -1

        moved_parent = tmp_path / "runtime.old"
        parent.rename(moved_parent)
        parent.mkdir()
        lock.write_bytes(b"replacement secret")
        context = multiprocessing.get_context("spawn")
        contender_ready = context.Event()
        contender_entered = context.Event()
        queue = context.Queue()
        contender = context.Process(
            target=_contend_replaced_lock,
            args=(str(lock), contender_ready, contender_entered, queue),
        )
        contender.start()
        assert contender_ready.wait(timeout=5)
        assert not contender_entered.wait(timeout=0.5)

        os.write(release_write, b"1")
        os.close(release_write)
        release_write = -1
        child.wait(timeout=10)
        contender.join(timeout=10)
        assert child.returncode == 0
        assert contender.exitcode == 0
        assert queue.get(timeout=2) == ("contender_ok", None)
    finally:
        if release_read >= 0:
            os.close(release_read)
        if release_write >= 0:
            os.close(release_write)
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_hard_crash_after_atomic_rename_is_exactly_recoverable(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crash_after_rename, args=(str(target),))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 17
    assert target.read_bytes() == b"exact bytes"
    assert not list(tmp_path.glob("*.tmp"))

    publish_bytes_no_replace(target, b"exact bytes", allow_existing_exact=True)
    assert target.stat().st_nlink == 1


def test_publish_rejects_swapped_staging_inode_without_publishing_replacement(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifact.json"
    expected = b"expected"
    temporary = tmp_path / f".{target.name}.{json_module.sha256_bytes(expected)}.tmp"
    detached = tmp_path / "expected.detached"

    def swap_staging(stage: str) -> None:
        if stage != "after_pending_rename":
            return
        temporary.rename(detached)
        temporary.write_bytes(b"attacker")

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.publish_bytes_no_replace(target, expected, fault=swap_staging)

    assert exc_info.value.code == "storage_path_changed"
    assert not target.exists()
    assert detached.read_bytes() == expected
    assert temporary.read_bytes() == b"attacker"


def test_publish_preserves_external_replacement_arriving_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "artifact.json"
    detached = tmp_path / "published.detached"
    expected = b"expected"
    original_rename = json_module._rename_no_replace
    injected = False

    def publish_then_replace(parent_fd: int, source_name: str, target_name: str) -> None:
        nonlocal injected
        original_rename(parent_fd, source_name, target_name)
        if injected or target_name != target.name:
            return
        injected = True
        os.rename(target_name, detached.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        descriptor = os.open(
            target_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(descriptor, b"external replacement")
        finally:
            os.close(descriptor)

    monkeypatch.setattr(json_module, "_rename_no_replace", publish_then_replace)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.publish_bytes_no_replace(target, expected)

    assert injected
    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"external replacement"
    assert detached.read_bytes() == expected


def test_publish_rechecks_public_leaf_after_parent_fsync(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    detached = tmp_path / "published.detached"
    expected = b"expected"
    injected = False

    def replace_before_parent_fsync(stage: str) -> None:
        nonlocal injected
        if stage != "before_parent_fsync" or injected:
            return
        injected = True
        target.rename(detached)
        target.write_bytes(b"external replacement")

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.publish_bytes_no_replace(
            target,
            expected,
            fault=replace_before_parent_fsync,
        )

    assert injected
    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"external replacement"
    assert detached.read_bytes() == expected


def test_publish_failure_never_attempts_post_publication_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "artifact.json"
    expected = b"expected"
    temporary_name = f".{target.name}.{json_module.sha256_bytes(expected)}.tmp"
    original_exact = json_module._held_regular_is_exact
    original_rename = json_module._rename_no_replace
    rename_pairs: list[tuple[str, str]] = []

    def fail_public_exact(descriptor: int, content: bytes, *, name: str) -> bool:
        if name == target.name:
            return False
        return original_exact(descriptor, content, name=name)

    def record_rename(parent_fd: int, source_name: str, target_name: str) -> None:
        rename_pairs.append((source_name, target_name))
        original_rename(parent_fd, source_name, target_name)

    monkeypatch.setattr(json_module, "_held_regular_is_exact", fail_public_exact)
    monkeypatch.setattr(json_module, "_rename_no_replace", record_rename)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.publish_bytes_no_replace(target, expected)

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == expected
    assert (target.name, temporary_name) not in rename_pairs


def test_promote_rejects_swapped_staging_inode_without_publishing_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging.json"
    target = tmp_path / "artifact.json"
    detached = tmp_path / "expected.detached"
    expected = b"expected"
    staging.write_bytes(expected)
    original_rename = json_module._rename_no_replace
    injected = False

    def swap_then_rename(parent_fd: int, source_name: str, target_name: str) -> None:
        nonlocal injected
        if injected:
            original_rename(parent_fd, source_name, target_name)
            return
        injected = True
        os.rename(source_name, detached.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        descriptor = os.open(
            source_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(descriptor, b"attacker")
        finally:
            os.close(descriptor)
        original_rename(parent_fd, source_name, target_name)

    monkeypatch.setattr(json_module, "_rename_no_replace", swap_then_rename)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.promote_bytes_no_replace(staging, target, expected)

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"attacker"
    assert detached.read_bytes() == expected
    assert not staging.exists()


def test_promote_preserves_external_replacement_arriving_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging.json"
    target = tmp_path / "artifact.json"
    detached = tmp_path / "published.detached"
    expected = b"expected"
    staging.write_bytes(expected)
    original_rename = json_module._rename_no_replace
    injected = False

    def publish_then_replace(parent_fd: int, source_name: str, target_name: str) -> None:
        nonlocal injected
        original_rename(parent_fd, source_name, target_name)
        if injected or target_name != target.name:
            return
        injected = True
        os.rename(target_name, detached.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        descriptor = os.open(
            target_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(descriptor, b"external replacement")
        finally:
            os.close(descriptor)

    monkeypatch.setattr(json_module, "_rename_no_replace", publish_then_replace)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.promote_bytes_no_replace(staging, target, expected)

    assert injected
    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"external replacement"
    assert detached.read_bytes() == expected
    assert not staging.exists()


def test_replace_preserves_concurrent_target_replacement(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    detached = tmp_path / "original.detached"
    target.write_bytes(b"old")
    target.chmod(0o600)

    def replace_target(stage: str) -> None:
        if stage != "after_pending_rename":
            return
        target.rename(detached)
        target.write_bytes(b"external replacement")

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"new",
            transition_id="concurrent-before-exchange",
            expected_current=b"old",
            fault=replace_target,
        )

    assert exc_info.value.code == "unsafe_storage"
    assert target.read_bytes() == b"external replacement"
    assert detached.read_bytes() == b"old"


def test_replace_preserves_external_replacement_arriving_after_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "artifact.json"
    detached_new = tmp_path / "published.detached"
    target.write_bytes(b"old")
    target.chmod(0o600)
    original_exchange = json_module._exchange_names_between
    injected = False

    def exchange_then_replace(
        first_parent_fd: int,
        first_name: str,
        second_parent_fd: int,
        second_name: str,
    ) -> None:
        nonlocal injected
        original_exchange(first_parent_fd, first_name, second_parent_fd, second_name)
        if injected or second_name != target.name:
            return
        injected = True
        os.rename(
            second_name,
            detached_new.name,
            src_dir_fd=second_parent_fd,
            dst_dir_fd=second_parent_fd,
        )
        descriptor = os.open(
            second_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=second_parent_fd,
        )
        try:
            os.write(descriptor, b"external replacement")
        finally:
            os.close(descriptor)

    monkeypatch.setattr(json_module, "_exchange_names_between", exchange_then_replace)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"new",
            transition_id="concurrent-after-exchange",
            expected_current=b"old",
        )

    assert injected
    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"external replacement"
    assert detached_new.read_bytes() == b"new"


def test_replace_rechecks_public_leaf_after_parent_fsync(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    detached = tmp_path / "published.detached"
    target.write_bytes(b"old")
    target.chmod(0o600)
    injected = False

    def replace_before_parent_fsync(stage: str) -> None:
        nonlocal injected
        if stage != "after_transition_parent_fsync" or injected:
            return
        injected = True
        target.rename(detached)
        target.write_bytes(b"external replacement")

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"new",
            transition_id="replace-before-public-parent-fsync",
            expected_current=b"old",
            fault=replace_before_parent_fsync,
        )

    assert injected
    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"external replacement"
    assert detached.read_bytes() == b"new"


def test_incomplete_writing_attempt_is_never_truncated_or_rewritten(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    writing = tmp_path / f".{target.name}.{'a' * 32}.writing"
    writing.write_bytes(b"partial")
    writing.chmod(0o600)

    json_module.publish_bytes_no_replace(target, b"published")

    assert target.read_bytes() == b"published"
    assert writing.read_bytes() == b"partial"


def test_exact_nonempty_writing_prefix_is_resumed_on_held_inode(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifact.json"
    writing = tmp_path / f".{target.name}.{'a' * 32}.writing"
    writing.write_bytes(b"publ")
    writing.chmod(0o600)
    inode = writing.stat().st_ino

    json_module.publish_bytes_no_replace(target, b"published")

    assert target.read_bytes() == b"published"
    assert target.stat().st_ino == inode
    assert not writing.exists()


def test_closed_world_directory_listing_exposes_even_safe_tombstones(tmp_path: Path) -> None:
    tombstone = tmp_path / f".extra.{'a' * 32}.deleting"
    tombstone.write_bytes(b"")

    assert json_module.list_directory(tmp_path) == [tombstone.name]


def test_repeated_atomic_replace_keeps_one_live_marker_and_bounded_retired_history(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    current = b"0"
    target.write_bytes(current)
    target.chmod(0o600)

    for value in range(1, 129):
        updated = str(value).encode("ascii")
        json_module.replace_bytes_atomic(
            target,
            updated,
            transition_id=f"state:{value}",
            expected_current=current,
        )
        current = updated

    transition_dir = tmp_path / ".transitions"
    completed_dir = transition_dir / "completed"
    assert target.read_bytes() == current
    transition_members = sorted(transition_dir.iterdir())
    retired_transition_members = [
        path for path in transition_members if path.name.startswith(".retired.")
    ]
    assert len(retired_transition_members) == 256
    assert [path.name for path in transition_members if not path.name.startswith(".retired.")] == [
        "completed"
    ]
    completion_members = sorted(completed_dir.iterdir())
    live_markers = [path for path in completion_members if path.name.endswith(".json")]
    retired_completion_members = [
        path for path in completion_members if path.name.startswith(".retired.")
    ]
    assert len(live_markers) == 1
    assert len(retired_completion_members) == 127
    assert all(path.is_file() and 0 < path.stat().st_size < 4096 for path in completion_members)
    assert sum(path.stat().st_size for path in completion_members) < 64 * 1024 * 1024


def test_retired_completion_preserves_historical_idempotency_binding(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    for previous, updated, transition_id in (
        (b"zero", b"one", "historical-id"),
        (b"one", b"two", "later-id-2"),
        (b"two", b"three", "later-id-3"),
    ):
        json_module.replace_bytes_atomic(
            target,
            updated,
            transition_id=transition_id,
            expected_current=previous,
        )

    def storage_snapshot() -> tuple[tuple[object, ...], ...]:
        entries: list[tuple[object, ...]] = []
        for path in sorted(tmp_path.rglob("*")):
            metadata = path.lstat()
            entries.append(
                (
                    path.relative_to(tmp_path).as_posix(),
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    path.read_bytes() if path.is_file() else None,
                )
            )
        return tuple(entries)

    # Replaying the exact historical request is an idempotent no-op; it must
    # not roll the public target back from the later committed generation.
    before_exact_replay = storage_snapshot()
    json_module.replace_bytes_atomic(
        target,
        b"one",
        transition_id="historical-id",
        expected_current=b"zero",
    )
    assert target.read_bytes() == b"three"
    assert storage_snapshot() == before_exact_replay

    # Reusing that retired ID for a new endpoint mapping must remain a conflict.
    before_conflict = storage_snapshot()
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"four",
            transition_id="historical-id",
            expected_current=b"three",
        )

    assert exc_info.value.code == "idempotency_conflict"
    assert target.read_bytes() == b"three"
    assert storage_snapshot() == before_conflict


def test_completed_transition_matches_requires_the_current_live_generation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"A")
    target.chmod(0o600)
    for previous, updated, transition_id in (
        (b"A", b"B", "cycle-t1"),
        (b"B", b"C", "cycle-t2"),
        (b"C", b"B", "cycle-t3"),
    ):
        json_module.replace_bytes_atomic(
            target,
            updated,
            transition_id=transition_id,
            expected_current=previous,
        )

    assert not json_module.completed_transition_matches(
        target,
        transition_id="cycle-t1",
        previous_data=b"A",
        data=b"B",
        replace_targets={target.name},
    )
    assert json_module.completed_transition_matches(
        target,
        transition_id="cycle-t3",
        previous_data=b"C",
        data=b"B",
        replace_targets={target.name},
    )


@pytest.mark.parametrize("forged_public", [b"forged", b"zero"])
def test_historical_replay_requires_current_public_completion_provenance(
    tmp_path: Path,
    forged_public: bytes,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    for previous, updated, transition_id in (
        (b"zero", b"one", "provenance-old-id"),
        (b"one", b"two", "provenance-later-id-2"),
        (b"two", b"three", "provenance-later-id-3"),
    ):
        json_module.replace_bytes_atomic(
            target,
            updated,
            transition_id=transition_id,
            expected_current=previous,
        )

    target.write_bytes(forged_public)
    before = target.read_bytes()
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"one",
            transition_id="provenance-old-id",
            expected_current=b"zero",
        )

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == before


def test_historical_replay_holds_current_completion_marker_through_final_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    for previous, updated, transition_id in (
        (b"zero", b"one", "held-provenance-old-id"),
        (b"one", b"two", "held-provenance-later-id-2"),
        (b"two", b"three", "held-provenance-later-id-3"),
    ):
        json_module.replace_bytes_atomic(
            target,
            updated,
            transition_id=transition_id,
            expected_current=previous,
        )

    completed = tmp_path / ".transitions" / "completed"
    live_marker = next(completed.glob("*.json"))
    detached_live = tmp_path / "detached-live-completion"
    retired_raw = next(completed.glob(".retired.*.artifact")).read_bytes()
    original_match = json_module._live_completion_matches_public

    def replace_after_provenance_match(*args: object, **kwargs: object) -> bool:
        matched = original_match(*args, **kwargs)
        if matched:
            live_marker.rename(detached_live)
            live_marker.write_bytes(retired_raw)
            live_marker.chmod(0o600)
        return matched

    monkeypatch.setattr(
        json_module,
        "_live_completion_matches_public",
        replace_after_provenance_match,
    )
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"one",
            transition_id="held-provenance-old-id",
            expected_current=b"zero",
        )

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"three"
    assert detached_live.exists()


def test_historical_replay_holds_retired_request_id_marker_through_final_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    for previous, updated, transition_id in (
        (b"zero", b"one", "held-retired-id"),
        (b"one", b"two", "held-retired-later-id-2"),
        (b"two", b"three", "held-retired-later-id-3"),
    ):
        json_module.replace_bytes_atomic(
            target,
            updated,
            transition_id=transition_id,
            expected_current=previous,
        )

    completed = tmp_path / ".transitions" / "completed"
    retired = list(completed.glob(".retired.*.artifact"))
    request_marker = next(
        path
        for path in retired
        if json.loads(path.read_bytes()).get("transition_id") == "held-retired-id"
    )
    replacement_raw = next(path.read_bytes() for path in retired if path != request_marker)
    detached_request = tmp_path / "detached-request-completion"
    original_lookup = json_module._lookup_completion

    def replace_after_request_lookup(*args: object, **kwargs: object) -> object:
        found = original_lookup(*args, **kwargs)
        if found:
            request_marker.rename(detached_request)
            request_marker.write_bytes(replacement_raw)
            request_marker.chmod(0o600)
        return found

    monkeypatch.setattr(json_module, "_lookup_completion", replace_after_request_lookup)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"one",
            transition_id="held-retired-id",
            expected_current=b"zero",
        )

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"three"
    assert detached_request.exists()


def test_completion_prune_crash_window_allows_two_markers_then_recovers(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"one",
        transition_id="state:one",
        expected_current=b"zero",
    )

    def stop_after_second_marker(stage: str) -> None:
        if stage == "after_completion_publish":
            raise RuntimeError("simulated crash after completion publication")

    with pytest.raises(RuntimeError, match="simulated crash"):
        json_module.replace_bytes_atomic(
            target,
            b"two",
            transition_id="state:two",
            expected_current=b"one",
            fault=stop_after_second_marker,
        )

    completed = tmp_path / ".transitions" / "completed"
    assert len(list(completed.glob("*.json"))) == 2
    assert json_module.active_transition_targets(
        tmp_path,
        replace_targets={target.name},
    ) == {target.name}

    json_module.replace_bytes_atomic(
        target,
        b"two",
        transition_id="state:two",
        expected_current=b"one",
    )

    assert target.read_bytes() == b"two"
    assert len(list(completed.glob("*.json"))) == 1
    assert json_module.active_transition_targets(
        tmp_path,
        replace_targets={target.name},
    ) == set()


def test_crash_after_old_completion_unlink_keeps_current_marker_recoverable(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"one",
        transition_id="state:one",
        expected_current=b"zero",
    )

    def stop_after_prune(stage: str) -> None:
        if stage == "after_completion_prune_unlink":
            raise RuntimeError("simulated crash after old marker unlink")

    with pytest.raises(RuntimeError, match="simulated crash"):
        json_module.replace_bytes_atomic(
            target,
            b"two",
            transition_id="state:two",
            expected_current=b"one",
            fault=stop_after_prune,
        )

    completed = tmp_path / ".transitions" / "completed"
    assert len(list(completed.glob("*.json"))) == 1
    assert json_module.active_transition_targets(
        tmp_path,
        replace_targets={target.name},
    ) == {target.name}

    json_module.replace_bytes_atomic(
        target,
        b"two",
        transition_id="state:two",
        expected_current=b"one",
    )
    assert target.read_bytes() == b"two"
    assert len(list(completed.glob("*.json"))) == 1
    assert json_module.active_transition_targets(
        tmp_path,
        replace_targets={target.name},
    ) == set()


def test_active_cleanup_keeps_exact_current_completion_marker_bound(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)
    detached = tmp_path / "detached-current-completion"
    detached_raw: bytes | None = None

    def move_current_before_active_cleanup(stage: str) -> None:
        nonlocal detached_raw
        if stage == "before_active_cleanup":
            completed = tmp_path / ".transitions" / "completed"
            marker = next(completed.glob("*.json"))
            detached_raw = marker.read_bytes()
            marker.rename(detached)

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"new",
            transition_id="completion-binding",
            expected_current=b"old",
            fault=move_current_before_active_cleanup,
        )

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"new"
    assert detached_raw is not None
    assert detached.read_bytes() == detached_raw
    assert list((tmp_path / ".transitions").glob("owner.*.json"))


def test_completion_prune_preserves_racing_external_hardlink_bytes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"one",
        transition_id="state:one",
        expected_current=b"zero",
    )
    completed = tmp_path / ".transitions" / "completed"
    old_marker = next(completed.glob("*.json"))
    old_bytes = old_marker.read_bytes()
    detached = tmp_path / "detached-completion.json"

    def link_after_precheck(stage: str) -> None:
        if stage == "before_completion_prune_unlink":
            os.link(old_marker, detached)

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"two",
            transition_id="state:two",
            expected_current=b"one",
            fault=link_after_precheck,
        )

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"two"
    assert detached.read_bytes() == old_bytes
    assert len(list(completed.glob("*.json"))) == 2


def test_completion_retirement_fails_if_observed_member_disappears_before_relocation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"one",
        transition_id="retire-disappearing-old",
        expected_current=b"zero",
    )
    completed = tmp_path / ".transitions" / "completed"
    old_marker = next(completed.glob("*.json"))
    old_raw = old_marker.read_bytes()
    detached = tmp_path / "detached-disappearing-completion"

    def move_before_retirement(stage: str) -> None:
        if stage == "before_completion_prune_unlink":
            old_marker.rename(detached)

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"two",
            transition_id="retire-disappearing-current",
            expected_current=b"one",
            fault=move_before_retirement,
        )

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"two"
    assert detached.read_bytes() == old_raw
    assert len(list(completed.glob("*.json"))) == 1


def test_completion_prune_never_unlinks_current_marker_after_old_name_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"one",
        transition_id="quarantine-old",
        expected_current=b"zero",
    )
    completed = tmp_path / ".transitions" / "completed"
    old_marker = next(completed.glob("*.json"))
    old_name = old_marker.name
    detached_old = tmp_path / "detached-old-completion"
    original_rename = json_module._rename_no_replace
    current_raw: bytes | None = None

    def swap_old_and_current_before_relocation(
        parent_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal current_raw
        if source_name == old_name:
            current_marker = next(path for path in completed.glob("*.json") if path.name != old_name)
            current_raw = current_marker.read_bytes()
            old_marker.rename(detached_old)
            current_marker.rename(completed / old_name)
        original_rename(parent_fd, source_name, target_name)

    monkeypatch.setattr(json_module, "_rename_no_replace", swap_old_and_current_before_relocation)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"two",
            transition_id="quarantine-current",
            expected_current=b"one",
        )

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"two"
    assert current_raw is not None
    assert detached_old.exists()
    assert any(
        path.is_file() and path.read_bytes() == current_raw
        for path in completed.iterdir()
    )


def test_completion_prune_fails_closed_when_unique_quarantine_is_occupied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"one",
        transition_id="quarantine-occupied-old",
        expected_current=b"zero",
    )
    completed = tmp_path / ".transitions" / "completed"
    old_marker = next(completed.glob("*.json"))
    old_name = old_marker.name
    old_raw = old_marker.read_bytes()
    original_rename = json_module._rename_no_replace
    occupied_raw = b"occupied quarantine"

    def occupy_quarantine_before_relocation(
        parent_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        if source_name == old_name:
            descriptor = os.open(
                target_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                os.write(descriptor, occupied_raw)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        original_rename(parent_fd, source_name, target_name)

    monkeypatch.setattr(json_module, "_rename_no_replace", occupy_quarantine_before_relocation)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"two",
            transition_id="quarantine-occupied-current",
            expected_current=b"one",
        )

    assert exc_info.value.code == "output_conflict"
    assert old_marker.read_bytes() == old_raw
    assert any(
        path.is_file() and path.read_bytes() == occupied_raw
        for path in completed.iterdir()
    )


def test_completion_retirement_never_unlinks_a_name_swapped_after_relocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"one",
        transition_id="retire-old",
        expected_current=b"zero",
    )
    completed = tmp_path / ".transitions" / "completed"
    old_marker = next(completed.glob("*.json"))
    old_raw = old_marker.read_bytes()
    original_unlink = json_module.os.unlink
    detached_retired = tmp_path / "detached-retired-completion"
    current_raw: bytes | None = None
    unlink_reached = False
    completed_identity = (completed.stat().st_dev, completed.stat().st_ino)

    def swap_at_before_quarantine_unlink(
        path: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal current_raw, unlink_reached
        dir_fd = kwargs.get("dir_fd")
        directory_identity = (
            (os.fstat(dir_fd).st_dev, os.fstat(dir_fd).st_ino)
            if isinstance(dir_fd, int)
            else None
        )
        if (
            isinstance(path, str)
            and path.endswith(".quarantine")
            and directory_identity == completed_identity
        ):
            unlink_reached = True
            quarantine = completed / path
            current_marker = next(
                candidate
                for candidate in completed.glob("*.json")
                if candidate.name != old_marker.name
            )
            current_raw = current_marker.read_bytes()
            quarantine.rename(detached_retired)
            current_marker.rename(quarantine)
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(json_module.os, "unlink", swap_at_before_quarantine_unlink)
    json_module.replace_bytes_atomic(
        target,
        b"two",
        transition_id="retire-current",
        expected_current=b"one",
    )

    assert not unlink_reached
    assert target.read_bytes() == b"two"
    assert current_raw is None
    assert any(path.read_bytes() == old_raw for path in completed.iterdir() if path.is_file())
    assert len(list(completed.glob("*.json"))) == 1


def test_two_completion_markers_without_active_owner_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"one",
        transition_id="state:one",
        expected_current=b"zero",
    )
    completed = tmp_path / ".transitions" / "completed"
    first_marker = next(completed.glob("*.json"))
    first_name = first_marker.name
    first_raw = first_marker.read_bytes()
    json_module.replace_bytes_atomic(
        target,
        b"two",
        transition_id="state:two",
        expected_current=b"one",
    )
    restored = completed / first_name
    restored.write_bytes(first_raw)
    restored.chmod(0o600)

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.active_transition_targets(
            tmp_path,
            replace_targets={target.name},
        )

    assert exc_info.value.code == "unsafe_storage"


def test_completed_markers_require_the_callers_full_target_set(tmp_path: Path) -> None:
    first = tmp_path / "record.json"
    second = tmp_path / "init.started"
    first.write_bytes(b"record-zero")
    second.write_bytes(b"started-zero")
    first.chmod(0o600)
    second.chmod(0o600)
    allowed = {first.name, second.name}

    json_module.replace_bytes_atomic(
        first,
        b"record-one",
        transition_id="record:one",
        expected_current=b"record-zero",
        allowed_transition_targets=allowed,
    )
    json_module.replace_bytes_atomic(
        second,
        b"started-one",
        transition_id="started:one",
        expected_current=b"started-zero",
        allowed_transition_targets=allowed,
    )

    assert json_module.active_transition_targets(
        tmp_path,
        replace_targets=allowed,
    ) == set()
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.active_transition_targets(
            tmp_path,
            replace_targets={second.name},
        )

    assert exc_info.value.code == "unsafe_storage"


@pytest.mark.parametrize(
    "stage",
    [
        "after_owner_write_before_fsync",
        "after_owner_file_fsync",
        "after_owner_rename_before_parent_fsync",
        "after_owner_staging_fsync",
        "after_payload_write_before_fsync",
        "after_payload_file_fsync",
        "after_payload_rename_before_parent_fsync",
        "after_pending_rename",
        "after_exchange",
        "after_transition_parent_fsync",
        "after_exchange_fsync",
        "after_completion_write_before_fsync",
        "after_completion_file_fsync",
        "after_completion_rename_before_parent_fsync",
        "after_completion_publish",
        "after_completion_writing_cleanup",
        "before_active_cleanup",
        "after_retired_leaf_unlink",
        "after_active_writing_cleanup",
        "after_active_owner_unlink",
    ],
)
def test_replace_recovers_every_durable_transition_boundary_after_hard_crash(
    tmp_path: Path,
    stage: str,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_replace_owner_bootstrap,
        args=(str(target), stage),
    )
    process.start()
    process.join(timeout=10)

    assert process.exitcode == 29
    json_module.replace_bytes_atomic(
        target,
        b"new",
        transition_id="storage-crash",
        expected_current=b"old",
    )
    assert target.read_bytes() == b"new"
    assert json_module.active_transition_targets(
        tmp_path,
        replace_targets={target.name},
    ) == set()


@pytest.mark.parametrize("kind", ["owner", "payload", "completion"])
def test_exact_replay_recovers_a_process_killed_mid_transition_write(
    tmp_path: Path,
    kind: str,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_during_transition_write,
        args=(str(target), kind),
    )
    process.start()
    process.join(timeout=10)

    assert process.exitcode == 37
    json_module.replace_bytes_atomic(
        target,
        b"new",
        transition_id="partial-write-crash",
        expected_current=b"old",
    )
    assert target.read_bytes() == b"new"
    transition_dir = tmp_path / ".transitions"
    assert all(
        path.name == "completed" or path.name.startswith(".retired.")
        for path in transition_dir.iterdir()
    )
    assert json_module.active_transition_targets(
        tmp_path,
        replace_targets={target.name},
    ) == set()
    assert not list((transition_dir / "completed").glob("*.writing"))


def test_hardlink_created_after_precheck_keeps_retired_public_bytes_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    external = tmp_path / "external-old-state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)
    original_exchange = json_module._exchange_names_between

    def link_then_exchange(
        first_parent_fd: int,
        first_name: str,
        second_parent_fd: int,
        second_name: str,
    ) -> None:
        os.link(target, external)
        original_exchange(first_parent_fd, first_name, second_parent_fd, second_name)

    monkeypatch.setattr(json_module, "_exchange_names_between", link_then_exchange)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"new",
            transition_id="hardlink-after-precheck",
            expected_current=b"old",
        )

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"new"
    assert external.read_bytes() == b"old"


def test_transition_id_cannot_be_rebound_to_new_endpoints(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"new",
        transition_id="same-id",
        expected_current=b"old",
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"newer",
            transition_id="same-id",
            expected_current=b"new",
        )

    assert exc_info.value.code == "idempotency_conflict"
    assert target.read_bytes() == b"new"


def test_different_transition_forward_completes_prepared_owner_before_rejecting(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)

    def stop_after_payload(stage: str) -> None:
        if stage == "after_pending_rename":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        json_module.replace_bytes_atomic(
            target,
            b"middle",
            transition_id="first",
            expected_current=b"old",
            fault=stop_after_payload,
        )

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"final",
            transition_id="second",
            expected_current=b"old",
        )

    assert exc_info.value.code == "storage_recovery_required"
    assert target.read_bytes() == b"middle"
    json_module.replace_bytes_atomic(
        target,
        b"final",
        transition_id="second",
        expected_current=b"middle",
    )
    assert target.read_bytes() == b"final"


@pytest.mark.parametrize(
    ("mode", "current", "expected", "error_code"),
    [
        (0o644, b"old", b"old", "unsafe_storage"),
        (0o600, b"changed", b"old", "storage_path_changed"),
    ],
)
def test_replace_rejects_unsafe_or_stale_public_target_without_bootstrap_mutation(
    tmp_path: Path,
    mode: int,
    current: bytes,
    expected: bytes,
    error_code: str,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(current)
    target.chmod(mode)

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"new",
            transition_id="must-not-bootstrap",
            expected_current=expected,
        )

    assert exc_info.value.code == error_code
    assert target.read_bytes() == current
    assert sorted(path.name for path in tmp_path.iterdir()) == [target.name]


def test_active_transition_for_foreign_target_is_closed_world_blocker(tmp_path: Path) -> None:
    first = tmp_path / "state.json"
    second = tmp_path / "batch-report.json"
    first.write_bytes(b"old")
    second.write_bytes(b"report-old")
    first.chmod(0o600)
    second.chmod(0o600)

    def stop_after_payload(stage: str) -> None:
        if stage == "after_pending_rename":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        json_module.replace_bytes_atomic(
            first,
            b"new",
            transition_id="first",
            expected_current=b"old",
            fault=stop_after_payload,
        )

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            second,
            b"report-new",
            transition_id="second",
            expected_current=b"report-old",
        )

    assert exc_info.value.code == "unsafe_storage"
    assert first.read_bytes() == b"old"
    assert second.read_bytes() == b"report-old"


def test_tampered_completion_marker_blocks_exact_replay(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"new",
        transition_id="tamper-marker",
        expected_current=b"old",
    )
    marker = (
        tmp_path
        / ".transitions"
        / "completed"
        / f"{json_module.sha256_bytes(b'tamper-marker')}.json"
    )
    marker.write_bytes(b"{}")

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"new",
            transition_id="tamper-marker",
            expected_current=b"old",
        )

    assert exc_info.value.code == "unsafe_storage"
    assert target.read_bytes() == b"new"


def test_exact_completion_replay_rejects_public_name_replacement_after_marker_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    detached = tmp_path / "completed.detached"
    target.write_bytes(b"old")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"new",
        transition_id="replay-race",
        expected_current=b"old",
    )
    original_lookup = json_module._lookup_completion

    def replace_after_lookup(completed_fd: int, transition_id: str, expected: bytes) -> bool:
        found = original_lookup(completed_fd, transition_id, expected)
        if found:
            target.rename(detached)
            target.write_bytes(b"external")
            target.chmod(0o600)
        return found

    monkeypatch.setattr(json_module, "_lookup_completion", replace_after_lookup)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"new",
            transition_id="replay-race",
            expected_current=b"old",
        )

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"external"
    assert detached.read_bytes() == b"new"


def test_completed_transition_lookup_rejects_transition_namespace_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"new",
        transition_id="detached-transition-namespace",
        expected_current=b"old",
    )
    transition_dir = tmp_path / ".transitions"
    detached = tmp_path / "detached-transitions"
    replacement = tmp_path / "replacement-transitions"
    replacement.mkdir(mode=0o700)
    (replacement / "completed").mkdir(mode=0o700)
    original_lookup = json_module._lookup_completion

    def replace_namespace_after_lookup(
        completed_fd: int,
        transition_id: str,
        expected: bytes,
    ) -> bool:
        found = original_lookup(completed_fd, transition_id, expected)
        transition_dir.rename(detached)
        replacement.rename(transition_dir)
        return found

    monkeypatch.setattr(json_module, "_lookup_completion", replace_namespace_after_lookup)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.completed_transition_matches(
            target,
            transition_id="detached-transition-namespace",
            previous_data=b"old",
            data=b"new",
            replace_targets={target.name},
        )

    assert exc_info.value.code == "storage_path_changed"
    assert list((transition_dir / "completed").iterdir()) == []
    assert len(list((detached / "completed").iterdir())) == 1


def test_completed_transition_lookup_rejects_completed_child_replacement_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"new",
        transition_id="detached-completed-child",
        expected_current=b"old",
    )
    completed = tmp_path / ".transitions" / "completed"
    detached = tmp_path / ".transitions" / "detached-completed"
    replacement = tmp_path / "replacement-completed"
    replacement.mkdir(mode=0o700)
    original_exact = json_module._held_regular_is_exact
    target_checks = 0

    def replace_completed_during_final_target_check(
        descriptor: int,
        expected: bytes,
        *,
        name: str,
    ) -> bool:
        nonlocal target_checks
        if name == target.name:
            target_checks += 1
            if target_checks == 2:
                completed.rename(detached)
                replacement.rename(completed)
        return original_exact(descriptor, expected, name=name)

    monkeypatch.setattr(json_module, "_held_regular_is_exact", replace_completed_during_final_target_check)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.completed_transition_matches(
            target,
            transition_id="detached-completed-child",
            previous_data=b"old",
            data=b"new",
            replace_targets={target.name},
        )

    assert exc_info.value.code == "storage_path_changed"
    assert list(completed.iterdir()) == []
    assert len(list(detached.iterdir())) == 1


@pytest.mark.parametrize(
    "replacement_stage",
    [
        "before_active_cleanup",
        "after_retired_leaf_unlink",
        "after_active_writing_cleanup",
        "after_active_owner_unlink",
    ],
)
def test_replace_fails_closed_if_transition_namespace_is_replaced_during_cleanup(
    tmp_path: Path,
    replacement_stage: str,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)
    transition_dir = tmp_path / ".transitions"
    detached = tmp_path / "detached-transitions"
    replacement = tmp_path / "replacement-transitions"

    def replace_namespace(stage: str) -> None:
        if stage != replacement_stage:
            return
        replacement.mkdir(mode=0o700)
        (replacement / "completed").mkdir(mode=0o700)
        transition_dir.rename(detached)
        replacement.rename(transition_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.replace_bytes_atomic(
            target,
            b"new",
            transition_id=f"namespace-replaced-{replacement_stage}",
            expected_current=b"old",
            fault=replace_namespace,
        )

    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"new"
    assert list((transition_dir / "completed").iterdir()) == []
    assert len(list((detached / "completed").iterdir())) == 1


@pytest.mark.parametrize("reader", ["active-targets", "pending", "owner"])
def test_transition_readers_reject_namespace_replacement_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: str,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)

    def stop_with_active_transition(stage: str) -> None:
        if stage == "after_pending_rename":
            raise RuntimeError("active transition staged")

    with pytest.raises(RuntimeError, match="active transition staged"):
        json_module.replace_bytes_atomic(
            target,
            b"new",
            transition_id="reader-namespace-race",
            expected_current=b"old",
            fault=stop_with_active_transition,
        )

    transition_dir = tmp_path / ".transitions"
    active_namespace = tmp_path / "active-transitions"
    transition_dir.rename(active_namespace)
    transition_dir.mkdir(mode=0o700)
    (transition_dir / "completed").mkdir(mode=0o700)
    detached_empty = tmp_path / "detached-empty-transitions"
    original_validate = json_module._validate_transition_directory_fd
    swapped = False

    def replace_after_validation(*args, **kwargs):
        nonlocal swapped
        result = original_validate(*args, **kwargs)
        if not swapped:
            swapped = True
            transition_dir.rename(detached_empty)
            active_namespace.rename(transition_dir)
        return result

    monkeypatch.setattr(
        json_module,
        "_validate_transition_directory_fd",
        replace_after_validation,
    )
    with pytest.raises(BatchRuntimeError) as exc_info:
        if reader == "active-targets":
            json_module.active_transition_targets(
                tmp_path,
                replace_targets={target.name},
            )
        elif reader == "pending":
            json_module.read_pending_transitions(
                target,
                max_bytes=1024,
                replace_targets={target.name},
            )
        else:
            json_module.read_active_transition_owner(
                target,
                replace_targets={target.name},
            )

    assert exc_info.value.code == "storage_path_changed"
    assert len(list(transition_dir.glob("owner.*.json"))) == 1


def test_cleanup_crash_cannot_orphan_an_incomplete_writing_attempt(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"seed")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"old",
        transition_id="seed-transition-storage",
        expected_current=b"seed",
    )
    target_hash = json_module.sha256_bytes(target.name.encode("utf-8"))
    partial = (
        tmp_path
        / ".transitions"
        / f".owner.{target_hash}.json.{'a' * 32}.writing"
    )
    expected_owner = json_module._transition_owner_bytes(
        os.stat(tmp_path),
        os.stat(tmp_path / ".transitions"),
        target.name,
        "storage-crash",
        json_module.sha256_bytes(b"old"),
        len(b"old"),
        json_module.sha256_bytes(b"new"),
        len(b"new"),
        0o600,
    )
    partial.write_bytes(expected_owner[: len(expected_owner) // 2])
    partial.chmod(0o600)

    process = multiprocessing.get_context("spawn").Process(
        target=_crash_replace_owner_bootstrap,
        args=(str(target), "after_active_owner_unlink"),
    )
    process.start()
    process.join(timeout=10)

    assert process.exitcode == 29
    json_module.replace_bytes_atomic(
        target,
        b"new",
        transition_id="storage-crash",
        expected_current=b"old",
    )
    assert target.read_bytes() == b"new"
    assert json_module.active_transition_targets(
        tmp_path,
        replace_targets={target.name},
    ) == set()


@pytest.mark.parametrize(
    ("kind", "error_code"),
    [
        ("unknown", "unsafe_storage"),
        ("symlink", "unsafe_storage"),
        ("hardlink", "unsafe_storage"),
        ("oversized", "resource_limit"),
        ("malformed", "unsafe_storage"),
    ],
)
def test_completed_transition_namespace_is_closed_world(
    tmp_path: Path,
    kind: str,
    error_code: str,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"new",
        transition_id="closed-world-seed",
        expected_current=b"old",
    )
    completed = tmp_path / ".transitions" / "completed"
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    outside.chmod(0o600)
    name = "legacy.marker" if kind == "unknown" else f"{'0' * 64}.json"
    injected = completed / name
    if kind == "symlink":
        injected.symlink_to(outside)
    elif kind == "hardlink":
        injected.hardlink_to(outside)
    elif kind == "oversized":
        with injected.open("wb") as stream:
            stream.truncate(4097)
        injected.chmod(0o600)
    elif kind == "malformed":
        injected.write_bytes(b"{}")
        injected.chmod(0o600)
    else:
        injected.write_bytes(b"hidden")
        injected.chmod(0o600)

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.active_transition_targets(
            tmp_path,
            replace_targets={target.name},
        )

    assert exc_info.value.code == error_code
    assert target.read_bytes() == b"new"
    assert outside.read_bytes() == b"outside"


def test_completed_transition_size_limit_is_checked_before_any_marker_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"new",
        transition_id="closed-world-size-seed",
        expected_current=b"old",
    )
    oversized = tmp_path / ".transitions" / "completed" / f"{'0' * 64}.json"
    with oversized.open("wb") as stream:
        stream.truncate(4097)
    oversized.chmod(0o600)

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("oversized completion marker reached os.read")

    monkeypatch.setattr(json_module.os, "read", forbidden_read)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.active_transition_targets(
            tmp_path,
            replace_targets={target.name},
        )

    assert exc_info.value.code == "resource_limit"


def test_completed_transition_member_cap_is_enforced(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"zero")
    target.chmod(0o600)
    json_module.replace_bytes_atomic(
        target,
        b"one",
        transition_id="completion-cap-one",
        expected_current=b"zero",
    )
    json_module.replace_bytes_atomic(
        target,
        b"two",
        transition_id="completion-cap-two",
        expected_current=b"one",
    )
    monkeypatch.setattr(json_module, "_MAX_COMPLETION_ENTRIES", 0)

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.active_transition_targets(
            tmp_path,
            replace_targets={target.name},
        )

    assert exc_info.value.code == "resource_limit"


def test_directory_publish_rejects_swapped_staging_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    target = tmp_path / "published"
    detached = tmp_path / "expected.detached"
    staging.mkdir()
    (staging / "member.txt").write_text("expected", encoding="utf-8")
    original_rename = json_module._rename_no_replace
    injected = False

    def swap_then_rename(parent_fd: int, source_name: str, target_name: str) -> None:
        nonlocal injected
        if injected:
            original_rename(parent_fd, source_name, target_name)
            return
        injected = True
        os.rename(source_name, detached.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.mkdir(source_name, dir_fd=parent_fd)
        descriptor = os.open(
            f"{source_name}/member.txt",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(descriptor, b"attacker")
        finally:
            os.close(descriptor)
        original_rename(parent_fd, source_name, target_name)

    monkeypatch.setattr(json_module, "_rename_no_replace", swap_then_rename)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.publish_directory_no_replace(staging, target)

    assert exc_info.value.code == "storage_path_changed"
    assert (target / "member.txt").read_text(encoding="utf-8") == "attacker"
    assert (detached / "member.txt").read_text(encoding="utf-8") == "expected"
    assert not staging.exists()


def test_directory_publish_preserves_post_fsync_external_replacement(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    target = tmp_path / "published"
    detached = tmp_path / "published.detached"
    staging.mkdir()
    (staging / "member.txt").write_text("expected", encoding="utf-8")
    injected = False

    def replace_after_rename(stage: str) -> None:
        nonlocal injected
        if stage != "before_parent_fsync" or injected:
            return
        injected = True
        target.rename(detached)
        target.mkdir()
        (target / "member.txt").write_text("external replacement", encoding="utf-8")

    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.publish_directory_no_replace(staging, target, fault=replace_after_rename)

    assert injected
    assert exc_info.value.code == "storage_path_changed"
    assert (target / "member.txt").read_text(encoding="utf-8") == "external replacement"
    assert (detached / "member.txt").read_text(encoding="utf-8") == "expected"
    assert not staging.exists()


def test_directory_publish_rejects_in_place_member_drift_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    target = tmp_path / "published"
    staging.mkdir()
    member = staging / "member.txt"
    member.write_text("expected", encoding="utf-8")
    original_rename = json_module._rename_no_replace
    injected = False

    def mutate_then_rename(parent_fd: int, source_name: str, target_name: str) -> None:
        nonlocal injected
        if not injected and source_name == staging.name:
            injected = True
            member.write_text("tampered", encoding="utf-8")
        original_rename(parent_fd, source_name, target_name)

    monkeypatch.setattr(json_module, "_rename_no_replace", mutate_then_rename)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.publish_directory_no_replace(staging, target)

    assert injected
    assert exc_info.value.code == "storage_path_changed"
    assert (target / "member.txt").read_text(encoding="utf-8") == "tampered"
    assert not staging.exists()


def test_directory_publish_rejects_root_mode_drift_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    target = tmp_path / "published"
    staging.mkdir(mode=0o700)
    (staging / "member.txt").write_text("expected", encoding="utf-8")
    original_rename = json_module._rename_no_replace

    def chmod_then_rename(parent_fd: int, source_name: str, target_name: str) -> None:
        os.chmod(staging, 0o777)
        original_rename(parent_fd, source_name, target_name)

    monkeypatch.setattr(json_module, "_rename_no_replace", chmod_then_rename)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.publish_directory_no_replace(staging, target)

    assert exc_info.value.code == "storage_path_changed"
    assert stat.S_IMODE(target.stat().st_mode) == 0o777


def test_promote_rechecks_public_leaf_after_parent_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging.json"
    target = tmp_path / "artifact.json"
    detached = tmp_path / "published.detached"
    expected = b"expected"
    staging.write_bytes(expected)
    original_fsync = json_module._fsync
    injected = False

    def replace_after_fsync(descriptor: int, *, label: str) -> None:
        nonlocal injected
        original_fsync(descriptor, label=label)
        if injected or not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            return
        injected = True
        target.rename(detached)
        target.write_bytes(b"external replacement")

    monkeypatch.setattr(json_module, "_fsync", replace_after_fsync)
    with pytest.raises(BatchRuntimeError) as exc_info:
        json_module.promote_bytes_no_replace(staging, target, expected)

    assert injected
    assert exc_info.value.code == "storage_path_changed"
    assert target.read_bytes() == b"external replacement"
    assert detached.read_bytes() == expected


def test_macos_root_owned_tmp_alias_is_allowed_but_user_symlink_is_not(tmp_path: Path) -> None:
    if not Path("/tmp").is_symlink():
        pytest.skip("macOS /tmp alias is not present on this platform")
    with tempfile.TemporaryDirectory(prefix="paper-reader-batch-", dir="/tmp") as temp_dir:
        alias_parent = Path(temp_dir)
        alias_target = alias_parent / "artifact.json"
        publish_bytes_no_replace(alias_target, b"{}")
        assert alias_target.read_bytes() == b"{}"

        outside = tmp_path / "outside"
        outside.mkdir()
        user_link = alias_parent / "user-link"
        user_link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(BatchRuntimeError, match="symlink|unsafe"):
            publish_bytes_no_replace(user_link / "escape.json", b"{}")
