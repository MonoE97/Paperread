from __future__ import annotations

import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
import paper_reader.local_publication as local_publication
import paper_reader.local_publish as local_publish_module
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
    assert hasattr(local_publication, "candidate_core_digest")
    assert payload["data"]["candidate_digest"] == local_publication.candidate_core_digest(candidate)
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


def test_candidate_build_uses_loaded_run_manifest_bytes_after_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.candidate_builder as candidate_builder

    run_dir = _sealed_run(tmp_path)
    run_before = (run_dir / "run.json").read_bytes()
    original_snapshots = candidate_builder._sealed_snapshots

    def snapshots_then_swap(*args, **kwargs):
        snapshots = original_snapshots(*args, **kwargs)
        (run_dir / "run.json").write_bytes(b"run manifest swapped after loading")
        return snapshots

    monkeypatch.setattr(candidate_builder, "_sealed_snapshots", snapshots_then_swap)

    result = _invoke(["candidate", "build", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    candidate_dir = Path(_result_payload(result)["data"]["candidate_dir"])
    assert (candidate_dir / "run.json").read_bytes() == run_before


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
    original_write = candidate_builder.atomic_write_json
    failed = False

    def fail_once(path: Path, value):
        nonlocal failed
        if Path(path).name == "run.json" and not failed:
            failed = True
            raise OSError("injected failure after candidate tree publication")
        return original_write(path, value)

    monkeypatch.setattr(candidate_builder, "atomic_write_json", fail_once)

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
        "candidate_digest": local_publication.candidate_core_digest(candidate),
        "target_path": str(target),
        "content_sha256": candidate.content_sha256,
        "content_length": candidate.content_length,
    }
    receipt_path = Path(payload["data"]["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["format"] == "paper_reader.local-receipt.v2-internal"
    assert receipt["candidate_digest"] == local_publication.candidate_core_digest(candidate)
    assert receipt["intent_path"] == "publication-intent.json"
    assert receipt["intent_sha256"] == hashlib.sha256(intent_path.read_bytes()).hexdigest()
    assert receipt["content_sha256"] == candidate.content_sha256
    assert receipt["target_path"] == str(target)
    assert not list(run_dir.rglob("write-payload.json"))
    assert not list(run_dir.rglob("*authorization*"))


def test_local_publish_uses_captured_verified_bytes_after_candidate_note_path_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_dir, candidate_path = _built_candidate(tmp_path)
    note_path = candidate_path.parent / "note.md"
    verified_bytes = note_path.read_bytes()
    original_load = local_publish_module._load_candidate

    def load_then_overwrite(candidate_input: Path, **kwargs):
        loaded = original_load(candidate_input, **kwargs)
        note_path.write_bytes(b"attacker bytes after verification")
        return loaded

    monkeypatch.setattr(local_publish_module, "_load_candidate", load_then_overwrite)

    result = _invoke(["local", "publish", str(candidate_path)])

    assert result.exit_code == 0, result.stderr
    target = tmp_path / "paper_note.md"
    assert target.read_bytes() == verified_bytes
    assert target.read_bytes() != note_path.read_bytes()


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
            return local_publication.publish_local_candidate(candidate_path)
        except Exception as exc:  # asserted below with the real exception preserved
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: publish(), range(2)))

    successes = [item for item in outcomes if isinstance(item, local_publication.PublishedLocalCandidate)]
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

    def injected_failure(content: bytes, target: Path) -> Path:
        nonlocal failed
        if Path(target) == tmp_path / "paper_note.md" and not failed:
            failed = True
            raise OSError("injected failure after intent commit")
        return original_publish(content, target)

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
    original_write = local_publish_module.atomic_write_json
    failed = False

    def fail_run_once(path: Path, value):
        nonlocal failed
        if Path(path).name == "run.json" and not failed:
            failed = True
            raise OSError("injected failure before run manifest update")
        return original_write(path, value)

    monkeypatch.setattr(local_publish_module, "atomic_write_json", fail_run_once)

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
