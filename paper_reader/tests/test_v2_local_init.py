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

    def publish_then_race(staging: Path, destination: Path) -> Path:
        nonlocal injected
        published = original_publish(staging, destination)
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
    assert target.read_bytes() == b"external competing note"
    assert not (tmp_path / "paper_note_v2.md").exists()
