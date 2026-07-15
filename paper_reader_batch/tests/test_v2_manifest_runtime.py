import hashlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paper_reader_batch import v2_cli, v2_json, v2_manifest
from paper_reader_batch.v2_cli import app
from paper_reader_batch.v2_contracts import EVENT_SCHEMA_VERSION, MANIFEST_SCHEMA_VERSION, STATE_SCHEMA_VERSION
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    active_transition_targets,
    canonical_json_bytes,
    ensure_directory,
    initialize_locked_secret,
    list_directory,
    locked_file,
)
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest, validate_manifest_file
from paper_reader_batch.v2_receipts import RequestReceipt, RequestReceiptStore
from paper_reader_batch.v2_run import initialize_run


runner = CliRunner()
REQUEST_1 = "11111111-1111-4111-8111-111111111111"
REQUEST_2 = "22222222-2222-4222-8222-222222222222"


def _json_result(result) -> dict:
    assert len(result.stdout.splitlines()) == 1, (result.stdout, result.stderr)
    return json.loads(result.stdout)


def _snapshot(path: Path) -> dict[str, tuple[int, int, str]]:
    snapshot: dict[str, tuple[int, int, str]] = {}
    if not path.exists():
        return snapshot
    for candidate in sorted(path.rglob("*")):
        stat = candidate.lstat()
        digest = ""
        if candidate.is_file():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        snapshot[str(candidate.relative_to(path))] = (stat.st_mtime_ns, stat.st_size, digest)
    return snapshot


def _strong_receipt_snapshot(
    path: Path,
) -> dict[str, tuple[int, int, int, int, str, bytes]]:
    snapshot: dict[str, tuple[int, int, int, int, str, bytes]] = {}
    for candidate in [path, *sorted(path.rglob("*"))]:
        metadata = candidate.lstat()
        raw = candidate.read_bytes() if candidate.is_file() else b""
        relative = "." if candidate == path else str(candidate.relative_to(path))
        snapshot[relative] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
            metadata.st_size,
            hashlib.sha256(raw).hexdigest(),
            raw,
        )
    return snapshot


def _make_paths_input(tmp_path: Path) -> tuple[Path, Path]:
    pdf = tmp_path / "论文.pdf"
    pdf.write_bytes(b"%PDF-1.7\nsource bytes\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(f"{pdf}\n", encoding="utf-8")
    return pdf, paths


def test_oversized_manifest_request_is_read_only_on_first_attempt_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"
    monkeypatch.setattr(v2_json, "MAX_JSON_ARTIFACT_BYTES", 64)

    receipt_dir = skill_root / ".paper_reader_batch" / "request-receipts"
    for _attempt in range(2):
        with pytest.raises(BatchRuntimeError) as exc_info:
            create_pdf_paths_manifest(
                paths,
                batch_title="oversized manifest",
                output=output,
                request_id=REQUEST_1,
                skill_root=skill_root,
                created_at="2026-07-10T00:00:00Z",
            )

        assert exc_info.value.code == "resource_limit"
        assert not output.exists()
        assert receipt_dir.is_dir()
        assert list(receipt_dir.iterdir()) == []


@pytest.mark.parametrize("oversized_stage", ["reserved", "committed"])
def test_receipt_preflights_both_canonical_sizes_before_reserved_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oversized_stage: str,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    target = (tmp_path / "output.json").resolve()
    store = RequestReceiptStore(skill_root)
    plan = {
        "semantic_result": {"value": "x" * 128},
        "payload": "bounded",
    }
    unsigned = RequestReceipt(
        request_id=REQUEST_1,
        command="test receipt size",
        request_fingerprint="a" * 64,
        requested_target=str(target),
        target=str(target),
        status="reserved",
        plan=plan,
        result=None,
        integrity_hmac="0" * 64,
    )
    reserved = unsigned.model_copy(
        update={"integrity_hmac": store._signature(b"test-key", unsigned)}
    )
    committed = store._committed_receipt(reserved, b"test-key")
    reserved_size = len(canonical_json_bytes(reserved))
    assert len(canonical_json_bytes(committed)) > reserved_size
    limit = reserved_size - 1 if oversized_stage == "reserved" else reserved_size
    monkeypatch.setattr(v2_json, "MAX_JSON_ARTIFACT_BYTES", limit)

    def forbidden_publish(*_args, **_kwargs) -> None:
        raise AssertionError("oversized receipt reached output publication")

    for _attempt in range(2):
        with pytest.raises(BatchRuntimeError) as exc_info:
            store.execute(
                request_id=REQUEST_1,
                command="test receipt size",
                request_fingerprint="a" * 64,
                requested_target=target,
                target_factory=lambda _reserved: target,
                plan_factory=lambda _target: plan,
                publish=forbidden_publish,
                inspect=lambda _target, _plan: False,
            )

        assert exc_info.value.code == "resource_limit"
        assert not target.exists()
        assert not store._receipt_path(REQUEST_1).exists()


def test_pdf_source_rejects_initial_size_limit_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "oversized.pdf"
    pdf.write_bytes(b"%PDF-12")
    monkeypatch.setattr(v2_manifest, "MAX_PDF_SOURCE_BYTES", 6)

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("oversized PDF reached os.read")

    monkeypatch.setattr(v2_manifest.os, "read", forbidden_read)
    with pytest.raises(BatchRuntimeError) as exc_info:
        v2_manifest._pdf_source(pdf)

    assert exc_info.value.code == "source_too_large"
    assert exc_info.value.details == {"size_bytes": 7, "max_bytes": 6}


def test_pdf_source_rejects_growth_past_cumulative_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "growing.pdf"
    pdf.write_bytes(b"%PDF-")
    chunks = iter([b"%PDF-", b"12"])
    monkeypatch.setattr(v2_manifest, "MAX_PDF_SOURCE_BYTES", 6)
    monkeypatch.setattr(
        v2_manifest.os,
        "read",
        lambda _descriptor, _requested: next(chunks, b""),
    )

    with pytest.raises(BatchRuntimeError) as exc_info:
        v2_manifest._pdf_source(pdf)

    assert exc_info.value.code == "source_too_large"
    assert exc_info.value.details == {"size_bytes": 7, "max_bytes": 6}


def test_pdf_source_rejects_fifo_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fifo = tmp_path / "source.pdf"
    os.mkfifo(fifo)

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("PDF FIFO reached os.open")

    monkeypatch.setattr(v2_manifest.os, "open", forbidden_open)
    with pytest.raises(BatchRuntimeError) as exc_info:
        v2_manifest._pdf_source(fifo)

    assert exc_info.value.code == "invalid_pdf"


def _manifest_process(
    barrier,
    queue,
    paths: str,
    output: str,
    skill_root: str,
    request_id: str,
) -> None:
    barrier.wait()
    try:
        outcome = create_pdf_paths_manifest(
            Path(paths),
            batch_title="process race",
            output=Path(output),
            request_id=request_id,
            skill_root=Path(skill_root),
        )
    except Exception as exc:  # child process reports the structured domain error
        queue.put(("error", getattr(exc, "code", type(exc).__name__), str(exc)))
    else:
        queue.put(("ok", outcome.replayed, ""))


def _init_process(
    barrier,
    queue,
    manifest: str,
    skill_root: str,
    request_id: str,
) -> None:
    barrier.wait()
    try:
        outcome = initialize_run(
            Path(manifest),
            request_id=request_id,
            skill_root=Path(skill_root),
            initialized_at="2026-07-10T00:00:00Z",
        )
    except Exception as exc:
        queue.put(("error", getattr(exc, "code", type(exc).__name__), str(exc)))
    else:
        queue.put(("ok", outcome.replayed, outcome.result["run_dir"]))


def _crash_manifest_receipt(
    paths: str,
    output: str,
    skill_root: str,
    stage: str,
    occurrence: int = 1,
) -> None:
    seen = 0

    def crash(current: str) -> None:
        nonlocal seen
        if current == stage:
            seen += 1
            if seen == occurrence:
                os._exit(77)

    create_pdf_paths_manifest(
        Path(paths),
        batch_title="receipt hard crash",
        output=Path(output),
        request_id=REQUEST_1,
        skill_root=Path(skill_root),
        created_at="2026-07-10T00:00:00Z",
        fault=crash,
    )


def _crash_during_initial_partial_receipt(skill_root: str, request_id: str) -> None:
    root = Path(skill_root) / ".paper_reader_batch"
    receipts = root / "request-receipts"
    lock_path = root / "request-receipts.lock"
    with locked_file(lock_path, create=True) as descriptor:
        initialize_locked_secret(descriptor)
        ensure_directory(receipts)
        writing = receipts / (
            f".{request_id}.json.0123456789abcdef0123456789abcdef.writing"
        )
        file_descriptor = os.open(writing, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(file_descriptor, b'{"schema_version":')
            os.fsync(file_descriptor)
        finally:
            os.close(file_descriptor)
        os._exit(77)


def test_pdf_manifest_cli_binds_source_and_replays_exact_request(tmp_path: Path, monkeypatch) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    monkeypatch.setattr(v2_cli, "_batch_root", lambda: skill_root)
    pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"
    args = [
        "manifest",
        "from-pdf-paths",
        str(paths),
        "--batch-title",
        "长标题 批次",
        "--output",
        str(output),
        "--request-id",
        REQUEST_1,
    ]

    first = runner.invoke(app, args, terminal_width=24)
    first_payload = _json_result(first)
    assert first.exit_code == 0, first_payload
    assert first_payload["replayed"] is False
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    source = manifest["items"][0]["source"]
    assert source["path"] == str(pdf.resolve())
    assert source["size_bytes"] == pdf.stat().st_size
    assert source["sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert source["file_identity"] == {"device": pdf.stat().st_dev, "inode": pdf.stat().st_ino}
    exact_bytes = output.read_bytes()

    replay = runner.invoke(app, args, terminal_width=24)
    replay_payload = _json_result(replay)
    assert replay.exit_code == 0, replay_payload
    assert replay_payload["replayed"] is True
    assert replay_payload["result"] == first_payload["result"]
    assert output.read_bytes() == exact_bytes

    pdf.write_bytes(b"%PDF-1.7\nchanged bytes\n")
    conflict = runner.invoke(app, args)
    conflict_payload = _json_result(conflict)
    assert conflict.exit_code != 0
    assert conflict_payload["error"]["code"] == "idempotency_conflict"
    assert output.read_bytes() == exact_bytes


def test_manifest_rejects_nul_pdf_path_before_any_source_filesystem_access(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="NUL boundary",
        output=manifest_path,
        request_id=REQUEST_1,
        skill_root=skill_root,
        created_at="2026-07-10T00:00:00Z",
    )
    payload = json.loads(manifest_path.read_bytes())
    payload["items"][0]["source"]["path"] = "/tmp/paper\x00.pdf"
    manifest_path.write_bytes(canonical_json_bytes(payload))

    def must_not_touch_source(*_args, **_kwargs):
        raise AssertionError("source filesystem access happened before contract validation")

    monkeypatch.setattr("paper_reader_batch.v2_manifest._pdf_source", must_not_touch_source)
    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_manifest_file(manifest_path)

    assert exc_info.value.code == "invalid_manifest"


def test_manifest_rejects_pdf_aliases_and_duplicate_zotero_keys_before_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    monkeypatch.setattr(v2_cli, "_batch_root", lambda: skill_root)
    pdf, _paths = _make_paths_input(tmp_path)
    hardlink = tmp_path / "hardlink.pdf"
    hardlink.hardlink_to(pdf)
    duplicate_paths = tmp_path / "duplicate-paths.txt"
    duplicate_paths.write_text(f"{pdf}\n{hardlink}\n", encoding="utf-8")
    output = tmp_path / "duplicate.json"

    result = runner.invoke(
        app,
        [
            "manifest",
            "from-pdf-paths",
            str(duplicate_paths),
            "--batch-title",
            "duplicates",
            "--output",
            str(output),
            "--request-id",
            REQUEST_1,
        ],
    )
    payload = _json_result(result)
    assert result.exit_code != 0
    assert payload["error"]["code"] == "duplicate_source"
    assert not output.exists()

    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "collection": {"key": "COLL1", "name": "Collection"},
                "items": [
                    {"item_key": "KEY1", "title": "One"},
                    {"item_key": "KEY1", "title": "Duplicate"},
                ],
            }
        ),
        encoding="utf-8",
    )
    collection_output = tmp_path / "collection.json"
    result = runner.invoke(
        app,
        [
            "manifest",
            "from-zotero-collection",
            "COLL1",
            "--inventory",
            str(inventory),
            "--batch-title",
            "collection",
            "--output",
            str(collection_output),
            "--request-id",
            REQUEST_2,
        ],
    )
    payload = _json_result(result)
    assert result.exit_code != 0
    assert payload["error"]["code"] == "duplicate_source"
    assert not collection_output.exists()


def test_zotero_collection_manifest_accepts_name_only_inventory_and_replays(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    monkeypatch.setattr(v2_cli, "_batch_root", lambda: skill_root)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "collection": {"name": "仅名称集合"},
                "items": [{"item_key": "ITEM1", "title": "论文一"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "manifest.json"
    args = [
        "manifest",
        "from-zotero-collection",
        "仅名称集合",
        "--inventory",
        str(inventory),
        "--batch-title",
        "name-only collection",
        "--output",
        str(output),
        "--request-id",
        REQUEST_1,
    ]

    first = runner.invoke(app, args)
    first_payload = _json_result(first)
    assert first.exit_code == 0, first_payload
    assert first_payload["replayed"] is False
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["source_summary"]["collection_key"] is None
    assert manifest["source_summary"]["collection_name"] == "仅名称集合"
    assert manifest["items"][0]["source"]["collection_key"] is None
    exact_bytes = output.read_bytes()

    replay = runner.invoke(app, args)
    replay_payload = _json_result(replay)
    assert replay.exit_code == 0, replay_payload
    assert replay_payload["replayed"] is True
    assert replay_payload["result"] == first_payload["result"]
    assert output.read_bytes() == exact_bytes

    wrong_output = tmp_path / "wrong-name.json"
    before = _snapshot(skill_root)
    mismatch = runner.invoke(
        app,
        [
            *args[:2],
            "另一个集合",
            *args[3:8],
            str(wrong_output),
            "--request-id",
            REQUEST_2,
        ],
    )
    mismatch_payload = _json_result(mismatch)
    assert mismatch.exit_code != 0
    assert mismatch_payload["error"]["code"] == "collection_mismatch"
    assert not wrong_output.exists()
    assert _snapshot(skill_root) == before


@pytest.mark.parametrize(
    "inventory",
    [
        {
            "collection": {"key": 123, "name": "Collection"},
            "items": [{"item_key": "ITEM1", "title": "One"}],
        },
        {
            "collection": {"key": "COLL1", "name": 123},
            "items": [{"item_key": "ITEM1", "title": "One"}],
        },
        {
            "collection": {"key": "COLL1", "name": "Collection"},
            "items": [{"item_key": 123, "title": "One"}],
        },
        {
            "collection": {"key": "COLL1", "name": "Collection"},
            "items": [{"item_key": "ITEM1", "title": 123}],
        },
    ],
)
def test_zotero_collection_manifest_rejects_implicitly_coerced_inventory_fields(
    tmp_path: Path,
    monkeypatch,
    inventory: dict,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    monkeypatch.setattr(v2_cli, "_batch_root", lambda: skill_root)
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    output = tmp_path / "manifest.json"

    result = runner.invoke(
        app,
        [
            "manifest",
            "from-zotero-collection",
            "Collection" if inventory["collection"].get("name") == "Collection" else "COLL1",
            "--inventory",
            str(inventory_path),
            "--batch-title",
            "strict inventory",
            "--output",
            str(output),
            "--request-id",
            REQUEST_1,
        ],
    )
    payload = _json_result(result)

    assert result.exit_code != 0
    assert payload["error"]["code"] == "invalid_inventory"
    assert not output.exists()
    assert not (skill_root / ".paper_reader_batch").exists()


def test_manifest_receipt_resumes_before_and_after_publication_without_scan(tmp_path: Path) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"

    def fail_after_publication(stage: str) -> None:
        if stage == "after_publish":
            raise RuntimeError("injected after publication")

    with pytest.raises(RuntimeError, match="after publication"):
        create_pdf_paths_manifest(
            paths,
            batch_title="crash replay",
            output=output,
            request_id=REQUEST_1,
            skill_root=skill_root,
            fault=fail_after_publication,
        )
    assert output.exists()
    published = output.read_bytes()

    resumed = create_pdf_paths_manifest(
        paths,
        batch_title="crash replay",
        output=output,
        request_id=REQUEST_1,
        skill_root=skill_root,
    )
    assert resumed.replayed is True
    assert output.read_bytes() == published

    output_2 = tmp_path / "manifest-2.json"

    def fail_before_publication(stage: str) -> None:
        if stage == "receipt_reserved":
            raise RuntimeError("injected before publication")

    with pytest.raises(RuntimeError, match="before publication"):
        create_pdf_paths_manifest(
            paths,
            batch_title="before crash",
            output=output_2,
            request_id=REQUEST_2,
            skill_root=skill_root,
            fault=fail_before_publication,
        )
    assert not output_2.exists()
    resumed_2 = create_pdf_paths_manifest(
        paths,
        batch_title="before crash",
        output=output_2,
        request_id=REQUEST_2,
        skill_root=skill_root,
    )
    assert resumed_2.replayed is True
    assert output_2.exists()


@pytest.mark.parametrize("stage", ["after_writing_fsync", "after_file_fsync"])
def test_manifest_receipt_hard_crash_recovers_original_reserved_plan(tmp_path: Path, stage: str) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_manifest_receipt,
        args=(str(paths), str(output), str(skill_root), stage),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 77
    assert not output.exists()

    recovered = create_pdf_paths_manifest(
        paths,
        batch_title="receipt hard crash",
        output=output,
        request_id=REQUEST_1,
        skill_root=skill_root,
        created_at="2026-07-10T00:00:00Z",
    )
    assert recovered.replayed is True
    assert output.exists()
    receipt_dir = skill_root / ".paper_reader_batch" / "request-receipts"
    receipt_name = f"{REQUEST_1}.json"
    assert set(list_directory(receipt_dir)) == {receipt_name, ".transitions"}
    assert active_transition_targets(
        receipt_dir,
        replace_targets={receipt_name},
    ) == set()


def test_pending_receipt_keeps_target_reserved_against_another_request(tmp_path: Path) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_manifest_receipt,
        args=(str(paths), str(output), str(skill_root), "after_file_fsync"),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 77
    assert not output.exists()

    with pytest.raises(BatchRuntimeError) as exc_info:
        create_pdf_paths_manifest(
            paths,
            batch_title="competing request",
            output=output,
            request_id=REQUEST_2,
            skill_root=skill_root,
            created_at="2026-07-10T00:00:00Z",
        )
    assert exc_info.value.code == "output_conflict"
    assert not output.exists()

    recovered = create_pdf_paths_manifest(
        paths,
        batch_title="receipt hard crash",
        output=output,
        request_id=REQUEST_1,
        skill_root=skill_root,
        created_at="2026-07-10T00:00:00Z",
    )
    assert recovered.replayed is True
    assert output.exists()


@pytest.mark.parametrize("stage", ["after_writing_fsync", "after_file_fsync"])
def test_committed_receipt_replacement_pending_recovers_exactly(
    tmp_path: Path,
    stage: str,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_manifest_receipt,
        args=(str(paths), str(output), str(skill_root), stage, 2),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 77
    assert output.exists()
    receipt_dir = skill_root / ".paper_reader_batch" / "request-receipts"
    receipt_name = f"{REQUEST_1}.json"
    assert (receipt_dir / receipt_name).exists()
    assert active_transition_targets(
        receipt_dir,
        replace_targets={receipt_name},
    ) == {receipt_name}

    recovered = create_pdf_paths_manifest(
        paths,
        batch_title="receipt hard crash",
        output=output,
        request_id=REQUEST_1,
        skill_root=skill_root,
        created_at="2026-07-10T00:00:00Z",
    )
    assert recovered.replayed is True
    receipt = json.loads((receipt_dir / f"{REQUEST_1}.json").read_bytes())
    assert receipt["status"] == "committed"
    assert set(list_directory(receipt_dir)) == {receipt_name, ".transitions"}
    assert active_transition_targets(
        receipt_dir,
        replace_targets={receipt_name},
    ) == set()


def test_pending_receipt_commit_conflict_is_zero_mutation(tmp_path: Path) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_manifest_receipt,
        args=(str(paths), str(output), str(skill_root), "after_file_fsync", 2),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 77
    receipt_dir = skill_root / ".paper_reader_batch" / "request-receipts"
    before = _snapshot(receipt_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        create_pdf_paths_manifest(
            paths,
            batch_title="changed input",
            output=output,
            request_id=REQUEST_1,
            skill_root=skill_root,
            created_at="2026-07-10T00:00:00Z",
        )
    assert exc_info.value.code == "idempotency_conflict"
    assert _snapshot(receipt_dir) == before


def test_unpublished_receipt_conflict_is_checked_before_other_receipt_recovery(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    first_output = tmp_path / "first.json"

    def stop_first_before_publication(stage: str) -> None:
        if stage == "after_file_fsync":
            raise RuntimeError("first receipt staged")

    with pytest.raises(RuntimeError, match="first receipt staged"):
        create_pdf_paths_manifest(
            paths,
            batch_title="first original",
            output=first_output,
            request_id=REQUEST_1,
            skill_root=skill_root,
            created_at="2026-07-10T00:00:00Z",
            fault=stop_first_before_publication,
        )

    second_output = tmp_path / "second.json"
    second_file_fsyncs = 0

    def stop_second_during_commit(stage: str) -> None:
        nonlocal second_file_fsyncs
        if stage == "after_file_fsync":
            second_file_fsyncs += 1
            if second_file_fsyncs == 2:
                raise RuntimeError("second receipt commit staged")

    with pytest.raises(RuntimeError, match="second receipt commit staged"):
        create_pdf_paths_manifest(
            paths,
            batch_title="second original",
            output=second_output,
            request_id=REQUEST_2,
            skill_root=skill_root,
            created_at="2026-07-10T00:00:00Z",
            fault=stop_second_during_commit,
        )

    receipt_dir = skill_root / ".paper_reader_batch" / "request-receipts"
    assert active_transition_targets(
        receipt_dir,
        replace_targets={f"{REQUEST_2}.json"},
    ) == {f"{REQUEST_2}.json"}
    before = _strong_receipt_snapshot(receipt_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        create_pdf_paths_manifest(
            paths,
            batch_title="first changed",
            output=tmp_path / "first-changed.json",
            request_id=REQUEST_1,
            skill_root=skill_root,
            created_at="2026-07-10T00:00:00Z",
        )

    assert exc_info.value.code == "idempotency_conflict"
    assert _strong_receipt_snapshot(receipt_dir) == before

    replayed = create_pdf_paths_manifest(
        paths,
        batch_title="first original",
        output=first_output,
        request_id=REQUEST_1,
        skill_root=skill_root,
        created_at="2026-07-10T00:00:00Z",
    )

    assert replayed.replayed is True
    assert first_output.exists()
    assert second_output.exists()
    assert active_transition_targets(
        receipt_dir,
        replace_targets={f"{REQUEST_1}.json", f"{REQUEST_2}.json"},
    ) == set()


def test_partial_first_lock_secret_is_reinitialized_only_before_receipt_evidence(tmp_path: Path) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_manifest_receipt,
        args=(str(paths), str(output), str(skill_root), "after_secret_partial_write"),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 77
    lock_path = skill_root / ".paper_reader_batch" / "request-receipts.lock"
    assert lock_path.stat().st_size == 1

    recovered = create_pdf_paths_manifest(
        paths,
        batch_title="receipt hard crash",
        output=output,
        request_id=REQUEST_1,
        skill_root=skill_root,
        created_at="2026-07-10T00:00:00Z",
    )
    assert recovered.replayed is False
    assert lock_path.stat().st_size == 32

    lock_path.write_bytes(b"x")
    with pytest.raises(BatchRuntimeError) as exc_info:
        create_pdf_paths_manifest(
            paths,
            batch_title="receipt hard crash",
            output=output,
            request_id=REQUEST_1,
            skill_root=skill_root,
            created_at="2026-07-10T00:00:00Z",
        )
    assert exc_info.value.code == "storage_path_changed"


@pytest.mark.parametrize("first_request", [REQUEST_1, REQUEST_2])
def test_incomplete_initial_receipt_writing_stays_immutable_and_does_not_poison_requests(
    tmp_path: Path,
    first_request: str,
) -> None:
    skill_root = tmp_path / "skill"
    receipt_root = skill_root / ".paper_reader_batch"
    receipt_dir = receipt_root / "request-receipts"
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_during_initial_partial_receipt,
        args=(str(skill_root), REQUEST_1),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 77
    partial = receipt_dir / (
        f".{REQUEST_1}.json.0123456789abcdef0123456789abcdef.writing"
    )
    assert partial.read_bytes() == b'{"schema_version":'
    _pdf, paths = _make_paths_input(tmp_path)
    first_output = tmp_path / "first.json"

    first = create_pdf_paths_manifest(
        paths,
        batch_title="first request",
        output=first_output,
        request_id=first_request,
        skill_root=skill_root,
        created_at="2026-07-10T00:00:00Z",
    )

    assert first.replayed is False
    assert first_output.exists()
    assert partial.exists()
    assert partial.read_bytes() == b'{"schema_version":'

    second_request = REQUEST_2 if first_request == REQUEST_1 else REQUEST_1
    second_output = tmp_path / "second.json"
    second = create_pdf_paths_manifest(
        paths,
        batch_title="second request",
        output=second_output,
        request_id=second_request,
        skill_root=skill_root,
        created_at="2026-07-10T00:00:00Z",
    )
    assert second.replayed is False
    assert second_output.exists()
    assert partial.read_bytes() == b'{"schema_version":'


@pytest.mark.parametrize("unsafe_kind", ["hardlink", "symlink"])
def test_incomplete_receipt_cleanup_refuses_unsafe_writing_entries(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    skill_root = tmp_path / "skill"
    receipt_root = skill_root / ".paper_reader_batch"
    receipt_dir = receipt_root / "request-receipts"
    receipt_dir.mkdir(parents=True)
    (receipt_root / "request-receipts.lock").write_bytes(b"k" * 32)
    outside = tmp_path / "outside"
    outside.write_bytes(b'{"schema_version":')
    writing = receipt_dir / (
        f".{REQUEST_1}.json.0123456789abcdef0123456789abcdef.writing"
    )
    if unsafe_kind == "hardlink":
        writing.hardlink_to(outside)
    else:
        writing.symlink_to(outside)
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"
    before = _snapshot(tmp_path)

    with pytest.raises(BatchRuntimeError) as exc_info:
        create_pdf_paths_manifest(
            paths,
            batch_title="unsafe incomplete receipt",
            output=output,
            request_id=REQUEST_2,
            skill_root=skill_root,
            created_at="2026-07-10T00:00:00Z",
        )

    assert exc_info.value.code in {"unsafe_path", "unsafe_storage"}
    assert _snapshot(tmp_path) == before
    assert outside.read_bytes() == b'{"schema_version":'
    assert not output.exists()


def test_incomplete_receipt_is_not_cleaned_before_other_durable_receipts_validate(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    first_output = tmp_path / "first.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="durable owner",
        output=first_output,
        request_id=REQUEST_1,
        skill_root=skill_root,
        created_at="2026-07-10T00:00:00Z",
    )
    receipt_dir = skill_root / ".paper_reader_batch" / "request-receipts"
    durable = receipt_dir / f"{REQUEST_1}.json"
    durable.write_bytes(durable.read_bytes() + b"\n")
    partial = receipt_dir / (
        f".{REQUEST_2}.json.0123456789abcdef0123456789abcdef.writing"
    )
    partial.write_bytes(b'{"schema_version":')
    second_output = tmp_path / "second.json"
    before = _snapshot(tmp_path)

    with pytest.raises(BatchRuntimeError) as exc_info:
        create_pdf_paths_manifest(
            paths,
            batch_title="must fail read only",
            output=second_output,
            request_id=REQUEST_2,
            skill_root=skill_root,
            created_at="2026-07-10T00:00:00Z",
        )

    assert exc_info.value.code == "receipt_corrupt"
    assert _snapshot(tmp_path) == before
    assert partial.exists()
    assert not second_output.exists()


def test_manifest_target_is_owned_by_one_request_across_processes(tmp_path: Path) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(
            target=_manifest_process,
            args=(barrier, queue, str(paths), str(output), str(skill_root), request_id),
        )
        for request_id in [REQUEST_1, REQUEST_2]
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    outcomes = sorted(queue.get(timeout=2) for _ in processes)
    assert [(kind, value) for kind, value, _message in outcomes] == [
        ("error", "output_conflict"),
        ("ok", False),
    ], outcomes
    receipts = [
        path
        for path in (skill_root / ".paper_reader_batch" / "request-receipts").glob("*.json")
        if not path.name.startswith(".")
    ]
    assert len(receipts) == 1

    owner_request = json.loads(receipts[0].read_text(encoding="utf-8"))["request_id"]
    owner = create_pdf_paths_manifest(
        paths,
        batch_title="process race",
        output=output,
        request_id=owner_request,
        skill_root=skill_root,
    )
    assert owner.replayed is True


@pytest.mark.parametrize("tamper", ["whitespace", "reordered", "nan"])
def test_committed_receipt_tamper_is_read_only_failure(tmp_path: Path, tamper: str) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="receipt tamper",
        output=output,
        request_id=REQUEST_1,
        skill_root=skill_root,
    )
    receipt = skill_root / ".paper_reader_batch" / "request-receipts" / f"{REQUEST_1}.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    if tamper == "whitespace":
        tampered = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    elif tamper == "reordered":
        tampered = json.dumps(
            dict(reversed(list(payload.items()))),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    else:
        tampered = receipt.read_bytes().replace(b'"status":"committed"', b'"status":NaN')
    receipt.write_bytes(tampered)
    before_output = output.read_bytes()
    before_receipt = receipt.read_bytes()

    with pytest.raises(BatchRuntimeError) as exc_info:
        create_pdf_paths_manifest(
            paths,
            batch_title="receipt tamper",
            output=output,
            request_id=REQUEST_1,
            skill_root=skill_root,
        )
    assert getattr(exc_info.value, "code", "") == "receipt_corrupt"
    assert output.read_bytes() == before_output
    assert receipt.read_bytes() == before_receipt


@pytest.mark.parametrize("tamper", ["target", "nested_plan"])
def test_reserved_receipt_semantic_tamper_never_publishes(tmp_path: Path, tamper: str) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"

    def stop_after_reservation(stage: str) -> None:
        if stage == "receipt_reserved":
            raise RuntimeError("stop after reservation")

    with pytest.raises(RuntimeError):
        create_pdf_paths_manifest(
            paths,
            batch_title="reserved tamper",
            output=output,
            request_id=REQUEST_1,
            skill_root=skill_root,
            fault=stop_after_reservation,
        )
    receipt = skill_root / ".paper_reader_batch" / "request-receipts" / f"{REQUEST_1}.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    outside = tmp_path / "outside.json"
    if tamper == "target":
        payload["target"] = str(outside)
    else:
        payload["plan"]["manifest"]["batch_title"] = "attacker content"
    receipt.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    before = receipt.read_bytes()

    with pytest.raises(BatchRuntimeError) as exc_info:
        create_pdf_paths_manifest(
            paths,
            batch_title="reserved tamper",
            output=output,
            request_id=REQUEST_1,
            skill_root=skill_root,
        )
    assert exc_info.value.code == "receipt_corrupt"
    assert not output.exists()
    assert not outside.exists()
    assert receipt.read_bytes() == before


def test_manifest_request_fingerprint_binds_created_at_override(tmp_path: Path) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    output = tmp_path / "manifest.json"

    def stop(stage: str) -> None:
        if stage == "receipt_reserved":
            raise RuntimeError("reserved")

    with pytest.raises(RuntimeError):
        create_pdf_paths_manifest(
            paths,
            batch_title="time bound",
            output=output,
            request_id=REQUEST_1,
            skill_root=skill_root,
            created_at="2026-07-10T00:00:00Z",
            fault=stop,
        )
    with pytest.raises(BatchRuntimeError) as exc_info:
        create_pdf_paths_manifest(
            paths,
            batch_title="time bound",
            output=output,
            request_id=REQUEST_1,
            skill_root=skill_root,
            created_at="2026-07-10T00:00:01Z",
        )
    assert exc_info.value.code == "idempotency_conflict"
    assert not output.exists()


def test_run_init_creates_v2_journal_tree_and_rejects_v1_before_mutation(tmp_path: Path, monkeypatch) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    monkeypatch.setattr(v2_cli, "_batch_root", lambda: skill_root)
    _pdf, paths = _make_paths_input(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_result = runner.invoke(
        app,
        [
            "manifest",
            "from-pdf-paths",
            str(paths),
            "--batch-title",
            "journal run",
            "--output",
            str(manifest_path),
            "--request-id",
            REQUEST_1,
        ],
    )
    assert manifest_result.exit_code == 0, manifest_result.output
    run_dir = tmp_path / "run"
    init_args = [
        "run",
        "init",
        "--manifest",
        str(manifest_path),
        "--output",
        str(run_dir),
        "--request-id",
        REQUEST_2,
    ]

    initialized = runner.invoke(app, init_args)
    initialized_payload = _json_result(initialized)
    assert initialized.exit_code == 0, initialized_payload
    assert initialized_payload["result"]["run_dir"] == str(run_dir.resolve())
    event = json.loads((run_dir / "events" / "00000000000000000001.json").read_text(encoding="utf-8"))
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert event["schema_version"] == EVENT_SCHEMA_VERSION
    assert state["schema_version"] == STATE_SCHEMA_VERSION
    assert event["manifest_sha256"] == state["manifest_sha256"]
    for path in [
        run_dir / "results" / "worker",
        run_dir / "results" / "local-prepare",
        run_dir / "results" / "write",
        run_dir / "results" / "reconcile",
        run_dir / ".run.lock",
    ]:
        assert path.exists()
    assert not (run_dir / "batch-report.json").exists()
    assert not (run_dir / "batch-report.md").exists()

    replay = runner.invoke(app, init_args)
    replay_payload = _json_result(replay)
    assert replay.exit_code == 0
    assert replay_payload["replayed"] is True
    assert replay_payload["result"] == initialized_payload["result"]

    historical = tmp_path / "historical"
    historical.mkdir()
    v1_manifest = historical / "manifest.json"
    v1_manifest.write_text(json.dumps({"schema_version": "paper_reader_batch.manifest.v1"}), encoding="utf-8")
    before = _snapshot(historical)
    rejected_output = historical / "must-not-exist"
    rejected = runner.invoke(
        app,
        [
            "run",
            "init",
            "--manifest",
            str(v1_manifest),
            "--output",
            str(rejected_output),
            "--request-id",
            "33333333-3333-4333-8333-333333333333",
        ],
    )
    rejected_payload = _json_result(rejected)
    assert rejected.exit_code != 0
    assert rejected_payload["error"]["code"] == "unsupported_run_schema"
    assert _snapshot(historical) == before


def test_default_run_init_allocation_and_same_request_replay_are_cross_process_safe(tmp_path: Path) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _pdf, paths = _make_paths_input(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title="Concurrent Allocation",
        output=manifest_path,
        request_id="44444444-4444-4444-8444-444444444444",
        skill_root=skill_root,
    )
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    requests = [REQUEST_1, REQUEST_2]
    processes = [
        context.Process(
            target=_init_process,
            args=(barrier, queue, str(manifest_path), str(skill_root), request_id),
        )
        for request_id in requests
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    outcomes = [queue.get(timeout=2) for _ in processes]
    assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
    run_dirs = sorted(outcome[2] for outcome in outcomes)
    assert len(set(run_dirs)) == 2
    assert run_dirs[0].endswith("/concurrent-allocation")
    assert run_dirs[1].endswith("/concurrent-allocation_v2")

    replay_barrier = context.Barrier(2)
    replay_queue = context.Queue()
    replay_processes = [
        context.Process(
            target=_init_process,
            args=(replay_barrier, replay_queue, str(manifest_path), str(skill_root), REQUEST_1),
        )
        for _index in range(2)
    ]
    for process in replay_processes:
        process.start()
    for process in replay_processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    replay_outcomes = [replay_queue.get(timeout=2) for _ in replay_processes]
    assert all(outcome[0] == "ok" and outcome[1] is True for outcome in replay_outcomes)
    assert replay_outcomes[0][2] == replay_outcomes[1][2]
