from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

import paper_reader_batch.v2_report as report_module
from paper_reader_batch.v2_contracts import ArtifactRef, BatchReport
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    MAX_JSON_ARTIFACT_BYTES,
    MAX_OPAQUE_ARTIFACT_BYTES,
    canonical_json_bytes,
    sha256_bytes,
)
from paper_reader_batch.v2_manifest import create_pdf_paths_manifest
from paper_reader_batch.v2_local_prepare import claim_local_prepare
from paper_reader_batch.v2_run import initialize_run
from paper_reader_batch.v2_report import (
    _cell,
    _extract_markdown_takeaway,
    _read_candidate_inner,
    _replace_or_publish,
    _takeaway_from_candidate,
    run_report,
)
from paper_reader_batch.v2_worker import claim_worker, finish_worker
from test_v2_artifact_closure import _local_fixture, _summary


MANIFEST_REQUEST = "11111111-1111-4111-8111-111111111111"
INIT_REQUEST = "22222222-2222-4222-8222-222222222222"
CLAIM_REQUEST = "33333333-3333-4333-8333-333333333333"
FINISH_REQUEST = "44444444-4444-4444-8444-444444444444"
REPORT_ADVANCE_REQUEST = "55555555-5555-4555-8555-555555555555"


def _crash_report_replace(run_dir: str, target_name: str, stage: str) -> None:
    import paper_reader_batch.v2_report as child_report

    original = child_report.replace_bytes_atomic

    def crashing_replace(path: Path, data: bytes, **kwargs) -> None:
        def crash(observed: str) -> None:
            if path.name == target_name and observed == stage:
                os._exit(31)

        original(path, data, fault=crash, **kwargs)

    child_report.replace_bytes_atomic = crashing_replace
    child_report.run_report(Path(run_dir))


def _initialized_local_run(tmp_path: Path, *, batch_title: str = "report batch") -> Path:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nreport fixture\n")
    paths = tmp_path / "paths.txt"
    paths.write_text(str(pdf), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_pdf_paths_manifest(
        paths,
        batch_title=batch_title,
        output=manifest,
        request_id=MANIFEST_REQUEST,
        skill_root=skill_root,
    )
    run_dir = tmp_path / "run"
    initialize_run(
        manifest,
        request_id=INIT_REQUEST,
        skill_root=skill_root,
        output=run_dir,
        initialized_at="2026-07-10T00:00:00Z",
    )
    return run_dir


def _advance_report_journal(run_dir: Path, *, now: str = "2026-07-10T00:02:00Z") -> None:
    claim_local_prepare(
        run_dir,
        worker_id="report-generation-fixture",
        request_id=REPORT_ADVANCE_REQUEST,
        now=now,
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _candidate_for_fallback(
    tmp_path: Path,
    *,
    tldr: str | None,
    one_sentence_summary: str,
) -> tuple[ArtifactRef, Path, Path]:
    run_dir = tmp_path / "paper-run"
    candidate_dir = run_dir / "candidates" / "candidate_test"
    candidate_dir.mkdir(parents=True)
    note = candidate_dir / "note.md"
    note.write_text("# 测试笔记\n\n这里没有三十秒结论字段。\n", encoding="utf-8")
    summary_path = candidate_dir / "summary.json"
    summary = _summary(
        run_id="run_test",
        evidence_digest="a" * 64,
        locator="context.md page 1",
        english_fallback=False,
        allowed_mixed=False,
        semantic_mutation=None,
    )
    summary["tldr"] = tldr
    summary["one_sentence_summary"] = one_sentence_summary
    summary_path.write_bytes(canonical_json_bytes(summary))

    def inner_ref(path: Path, role: str, media_type: str) -> dict[str, object]:
        raw = path.read_bytes()
        return {
            "role": role,
            "path": path.relative_to(run_dir).as_posix(),
            "sha256": sha256_bytes(raw),
            "size_bytes": len(raw),
            "media_type": media_type,
        }

    source = {
        "source_type": "local_pdf",
        "requested_path": str(tmp_path / "paper.pdf"),
        "resolved_path": str(tmp_path / "paper.pdf"),
        "sha256": "b" * 64,
        "size_bytes": 10,
        "device": 1,
        "inode": 2,
    }
    placeholder = inner_ref(summary_path, "sealed_review", "application/json")
    candidate = {
        "schema_version": "paper_reader.candidate.v2",
        "candidate_id": "candidate_test",
        "run_id": "run_test",
        "created_at": "2026-07-10T00:00:00Z",
        "source": source,
        "target": {
            "target_type": "local",
            "resolved_path": str(tmp_path / "paper_note.md"),
            "parent_device": 1,
            "parent_inode": 1,
        },
        "evidence_manifest": {**placeholder, "role": "evidence_manifest_snapshot"},
        "sealed_review": placeholder,
        "note_title": "[Codex Summary] 测试",
        "tags": ["codex-summary", "paper-summary"],
        "content_sha256": sha256_bytes(note.read_bytes()),
        "content_length": len(note.read_bytes()),
        "artifacts": [
            inner_ref(note, "note_markdown", "text/markdown"),
            inner_ref(summary_path, "summary_snapshot", "application/json"),
        ],
        "gate": {
            "status": "write_ready",
            "evaluated_at": "2026-07-10T00:00:00Z",
            "checks": [],
            "blockers": [],
        },
        "live_preflight": None,
    }
    candidate_path = candidate_dir / "candidate.json"
    candidate_path.write_bytes(canonical_json_bytes(candidate))
    raw = candidate_path.read_bytes()
    return (
        ArtifactRef(
            path=str(candidate_path),
            size_bytes=len(raw),
            sha256=sha256_bytes(raw),
            schema_version="paper_reader.candidate.v2",
            artifact_id="candidate_test",
        ),
        note,
        summary_path,
    )


def test_run_report_is_byte_identical_until_a_new_journal_event(tmp_path: Path) -> None:
    run_dir = _initialized_local_run(tmp_path)
    snapshot = json.loads((run_dir / "state.json").read_text())
    snapshot["batch_status"] = "succeeded"
    (run_dir / "state.json").write_text(json.dumps(snapshot), encoding="utf-8")
    event_bytes = _tree_bytes(run_dir / "events")

    first = run_report(run_dir)
    first_json = (run_dir / "batch-report.json").read_bytes()
    first_md = (run_dir / "batch-report.md").read_bytes()
    report = BatchReport.model_validate_json(first_json)

    assert first["batch_status"] == "ready"
    assert report.batch_status == "ready"
    assert report.effective_write_policy == "local_only"
    assert report.items[0].status == "queued"
    assert first_json == canonical_json_bytes(report)
    assert report.report_generation_id != "0" * 64
    assert report.report_markdown_sha256 == sha256_bytes(first_md)
    assert b"Effective write policy: local_only" in first_md
    assert _tree_bytes(run_dir / "events") == event_bytes

    second = run_report(run_dir)
    second_json = (run_dir / "batch-report.json").read_bytes()
    second_md = (run_dir / "batch-report.md").read_bytes()

    assert second_json == first_json
    assert second_md == first_md
    assert second["report_sha256"] == sha256_bytes(second_json)
    assert second["report_markdown_sha256"] == sha256_bytes(second_md)
    assert _tree_bytes(run_dir / "events") == event_bytes

    _advance_report_journal(run_dir)
    third = run_report(run_dir)
    third_json = (run_dir / "batch-report.json").read_bytes()
    third_md = (run_dir / "batch-report.md").read_bytes()

    assert third_json != second_json
    assert third_md != second_md
    assert third["report_sha256"] == sha256_bytes(third_json)
    assert third["report_markdown_sha256"] == sha256_bytes(third_md)
    assert not list(run_dir.glob(".batch-report.*.writing"))
    assert not list(run_dir.glob(".batch-report.*.tmp"))


def test_report_json_is_the_commit_marker_for_one_markdown_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import paper_reader_batch.v2_report as report_module

    run_dir = _initialized_local_run(tmp_path)
    run_report(run_dir)
    committed = BatchReport.model_validate_json((run_dir / "batch-report.json").read_bytes())
    assert committed.report_markdown_sha256 == sha256_bytes((run_dir / "batch-report.md").read_bytes())
    _advance_report_journal(run_dir)

    original_replace = report_module._replace_or_publish
    calls = 0

    def fail_before_json(path: Path, content: bytes, *, transition_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected crash before JSON commit marker")
        original_replace(path, content, transition_id=transition_id)

    monkeypatch.setattr(report_module, "_replace_or_publish", fail_before_json)
    with pytest.raises(OSError, match="injected crash"):
        run_report(run_dir)

    stale_marker = BatchReport.model_validate_json((run_dir / "batch-report.json").read_bytes())
    assert stale_marker.report_generation_id == committed.report_generation_id
    assert stale_marker.report_markdown_sha256 != sha256_bytes((run_dir / "batch-report.md").read_bytes())

    monkeypatch.setattr(report_module, "_replace_or_publish", original_replace)
    repaired = run_report(run_dir)
    repaired_model = BatchReport.model_validate_json((run_dir / "batch-report.json").read_bytes())
    assert repaired_model.report_generation_id != committed.report_generation_id
    assert repaired_model.report_markdown_sha256 == sha256_bytes((run_dir / "batch-report.md").read_bytes())
    assert repaired["report_generation_id"] == repaired_model.report_generation_id


def test_run_report_rejects_markdown_inode_replacement_after_json_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _initialized_local_run(tmp_path)
    run_report(run_dir)
    _advance_report_journal(run_dir)
    markdown_path = run_dir / "batch-report.md"
    detached_markdown = tmp_path / "detached-batch-report.md"
    original_replace = report_module._replace_or_publish

    def replace_then_swap_markdown(
        path: Path,
        content: bytes,
        *,
        transition_id: str,
    ) -> None:
        original_replace(path, content, transition_id=transition_id)
        if path.name == "batch-report.json":
            exact_markdown = markdown_path.read_bytes()
            markdown_path.rename(detached_markdown)
            markdown_path.write_bytes(exact_markdown)
            markdown_path.chmod(0o600)

    monkeypatch.setattr(
        report_module,
        "_replace_or_publish",
        replace_then_swap_markdown,
    )
    with pytest.raises(BatchRuntimeError) as exc_info:
        run_report(run_dir)

    assert exc_info.value.code == "storage_path_changed"
    assert detached_markdown.read_bytes() == markdown_path.read_bytes()


@pytest.mark.parametrize("target_name", ["batch-report.md", "batch-report.json"])
@pytest.mark.parametrize(
    ("stage", "requires_active_recovery"),
    [
        ("after_file_fsync", True),
        ("after_exchange_fsync", True),
        ("after_active_owner_unlink", False),
    ],
)
def test_report_transition_hard_crash_recovers_exact_journal_generation(
    tmp_path: Path,
    target_name: str,
    stage: str,
    requires_active_recovery: bool,
) -> None:
    run_dir = _initialized_local_run(tmp_path)
    run_report(run_dir)
    _advance_report_journal(run_dir)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_report_replace,
        args=(str(run_dir), target_name, stage),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 31

    load_view = __import__(
        "paper_reader_batch.v2_journal",
        fromlist=["load_run_view"],
    ).load_run_view
    if requires_active_recovery:
        with pytest.raises(BatchRuntimeError) as exc_info:
            load_view(run_dir)
        assert exc_info.value.code == "storage_recovery_required"
    else:
        assert load_view(run_dir).state.latest_event_sha256 is not None

    run_report(run_dir)
    report = BatchReport.model_validate_json((run_dir / "batch-report.json").read_bytes())
    markdown = (run_dir / "batch-report.md").read_bytes()
    assert report.generated_at == "2026-07-10T00:02:00Z"
    assert report.report_markdown_sha256 == sha256_bytes(markdown)


def test_report_markdown_sanitizes_manifest_title_to_one_heading_line(tmp_path: Path) -> None:
    run_dir = _initialized_local_run(
        tmp_path,
        batch_title="正常标题\n\n- Batch status: succeeded",
    )

    run_report(run_dir)

    markdown = (run_dir / "batch-report.md").read_text(encoding="utf-8")
    assert markdown.splitlines()[0] == (
        "# paper_reader_batch Report: 正常标题 - Batch status: succeeded"
    )
    assert markdown.splitlines().count("- Batch status: ready") == 1
    assert "\n- Batch status: succeeded\n" not in markdown


def test_report_table_cell_collapses_carriage_returns_and_escapes_raw_html() -> None:
    rendered = _cell("失败原因\r- Batch status: succeeded <script>alert(1)</script>")

    assert rendered == (
        "失败原因 - Batch status: succeeded &lt;script&gt;alert(1)&lt;/script&gt;"
    )
    assert len(rendered.splitlines()) == 1


def test_takeaway_parser_uses_only_canonical_section_and_preserves_escaped_pipe() -> None:
    markdown = """# note

```markdown
| 30 秒结论 | 伪造值 |
```

## 0. 阅读结论

| 项目 | 内容 |
| --- | --- |
| 30 秒结论 | A \\| B |

## 1. 其他
| 30 秒结论 | 另一个伪造值 |
"""

    assert _extract_markdown_takeaway(markdown) == (
        "A | B",
        "rendered_note_30_second_row",
    )


def test_takeaway_parser_rejects_duplicate_canonical_rows() -> None:
    markdown = """## 0. 阅读结论

| 项目 | 内容 |
| --- | --- |
| 30 秒结论 | 第一条 |
| 30 秒结论 | 第二条 |
"""

    with pytest.raises(BatchRuntimeError) as exc_info:
        _extract_markdown_takeaway(markdown)

    assert exc_info.value.code == "report_source_invalid"


def test_run_report_extracts_exact_candidate_30_second_conclusion_without_resummarizing(
    tmp_path: Path,
) -> None:
    built = _local_fixture(tmp_path / "paper-reader-artifacts")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(built.manifest))
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    run_dir = tmp_path / "run"
    initialize_run(
        manifest_path,
        request_id=INIT_REQUEST,
        skill_root=skill_root,
        output=run_dir,
        initialized_at="2026-07-10T00:00:00Z",
    )
    assignment = claim_worker(
        run_dir,
        worker_id="worker-1",
        request_id=CLAIM_REQUEST,
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    result = built.result.model_copy(
        update={
            "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
            "claim_id": assignment["claim_id"],
            "attempt_id": assignment["attempt_id"],
            "attempt_number": assignment["attempt_number"],
            "lease_token_sha256": sha256_bytes(assignment["lease_token"].encode()),
        }
    )
    result_path = tmp_path / "worker-result.json"
    result_path.write_bytes(canonical_json_bytes(result))
    finish_worker(
        run_dir,
        "001",
        worker_id="worker-1",
        claim_id=assignment["claim_id"],
        attempt_id=assignment["attempt_id"],
        lease_token=assignment["lease_token"],
        result_path=result_path,
        request_id=FINISH_REQUEST,
        now="2026-07-10T00:00:02Z",
    )

    run_report(run_dir)
    report = BatchReport.model_validate_json((run_dir / "batch-report.json").read_bytes())
    item = report.items[0]
    candidate_note = built.candidate_path.parent / "note.md"

    assert item.status == "succeeded"
    assert item.write_status == "not_applicable"
    assert item.thirty_second_takeaway == "本文展示了可追溯的阅读流程。"
    assert item.takeaway_source_type == "rendered_note_30_second_section"
    assert item.takeaway_source_path == str(candidate_note)
    assert item.takeaway_source_sha256 == sha256_bytes(candidate_note.read_bytes())
    markdown = (run_dir / "batch-report.md").read_text(encoding="utf-8")
    assert "本文展示了可追溯的阅读流程。" in markdown
    assert "重新总结" not in markdown


@pytest.mark.parametrize(
    ("tldr", "one_sentence", "expected", "source_type"),
    [
        ("优先使用结构化 TLDR。", "不应使用的一句话。", "优先使用结构化 TLDR。", "structured_tldr_fallback"),
        (None, "使用一句话结论。", "使用一句话结论。", "structured_one_sentence_summary_fallback"),
    ],
)
def test_candidate_takeaway_falls_back_without_generating_new_summary(
    tmp_path: Path,
    tldr: str | None,
    one_sentence: str,
    expected: str,
    source_type: str,
) -> None:
    candidate_ref, _note, summary = _candidate_for_fallback(
        tmp_path,
        tldr=tldr,
        one_sentence_summary=one_sentence,
    )

    takeaway = _takeaway_from_candidate(candidate_ref)

    assert takeaway == {
        "thirty_second_takeaway": expected,
        "takeaway_source_type": source_type,
        "takeaway_source_path": str(summary),
        "takeaway_source_sha256": sha256_bytes(summary.read_bytes()),
    }


def test_candidate_takeaway_rejects_tampered_bound_note_ref(tmp_path: Path) -> None:
    candidate_ref, note, _summary_path = _candidate_for_fallback(
        tmp_path,
        tldr="fallback must not hide tampering",
        one_sentence_summary="also forbidden",
    )
    note.write_text("# 篡改后的内容\n", encoding="utf-8")

    with pytest.raises(BatchRuntimeError) as exc_info:
        _takeaway_from_candidate(candidate_ref)

    assert exc_info.value.code == "report_source_invalid"


@pytest.mark.parametrize(
    ("role", "basename", "media_type"),
    [
        ("note_markdown", "note.md", "text/markdown"),
        ("note_html", "note.html", "text/html"),
    ],
)
def test_report_candidate_opaque_member_is_capped_at_opaque_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    basename: str,
    media_type: str,
) -> None:
    candidate_ref, _note, _summary = _candidate_for_fallback(
        tmp_path,
        tldr="fallback",
        one_sentence_summary="fallback",
    )
    candidate_path = Path(candidate_ref.path)
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    run_dir = candidate_path.parent.parent.parent
    oversized = {
        "role": role,
        "path": (candidate_path.parent / basename).relative_to(run_dir).as_posix(),
        "sha256": "0" * 64,
        "size_bytes": MAX_OPAQUE_ARTIFACT_BYTES + 1,
        "media_type": media_type,
    }
    candidate["artifacts"] = [
        ref for ref in candidate["artifacts"] if ref["role"] != role
    ] + [oversized]
    observed: list[int | None] = []

    def reject_read(path: Path, *, code: str, max_bytes: int | None = None) -> bytes:
        observed.append(max_bytes)
        raise BatchRuntimeError(code, f"stopped before reading {path}")

    monkeypatch.setattr(report_module, "read_bytes", reject_read)
    with pytest.raises(BatchRuntimeError):
        _read_candidate_inner(
            run_dir,
            candidate_path.parent,
            candidate,
            role=role,
            basename=basename,
            media_type=media_type,
        )

    assert observed == [MAX_OPAQUE_ARTIFACT_BYTES]


@pytest.mark.parametrize("schema_version", ["paper_reader_batch.manifest.v1", "paper_reader_batch.manifest.v99"])
def test_run_report_rejects_v1_and_unknown_without_mutation(
    tmp_path: Path,
    schema_version: str,
) -> None:
    run_dir = _initialized_local_run(tmp_path)
    payload = json.loads((run_dir / "manifest.json").read_text())
    payload["schema_version"] = schema_version
    (run_dir / "manifest.json").write_bytes(canonical_json_bytes(payload))
    before = _tree_bytes(run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        run_report(run_dir)

    assert exc_info.value.code == "unsupported_run_schema"
    assert _tree_bytes(run_dir) == before
    assert not (run_dir / "batch-report.json").exists()
    assert not (run_dir / "batch-report.md").exists()


def test_report_json_existing_sparse_file_is_rejected_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "batch-report.json"
    with report.open("wb") as stream:
        stream.truncate(MAX_JSON_ARTIFACT_BYTES + 1)

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("oversized report JSON reached os.read")

    monkeypatch.setattr(report_module.os if hasattr(report_module, "os") else os, "read", forbidden_read)
    with pytest.raises(BatchRuntimeError) as exc_info:
        _replace_or_publish(report, b"{}", transition_id="report-sparse")

    assert exc_info.value.code == "report_output_invalid"


def test_generated_report_json_is_rejected_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "batch-report.json"
    monkeypatch.setattr(report_module, "MAX_JSON_ARTIFACT_BYTES", 4)

    with pytest.raises(BatchRuntimeError) as exc_info:
        _replace_or_publish(report, b"12345", transition_id="report-oversized")

    assert exc_info.value.code == "report_output_invalid"
    assert not report.exists()
