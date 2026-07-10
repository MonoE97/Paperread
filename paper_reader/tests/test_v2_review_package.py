from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paper_reader.contracts import (
    PaperReaderCommandResult,
    PaperReaderReview,
    PaperReaderReviewPackage,
    PaperReaderSummary,
)
from paper_reader.public_cli import app
from paper_reader.storage import canonical_json_bytes, canonical_json_sha256, rfc3339_utc


FIXTURE_PDF = Path(__file__).parent / "fixtures" / "minimal.pdf"


def _invoke(arguments: list[str]):
    return CliRunner().invoke(app, arguments)


def _result_payload(result) -> dict:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    PaperReaderCommandResult.model_validate(payload)
    return payload


def _tree_snapshot(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def _prepared_run(tmp_path: Path, *, preview: bool = False) -> tuple[Path, str]:
    source = tmp_path / "paper.pdf"
    shutil.copyfile(FIXTURE_PDF, source)
    initialized = _invoke(["run", "init-local", str(source)])
    run_dir = Path(_result_payload(initialized)["data"]["run_dir"])
    arguments = ["run", "prepare", str(run_dir), "--figure-limit", "0"]
    if preview:
        arguments.extend(["--preview-pages", "1"])
    prepared = _invoke(arguments)
    payload = _result_payload(prepared)
    return run_dir, payload["data"]["evidence_digest"]


def _write_summary_and_review(
    run_dir: Path,
    evidence_digest: str,
    *,
    locator: str = "context.md page 1",
    method: str = "方法先抽取正文，再对证据与结论执行结构化复核。",
    review_status: str = "passed",
) -> tuple[PaperReaderSummary, PaperReaderReview]:
    run_id = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["run_id"]
    summary_payload = {
        "schema_version": "paper_reader.summary.v2",
        "summary_id": "summary_test",
        "run_id": run_id,
        "created_at": rfc3339_utc(),
        "evidence_digest": evidence_digest,
        "paper_type": "method_paper",
        "trust_status": "usable_with_caveats",
        "review_status": review_status,
        "improvement_status": "not_needed",
        "trust_rationale": "正文页与结构化抽取结果可以相互核对。",
        "one_sentence_summary": "本文展示了一个可追溯的论文阅读流程。",
        "abstract_translation": "摘要说明该流程把正文证据与结构化结论连接起来。",
        "research_question": "如何生成能够追溯到原文页码的阅读笔记？",
        "method": method,
        "experiments": "作者使用示例论文验证抽取、复核与渲染链路。",
        "ai4s_relevance": "该流程可用于材料与物理方向的论文归档。",
        "key_points": ["完整抽取", "证据定位", "复核门禁"],
        "contributions": ["把阅读结论与证据定位放在同一份笔记中。"],
        "limitations": ["抽取质量仍受原始 PDF 排版影响。"],
        "follow_up_keywords": ["evidence locator", "paper reading"],
        "evidence_summary": [
            {
                "claim": "该流程保留了结论到正文页的定位关系。",
                "evidence": [
                    {
                        "type": "text",
                        "locator": locator,
                        "summary": "正文页展示了结构化阅读流程。",
                    }
                ],
                "confidence": "medium",
            }
        ],
    }
    summary_bytes = json.dumps(summary_payload, ensure_ascii=False, separators=(",", ":")).encode()
    summary = PaperReaderSummary.model_validate_json(summary_bytes)
    (run_dir / "summary.json").write_bytes(summary_bytes)
    review = PaperReaderReview(
        schema_version="paper_reader.review.v2",
        review_id="review_test",
        run_id=run_id,
        created_at=rfc3339_utc(),
        summary_sha256=canonical_json_sha256(summary),
        evidence_digest=evidence_digest,
        review_status=review_status,
        needs_improvement=False,
        review_issues=(),
        trust_status_recommendation="usable_with_caveats",
        improvement_requests=(),
    )
    (run_dir / "review.json").write_bytes(canonical_json_bytes(review))
    return summary, review


def test_review_validate_is_read_only_and_accepts_fully_bound_chinese_render(tmp_path: Path) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    summary, _review = _write_summary_and_review(run_dir, evidence_digest)
    before = _tree_snapshot(run_dir)

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["code"] == "review_valid"
    assert payload["data"]["run_id"] == summary.run_id
    assert payload["data"]["summary_sha256"] == canonical_json_sha256(summary)
    assert payload["data"]["evidence_digest"] == evidence_digest
    assert len(payload["data"]["rendered_note_sha256"]) == 64
    assert payload["data"]["blockers"] == []
    assert _tree_snapshot(run_dir) == before


def test_review_validation_retains_verified_evidence_bytes_after_path_overwrite(
    tmp_path: Path,
) -> None:
    from paper_reader.review_package import validate_review_run

    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    validation = validate_review_run(run_dir)
    assert validation.blockers == ()
    assert validation.evidence is not None
    metadata = validation.evidence.artifacts_by_role["metadata"][0]
    expected = metadata.raw_bytes

    metadata.path.write_bytes(b"metadata overwritten after validation")

    assert metadata.raw_bytes == expected
    assert metadata.raw_bytes != metadata.path.read_bytes()


def _blocker_codes(result) -> set[str]:
    payload = _result_payload(result)
    assert payload["code"] == "review_blocked"
    return {item["code"] for item in payload["data"]["blockers"]}


@pytest.mark.parametrize(
    "locator",
    [
        "context.md page 2",
        "context.md",
        "page 1 method section",
        "section_context.md page 1",
        "secondary_contexts/source.md",
    ],
)
def test_review_validate_blocks_nonmember_or_noncanonical_locators(
    locator: str,
    tmp_path: Path,
) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest, locator=locator)

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "invalid_evidence_locator" in _blocker_codes(result)


def test_review_validate_blocks_preview_hash_drift_failed_review_and_english_fallback(
    tmp_path: Path,
) -> None:
    preview_dir = tmp_path / "preview"
    preview_dir.mkdir()
    run_dir, evidence_digest = _prepared_run(preview_dir, preview=True)
    _write_summary_and_review(run_dir, evidence_digest)
    preview_result = _invoke(["review", "validate", str(run_dir)])
    assert preview_result.exit_code == 1
    assert "incomplete_evidence" in _blocker_codes(preview_result)

    drift_dir = tmp_path / "drift"
    drift_dir.mkdir()
    run_dir, evidence_digest = _prepared_run(drift_dir)
    _write_summary_and_review(run_dir, evidence_digest)
    payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    payload["one_sentence_summary"] = "本文在复核后发生了摘要字节漂移。"
    (run_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    drift_result = _invoke(["review", "validate", str(run_dir)])
    assert drift_result.exit_code == 1
    assert "summary_hash_mismatch" in _blocker_codes(drift_result)

    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    run_dir, evidence_digest = _prepared_run(failed_dir)
    _write_summary_and_review(run_dir, evidence_digest, review_status="failed")
    failed_result = _invoke(["review", "validate", str(run_dir)])
    assert failed_result.exit_code == 1
    assert "review_failed" in _blocker_codes(failed_result)

    english_dir = tmp_path / "english"
    english_dir.mkdir()
    run_dir, evidence_digest = _prepared_run(english_dir)
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        method="This method extracts the paper and validates the evidence chain.",
    )
    english_result = _invoke(["review", "validate", str(run_dir)])
    assert english_result.exit_code == 1
    assert "rendered_note_english_prose" in _blocker_codes(english_result)


def test_review_validate_rehashes_every_bound_evidence_file(tmp_path: Path) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    evidence_ref = next(item for item in run["artifacts"] if item["role"] == "evidence_manifest")
    evidence_manifest = json.loads((run_dir / evidence_ref["path"]).read_text(encoding="utf-8"))
    context_ref = next(item for item in evidence_manifest["files"] if item["role"] == "context")
    (run_dir / context_ref["path"]).write_text("tampered evidence", encoding="utf-8")

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "evidence_artifact_hash_mismatch" in _blocker_codes(result)


def test_review_validate_blocks_unreferenced_file_in_immutable_evidence_bundle(
    tmp_path: Path,
) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    evidence_ref = next(item for item in run["artifacts"] if item["role"] == "evidence_manifest")
    evidence_dir = (run_dir / evidence_ref["path"]).parent
    (evidence_dir / "unreferenced.bin").write_bytes(b"not in evidence manifest")

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "evidence_closed_world_mismatch" in _blocker_codes(result)


def test_review_seal_atomically_publishes_immutable_snapshots_and_validation(tmp_path: Path) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    summary, review = _write_summary_and_review(run_dir, evidence_digest)
    summary_before = canonical_json_bytes(summary)
    review_before = canonical_json_bytes(review)
    run_before = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    evidence_ref = next(item for item in run_before["artifacts"] if item["role"] == "evidence_manifest")
    evidence_before = (run_dir / evidence_ref["path"]).read_bytes()

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    payload = _result_payload(result)
    assert payload["code"] == "review_sealed"
    package_dir = Path(payload["data"]["review_package_dir"])
    assert package_dir.parent == run_dir / "reviews"
    assert sorted(path.name for path in package_dir.iterdir()) == [
        "evidence.json",
        "note.html",
        "note.md",
        "review-package.json",
        "review.json",
        "summary.json",
        "validation.json",
    ]
    assert (package_dir / "summary.json").read_bytes() == summary_before
    assert (package_dir / "review.json").read_bytes() == review_before
    assert (package_dir / "evidence.json").read_bytes() == evidence_before
    package = PaperReaderReviewPackage.model_validate_json(
        (package_dir / "review-package.json").read_bytes()
    )
    assert package.run_id == summary.run_id == review.run_id
    assert package.summary_sha256 == canonical_json_sha256(summary)
    assert package.review_sha256 == canonical_json_sha256(review)
    assert package.evidence_digest == evidence_digest
    assert package.gate.status == "passed"
    assert package.gate.blockers == ()
    for artifact in package.artifacts:
        path = run_dir / artifact.path
        assert path.is_file()
        assert artifact.size_bytes == path.stat().st_size
        assert artifact.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    validation = json.loads((package_dir / "validation.json").read_text(encoding="utf-8"))
    assert validation["format"] == "paper_reader.review-validation.v2-internal"
    assert validation["blockers"] == []
    assert validation["rendered_note_sha256"] == hashlib.sha256(
        (package_dir / "note.md").read_bytes()
    ).hexdigest()
    run_after = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_after["status"] == "reviewed"
    assert any(item["role"] == "review_package" for item in run_after["artifacts"])
    assert not list(run_dir.glob(".*.staging"))


def test_review_seal_uses_only_bytes_captured_by_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.review_package as review_package

    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    original_validate = review_package.validate_review_run
    captured: dict[str, object] = {}

    def validate_then_overwrite(run_path: Path):
        validation = original_validate(run_path)
        captured["validation"] = validation
        validation.summary_path.write_bytes(b"summary swapped after validation")
        validation.review_path.write_bytes(b"review swapped after validation")
        assert validation.evidence is not None
        validation.evidence.artifacts_by_role["metadata"][0].path.write_bytes(
            b"metadata swapped after validation"
        )
        return validation

    monkeypatch.setattr(review_package, "validate_review_run", validate_then_overwrite)

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    validation = captured["validation"]
    package_dir = Path(_result_payload(result)["data"]["review_package_dir"])
    assert (package_dir / "summary.json").read_bytes() == validation.summary_bytes
    assert (package_dir / "review.json").read_bytes() == validation.review_bytes
    assert (package_dir / "note.md").read_bytes() == validation.rendered_note_bytes
    assert (package_dir / "note.html").read_bytes() == validation.rendered_html_bytes


def test_review_seal_publication_fault_leaves_no_half_package_or_reviewed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    run_before = (run_dir / "run.json").read_bytes()

    def injected_failure(_staging: Path, _destination: Path) -> Path:
        raise OSError("injected review publication failure")

    monkeypatch.setattr("paper_reader.review_package.atomic_publish_tree", injected_failure)

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "review_seal_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "reviews").exists()
    assert not list(run_dir.glob(".*.staging"))


def test_review_seal_blocks_projected_run_size_before_package_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.review_package as review_package
    from paper_reader.resource_policy import V2_RESOURCE_POLICY

    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    run_before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(
        review_package,
        "V2_RESOURCE_POLICY",
        replace(V2_RESOURCE_POLICY, run_max_bytes=1),
        raising=False,
    )

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 1
    assert _result_payload(result)["code"] == "run_size_limit_exceeded"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert not (run_dir / "reviews").exists()


def test_review_run_update_fault_leaves_unbound_orphan_and_retry_binds_new_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.review_package as review_package

    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(run_dir, evidence_digest)
    run_before = (run_dir / "run.json").read_bytes()
    original_write = review_package.atomic_write_json
    failed = False

    def fail_once(path: Path, value):
        nonlocal failed
        if Path(path).name == "run.json" and not failed:
            failed = True
            raise OSError("injected failure after review package publication")
        return original_write(path, value)

    monkeypatch.setattr(review_package, "atomic_write_json", fail_once)

    first = _invoke(["review", "seal", str(run_dir)])

    assert first.exit_code == 1
    assert _result_payload(first)["code"] == "review_status_update_failed"
    assert (run_dir / "run.json").read_bytes() == run_before
    orphan_dirs = tuple((run_dir / "reviews").iterdir())
    assert len(orphan_dirs) == 1

    second = _invoke(["review", "seal", str(run_dir)])

    assert second.exit_code == 0, second.stderr
    run = json.loads((run_dir / "run.json").read_text())
    bound_paths = {
        item["path"] for item in run["artifacts"] if item["role"] == "review_package"
    }
    assert len(bound_paths) == 1
    assert not any(path.startswith(orphan_dirs[0].relative_to(run_dir).as_posix()) for path in bound_paths)


def test_review_seal_refuses_preview_without_creating_a_package(tmp_path: Path) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path, preview=True)
    _write_summary_and_review(run_dir, evidence_digest)

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 1
    payload = _result_payload(result)
    assert payload["code"] == "review_blocked"
    assert "incomplete_evidence" in {item["code"] for item in payload["data"]["blockers"]}
    assert not (run_dir / "reviews").exists()
