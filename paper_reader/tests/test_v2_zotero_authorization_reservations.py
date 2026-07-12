from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from paper_reader.storage import canonical_json_bytes
from paper_reader.zotero_lock import locked_zotero_parent

from test_v2_review_package import _invoke, _result_payload, _write_summary_and_review
from test_v2_zotero_authorization import _authorize, _candidate as _candidate_for_orphan
from test_v2_zotero_candidate import InMemoryZoteroProvider, _build
from test_v2_zotero_init import FIXTURE_PDF, _bundle, _initialize


NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def _authorize_process(
    candidate: Path,
    start,
    results,
) -> None:
    start.wait()
    try:
        authorized = _authorize(
            candidate,
            InMemoryZoteroProvider(),
            now=NOW,
            ttl_seconds=60,
        )
    except Exception as exc:
        results.put(("error", getattr(exc, "code", type(exc).__name__)))
    else:
        results.put(("ok", authorized.authorization.authorization_id))


def _hold_parent_lock(run_dir: Path, entered, release) -> None:
    with locked_zotero_parent(run_dir, "PARENT1"):
        entered.set()
        release.wait(10)


def _enter_parent_lock(run_dir: Path, entered) -> None:
    with locked_zotero_parent(run_dir, "PARENT1"):
        entered.set()


def _sealed_candidate(run_dir: Path, provider: InMemoryZoteroProvider) -> Path:
    prepared = _invoke(["run", "prepare", str(run_dir), "--figure-limit", "0"])
    assert prepared.exit_code == 0, prepared.stderr
    evidence_digest = _result_payload(prepared)["data"]["evidence_digest"]
    _write_summary_and_review(run_dir, evidence_digest)
    sealed = _invoke(["review", "seal", str(run_dir)])
    assert sealed.exit_code == 0, sealed.stderr
    return _build(run_dir, provider).candidate_dir / "candidate.json"


def _two_candidates(
    tmp_path: Path,
) -> tuple[Path, Path, Path, InMemoryZoteroProvider]:
    pdf_path = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, pdf_path)
    bundle_path = tmp_path / "discovery.json"
    bundle_path.write_text(json.dumps(_bundle(pdf_path)), encoding="utf-8")
    skill_root = tmp_path / "installed-skill"
    skill_root.mkdir()
    first_run = _initialize(bundle_path, "PARENT1", skill_root).run_dir
    second_run = _initialize(bundle_path, "PARENT1", skill_root).run_dir
    provider = InMemoryZoteroProvider()
    first_candidate = _sealed_candidate(first_run, provider)
    second_candidate = _sealed_candidate(second_run, provider)
    first_title = json.loads(first_candidate.read_text(encoding="utf-8"))["note_title"]
    second_title = json.loads(second_candidate.read_text(encoding="utf-8"))["note_title"]
    assert first_title == second_title
    return skill_root, first_candidate, second_candidate, provider


def _reservation_paths(skill_root: Path) -> tuple[Path, ...]:
    ledger = skill_root / ".zotero-authorization-reservations"
    return tuple(sorted(ledger.glob("*/*/record.json"))) if ledger.exists() else ()


def _commitment_paths(skill_root: Path) -> tuple[Path, ...]:
    index = skill_root / ".zotero-authorization-reservation-index"
    return tuple(sorted(index.glob("*/*/record.json"))) if index.exists() else ()


def _remove_commitment_scope(record_path: Path, scope: str) -> None:
    if scope == "record_tree":
        shutil.rmtree(record_path.parent)
    elif scope == "parent_tree":
        shutil.rmtree(record_path.parent.parent)
    elif scope == "root_tree":
        shutil.rmtree(record_path.parent.parent.parent)
    else:  # pragma: no cover - parametrization is the closed set
        raise AssertionError(scope)


@pytest.mark.parametrize("missing_side", ["ledger", "index"])
@pytest.mark.parametrize("missing_scope", ["record_tree", "parent_tree", "root_tree"])
def test_single_side_commitment_loss_is_repaired_before_active_collision_check(
    missing_side: str,
    missing_scope: str,
    tmp_path: Path,
) -> None:
    skill_root, first_candidate, second_candidate, provider = _two_candidates(tmp_path)
    first = _authorize(first_candidate, provider, now=NOW, ttl_seconds=60)
    ledger_path = _reservation_paths(skill_root)[0]
    index_path = _commitment_paths(skill_root)[0]
    expected_record = ledger_path.read_bytes()
    expected_witness = (ledger_path.parent / "witness.json").read_bytes()
    assert index_path.read_bytes() == expected_record
    assert (index_path.parent / "witness.json").read_bytes() == expected_witness

    missing_path = ledger_path if missing_side == "ledger" else index_path
    _remove_commitment_scope(missing_path, missing_scope)

    class NetworkForbiddenProvider:
        def get_parent(self, _item_key: str):
            raise AssertionError("single-side recovery reached Zotero readback")

        def get_children(self, _parent_key: str):
            raise AssertionError("single-side recovery reached Zotero readback")

    with pytest.raises(Exception) as exc_info:
        _authorize(
            second_candidate,
            NetworkForbiddenProvider(),
            now=NOW + timedelta(seconds=30),
            ttl_seconds=60,
        )

    assert getattr(exc_info.value, "code", None) == "authorization_active"
    repaired_ledger = _reservation_paths(skill_root)
    repaired_index = _commitment_paths(skill_root)
    assert len(repaired_ledger) == len(repaired_index) == 1
    assert repaired_ledger[0].read_bytes() == expected_record
    assert repaired_index[0].read_bytes() == expected_record
    assert (repaired_ledger[0].parent / "witness.json").read_bytes() == expected_witness
    assert (repaired_index[0].parent / "witness.json").read_bytes() == expected_witness
    assert json.loads(expected_record)["authorization_id"] == first.authorization.authorization_id


def test_active_reservation_blocks_same_parent_and_exact_title_across_runs_before_readback(
    tmp_path: Path,
) -> None:
    skill_root, first_candidate, second_candidate, provider = _two_candidates(tmp_path)
    first = _authorize(first_candidate, provider, now=NOW, ttl_seconds=60)
    second_run = second_candidate.parent.parent.parent
    run_before = (second_run / "run.json").read_bytes()

    class NetworkForbiddenProvider:
        def get_parent(self, _item_key: str):
            raise AssertionError("active cross-run reservation reached Zotero readback")

        def get_children(self, _parent_key: str):
            raise AssertionError("active cross-run reservation reached Zotero readback")

    with pytest.raises(Exception) as exc_info:
        _authorize(
            second_candidate,
            NetworkForbiddenProvider(),
            now=NOW + timedelta(seconds=30),
            ttl_seconds=60,
        )

    assert getattr(exc_info.value, "code", None) == "authorization_active"
    assert exc_info.value.data["authorization_id"] == first.authorization.authorization_id
    assert exc_info.value.data["run_id"] == first.authorization.run_id
    assert (second_run / "run.json").read_bytes() == run_before
    records = _reservation_paths(skill_root)
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record == {
        "authorization_digest": first.authorization_digest,
        "authorization_id": first.authorization.authorization_id,
        "candidate_digest": first.authorization.candidate_digest,
        "created_at": "2026-07-10T12:00:00Z",
        "expires_at": "2026-07-10T12:01:00Z",
        "note_title": first.authorization.note_title,
        "parent_key": "PARENT1",
        "reservation_digest": record["reservation_digest"],
        "reservation_id": record["reservation_id"],
        "run_id": first.authorization.run_id,
        "schema_version": "paper_reader.zotero-authorization-reservation.v2",
        "ttl_seconds": 60,
    }
    assert records[0].name == "record.json"
    assert records[0].parent.name == record["reservation_id"]
    assert (records[0].parent / "witness.json").is_file()
    assert records[0].read_bytes() == canonical_json_bytes(record)


def test_expired_reservation_is_ignored_but_never_deleted_or_modified(
    tmp_path: Path,
) -> None:
    skill_root, first_candidate, second_candidate, provider = _two_candidates(tmp_path)
    first = _authorize(first_candidate, provider, now=NOW, ttl_seconds=60)
    first_path = _reservation_paths(skill_root)[0]
    first_bytes = first_path.read_bytes()
    first_stat = first_path.stat()
    first_witness = first_path.parent / "witness.json"
    witness_bytes = first_witness.read_bytes()
    witness_stat = first_witness.stat()

    second = _authorize(
        second_candidate,
        provider,
        now=NOW + timedelta(seconds=61),
        ttl_seconds=30,
    )

    assert second.authorization.authorization_id != first.authorization.authorization_id
    records = _reservation_paths(skill_root)
    assert len(records) == 2
    assert first_path in records
    assert first_path.read_bytes() == first_bytes
    assert first_path.stat().st_ino == first_stat.st_ino
    assert first_path.stat().st_mtime_ns == first_stat.st_mtime_ns
    assert first_witness.read_bytes() == witness_bytes
    assert first_witness.stat().st_ino == witness_stat.st_ino
    assert first_witness.stat().st_mtime_ns == witness_stat.st_mtime_ns


def test_concurrent_cross_run_authorization_creates_only_one_active_reservation(
    tmp_path: Path,
) -> None:
    skill_root, first_candidate, second_candidate, provider = _two_candidates(tmp_path)

    def authorize(candidate: Path):
        try:
            return _authorize(candidate, provider, now=NOW, ttl_seconds=60)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(authorize, (first_candidate, second_candidate)))

    successes = [item for item in outcomes if hasattr(item, "authorization")]
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert getattr(failures[0], "code", None) == "authorization_active"
    assert len(_reservation_paths(skill_root)) == 1


def test_cross_process_authorization_creates_only_one_active_reservation(
    tmp_path: Path,
) -> None:
    skill_root, first_candidate, second_candidate, _provider = _two_candidates(tmp_path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_authorize_process,
            args=(candidate, start, results),
        )
        for candidate in (first_candidate, second_candidate)
    ]
    for process in processes:
        process.start()
    start.set()
    outcomes = [results.get(timeout=30) for _process in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert sorted(kind for kind, _value in outcomes) == ["error", "ok"]
    assert [value for kind, value in outcomes if kind == "error"] == [
        "authorization_active"
    ]
    assert len(_reservation_paths(skill_root)) == 1


def test_parent_lock_inode_replacement_does_not_split_the_lock_domain(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "installed-skill" / "runs" / "2026-07-10" / "paper"
    run_dir.mkdir(parents=True)
    context = multiprocessing.get_context("spawn")
    first_entered = context.Event()
    release_first = context.Event()
    second_entered = context.Event()
    first = context.Process(
        target=_hold_parent_lock,
        args=(run_dir, first_entered, release_first),
    )
    first.start()
    assert first_entered.wait(10)

    filename = hashlib.sha256(b"PARENT1").hexdigest() + ".lock"
    lock_path = run_dir.parent.parent.parent / ".zotero-parent-locks" / filename
    lock_path.unlink()
    lock_path.write_bytes(b"replacement")
    second = context.Process(
        target=_enter_parent_lock,
        args=(run_dir, second_entered),
    )
    second.start()
    try:
        assert not second_entered.wait(0.5)
    finally:
        release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert first.exitcode != 0
    assert second.exitcode == 0


@pytest.mark.parametrize("unsafe_kind", ["symlink_directory", "hardlink_file"])
def test_parent_lock_rejects_symlink_directory_and_hardlink_file(
    unsafe_kind: str,
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "installed-skill"
    run_dir = skill_root / "runs" / "2026-07-10" / "paper"
    run_dir.mkdir(parents=True)
    lock_dir = skill_root / ".zotero-parent-locks"
    outside = tmp_path / "outside"
    outside.mkdir()
    if unsafe_kind == "symlink_directory":
        lock_dir.symlink_to(outside, target_is_directory=True)
    else:
        lock_dir.mkdir()
        outside_lock = outside / "outside.lock"
        outside_lock.write_bytes(b"outside")
        lock_name = hashlib.sha256(b"PARENT1").hexdigest() + ".lock"
        os.link(outside_lock, lock_dir / lock_name)

    with pytest.raises(Exception) as exc_info:
        with locked_zotero_parent(run_dir, "PARENT1"):
            pass

    assert getattr(exc_info.value, "code", None) == "authorization_lock_unsafe"


def test_parent_lock_rejects_run_replacement_before_creating_lock_artifacts(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "installed-skill"
    run_dir = skill_root / "runs" / "2026-07-10" / "paper"
    run_dir.mkdir(parents=True)
    expected = run_dir.stat()
    detached = tmp_path / "detached-run"
    run_dir.rename(detached)
    run_dir.mkdir()

    with pytest.raises(Exception) as exc_info:
        with locked_zotero_parent(
            run_dir,
            "PARENT1",
            expected_run_path=run_dir,
            expected_run_device=expected.st_dev,
            expected_run_inode=expected.st_ino,
        ):
            pass

    assert getattr(exc_info.value, "code", None) == "run_directory_changed"
    assert not (skill_root / ".zotero-parent-locks").exists()


def test_durable_reservation_survives_authorization_publication_failure_and_blocks_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.zotero_authorization as module

    skill_root, first_candidate, second_candidate, provider = _two_candidates(tmp_path)
    first_run = first_candidate.parent.parent.parent
    first_run_before = (first_run / "run.json").read_bytes()

    def fail_publication(*_args, **_kwargs):
        raise OSError("injected authorization publication failure")

    monkeypatch.setattr(module, "anchored_artifact_publication", fail_publication)

    with pytest.raises(Exception) as first_error:
        _authorize(first_candidate, provider, now=NOW, ttl_seconds=60)

    assert getattr(first_error.value, "code", None) == "authorization_publication_failed"
    assert (first_run / "run.json").read_bytes() == first_run_before
    assert len(_reservation_paths(skill_root)) == 1

    class NetworkForbiddenProvider:
        def get_parent(self, _item_key: str):
            raise AssertionError("durable failed-attempt reservation reached Zotero readback")

        def get_children(self, _parent_key: str):
            raise AssertionError("durable failed-attempt reservation reached Zotero readback")

    with pytest.raises(Exception) as retry_error:
        _authorize(
            second_candidate,
            NetworkForbiddenProvider(),
            now=NOW + timedelta(seconds=30),
            ttl_seconds=60,
        )

    assert getattr(retry_error.value, "code", None) == "authorization_active"
    assert len(_reservation_paths(skill_root)) == 1


def test_atomic_reservation_commit_survives_post_rename_crash_and_blocks_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.zotero_authorization_reservations as reservations_module

    skill_root, first_candidate, second_candidate, provider = _two_candidates(tmp_path)
    first_run = first_candidate.parent.parent.parent
    run_before = (first_run / "run.json").read_bytes()
    original_publish = reservations_module.atomic_publish_tree

    def publish_then_crash(*args, **kwargs):
        original_publish(*args, **kwargs)
        raise OSError("injected crash after reservation rename")

    monkeypatch.setattr(
        reservations_module,
        "atomic_publish_tree",
        publish_then_crash,
    )
    with pytest.raises(Exception) as first_error:
        _authorize(first_candidate, provider, now=NOW, ttl_seconds=60)

    assert getattr(first_error.value, "code", None) == "authorization_reservation_failed"
    assert (first_run / "run.json").read_bytes() == run_before
    assert len(_reservation_paths(skill_root)) == 1
    monkeypatch.setattr(
        reservations_module,
        "atomic_publish_tree",
        original_publish,
    )

    class NetworkForbiddenProvider:
        def get_parent(self, _item_key: str):
            raise AssertionError("durable post-rename reservation reached Zotero readback")

        def get_children(self, _parent_key: str):
            raise AssertionError("durable post-rename reservation reached Zotero readback")

    with pytest.raises(Exception) as retry_error:
        _authorize(
            second_candidate,
            NetworkForbiddenProvider(),
            now=NOW + timedelta(seconds=1),
        )

    assert getattr(retry_error.value, "code", None) == "authorization_active"
    assert len(_reservation_paths(skill_root)) == 1
    assert len(_commitment_paths(skill_root)) == 1


@pytest.mark.parametrize(
    ("failed_side", "failed_phase", "reservation_is_durable"),
    [
        ("ledger", "before_rename", False),
        ("ledger", "after_rename", True),
        ("index", "before_rename", True),
        ("index", "after_rename", True),
    ],
)
def test_each_commitment_publish_fault_converges_without_duplicate_active_grant(
    failed_side: str,
    failed_phase: str,
    reservation_is_durable: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.zotero_authorization_reservations as reservations_module

    skill_root, first_candidate, second_candidate, provider = _two_candidates(tmp_path)
    original_publish = reservations_module.atomic_publish_tree
    failed = False

    def fail_selected_publish(staging: Path, destination: Path, **kwargs):
        nonlocal failed
        side = (
            "index"
            if ".zotero-authorization-reservation-index" in Path(destination).parts
            else "ledger"
        )
        if not failed and side == failed_side:
            failed = True
            if failed_phase == "before_rename":
                raise OSError("injected commitment failure before rename")
            original_publish(staging, destination, **kwargs)
            raise OSError("injected commitment failure after rename")
        return original_publish(staging, destination, **kwargs)

    monkeypatch.setattr(
        reservations_module,
        "atomic_publish_tree",
        fail_selected_publish,
    )
    with pytest.raises(Exception) as first_error:
        _authorize(first_candidate, provider, now=NOW, ttl_seconds=60)
    assert getattr(first_error.value, "code", None) == "authorization_reservation_failed"
    assert failed is True
    monkeypatch.setattr(
        reservations_module,
        "atomic_publish_tree",
        original_publish,
    )

    if reservation_is_durable:
        class NetworkForbiddenProvider:
            def get_parent(self, _item_key: str):
                raise AssertionError("durable commitment retry reached Zotero readback")

            def get_children(self, _parent_key: str):
                raise AssertionError("durable commitment retry reached Zotero readback")

        with pytest.raises(Exception) as retry_error:
            _authorize(
                second_candidate,
                NetworkForbiddenProvider(),
                now=NOW + timedelta(seconds=1),
                ttl_seconds=60,
            )
        assert getattr(retry_error.value, "code", None) == "authorization_active"
    else:
        retry = _authorize(
            second_candidate,
            provider,
            now=NOW + timedelta(seconds=1),
            ttl_seconds=60,
        )
        assert retry.authorization.run_id

    ledger = _reservation_paths(skill_root)
    index = _commitment_paths(skill_root)
    assert len(ledger) == len(index) == 1
    assert ledger[0].read_bytes() == index[0].read_bytes()
    assert (ledger[0].parent / "witness.json").read_bytes() == (
        index[0].parent / "witness.json"
    ).read_bytes()

def test_tampered_reservation_fails_closed_before_provider_or_run_mutation(
    tmp_path: Path,
) -> None:
    skill_root, first_candidate, second_candidate, provider = _two_candidates(tmp_path)
    _authorize(first_candidate, provider, now=NOW, ttl_seconds=1)
    reservation_path = _reservation_paths(skill_root)[0]
    reservation_path.write_bytes(b'{"schema_version":"tampered"}')
    second_run = second_candidate.parent.parent.parent
    run_before = (second_run / "run.json").read_bytes()

    class NetworkForbiddenProvider:
        def get_parent(self, _item_key: str):
            raise AssertionError("tampered reservation reached Zotero readback")

        def get_children(self, _parent_key: str):
            raise AssertionError("tampered reservation reached Zotero readback")

    with pytest.raises(Exception) as exc_info:
        _authorize(
            second_candidate,
            NetworkForbiddenProvider(),
            now=NOW + timedelta(seconds=2),
        )

    assert getattr(exc_info.value, "code", None) == "authorization_reservation_tampered"
    assert (second_run / "run.json").read_bytes() == run_before
    assert reservation_path.read_bytes() == b'{"schema_version":"tampered"}'


@pytest.mark.parametrize(
    "root_name",
    [
        ".zotero-authorization-reservations",
        ".zotero-authorization-reservation-index",
    ],
)
def test_unsafe_commitment_root_is_classified_as_tamper_before_provider(
    root_name: str,
    tmp_path: Path,
) -> None:
    skill_root, first_candidate, second_candidate, provider = _two_candidates(tmp_path)
    _authorize(first_candidate, provider, now=NOW, ttl_seconds=60)
    root = skill_root / root_name
    detached = skill_root / f"{root_name}.detached"
    outside = tmp_path / f"{root_name}.outside"
    outside.mkdir()
    root.rename(detached)
    root.symlink_to(outside, target_is_directory=True)

    class NetworkForbiddenProvider:
        def get_parent(self, _item_key: str):
            raise AssertionError("unsafe commitment root reached Zotero readback")

        def get_children(self, _parent_key: str):
            raise AssertionError("unsafe commitment root reached Zotero readback")

    with pytest.raises(Exception) as exc_info:
        _authorize(
            second_candidate,
            NetworkForbiddenProvider(),
            now=NOW + timedelta(seconds=30),
            ttl_seconds=60,
        )

    assert getattr(exc_info.value, "code", None) == "authorization_reservation_tampered"


def test_canonical_reservation_rewrite_fails_closed_before_provider_or_run_mutation(
    tmp_path: Path,
) -> None:
    skill_root, first_candidate, second_candidate, provider = _two_candidates(tmp_path)
    _authorize(first_candidate, provider, now=NOW, ttl_seconds=60)
    reservation_path = _reservation_paths(skill_root)[0]
    rewritten = json.loads(reservation_path.read_bytes())
    rewritten["note_title"] = f"{rewritten['note_title']} tampered"
    reservation_path.write_bytes(canonical_json_bytes(rewritten))
    second_run = second_candidate.parent.parent.parent
    run_before = (second_run / "run.json").read_bytes()

    class NetworkForbiddenProvider:
        def get_parent(self, _item_key: str):
            raise AssertionError("canonical reservation tamper reached Zotero readback")

        def get_children(self, _parent_key: str):
            raise AssertionError("canonical reservation tamper reached Zotero readback")

    with pytest.raises(Exception) as exc_info:
        _authorize(
            second_candidate,
            NetworkForbiddenProvider(),
            now=NOW + timedelta(seconds=30),
        )

    assert getattr(exc_info.value, "code", None) == "authorization_reservation_tampered"
    assert (second_run / "run.json").read_bytes() == run_before
    assert len(_reservation_paths(skill_root)) == 1


def test_deleted_reservation_record_fails_closed_before_provider_or_run_mutation(
    tmp_path: Path,
) -> None:
    skill_root, first_candidate, second_candidate, provider = _two_candidates(tmp_path)
    _authorize(first_candidate, provider, now=NOW, ttl_seconds=60)
    reservation_path = _reservation_paths(skill_root)[0]
    reservation_path.unlink()
    second_run = second_candidate.parent.parent.parent
    run_before = (second_run / "run.json").read_bytes()

    class NetworkForbiddenProvider:
        def get_parent(self, _item_key: str):
            raise AssertionError("deleted reservation reached Zotero readback")

        def get_children(self, _parent_key: str):
            raise AssertionError("deleted reservation reached Zotero readback")

    with pytest.raises(Exception) as exc_info:
        _authorize(
            second_candidate,
            NetworkForbiddenProvider(),
            now=NOW + timedelta(seconds=30),
        )

    assert getattr(exc_info.value, "code", None) == "authorization_reservation_tampered"
    assert (second_run / "run.json").read_bytes() == run_before


def test_tampered_reservation_blocks_orphan_recovery_before_run_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.zotero_authorization as module

    candidate_path, provider = _candidate_for_orphan(tmp_path)
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
        _authorize(candidate_path, provider, now=NOW, ttl_seconds=60)
    assert getattr(first_error.value, "code", None) == "authorization_status_update_failed"
    reservation_path = _reservation_paths(run_dir.parent.parent.parent)[0]
    reservation_path.write_bytes(b'{"schema_version":"tampered"}')
    run_before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(module, "atomic_write_json", original_write)

    with pytest.raises(Exception) as retry_error:
        _authorize(candidate_path, provider, now=NOW + timedelta(seconds=1))

    assert getattr(retry_error.value, "code", None) == "authorization_reservation_tampered"
    assert (run_dir / "run.json").read_bytes() == run_before
