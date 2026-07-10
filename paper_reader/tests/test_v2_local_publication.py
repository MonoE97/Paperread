from __future__ import annotations

import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import paper_reader.local_publication as local_publication
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


def test_candidate_build_blocks_sealed_snapshot_and_original_evidence_tamper(
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
    assert any(item["role"] == "local_receipt" for item in run["artifacts"])
    receipt_path = Path(payload["data"]["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["format"] == "paper_reader.local-receipt.v2-internal"
    assert receipt["candidate_digest"] == local_publication.candidate_core_digest(candidate)
    assert receipt["content_sha256"] == candidate.content_sha256
    assert receipt["target_path"] == str(target)
    assert not list(run_dir.rglob("write-payload.json"))
    assert not list(run_dir.rglob("*authorization*"))


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


def test_concurrent_local_publish_has_exactly_one_winner(tmp_path: Path) -> None:
    _run_dir, candidate_path = _built_candidate(tmp_path)

    def publish():
        try:
            return local_publication.publish_local_candidate(candidate_path)
        except Exception as exc:  # asserted below with the real exception preserved
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: publish(), range(2)))

    successes = [item for item in outcomes if isinstance(item, local_publication.PublishedLocalCandidate)]
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], local_publication.LocalPublicationError)
    assert failures[0].code == "publish_conflict"
    assert (tmp_path / "paper_note.md").read_bytes() == (candidate_path.parent / "note.md").read_bytes()


def test_local_publish_fault_before_no_replace_leaves_target_and_status_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, candidate_path = _built_candidate(tmp_path)
    run_before = (run_dir / "run.json").read_bytes()

    def injected_failure(_source: Path, _target: Path) -> Path:
        raise OSError("injected file publication failure")

    monkeypatch.setattr("paper_reader.local_publish.publish_file_no_replace", injected_failure)

    result = _invoke(["local", "publish", str(candidate_path)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "publish_failed"
    assert not (tmp_path / "paper_note.md").exists()
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "receipts").exists()
