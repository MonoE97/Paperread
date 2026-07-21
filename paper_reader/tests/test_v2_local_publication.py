from __future__ import annotations

import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
import paper_reader.candidate_integrity as candidate_integrity
import paper_reader.local_publish as local_publish_module
import paper_reader.storage as storage_module
from paper_reader.contracts import PaperReaderCandidate

from test_v2_review_package import (
    _invoke,
    _prepared_run,
    _result_payload,
    _write_summary_and_review,
)


def _sealed_run(tmp_path: Path) -> Path:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    sealed = _invoke(["review", "seal", str(run_dir)])
    assert sealed.exit_code == 0, sealed.stderr
    return run_dir


def test_candidate_build_rehashes_sealed_inputs_and_publishes_immutable_local_candidate(
    tmp_path: Path,
) -> None:
    run_dir = _sealed_run(tmp_path)

    result = _invoke(["candidate", "build", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["code"] == "candidate_built"
    candidate_dir = Path(payload["data"]["candidate_dir"])
    assert candidate_dir.parent == run_dir / "candidates"
    assert sorted(path.name for path in candidate_dir.iterdir()) == [
        "candidate.json",
        "evidence.json",
        "note.html",
        "note.md",
        "review-package.json",
        "review.json",
        "run.json",
        "source.json",
        "summary.json",
        "validation.json",
    ]
    candidate = PaperReaderCandidate.model_validate_json(
        (candidate_dir / "candidate.json").read_bytes()
    )
    assert payload["data"]["candidate_digest"] == candidate_integrity.candidate_core_digest(candidate)
    assert payload["data"]["candidate_digest"] == hashlib.sha256(
        (candidate_dir / "candidate.json").read_bytes()
    ).hexdigest()
    assert candidate.target.target_type == "local"
    assert candidate.target.resolved_path == str(tmp_path / "paper_note.md")
    assert candidate.source.source_type == "local_pdf"
    assert candidate.gate.status == "write_ready"
    assert candidate.live_preflight is None
    note_bytes = (candidate_dir / "note.md").read_bytes()
    assert candidate.content_sha256 == hashlib.sha256(note_bytes).hexdigest()
    assert candidate.content_length == len(note_bytes)
    assert candidate.note_title.startswith("[Codex Summary] paper - ")
    assert set(candidate.tags) >= {"codex-summary", "paper-summary"}
    for artifact in candidate.artifacts:
        path = run_dir / artifact.path
        assert path.is_file()
        assert artifact.size_bytes == path.stat().st_size
        assert artifact.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "candidate_built"
    assert any(item["role"] == "candidate" for item in run["artifacts"])
    forbidden = {
        path.name
        for path in run_dir.rglob("*")
        if path.name in {"write-payload.json", "authorization.json", "live-notes.json"}
    }
    assert forbidden == set()


def test_concurrent_local_candidate_builds_preserve_both_immutable_bindings(
    tmp_path: Path,
) -> None:
    from paper_reader.candidate_builder import build_local_candidate

    run_dir = _sealed_run(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: build_local_candidate(run_dir), range(2)))

    assert len({item.candidate_dir for item in outcomes}) == 2
    assert len({item.candidate.candidate_id for item in outcomes}) == 2
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    bound = [item for item in run["artifacts"] if item["role"] == "candidate"]
    assert len(bound) == 2
    assert {run_dir / item["path"] for item in bound} == {
        outcome.candidate_dir / "candidate.json" for outcome in outcomes
    }


@pytest.mark.parametrize("extra_path", ["extra.bin", "unreferenced/extra.bin"])
def test_local_publish_rejects_unreferenced_candidate_tree_members(
    extra_path: str,
    tmp_path: Path,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    extra = candidate_path.parent / extra_path
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"unreferenced candidate bytes")
    run_before = (run_dir / "run.json").read_bytes()

    with pytest.raises(Exception) as exc_info:
        local_publish_module.publish_local_candidate(candidate_path)

    assert getattr(exc_info.value, "code", None) == "candidate_tampered"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (tmp_path / "paper_note.md").exists()
    assert not (run_dir / "receipts").exists()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_local_publish_rejects_unreferenced_candidate_tree_links(
    link_kind: str,
    tmp_path: Path,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    outside = tmp_path / "outside-extra.bin"
    outside.write_bytes(b"outside bytes")
    extra = candidate_path.parent / "extra.bin"
    if link_kind == "symlink":
        extra.symlink_to(outside)
    else:
        os.link(outside, extra)
    run_before = (run_dir / "run.json").read_bytes()

    with pytest.raises(Exception) as exc_info:
        local_publish_module.publish_local_candidate(candidate_path)

    assert getattr(exc_info.value, "code", None) == "candidate_tampered"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert outside.read_bytes() == b"outside bytes"
    assert not (tmp_path / "paper_note.md").exists()
    assert not (run_dir / "receipts").exists()


def test_candidate_build_rejects_unreferenced_member_in_existing_candidate(
    tmp_path: Path,
) -> None:
    from paper_reader.candidate_builder import build_local_candidate

    run_dir = _sealed_run(tmp_path)
    first = build_local_candidate(run_dir)
    (first.candidate_dir / "extra.bin").write_bytes(b"unreferenced candidate bytes")

    with pytest.raises(Exception) as exc_info:
        build_local_candidate(run_dir)

    assert getattr(exc_info.value, "code", None) == "candidate_tampered"
    assert sorted((run_dir / "candidates").iterdir()) == [first.candidate_dir]


def test_local_candidate_retry_rejects_dropped_immutable_candidate_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.candidate_builder as module

    run_dir = _sealed_run(tmp_path)
    first = module.build_local_candidate(run_dir)
    original_locked_v2_run = module.locked_v2_run
    changed = False

    @contextmanager
    def drop_binding_before_lock(path: Path, **kwargs):
        nonlocal changed
        if not changed:
            payload = json.loads((run_dir / "run.json").read_bytes())
            payload["artifacts"] = [
                item for item in payload["artifacts"] if item["role"] != "candidate"
            ]
            (run_dir / "run.json").write_bytes(
                storage_module.canonical_json_bytes(payload)
            )
            changed = True
        with original_locked_v2_run(path, **kwargs) as loaded:
            yield loaded

    monkeypatch.setattr(module, "locked_v2_run", drop_binding_before_lock)

    with pytest.raises(Exception) as exc_info:
        module.build_local_candidate(run_dir)

    assert getattr(exc_info.value, "code", None) == "sealed_artifact_tampered"
    assert changed is True
    assert sorted((run_dir / "candidates").iterdir()) == [first.candidate_dir]


def test_local_candidate_retry_rejects_existing_candidate_child_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.candidate_builder as module

    run_dir = _sealed_run(tmp_path)
    first = module.build_local_candidate(run_dir)
    child_path = first.candidate_dir / "note.html"
    original_locked_v2_run = module.locked_v2_run
    changed = False

    @contextmanager
    def change_child_before_lock(path: Path, **kwargs):
        nonlocal changed
        if not changed:
            child_path.write_bytes(child_path.read_bytes() + b"\nchanged")
            changed = True
        with original_locked_v2_run(path, **kwargs) as loaded:
            yield loaded

    monkeypatch.setattr(module, "locked_v2_run", change_child_before_lock)

    with pytest.raises(Exception) as exc_info:
        module.build_local_candidate(run_dir)

    assert getattr(exc_info.value, "code", None) in {
        "candidate_tampered",
        "sealed_artifact_tampered",
    }
    assert changed is True
    assert sorted((run_dir / "candidates").iterdir()) == [first.candidate_dir]


def test_candidate_preflight_detects_child_swap_after_member_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.candidate_builder as module

    run_dir = _sealed_run(tmp_path)
    first = module.build_local_candidate(run_dir)
    child_path = first.candidate_dir / "note.html"
    original_load_candidate = local_publish_module._load_candidate
    changed = False

    def load_then_change_child(*args, **kwargs):
        nonlocal changed
        result = original_load_candidate(*args, **kwargs)
        if not changed and result[1] == first.candidate_dir / "candidate.json":
            child_path.write_bytes(child_path.read_bytes() + b"\nchanged-after-load")
            changed = True
        return result

    monkeypatch.setattr(
        local_publish_module,
        "_load_candidate",
        load_then_change_child,
    )

    with pytest.raises(Exception) as exc_info:
        module.build_local_candidate(run_dir)

    assert getattr(exc_info.value, "code", None) == "sealed_artifact_tampered"
    assert changed is True
    assert sorted((run_dir / "candidates").iterdir()) == [first.candidate_dir]


def test_candidate_build_blocks_sealed_snapshot_tamper(
    tmp_path: Path,
) -> None:
    sealed_case = tmp_path / "sealed"
    sealed_case.mkdir()
    run_dir = _sealed_run(sealed_case)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    package_ref = next(item for item in run["artifacts"] if item["role"] == "review_package")
    package_dir = (run_dir / package_ref["path"]).parent
    (package_dir / "note.md").write_text("tampered sealed note", encoding="utf-8")

    sealed_result = _invoke(["candidate", "build", str(run_dir)])

    assert sealed_result.exit_code == 1
    assert _result_payload(sealed_result)["code"] == "sealed_artifact_tampered"
    assert not (run_dir / "candidates").exists()


def test_candidate_build_uses_captured_review_package_bytes_after_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.candidate_builder as candidate_builder

    run_dir = _sealed_run(tmp_path)
    original_latest = candidate_builder._latest_review_package
    captured: dict[str, bytes] = {}

    def latest_then_swap(loaded):
        result = original_latest(loaded)
        package_path = result[1]
        captured["package"] = package_path.read_bytes()
        package_path.write_bytes(b"review package swapped after verification")
        return result

    monkeypatch.setattr(candidate_builder, "_latest_review_package", latest_then_swap)

    result = _invoke(["candidate", "build", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    candidate_dir = Path(_result_payload(result)["data"]["candidate_dir"])
    assert (candidate_dir / "review-package.json").read_bytes() == captured["package"]


def test_candidate_build_rejects_run_manifest_path_swap_after_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.candidate_builder as candidate_builder

    run_dir = _sealed_run(tmp_path)
    original_snapshots = candidate_builder._sealed_snapshots

    def snapshots_then_swap(*args, **kwargs):
        snapshots = original_snapshots(*args, **kwargs)
        (run_dir / "run.json").write_bytes(b"run manifest swapped after loading")
        return snapshots

    monkeypatch.setattr(candidate_builder, "_sealed_snapshots", snapshots_then_swap)

    result = _invoke(["candidate", "build", str(run_dir)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "candidate_status_update_failed"
    assert (run_dir / "run.json").read_bytes() == b"run manifest swapped after loading"


def test_candidate_build_blocks_original_evidence_tamper(tmp_path: Path) -> None:
    evidence_case = tmp_path / "evidence"
    evidence_case.mkdir()
    run_dir = _sealed_run(evidence_case)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    evidence_ref = next(item for item in run["artifacts"] if item["role"] == "evidence_manifest")
    evidence = json.loads((run_dir / evidence_ref["path"]).read_text(encoding="utf-8"))
    context_ref = next(item for item in evidence["files"] if item["role"] == "context")
    (run_dir / context_ref["path"]).write_text("tampered original evidence", encoding="utf-8")

    evidence_result = _invoke(["candidate", "build", str(run_dir)])

    assert evidence_result.exit_code == 1
    assert _result_payload(evidence_result)["code"] == "evidence_artifact_hash_mismatch"
    assert not (run_dir / "candidates").exists()


def test_local_publish_revalidates_original_bound_evidence_after_candidate_build(
    tmp_path: Path,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    evidence_ref = next(
        item for item in run["artifacts"] if item["role"] == "evidence_manifest"
    )
    evidence = json.loads(
        (run_dir / evidence_ref["path"]).read_text(encoding="utf-8")
    )
    context_ref = next(item for item in evidence["files"] if item["role"] == "context")
    (run_dir / context_ref["path"]).write_text(
        "tampered after candidate build",
        encoding="utf-8",
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "evidence_artifact_hash_mismatch"
    assert not (tmp_path / "paper_note.md").exists()
    assert not (run_dir / "publication-intent.json").exists()
    assert not (run_dir / "receipts").exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"


def test_local_publish_revalidates_original_evidence_at_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    evidence_ref = next(
        item for item in run["artifacts"] if item["role"] == "evidence_manifest"
    )
    evidence_path = run_dir / evidence_ref["path"]
    original_verify_source = local_publish_module.verify_local_source
    tampered = False

    def verify_source_then_tamper(source) -> None:
        nonlocal tampered
        original_verify_source(source)
        if not tampered:
            evidence_path.write_bytes(b"{}")
            tampered = True

    monkeypatch.setattr(
        local_publish_module,
        "verify_local_source",
        verify_source_then_tamper,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert tampered is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "evidence_artifact_hash_mismatch"
    assert not (tmp_path / "paper_note.md").exists()
    assert not (run_dir / "publication-intent.json").exists()
    assert not (run_dir / "receipts").exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"


def test_local_publish_revalidates_original_evidence_after_target_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    evidence_ref = next(
        item for item in run["artifacts"] if item["role"] == "evidence_manifest"
    )
    evidence_path = run_dir / evidence_ref["path"]
    original_receipt = local_publish_module._publish_or_verify_receipt
    tampered = False

    def publish_receipt_then_tamper(*args, **kwargs):
        nonlocal tampered
        result = original_receipt(*args, **kwargs)
        if not tampered:
            evidence_path.write_bytes(b"{}")
            tampered = True
        return result

    monkeypatch.setattr(
        local_publish_module,
        "_publish_or_verify_receipt",
        publish_receipt_then_tamper,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert tampered is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "evidence_artifact_hash_mismatch"
    assert (tmp_path / "paper_note.md").exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"


def test_local_publish_holds_original_evidence_through_final_source_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    evidence_ref = next(
        artifact
        for artifact in run["artifacts"]
        if artifact["role"] == "evidence_manifest"
    )
    evidence_path = run_dir / evidence_ref["path"]
    original_verify_source = local_publish_module._verify_held_source
    drifted = False
    published_verifications = 0

    def verify_source_then_drift_evidence(source_guard) -> None:
        nonlocal drifted, published_verifications
        original_verify_source(source_guard)
        current = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        if current["status"] == "published":
            published_verifications += 1
        if published_verifications == 2 and not drifted:
            evidence_path.write_bytes(b"{}")
            drifted = True

    monkeypatch.setattr(
        local_publish_module,
        "_verify_held_source",
        verify_source_then_drift_evidence,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert drifted is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "evidence_artifact_hash_mismatch"
    assert (tmp_path / "paper_note.md").exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "published"


def test_candidate_build_revalidates_source_target_and_atomic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_case = tmp_path / "source"
    source_case.mkdir()
    run_dir = _sealed_run(source_case)
    source = source_case / "paper.pdf"
    source.write_bytes(source.read_bytes() + b"\n% drift")
    source_result = _invoke(["candidate", "build", str(run_dir)])
    assert source_result.exit_code == 1
    assert _result_payload(source_result)["code"] == "source_changed"
    assert not (run_dir / "candidates").exists()

    target_case = tmp_path / "target"
    target_case.mkdir()
    run_dir = _sealed_run(target_case)
    (target_case / "paper_note.md").write_text("occupied", encoding="utf-8")
    target_result = _invoke(["candidate", "build", str(run_dir)])
    assert target_result.exit_code == 1
    assert _result_payload(target_result)["code"] == "publish_conflict"
    assert not (run_dir / "candidates").exists()

    fault_case = tmp_path / "fault"
    fault_case.mkdir()
    run_dir = _sealed_run(fault_case)
    run_before = (run_dir / "run.json").read_bytes()

    def injected_failure(_staging: Path, _destination: Path) -> Path:
        raise OSError("injected candidate publication failure")

    monkeypatch.setattr("paper_reader.candidate_builder.atomic_publish_tree", injected_failure)
    fault_result = _invoke(["candidate", "build", str(run_dir)])
    assert fault_result.exit_code == 1
    assert _result_payload(fault_result)["code"] == "candidate_publication_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "candidates").exists()
    assert not list(run_dir.glob(".*.staging"))


def test_candidate_build_blocks_projected_run_size_before_tree_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.candidate_builder as candidate_builder
    from paper_reader.resource_policy import V2_RESOURCE_POLICY

    run_dir = _sealed_run(tmp_path)
    run_before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(
        candidate_builder,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, run_max_bytes=1),
        raising=False,
    )

    result = _invoke(["candidate", "build", str(run_dir)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "run_size_limit_exceeded"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "candidates").exists()


def test_candidate_run_update_fault_leaves_unbound_orphan_and_retry_binds_new_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.candidate_builder as candidate_builder

    run_dir = _sealed_run(tmp_path)
    run_before = (run_dir / "run.json").read_bytes()
    original_cas = candidate_builder.cas_update_run
    failed = False

    def fail_once(loaded, value, **kwargs):
        nonlocal failed
        if loaded.manifest_path.name == "run.json" and not failed:
            failed = True
            raise OSError("injected failure after candidate tree publication")
        return original_cas(loaded, value, **kwargs)

    monkeypatch.setattr(candidate_builder, "cas_update_run", fail_once)

    first = _invoke(["candidate", "build", str(run_dir)])

    assert first.exit_code == 1
    assert _result_payload(first)["code"] == "candidate_status_update_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    orphan_dirs = tuple((run_dir / "candidates").iterdir())
    assert len(orphan_dirs) == 1

    second = _invoke(["candidate", "build", str(run_dir)])

    assert second.exit_code == 0, second.stderr
    run = json.loads((run_dir / "run.json").read_text())
    bound_paths = {item["path"] for item in run["artifacts"] if item["role"] == "candidate"}
    assert len(bound_paths) == 1
    assert not any(path.startswith(orphan_dirs[0].relative_to(run_dir).as_posix()) for path in bound_paths)


@pytest.mark.parametrize("stage", ["candidate_build", "local_publish"])
def test_broken_symlink_fixed_target_blocks_both_publication_preflights(
    stage: str,
    tmp_path: Path,
) -> None:
    if stage == "candidate_build":
        run_dir = _sealed_run(tmp_path)
        candidate_path = None
    else:
        run_dir, candidate_path = _built_candidate(tmp_path)
    target = tmp_path / "paper_note.md"
    target.symlink_to(tmp_path / "missing-note.md")

    result = (
        _invoke(["candidate", "build", str(run_dir)])
        if candidate_path is None
        else _invoke(["local", "publish", str(candidate_path)])
    )

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publish_conflict"
    assert target.is_symlink()
    assert not (run_dir / "receipts").exists()


def _built_candidate(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = _sealed_run(tmp_path)
    built = _invoke(["candidate", "build", str(run_dir)])
    assert built.exit_code == 0, built.stderr
    return run_dir, Path(_result_payload(built)["data"]["candidate_path"])


def _replace_bound_path(value: object, old: str, new: str) -> object:
    if isinstance(value, dict):
        return {
            key: _replace_bound_path(item, old, new)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_bound_path(item, old, new) for item in value]
    if isinstance(value, str) and (value == old or value.startswith(f"{old}/")):
        return f"{new}{value[len(old):]}"
    return value


def _rewrite_candidate_and_run_binding(
    run_dir: Path,
    candidate_path: Path,
    *,
    old_bound_path: str,
    new_bound_path: str,
) -> None:
    candidate_payload = _replace_bound_path(
        json.loads(candidate_path.read_bytes()),
        old_bound_path,
        new_bound_path,
    )
    candidate_bytes = storage_module.canonical_json_bytes(candidate_payload)
    candidate_path.write_bytes(candidate_bytes)

    run_payload = _replace_bound_path(
        json.loads((run_dir / "run.json").read_bytes()),
        old_bound_path,
        new_bound_path,
    )
    candidate_relative = candidate_path.relative_to(run_dir).as_posix()
    bound_refs = [
        item
        for item in run_payload["artifacts"]
        if item["role"] == "candidate" and item["path"] == candidate_relative
    ]
    assert len(bound_refs) == 1
    bound_refs[0]["sha256"] = hashlib.sha256(candidate_bytes).hexdigest()
    bound_refs[0]["size_bytes"] = len(candidate_bytes)
    (run_dir / "run.json").write_bytes(
        storage_module.canonical_json_bytes(run_payload)
    )


def _publication_tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if path.is_symlink():
            snapshot[relative] = (
                "symlink",
                os.readlink(path),
                metadata.st_ino,
                metadata.st_mtime_ns,
            )
        elif path.is_file():
            snapshot[relative] = (
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
                metadata.st_size,
                metadata.st_ino,
                metadata.st_mtime_ns,
            )
        else:
            snapshot[relative] = (
                "directory",
                metadata.st_ino,
                metadata.st_mtime_ns,
            )
    return snapshot


@pytest.mark.parametrize(
    "topology_tamper",
    ["candidate_directory", "note_markdown_filename"],
)
def test_local_publish_rejects_rebound_noncanonical_candidate_topology_without_mutation(
    topology_tamper: str,
    tmp_path: Path,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    if topology_tamper == "candidate_directory":
        original_dir = candidate_path.parent
        original_relative = original_dir.relative_to(run_dir).as_posix()
        rebound_dir = original_dir.with_name("renamed-candidate")
        original_dir.rename(rebound_dir)
        candidate_path = rebound_dir / "candidate.json"
        _rewrite_candidate_and_run_binding(
            run_dir,
            candidate_path,
            old_bound_path=original_relative,
            new_bound_path=rebound_dir.relative_to(run_dir).as_posix(),
        )
    else:
        original_note = candidate_path.parent / "note.md"
        rebound_note = candidate_path.parent / "renamed-note.md"
        original_note.rename(rebound_note)
        _rewrite_candidate_and_run_binding(
            run_dir,
            candidate_path,
            old_bound_path=original_note.relative_to(run_dir).as_posix(),
            new_bound_path=rebound_note.relative_to(run_dir).as_posix(),
        )
    before = _publication_tree_snapshot(tmp_path)

    with pytest.raises(Exception) as exc_info:
        local_publish_module.publish_local_candidate(candidate_path)

    assert getattr(exc_info.value, "code", None) == "candidate_tampered"
    assert _publication_tree_snapshot(tmp_path) == before
    assert not (tmp_path / "paper_note.md").exists()


def test_candidate_build_does_not_clobber_unseen_valid_run_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.candidate_builder as module

    run_dir = _sealed_run(tmp_path)
    original_cas = module.cas_update_run
    external_bytes: bytes | None = None

    def mutate_current_then_cas(loaded, value, **kwargs):
        nonlocal external_bytes
        path = loaded.manifest_path
        if external_bytes is None:
            payload = json.loads(path.read_bytes())
            payload["created_at"] = "2026-07-10T00:00:01Z"
            external_bytes = storage_module.canonical_json_bytes(payload)
            path.write_bytes(external_bytes)
        return original_cas(loaded, value, **kwargs)

    monkeypatch.setattr(module, "cas_update_run", mutate_current_then_cas)

    with pytest.raises(Exception) as exc_info:
        module.build_local_candidate(run_dir)

    assert external_bytes is not None
    assert getattr(exc_info.value, "code", None) in {
        "candidate_status_update_failed",
        "run_directory_changed",
    }
    assert (run_dir / "run.json").read_bytes() == external_bytes


def test_local_publish_holds_source_identity_across_target_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    source_path = tmp_path / "paper.pdf"
    original_source = source_path.read_bytes()
    replacement = bytes([original_source[0] ^ 1]) + original_source[1:]
    original_intent = local_publish_module._publish_or_verify_intent
    changed = False

    def publish_intent_then_change_source(*args, **kwargs):
        nonlocal changed
        guard = original_intent(*args, **kwargs)
        if not changed:
            source_path.write_bytes(replacement)
            changed = True
        return guard

    monkeypatch.setattr(
        local_publish_module,
        "_publish_or_verify_intent",
        publish_intent_then_change_source,
    )

    with pytest.raises(Exception) as exc_info:
        local_publish_module.publish_local_candidate(candidate_path)

    assert changed is True
    assert getattr(exc_info.value, "code", None) == "source_changed"
    assert not (tmp_path / "paper_note.md").exists()
    assert json.loads((run_dir / "run.json").read_bytes())["status"] == "candidate_built"


def test_local_publish_holds_candidate_tree_across_target_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    note_html = candidate_path.parent / "note.html"
    original_intent = local_publish_module._publish_or_verify_intent
    changed = False

    def publish_intent_then_change_candidate(*args, **kwargs):
        nonlocal changed
        guard = original_intent(*args, **kwargs)
        if not changed:
            raw = note_html.read_bytes()
            note_html.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
            changed = True
        return guard

    monkeypatch.setattr(
        local_publish_module,
        "_publish_or_verify_intent",
        publish_intent_then_change_candidate,
    )

    with pytest.raises(Exception) as exc_info:
        local_publish_module.publish_local_candidate(candidate_path)

    assert changed is True
    assert getattr(exc_info.value, "code", None) == "candidate_tampered"
    assert not (tmp_path / "paper_note.md").exists()
    assert json.loads((run_dir / "run.json").read_bytes())["status"] == "candidate_built"


@pytest.mark.parametrize("failure_point", ["run_size", "target_anchor"])
def test_local_publish_closes_held_source_when_precommit_preflight_fails(
    failure_point: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    captured: list[tuple[int, int]] = []
    original_open_source = local_publish_module.open_resolved_source_guard

    def capture_source_guard(*args, **kwargs):
        guard = original_open_source(*args, **kwargs)
        captured.append((guard.descriptor, guard.parent.descriptor))
        return guard

    monkeypatch.setattr(
        local_publish_module,
        "open_resolved_source_guard",
        capture_source_guard,
    )
    if failure_point == "run_size":
        def fail_projected_size(*_args, **_kwargs):
            raise local_publish_module.RunSizeLimitError(
                actual_bytes=2,
                max_bytes=1,
            )

        monkeypatch.setattr(
            local_publish_module,
            "enforce_projected_run_size",
            fail_projected_size,
        )
    else:
        original_anchor_open = local_publish_module.DirectoryAnchor.open
        target = tmp_path / "paper_note.md"

        def fail_target_anchor(path: Path, *, manifest_path: Path):
            if Path(manifest_path) == target:
                raise local_publish_module.RunLoadError(
                    "run_manifest_unreadable",
                    "injected target anchor failure",
                    manifest_path=target,
                )
            return original_anchor_open(path, manifest_path=manifest_path)

        monkeypatch.setattr(
            local_publish_module.DirectoryAnchor,
            "open",
            fail_target_anchor,
        )

    with pytest.raises(Exception):
        local_publish_module.publish_local_candidate(candidate_path)

    assert captured
    for descriptor_pair in captured:
        for descriptor in descriptor_pair:
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            os.close(descriptor)
            pytest.fail(f"held source descriptor leaked after {failure_point}: {descriptor}")
    assert json.loads((run_dir / "run.json").read_bytes())["status"] == "candidate_built"


def _candidate_tree_snapshot(candidate_dir: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(candidate_dir).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(item for item in candidate_dir.rglob("*") if item.is_file())
    }


def test_local_publish_revalidates_and_atomically_copies_exact_candidate_markdown(
    tmp_path: Path,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    candidate_dir = candidate_path.parent
    candidate = PaperReaderCandidate.model_validate_json(candidate_path.read_bytes())
    candidate_before = _candidate_tree_snapshot(candidate_dir)
    note_bytes = (candidate_dir / "note.md").read_bytes()

    result = _invoke(["local", "publish", str(candidate_path)])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["code"] == "published"
    target = Path(candidate.target.resolved_path)
    assert payload["data"]["target_path"] == str(target)
    assert target.read_bytes() == note_bytes
    assert hashlib.sha256(target.read_bytes()).hexdigest() == candidate.content_sha256
    assert not os.path.samefile(target, candidate_dir / "note.md")
    assert not os.path.samefile(target, tmp_path / "paper.pdf")
    assert _candidate_tree_snapshot(candidate_dir) == candidate_before
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "published"
    assert [item["role"] for item in run["artifacts"]].count("local_publication_intent") == 1
    assert [item["role"] for item in run["artifacts"]].count("local_receipt") == 1
    intent_path = run_dir / "publication-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    assert intent == {
        "format": "paper_reader.local-publication-intent.v2-internal",
        "run_id": candidate.run_id,
        "candidate_id": candidate.candidate_id,
        "candidate_digest": candidate_integrity.candidate_core_digest(candidate),
        "target_path": str(target),
        "content_sha256": candidate.content_sha256,
        "content_length": candidate.content_length,
    }
    receipt_path = Path(payload["data"]["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["format"] == "paper_reader.local-receipt.v2-internal"
    assert receipt["candidate_digest"] == candidate_integrity.candidate_core_digest(candidate)
    assert receipt["intent_path"] == "publication-intent.json"
    assert receipt["intent_sha256"] == hashlib.sha256(intent_path.read_bytes()).hexdigest()
    assert receipt["content_sha256"] == candidate.content_sha256
    assert receipt["target_path"] == str(target)
    assert not list(run_dir.rglob("write-payload.json"))
    assert not list(run_dir.rglob("*authorization*"))


def test_local_publish_rejects_candidate_note_path_race_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_dir, candidate_path = _built_candidate(tmp_path)
    note_path = candidate_path.parent / "note.md"
    original_load = local_publish_module._load_candidate

    def load_then_overwrite(candidate_input: Path, **kwargs):
        loaded = original_load(candidate_input, **kwargs)
        note_path.write_bytes(b"attacker bytes after verification")
        return loaded

    monkeypatch.setattr(local_publish_module, "_load_candidate", load_then_overwrite)

    result = _invoke(["local", "publish", str(candidate_path)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "candidate_tampered"
    assert not (tmp_path / "paper_note.md").exists()


@pytest.mark.parametrize(
    "relative_path",
    ["candidate.json", "note.md", "note.html", "summary.json", "validation.json"],
)
def test_local_publish_rejects_any_candidate_byte_tamper(
    relative_path: str,
    tmp_path: Path,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    target = tmp_path / "paper_note.md"
    artifact = candidate_path.parent / relative_path
    artifact.write_bytes(artifact.read_bytes() + b"\n")

    result = _invoke(["local", "publish", str(candidate_path)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "candidate_tampered"
    assert not target.exists()
    assert not (run_dir / "receipts").exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"


def test_local_publish_blocks_source_drift_and_fixed_target_conflict(tmp_path: Path) -> None:
    source_case = tmp_path / "source"
    source_case.mkdir()
    run_dir, candidate_path = _built_candidate(source_case)
    source = source_case / "paper.pdf"
    source.write_bytes(source.read_bytes() + b"\n% drift")

    source_result = _invoke(["local", "publish", str(candidate_path)])

    assert source_result.exit_code == 1
    assert _result_payload(source_result)["code"] == "source_changed"
    assert not (source_case / "paper_note.md").exists()

    target_case = tmp_path / "target"
    target_case.mkdir()
    run_dir, candidate_path = _built_candidate(target_case)
    target = target_case / "paper_note.md"
    target.write_bytes(b"competing target")

    target_result = _invoke(["local", "publish", str(candidate_path)])

    assert target_result.exit_code == 1
    assert _result_payload(target_result)["code"] == "publish_conflict"
    assert target.read_bytes() == b"competing target"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"


def test_concurrent_local_publish_converges_on_one_exact_publication(tmp_path: Path) -> None:
    _run_dir, candidate_path = _built_candidate(tmp_path)

    def publish():
        try:
            return local_publish_module.publish_local_candidate(candidate_path)
        except Exception as exc:  # asserted below with the real exception preserved
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: publish(), range(2)))

    successes = [
        item for item in outcomes if isinstance(item, local_publish_module.PublishedLocalCandidate)
    ]
    assert len(successes) == 2
    assert not [item for item in outcomes if isinstance(item, Exception)]
    assert successes[0].receipt_path == successes[1].receipt_path
    assert (tmp_path / "paper_note.md").read_bytes() == (candidate_path.parent / "note.md").read_bytes()


def test_local_publish_recovers_after_intent_commit_before_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    run_before = (run_dir / "run.json").read_bytes()

    original_publish = local_publish_module.publish_bytes_no_replace
    failed = False

    def injected_failure(content: bytes, target: Path, **kwargs) -> Path:
        nonlocal failed
        if Path(target) == tmp_path / "paper_note.md" and not failed:
            failed = True
            raise OSError("injected failure after intent commit")
        return original_publish(content, target, **kwargs)

    monkeypatch.setattr("paper_reader.local_publish.publish_bytes_no_replace", injected_failure, raising=False)

    result = _invoke(["local", "publish", str(candidate_path)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publish_failed"
    assert (run_dir / "publication-intent.json").is_file()
    assert not (tmp_path / "paper_note.md").exists()
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "receipts").exists()

    retry = _invoke(["local", "publish", str(candidate_path)])

    assert retry.exit_code == 0, retry.stderr
    assert (tmp_path / "paper_note.md").read_bytes() == (candidate_path.parent / "note.md").read_bytes()


def test_local_publish_recovery_rejects_hardlinked_exact_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    target = tmp_path / "paper_note.md"
    note_bytes = (candidate_path.parent / "note.md").read_bytes()
    original_publish = local_publish_module.publish_bytes_no_replace
    failed = False

    def fail_target_once(content: bytes, destination: Path, **kwargs) -> Path:
        nonlocal failed
        if Path(destination) == target and not failed:
            failed = True
            raise OSError("injected failure after intent commit")
        return original_publish(content, destination, **kwargs)

    monkeypatch.setattr(local_publish_module, "publish_bytes_no_replace", fail_target_once)

    first = _invoke(["local", "publish", str(candidate_path)])
    assert first.exit_code == 1
    assert _result_payload(first)["code"] == "publish_failed"
    assert (run_dir / "publication-intent.json").is_file()

    attacker_file = tmp_path / "attacker-controlled.md"
    attacker_file.write_bytes(note_bytes)
    os.link(attacker_file, target)
    assert target.stat().st_nlink == 2

    retry = _invoke(["local", "publish", str(candidate_path)])

    assert retry.exit_code == 1
    assert _result_payload(retry)["code"] == "publish_conflict"
    assert os.path.samefile(attacker_file, target)
    assert not (run_dir / "receipts").exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"


def test_local_publish_rejects_hardlinked_candidate_manifest(tmp_path: Path) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    extra_link = tmp_path / "candidate-hardlink.json"
    os.link(candidate_path, extra_link)
    assert candidate_path.stat().st_nlink == 2

    result = _invoke(["local", "publish", str(candidate_path)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "candidate_tampered"
    assert not (tmp_path / "paper_note.md").exists()
    assert not (run_dir / "publication-intent.json").exists()


def test_local_publish_rejects_target_parent_replacement_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    original_run = (run_dir / "run.json").read_bytes()
    original_rename = storage_module._native_renameat_tree_no_replace
    detached_parent = tmp_path.with_name(f"{tmp_path.name}-detached")
    outside_parent = tmp_path.with_name(f"{tmp_path.name}-outside")
    outside_parent.mkdir()
    swapped = False

    def swap_parent_then_rename(*args, **kwargs):
        nonlocal swapped
        destination = Path(kwargs["destination"])
        if destination.name == "paper_note.md" and not swapped:
            swapped = True
            tmp_path.rename(detached_parent)
            tmp_path.symlink_to(outside_parent, target_is_directory=True)
        return original_rename(*args, **kwargs)

    monkeypatch.setattr(
        storage_module,
        "_native_renameat_tree_no_replace",
        swap_parent_then_rename,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert swapped is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] in {
        "invalid_local_target",
        "run_directory_changed",
    }
    assert not (outside_parent / "paper_note.md").exists()
    assert (detached_parent / run_dir.name / "run.json").read_bytes() == original_run


def test_local_publish_blocks_projected_run_size_before_target_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paper_reader.resource_policy import V2_RESOURCE_POLICY

    run_dir, candidate_path = _built_candidate(tmp_path)
    run_before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(
        local_publish_module,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, run_max_bytes=1),
        raising=False,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "run_size_limit_exceeded"
    assert not (tmp_path / "paper_note.md").exists()
    assert not (run_dir / "publication-intent.json").exists()
    assert not (run_dir / "receipts").exists()
    assert (run_dir / "run.json").read_bytes() == run_before


def test_local_publish_rejects_exact_preexisting_target_without_claiming_intent(
    tmp_path: Path,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    note_bytes = (candidate_path.parent / "note.md").read_bytes()
    target = tmp_path / "paper_note.md"
    target.write_bytes(note_bytes)
    target_before = (target.stat().st_ino, target.stat().st_mtime_ns, target.read_bytes())

    result = _invoke(["local", "publish", str(candidate_path)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publish_conflict"
    assert (target.stat().st_ino, target.stat().st_mtime_ns, target.read_bytes()) == target_before
    assert not (run_dir / "publication-intent.json").exists()
    assert not (run_dir / "receipts").exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"


def test_local_publish_recovers_after_target_commit_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    original = getattr(local_publish_module, "_publish_or_verify_receipt", None)
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected failure after target commit")
        assert original is not None
        return original(*args, **kwargs)

    monkeypatch.setattr(
        local_publish_module,
        "_publish_or_verify_receipt",
        fail_once,
        raising=False,
    )

    first = _invoke(["local", "publish", str(candidate_path)])

    assert first.exit_code == 1
    assert _result_payload(first)["code"] == "publication_recovery_required"
    assert (run_dir / "publication-intent.json").is_file()
    target = tmp_path / "paper_note.md"
    target_before = (target.stat().st_ino, target.stat().st_mtime_ns, target.read_bytes())
    assert not (run_dir / "receipts").exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"

    second = _invoke(["local", "publish", str(candidate_path)])

    assert second.exit_code == 0, second.stderr
    assert (target.stat().st_ino, target.stat().st_mtime_ns, target.read_bytes()) == target_before
    assert json.loads((run_dir / "run.json").read_text())["status"] == "published"


def test_local_publish_recovers_after_receipt_commit_before_run_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    original_cas = local_publish_module.cas_update_run
    failed = False

    def fail_run_once(loaded, value, **kwargs):
        nonlocal failed
        if loaded.manifest_path.name == "run.json" and not failed:
            failed = True
            raise OSError("injected failure before run manifest update")
        return original_cas(loaded, value, **kwargs)

    monkeypatch.setattr(local_publish_module, "cas_update_run", fail_run_once)

    first = _invoke(["local", "publish", str(candidate_path)])

    assert first.exit_code == 1
    assert _result_payload(first)["code"] == "publication_recovery_required"
    target = tmp_path / "paper_note.md"
    target_before = (target.stat().st_ino, target.stat().st_mtime_ns, target.read_bytes())
    receipts = list((run_dir / "receipts").glob("*.json"))
    assert len(receipts) == 1
    assert (run_dir / "publication-intent.json").is_file()
    receipt_before = receipts[0].read_bytes()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"

    second = _invoke(["local", "publish", str(candidate_path)])

    assert second.exit_code == 0, second.stderr
    payload = _result_payload(second)
    assert Path(payload["data"]["receipt_path"]) == receipts[0]
    assert receipts[0].read_bytes() == receipt_before
    assert (target.stat().st_ino, target.stat().st_mtime_ns, target.read_bytes()) == target_before
    assert json.loads((run_dir / "run.json").read_text())["status"] == "published"


def test_local_publish_detects_target_replacement_before_receipt_and_never_marks_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    target = tmp_path / "paper_note.md"
    detached_target = tmp_path / "detached-published-note.md"
    original_publish_receipt = local_publish_module._publish_or_verify_receipt
    replaced = False

    def replace_target_then_publish_receipt(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            target.rename(detached_target)
            target.write_bytes(b"attacker replacement")
            replaced = True
        return original_publish_receipt(*args, **kwargs)

    monkeypatch.setattr(
        local_publish_module,
        "_publish_or_verify_receipt",
        replace_target_then_publish_receipt,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert replaced is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publication_recovery_required"
    assert target.read_bytes() == b"attacker replacement"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"


def test_local_publish_rejects_exact_byte_target_inode_swap_before_guard_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    target = tmp_path / "paper_note.md"
    detached_target = tmp_path / "detached-published-note.md"
    original_publish_target = local_publish_module._publish_or_recover_target
    original_inode: int | None = None
    replacement_inode: int | None = None

    def publish_target_then_swap_exact_inode(*args, **kwargs):
        nonlocal original_inode, replacement_inode
        result = original_publish_target(*args, **kwargs)
        original_inode = target.stat().st_ino
        target.rename(detached_target)
        target.write_bytes((candidate_path.parent / "note.md").read_bytes())
        replacement_inode = target.stat().st_ino
        return result

    monkeypatch.setattr(
        local_publish_module,
        "_publish_or_recover_target",
        publish_target_then_swap_exact_inode,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert original_inode is not None
    assert replacement_inode is not None
    assert replacement_inode != original_inode
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publication_recovery_required"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"


def test_local_publish_binds_storage_published_inode_across_helper_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    target = tmp_path / "paper_note.md"
    detached_target = tmp_path / "detached-storage-published-note.md"
    original_publish = local_publish_module.publish_bytes_no_replace
    original_inode: int | None = None
    replacement_inode: int | None = None

    def publish_then_swap_exact_inode(content: bytes, destination: Path, **kwargs):
        nonlocal original_inode, replacement_inode
        result = original_publish(content, destination, **kwargs)
        if Path(destination) == target:
            original_inode = target.stat().st_ino
            target.rename(detached_target)
            target.write_bytes(content)
            replacement_inode = target.stat().st_ino
        return result

    monkeypatch.setattr(
        local_publish_module,
        "publish_bytes_no_replace",
        publish_then_swap_exact_inode,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert original_inode is not None
    assert replacement_inode is not None
    assert replacement_inode != original_inode
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publication_recovery_required"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"


def test_local_publish_recovery_never_closes_verified_target_before_guarding_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    target = tmp_path / "paper_note.md"
    original_publish_receipt = local_publish_module._publish_or_verify_receipt
    receipt_failed = False

    def fail_receipt_once(*args, **kwargs):
        nonlocal receipt_failed
        if not receipt_failed:
            receipt_failed = True
            raise OSError("leave an exact committed target for recovery")
        return original_publish_receipt(*args, **kwargs)

    monkeypatch.setattr(
        local_publish_module,
        "_publish_or_verify_receipt",
        fail_receipt_once,
    )
    first = _invoke(["local", "publish", str(candidate_path)])
    assert first.exit_code == 1
    assert _result_payload(first)["code"] == "publication_recovery_required"
    original_inode = target.stat().st_ino
    exact_bytes = target.read_bytes()
    original_verify = getattr(local_publish_module, "_verify_exact_target", None)
    swapped = False

    def swap_after_closed_verification(*args, **kwargs):
        nonlocal swapped
        assert original_verify is not None
        verified = original_verify(*args, **kwargs)
        if verified and not swapped:
            detached = tmp_path / "detached-recovery-target.md"
            target.rename(detached)
            target.write_bytes(exact_bytes)
            swapped = True
        return verified

    monkeypatch.setattr(
        local_publish_module,
        "_verify_exact_target",
        swap_after_closed_verification,
        raising=False,
    )

    second = _invoke(["local", "publish", str(candidate_path)])

    assert second.exit_code == 0, second.stderr
    assert swapped is False
    assert target.stat().st_ino == original_inode
    assert json.loads((run_dir / "run.json").read_text())["status"] == "published"


def test_local_publish_preserves_published_run_if_target_changes_after_run_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    target = tmp_path / "paper_note.md"
    detached_target = tmp_path / "detached-published-note.md"
    original_cas = local_publish_module.cas_update_run
    replaced = False

    def replace_target_after_published_run(loaded, value, **kwargs):
        nonlocal replaced
        result = original_cas(loaded, value, **kwargs)
        if (
            loaded.manifest_path == run_dir / "run.json"
            and getattr(value, "status", None) == "published"
            and not replaced
        ):
            target.rename(detached_target)
            target.write_bytes(b"attacker replacement after run commit")
            replaced = True
        return result

    monkeypatch.setattr(
        local_publish_module,
        "cas_update_run",
        replace_target_after_published_run,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert replaced is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publication_recovery_required"
    assert target.read_bytes() == b"attacker replacement after run commit"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "published"


def test_local_publish_detects_source_snapshot_replacement_after_run_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    source_snapshot = run_dir / "source" / "source.json"
    detached_snapshot = run_dir / "source" / "source.detached.json"
    snapshot_raw = source_snapshot.read_bytes()
    original_cas = local_publish_module.cas_update_run
    replaced = False

    def replace_source_after_published_run(loaded, value, **kwargs):
        nonlocal replaced
        result = original_cas(loaded, value, **kwargs)
        if (
            loaded.manifest_path == run_dir / "run.json"
            and getattr(value, "status", None) == "published"
            and not replaced
        ):
            source_snapshot.rename(detached_snapshot)
            source_snapshot.write_bytes(snapshot_raw)
            replaced = True
        return result

    monkeypatch.setattr(
        local_publish_module,
        "cas_update_run",
        replace_source_after_published_run,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert replaced is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publication_recovery_required"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "published"


def test_local_publish_never_rolls_back_if_run_write_commits_then_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    original_cas = local_publish_module.cas_update_run
    original_exchange = storage_module._native_exchangeat
    failed_after_commit = False
    run_exchanges = 0

    def commit_published_run_then_raise(loaded, value, **kwargs):
        nonlocal failed_after_commit
        result = original_cas(loaded, value, **kwargs)
        if (
            loaded.manifest_path == run_dir / "run.json"
            and getattr(value, "status", None) == "published"
            and not failed_after_commit
        ):
            failed_after_commit = True
            raise OSError("injected failure after published run replacement")
        return result

    monkeypatch.setattr(
        local_publish_module,
        "cas_update_run",
        commit_published_run_then_raise,
    )

    def count_run_exchanges(*args, **kwargs):
        nonlocal run_exchanges
        run_exchanges += 1
        return original_exchange(*args, **kwargs)

    monkeypatch.setattr(
        storage_module,
        "_native_exchangeat",
        count_run_exchanges,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert failed_after_commit is True
    assert run_exchanges == 1
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publication_recovery_required"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "published"


@pytest.mark.parametrize("artifact_kind", ["intent", "receipt"])
def test_local_publish_rejects_finalization_sidecar_drift(
    artifact_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    original_cas = local_publish_module.cas_update_run
    corrupted = False

    def corrupt_sidecar_before_published_run(loaded, value, **kwargs):
        nonlocal corrupted
        if (
            loaded.manifest_path == run_dir / "run.json"
            and getattr(value, "status", None) == "published"
            and not corrupted
        ):
            if artifact_kind == "intent":
                sidecar = run_dir / "publication-intent.json"
            else:
                receipts = list((run_dir / "receipts").glob("*.json"))
                assert len(receipts) == 1
                sidecar = receipts[0]
            sidecar.write_bytes(f"corrupted {artifact_kind}".encode())
            corrupted = True
        return original_cas(loaded, value, **kwargs)

    monkeypatch.setattr(
        local_publish_module,
        "cas_update_run",
        corrupt_sidecar_before_published_run,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert corrupted is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publication_recovery_required"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "candidate_built"


def test_local_publish_rejects_silent_run_manifest_replacement_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    original_cas = local_publish_module.cas_update_run
    replaced = False

    def replace_published_run_after_commit(loaded, value, **kwargs):
        nonlocal replaced
        result = original_cas(loaded, value, **kwargs)
        if (
            loaded.manifest_path == run_dir / "run.json"
            and getattr(value, "status", None) == "published"
            and not replaced
        ):
            (run_dir / "run.json").write_bytes(b"silently replaced published run")
            replaced = True
        return result

    monkeypatch.setattr(
        local_publish_module,
        "cas_update_run",
        replace_published_run_after_commit,
    )

    result = _invoke(["local", "publish", str(candidate_path)])

    assert replaced is True
    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publication_recovery_required"
    assert (run_dir / "run.json").read_bytes() == b"silently replaced published run"


def test_local_publish_preserves_published_run_on_idempotent_sidecar_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    first = _invoke(["local", "publish", str(candidate_path)])
    assert first.exit_code == 0, first.stderr
    intent_path = run_dir / "publication-intent.json"
    original_receipt = local_publish_module._publish_or_verify_receipt
    corrupted = False

    def corrupt_intent_after_receipt(*args, **kwargs):
        nonlocal corrupted
        result = original_receipt(*args, **kwargs)
        if not corrupted:
            intent_path.write_bytes(b"corrupted published intent")
            corrupted = True
        return result

    monkeypatch.setattr(
        local_publish_module,
        "_publish_or_verify_receipt",
        corrupt_intent_after_receipt,
    )

    second = _invoke(["local", "publish", str(candidate_path)])

    assert corrupted is True
    assert second.exit_code == 1
    assert _result_payload(second)["code"] == "publication_recovery_required"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "published"


def test_local_publish_is_idempotent_after_published_run(
    tmp_path: Path,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    first = _invoke(["local", "publish", str(candidate_path)])
    assert first.exit_code == 0, first.stderr
    first_payload = _result_payload(first)
    target = tmp_path / "paper_note.md"
    receipt = Path(first_payload["data"]["receipt_path"])
    snapshot = {
        "target": (target.stat().st_ino, target.stat().st_mtime_ns, target.read_bytes()),
        "intent": (run_dir / "publication-intent.json").read_bytes(),
        "receipt": (receipt.stat().st_ino, receipt.stat().st_mtime_ns, receipt.read_bytes()),
        "run": (run_dir / "run.json").read_bytes(),
    }

    second = _invoke(["local", "publish", str(candidate_path)])

    assert second.exit_code == 0, second.stderr
    second_payload = _result_payload(second)
    assert second_payload["data"]["receipt_path"] == str(receipt)
    assert (target.stat().st_ino, target.stat().st_mtime_ns, target.read_bytes()) == snapshot["target"]
    assert (receipt.stat().st_ino, receipt.stat().st_mtime_ns, receipt.read_bytes()) == snapshot["receipt"]
    assert (run_dir / "publication-intent.json").read_bytes() == snapshot["intent"]
    assert (run_dir / "run.json").read_bytes() == snapshot["run"]


def test_second_candidate_with_same_note_bytes_cannot_claim_first_candidate_intent(
    tmp_path: Path,
) -> None:
    run_dir = _sealed_run(tmp_path)
    first_build = _invoke(["candidate", "build", str(run_dir)])
    second_build = _invoke(["candidate", "build", str(run_dir)])
    assert first_build.exit_code == 0, first_build.stderr
    assert second_build.exit_code == 0, second_build.stderr
    first_path = Path(_result_payload(first_build)["data"]["candidate_path"])
    second_path = Path(_result_payload(second_build)["data"]["candidate_path"])
    assert first_path != second_path
    assert (first_path.parent / "note.md").read_bytes() == (second_path.parent / "note.md").read_bytes()

    first_publish = _invoke(["local", "publish", str(first_path)])
    second_publish = _invoke(["local", "publish", str(second_path)])

    assert first_publish.exit_code == 0, first_publish.stderr
    assert second_publish.exit_code == 1
    assert _result_payload(second_publish)["code"] == "publication_identity_conflict"
    assert len(list((run_dir / "receipts").glob("*.json"))) == 1
    intent = json.loads((run_dir / "publication-intent.json").read_text())
    first = PaperReaderCandidate.model_validate_json(first_path.read_bytes())
    assert intent["candidate_id"] == first.candidate_id
