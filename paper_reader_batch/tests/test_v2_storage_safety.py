import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import locked_file, publish_bytes_no_replace, read_bytes
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest


REQUEST_1 = "11111111-1111-4111-8111-111111111111"


def _crash_after_rename(target: str) -> None:
    def crash(stage: str) -> None:
        if stage == "after_rename":
            os._exit(17)

    publish_bytes_no_replace(Path(target), b"exact bytes", fault=crash)


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
