from __future__ import annotations

import hashlib
import json
import os
import fcntl
import multiprocessing
import queue
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paper_reader.contracts import PaperReaderCommandResult
from paper_reader.local_lifecycle import initialize_local_run
from paper_reader.public_cli import app


FIXTURE_PDF = Path(__file__).parent / "fixtures" / "minimal.pdf"


def _process_initialize_local(source: str, start, messages) -> None:
    messages.put(("ready", None))
    start.wait(timeout=10)
    try:
        initialized = initialize_local_run(Path(source))
    except Exception as exc:
        messages.put(("error", f"{type(exc).__name__}: {exc}"))
    else:
        messages.put(("done", str(initialized.run_dir)))


def _invoke(arguments: list[str]):
    return CliRunner().invoke(app, arguments)


def _result_payload(result) -> dict:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    return payload


def _tree_snapshot(root: Path) -> dict[str, tuple[str, int]]:
    snapshot: dict[str, tuple[str, int]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        snapshot[path.relative_to(root).as_posix()] = (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
    return snapshot


def test_locked_source_binding_detaches_descriptor_before_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.local_lifecycle as module

    descriptor = os.open(tmp_path, os.O_RDONLY)
    binding = module._LockedSourceBinding.__new__(module._LockedSourceBinding)
    binding.path_descriptor = descriptor
    original_close = os.close

    def close_then_raise(target: int) -> None:
        original_close(target)
        raise OSError("injected close failure")

    monkeypatch.setattr(module.os, "close", close_then_raise)

    with pytest.raises(OSError, match="injected close failure"):
        binding.close()

    monkeypatch.setattr(module.os, "close", original_close)
    replacement = os.open(tmp_path, os.O_RDONLY)
    if replacement != descriptor:
        os.dup2(replacement, descriptor)
        original_close(replacement)
        replacement = descriptor
    try:
        binding.close()
        os.fstat(replacement)
    finally:
        original_close(replacement)


def test_init_local_reserves_first_free_version_without_touching_history(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    historical = tmp_path / "paper_analysis"
    historical.mkdir()
    (historical / "run.json").write_text('{"status":"prepared"}\n', encoding="utf-8")
    (historical / "opaque.bin").write_bytes(b"historical-v1-bytes")
    occupied_note = tmp_path / "paper_note_v2.md"
    occupied_note.write_bytes(b"existing-note-v2")
    before_tree = _tree_snapshot(historical)
    before_note = (hashlib.sha256(occupied_note.read_bytes()).hexdigest(), occupied_note.stat().st_mtime_ns)

    result = _invoke(["run", "init-local", str(source)])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["code"] == "initialized"
    assert payload["data"]["run_dir"] == str(tmp_path / "paper_analysis_v3")
    assert payload["data"]["target_path"] == str(tmp_path / "paper_note_v3.md")
    assert _tree_snapshot(historical) == before_tree
    assert (hashlib.sha256(occupied_note.read_bytes()).hexdigest(), occupied_note.stat().st_mtime_ns) == before_note

    run_dir = tmp_path / "paper_analysis_v3"
    assert sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()) == [
        "run.json",
        "source/source.json",
    ]
    run_payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    source_payload = json.loads((run_dir / "source" / "source.json").read_text(encoding="utf-8"))
    assert run_payload["schema_version"] == "paper_reader.run.v2"
    assert run_payload["status"] == "initialized"
    assert run_payload["source"] == source_payload
    assert run_payload["target"] == {
        "target_type": "local",
        "resolved_path": str(tmp_path / "paper_note_v3.md"),
        "parent_device": tmp_path.stat().st_dev,
        "parent_inode": tmp_path.stat().st_ino,
    }
    assert run_payload["source"]["resolved_path"] == str(source.resolve())
    assert run_payload["source"]["size_bytes"] == source.stat().st_size
    assert run_payload["source"]["device"] == source.stat().st_dev
    assert run_payload["source"]["inode"] == source.stat().st_ino
    assert run_payload["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert not (tmp_path / "paper_note_v3.md").exists()


def test_init_local_rejects_an_unreadable_pdf_before_any_output(tmp_path: Path) -> None:
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"this is not a PDF")
    before = sorted(path.name for path in tmp_path.iterdir())

    result = _invoke(["run", "init-local", str(source)])

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["code"] == "invalid_local_pdf"
    assert payload["data"]["source_pdf"] == str(source)
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_init_local_rejects_oversized_pdf_before_fingerprinting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "oversized.pdf"
    with source.open("wb") as handle:
        handle.seek((256 * 1024 * 1024) + 1)
        handle.write(b"\0")

    def forbidden_fingerprint(_path: Path):
        pytest.fail("oversized source reached fingerprinting")

    monkeypatch.setattr("paper_reader.storage.fingerprint_resolved_source", forbidden_fingerprint)

    result = _invoke(["run", "init-local", str(source)])

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["code"] == "source_too_large"
    assert payload["data"]["max_size_bytes"] == 256 * 1024 * 1024
    assert not (tmp_path / "oversized_analysis").exists()


def test_init_local_fault_during_atomic_reservation_leaves_no_partial_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)

    def injected_failure(_staging: Path, _destination: Path) -> Path:
        raise OSError("injected reservation failure")

    monkeypatch.setattr("paper_reader.local_lifecycle.atomic_publish_tree", injected_failure)

    result = _invoke(["run", "init-local", str(source)])

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["code"] == "initialization_failed"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["paper.pdf"]


def test_init_local_binds_relative_symlink_input_to_one_absolute_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = tmp_path / "sources"
    inputs = tmp_path / "inputs"
    sources.mkdir()
    inputs.mkdir()
    source = sources / "real.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    alias = inputs / "alias.pdf"
    alias.symlink_to(source)
    monkeypatch.chdir(inputs)

    result = _invoke(["run", "init-local", "alias.pdf"])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["data"]["run_dir"] == str(sources / "real_analysis")
    manifest = json.loads((sources / "real_analysis" / "run.json").read_text(encoding="utf-8"))
    assert manifest["source"]["requested_path"] == "alias.pdf"
    assert manifest["source"]["resolved_path"] == str(source.resolve())
    assert manifest["target"]["resolved_path"] == str(sources / "real_note.md")

    monkeypatch.chdir(tmp_path)
    status = _invoke(["run", "status", str(sources / "real_analysis")])
    assert status.exit_code == 0
    assert _result_payload(status)["data"]["run_id"] == manifest["run_id"]


def test_init_local_treats_a_hardlinked_target_as_occupied(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    source_before = hashlib.sha256(source.read_bytes()).hexdigest()
    os.link(source, tmp_path / "paper_note.md")

    result = _invoke(["run", "init-local", str(source)])

    assert result.exit_code == 0
    payload = _result_payload(result)
    assert payload["data"]["run_dir"] == str(tmp_path / "paper_analysis_v2")
    assert payload["data"]["target_path"] == str(tmp_path / "paper_note_v2.md")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_before
    assert os.path.samefile(source, tmp_path / "paper_note.md")


@pytest.mark.parametrize("occupied_path", ["analysis", "note"])
def test_init_local_treats_broken_symlink_pair_members_as_occupied(
    occupied_path: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    occupied = (
        tmp_path / "paper_analysis"
        if occupied_path == "analysis"
        else tmp_path / "paper_note.md"
    )
    occupied.symlink_to(tmp_path / f"missing-{occupied_path}")

    result = _invoke(["run", "init-local", str(source)])

    assert result.exit_code == 0
    payload = _result_payload(result)
    assert payload["data"]["run_dir"] == str(tmp_path / "paper_analysis_v2")
    assert payload["data"]["target_path"] == str(tmp_path / "paper_note_v2.md")
    assert occupied.is_symlink()


def test_concurrent_init_local_calls_reserve_unique_versions(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)

    with ThreadPoolExecutor(max_workers=4) as executor:
        initialized = list(executor.map(initialize_local_run, [source] * 4))

    assert {item.run_dir.name for item in initialized} == {
        "paper_analysis",
        "paper_analysis_v2",
        "paper_analysis_v3",
        "paper_analysis_v4",
    }
    assert len({item.run.run_id for item in initialized}) == 4
    for item in initialized:
        assert (item.run_dir / "run.json").is_file()
        assert (item.run_dir / "source" / "source.json").is_file()


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_init_local_honors_advisory_lock_on_the_source_inode(
    alias_kind: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    alias = tmp_path / "alias.pdf"
    if alias_kind == "symlink":
        alias.symlink_to(source)
    else:
        os.link(source, alias)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    messages = context.Queue()

    with source.open("rb") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        process = context.Process(
            target=_process_initialize_local,
            args=(str(alias), start, messages),
        )
        process.start()
        assert messages.get(timeout=10) == ("ready", None)
        start.set()
        with pytest.raises(queue.Empty):
            messages.get(timeout=1.5)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    status, detail = messages.get(timeout=10)
    process.join(timeout=10)
    assert process.exitcode == 0
    assert status == "done", detail


def test_init_local_rejects_source_path_exchange_after_open_before_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.local_lifecycle as local_lifecycle

    source = tmp_path / "paper.pdf"
    detached_source = tmp_path / "detached-paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    original_flock = local_lifecycle.fcntl.flock
    exchanged = False

    def exchange_path_after_lock(descriptor: int, operation: int) -> None:
        nonlocal exchanged
        original_flock(descriptor, operation)
        if operation == fcntl.LOCK_EX and not exchanged:
            source.rename(detached_source)
            shutil.copyfile(FIXTURE_PDF, source)
            exchanged = True

    monkeypatch.setattr(local_lifecycle.fcntl, "flock", exchange_path_after_lock)

    result = _invoke(["run", "init-local", str(source)])

    assert exchanged is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "source_changed"
    assert not (tmp_path / "paper_analysis").exists()
    assert not (tmp_path / "paper_note.md").exists()


def test_init_local_rejects_same_inode_content_drift_after_fingerprinting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.local_lifecycle as local_lifecycle

    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    original_flock = local_lifecycle.fcntl.flock
    drifted = False

    def drift_content_after_lock(descriptor: int, operation: int) -> None:
        nonlocal drifted
        original_flock(descriptor, operation)
        if operation == fcntl.LOCK_EX and not drifted:
            with source.open("ab") as handle:
                handle.write(b"\n% drift after fingerprint")
            drifted = True

    monkeypatch.setattr(local_lifecycle.fcntl, "flock", drift_content_after_lock)

    result = _invoke(["run", "init-local", str(source)])

    assert drifted is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "source_changed"
    assert not (tmp_path / "paper_analysis").exists()
    assert not (tmp_path / "paper_note.md").exists()


def test_init_local_rejects_source_parent_replacement_after_locked_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.local_lifecycle as local_lifecycle

    source_parent = tmp_path / "source-parent"
    detached_parent = tmp_path / "detached-source-parent"
    source_parent.mkdir()
    source = source_parent / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    original_stat = local_lifecycle.os.stat
    named_source_stats = 0
    swapped = False

    def swap_parent_after_named_source_stat(path, *args, **kwargs):
        nonlocal named_source_stats, swapped
        metadata = original_stat(path, *args, **kwargs)
        if (
            path == source.name
            and kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
        ):
            named_source_stats += 1
            if named_source_stats == 2 and not swapped:
                swapped = True
                source_parent.rename(detached_parent)
                source_parent.mkdir()
                shutil.copyfile(FIXTURE_PDF, source)
        return metadata

    monkeypatch.setattr(local_lifecycle.os, "stat", swap_parent_after_named_source_stat)

    result = _invoke(["run", "init-local", str(source)])

    assert swapped is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "source_changed"
    assert not (source_parent / "paper_analysis").exists()
    assert not (detached_parent / "paper_analysis").exists()
    assert not (source_parent / "paper_note.md").exists()
    assert not (detached_parent / "paper_note.md").exists()


def test_init_local_rejects_source_path_exchange_after_tree_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.local_lifecycle as local_lifecycle

    source = tmp_path / "paper.pdf"
    detached_source = tmp_path / "detached-paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    original_publish = local_lifecycle.atomic_publish_tree
    exchanged = False

    def publish_then_exchange(staging: Path, destination: Path, **kwargs) -> Path:
        nonlocal exchanged
        published = original_publish(staging, destination, **kwargs)
        if destination.name == "paper_analysis" and not exchanged:
            source.rename(detached_source)
            shutil.copyfile(FIXTURE_PDF, source)
            exchanged = True
        return published

    monkeypatch.setattr(local_lifecycle, "atomic_publish_tree", publish_then_exchange)

    result = _invoke(["run", "init-local", str(source)])

    assert exchanged is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "source_changed"
    committed = json.loads((tmp_path / "paper_analysis" / "run.json").read_text())
    assert committed["status"] == "blocked"
    assert committed["gate"]["status"] == "blocked"
    assert {item["code"] for item in committed["gate"]["blockers"]} == {
        "source_changed"
    }
    assert not (tmp_path / "paper_note.md").exists()


@pytest.mark.parametrize("drift_kind", ["in_place", "replace"])
def test_init_local_rejects_run_manifest_drift_after_tree_commit(
    drift_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.local_lifecycle as local_lifecycle

    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    original_publish = local_lifecycle.atomic_publish_tree
    drifted = False

    def publish_then_drift_manifest(
        staging: Path,
        destination: Path,
        **kwargs,
    ):
        nonlocal drifted
        published = original_publish(staging, destination, **kwargs)
        if destination.name == "paper_analysis" and not drifted:
            manifest = destination / "run.json"
            if drift_kind == "in_place":
                manifest.write_bytes(b'{"corrupt":true}')
            else:
                replacement = destination / ".replacement-run.json"
                replacement.write_bytes(b'{"corrupt":true}')
                os.replace(replacement, manifest)
            drifted = True
        return published

    monkeypatch.setattr(
        local_lifecycle,
        "atomic_publish_tree",
        publish_then_drift_manifest,
    )

    result = _invoke(["run", "init-local", str(source)])

    assert drifted is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "initialization_failed"
    assert (tmp_path / "paper_analysis" / "run.json").read_bytes() == b'{"corrupt":true}'
    assert not (tmp_path / "paper_note.md").exists()


@pytest.mark.parametrize("drift_kind", ["source_snapshot", "extra_member"])
def test_init_local_rejects_published_closed_set_drift_after_tree_commit(
    drift_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.local_lifecycle as local_lifecycle

    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    original_publish = local_lifecycle.atomic_publish_tree
    drifted_path: Path | None = None

    def publish_then_drift_tree(
        staging: Path,
        destination: Path,
        **kwargs,
    ):
        nonlocal drifted_path
        published = original_publish(staging, destination, **kwargs)
        if destination.name == "paper_analysis" and drifted_path is None:
            if drift_kind == "source_snapshot":
                drifted_path = destination / "source" / "source.json"
                drifted_path.write_bytes(b'{"corrupt":true}')
            else:
                drifted_path = destination / "unexpected.bin"
                drifted_path.write_bytes(b"unexpected member")
        return published

    monkeypatch.setattr(
        local_lifecycle,
        "atomic_publish_tree",
        publish_then_drift_tree,
    )

    result = _invoke(["run", "init-local", str(source)])

    assert drifted_path is not None
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "initialization_failed"
    assert drifted_path.exists()
    assert not (tmp_path / "paper_note.md").exists()


def test_init_local_rejects_destination_replacement_after_tree_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.local_lifecycle as local_lifecycle

    source = tmp_path / "paper.pdf"
    detached_destination = tmp_path / "detached-paper-analysis"
    shutil.copyfile(FIXTURE_PDF, source)
    original_publish = local_lifecycle.atomic_publish_tree
    replaced = False

    def publish_then_replace_destination(
        staging: Path,
        destination: Path,
        **kwargs,
    ):
        nonlocal replaced
        published = original_publish(staging, destination, **kwargs)
        if destination.name == "paper_analysis" and not replaced:
            destination.rename(detached_destination)
            destination.mkdir()
            (destination / "run.json").write_bytes(b'{"corrupt":true}')
            replaced = True
        return published

    monkeypatch.setattr(
        local_lifecycle,
        "atomic_publish_tree",
        publish_then_replace_destination,
    )

    result = _invoke(["run", "init-local", str(source)])

    assert replaced is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "initialization_failed"
    assert (tmp_path / "paper_analysis" / "run.json").read_bytes() == b'{"corrupt":true}'
    detached_run = json.loads((detached_destination / "run.json").read_text())
    assert detached_run["status"] == "initialized"
    assert not (tmp_path / "paper_note.md").exists()


def test_init_local_blocked_update_does_not_overwrite_replaced_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.local_lifecycle as local_lifecycle

    source = tmp_path / "paper.pdf"
    target = tmp_path / "paper_note.md"
    run_path = tmp_path / "paper_analysis" / "run.json"
    shutil.copyfile(FIXTURE_PDF, source)
    original_publish = local_lifecycle.atomic_publish_tree
    original_write = local_lifecycle.atomic_write_bytes
    target_raced = False
    manifest_raced = False

    def publish_then_occupy_target(
        staging: Path,
        destination: Path,
        **kwargs,
    ):
        nonlocal target_raced
        published = original_publish(staging, destination, **kwargs)
        if destination.name == "paper_analysis" and not target_raced:
            target.write_bytes(b"external competing note")
            target_raced = True
        return published

    def replace_manifest_before_blocked_write(
        path: Path,
        content: bytes,
        **kwargs,
    ):
        nonlocal manifest_raced
        if Path(path) == run_path and not manifest_raced:
            replacement = run_path.parent / ".external-run.json"
            replacement.write_bytes(b'{"external":true}')
            os.replace(replacement, run_path)
            manifest_raced = True
        return original_write(path, content, **kwargs)

    monkeypatch.setattr(
        local_lifecycle,
        "atomic_publish_tree",
        publish_then_occupy_target,
    )
    monkeypatch.setattr(
        local_lifecycle,
        "atomic_write_bytes",
        replace_manifest_before_blocked_write,
    )

    result = _invoke(["run", "init-local", str(source)])

    assert target_raced is True
    assert manifest_raced is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "initialization_failed"
    assert run_path.read_bytes() == b'{"external":true}'
    assert target.read_bytes() == b"external competing note"
    assert not (tmp_path / "paper_analysis_v2").exists()


def test_init_local_blocks_raced_reservation_and_allocates_next_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.local_lifecycle as local_lifecycle

    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    target = tmp_path / "paper_note.md"
    original_publish = local_lifecycle.atomic_publish_tree
    injected = False

    def publish_then_race(staging: Path, destination: Path, **kwargs) -> Path:
        nonlocal injected
        published = original_publish(staging, destination, **kwargs)
        if destination.name == "paper_analysis" and not injected:
            target.write_bytes(b"external competing note")
            injected = True
        return published

    monkeypatch.setattr(local_lifecycle, "atomic_publish_tree", publish_then_race)

    result = _invoke(["run", "init-local", str(source)])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["data"]["run_dir"] == str(tmp_path / "paper_analysis_v2")
    blocked = json.loads((tmp_path / "paper_analysis" / "run.json").read_text())
    assert blocked["status"] == "blocked"
    assert blocked["gate"]["status"] == "blocked"
    assert {item["code"] for item in blocked["gate"]["blockers"]} == {
        "local_target_conflict"
    }
    retired_manifests = tuple(
        (tmp_path / "paper_analysis").glob(".run.json.*.tmp")
    )
    assert len(retired_manifests) == 1
    retired = json.loads(retired_manifests[0].read_bytes())
    assert retired["schema_version"] == "paper_reader.run.v2"
    assert retired["run_id"] == blocked["run_id"]
    assert retired["status"] == "initialized"
    assert target.read_bytes() == b"external competing note"
    assert not (tmp_path / "paper_note_v2.md").exists()
