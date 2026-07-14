from __future__ import annotations

from dataclasses import dataclass
import ast
from html import escape
import hashlib
import json
from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError

import paper_reader_batch.v2_artifacts as artifact_module
import paper_reader_batch.v2_worker as worker_module
from paper_reader_batch.v2_artifacts import (
    paper_reader_root_identity,
    _require_normalized_absolute_path,
    validate_worker_result_artifacts,
)
from paper_reader_batch.v2_contracts import (
    ArtifactRef,
    BatchManifest,
    FileIdentity,
    LocalPrepareResult,
    PdfManifestItem,
    PdfSource,
    SourceSummary,
    WorkerResult,
    ZoteroTitleManifestItem,
    ZoteroTitleSource,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_journal import load_run_view
from paper_reader_batch.v2_json import canonical_json_bytes, canonical_sha256, sha256_bytes
from paper_reader_batch.v2_local_prepare import claim_local_prepare, finish_local_prepare
from paper_reader_batch.v2_run import initialize_run, recover_run
from paper_reader_batch.v2_worker import claim_worker, finish_worker, worker_prompt


NOW = "2026-07-10T00:00:00Z"
MANIFEST_ID = "11111111-1111-4111-8111-111111111111"
CLAIM_ID = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = "33333333-3333-4333-8333-333333333333"
REVIEW_CHECKS = (
    "summary_schema",
    "review_schema",
    "run_binding",
    "evidence_binding",
    "locator_membership",
    "resolved_render_chinese_prose",
)
LOCAL_CANDIDATE_CHECKS = (
    "source_identity",
    "evidence_hashes",
    "sealed_review_hashes",
    "rendered_note_hash",
    "fixed_local_target",
)
ZOTERO_CANDIDATE_CHECKS = (
    "source_identity",
    "evidence_hashes",
    "sealed_review_hashes",
    "parent_fingerprint",
    "live_title_availability",
    "canonical_html_binding",
)


def _escape_markdown_title(note_title: str) -> str:
    prefix = "[Codex Summary] "
    fixed_prefix = prefix if note_title.startswith(prefix) else ""
    remainder = note_title[len(fixed_prefix) :]
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "&"):
        remainder = remainder.replace(character, f"\\{character}")
    return f"{fixed_prefix}{remainder}"


def _write(path: Path, content: bytes) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


def _json(path: Path, value: object, *, canonical: bool = True) -> bytes:
    raw = (
        canonical_json_bytes(value)
        if canonical
        else (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    )
    return _write(path, raw)


def _foreign_ref(run_dir: Path, path: Path, role: str, media: str) -> dict[str, object]:
    raw = path.read_bytes()
    return {
        "role": role,
        "path": path.relative_to(run_dir).as_posix(),
        "sha256": sha256_bytes(raw),
        "size_bytes": len(raw),
        "media_type": media,
    }


def _outer_ref(path: Path, schema: str, artifact_id: str) -> ArtifactRef:
    raw = path.read_bytes()
    return ArtifactRef(
        path=str(path),
        size_bytes=len(raw),
        sha256=sha256_bytes(raw),
        schema_version=schema,
        artifact_id=artifact_id,
    )


def _gate(status: str, checks: tuple[str, ...]) -> dict[str, object]:
    return {
        "status": status,
        "evaluated_at": NOW,
        "checks": list(checks),
        "blockers": [],
    }


def _render_like_single(note_md: bytes) -> bytes:
    def heading_text(value: str) -> str:
        for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "&"):
            value = value.replace(f"\\{character}", character)
        return value

    rendered: list[str] = []
    for raw_line in note_md.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            rendered.append(f"<h2>{escape(heading_text(line[3:]), quote=False)}</h2>")
        elif line.startswith("# "):
            rendered.append(f"<h1>{escape(heading_text(line[2:]), quote=False)}</h1>")
        else:
            rendered.append(f"<p>{escape(line, quote=False)}</p>")
    return ("\n".join(rendered) + "\n").encode("utf-8")


def _parent_fingerprint(*, key: str, title: str, doi: str, version: int) -> str:
    return canonical_sha256(
        {"key": key, "title": title.casefold(), "DOI": doi.casefold(), "version": version}
    )


@dataclass
class BuiltWorkerFixture:
    manifest: BatchManifest
    result: WorkerResult
    run_dir: Path
    run_path: Path
    candidate_path: Path
    source_path: Path
    evidence_path: Path

    def rewrite_final_run(self, payload: dict[str, object]) -> None:
        _json(self.run_path, payload)
        self.result = self.result.model_copy(
            update={
                "paper_reader_run": _outer_ref(
                    self.run_path,
                    "paper_reader.run.v2",
                    str(payload["run_id"]),
                )
            }
        )


@dataclass
class PreparedWorkerFixture:
    batch_run: Path
    built_worker: BuiltWorkerFixture
    local_result: LocalPrepareResult
    local_result_sha256: str
    worker_assignment: dict[str, object]


def _rewrite_candidate_notes(
    built: BuiltWorkerFixture,
    *,
    note_md: bytes | None = None,
    note_html: bytes | None = None,
) -> None:
    candidate = json.loads(built.candidate_path.read_text())
    candidate_dir = built.candidate_path.parent
    if note_md is not None:
        _write(candidate_dir / "note.md", note_md)
    if note_html is not None:
        _write(candidate_dir / "note.html", note_html)
    for role, name in (("note_markdown", "note.md"), ("note_html", "note.html")):
        ref = next(ref for ref in candidate["artifacts"] if ref["role"] == role)
        ref.update(_foreign_ref(built.run_dir, candidate_dir / name, role, ref["media_type"]))
    canonical_html = (candidate_dir / "note.html").read_text().rstrip("\r\n")
    candidate["content_sha256"] = sha256_bytes(canonical_html.encode())
    candidate["content_length"] = len(canonical_html)
    _json(built.candidate_path, candidate)

    candidate_ref = _foreign_ref(
        built.run_dir,
        built.candidate_path,
        "candidate",
        "application/json",
    )
    run = json.loads(built.run_path.read_text())
    run["artifacts"] = [
        candidate_ref if ref["role"] == "candidate" else ref for ref in run["artifacts"]
    ]
    built.rewrite_final_run(run)
    built.result = built.result.model_copy(
        update={
            "candidate": _outer_ref(
                built.candidate_path,
                "paper_reader.candidate.v2",
                "candidate_test",
            )
        }
    )


def _make_pdf_source(path: Path) -> PdfSource:
    stat = path.stat()
    return PdfSource(
        path=str(path),
        size_bytes=stat.st_size,
        sha256=sha256_bytes(path.read_bytes()),
        file_identity={"device": stat.st_dev, "inode": stat.st_ino},
    )


def _make_evidence(
    run_dir: Path,
    *,
    run_id: str,
    source_sha256: str,
    evidence_id: str = "evidence_test",
) -> tuple[Path, dict[str, object], dict[str, object]]:
    evidence_dir = run_dir / "evidence" / evidence_id
    files: list[dict[str, object]] = []
    specs = {
        "metadata.json": ("metadata", "application/json", canonical_json_bytes({"title": "测试论文"})),
        "extract.json": (
            "extract",
            "application/json",
            canonical_json_bytes({"pages": [{"page": 1, "text": "证据正文"}]}),
        ),
        "context.md": ("context", "text/markdown", b"# Methods\n\npage 1 evidence\n"),
        "section_context.md": ("section_context", "text/markdown", b"# Methods\n"),
        "secondary_sources.json": ("secondary_sources", "application/json", b"[]"),
    }
    for name, (role, media, raw) in specs.items():
        path = evidence_dir / name
        _write(path, raw)
        files.append(_foreign_ref(run_dir, path, role, media))
    evidence = {
        "format": "paper_reader.evidence.v2-internal",
        "evidence_id": evidence_id,
        "run_id": run_id,
        "created_at": NOW,
        "source_sha256": source_sha256,
        "complete": True,
        "degraded": False,
        "preview_pages": None,
        "files": files,
        "pages": [1],
        "sections": [{"title": "Methods", "start_page": 1, "end_page": 1}],
        "table_candidates": [{"index": 1, "page": 1, "section": "Methods"}],
        "figures": [],
        "resource_checks": [
            {"name": "pdf_page_count", "status": "passed", "actual": 1, "limit": 500, "message": None}
        ],
    }
    evidence_path = evidence_dir / "evidence.json"
    evidence_raw = _json(evidence_path, evidence)
    evidence_ref = _foreign_ref(run_dir, evidence_path, "evidence_manifest", "application/json")
    assert evidence_ref["sha256"] == sha256_bytes(evidence_raw)
    return evidence_path, evidence, evidence_ref


def _summary(
    *,
    run_id: str,
    evidence_digest: str,
    locator: str,
    english_fallback: bool,
    allowed_mixed: bool,
    semantic_mutation: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "paper_reader.summary.v2",
        "summary_id": "summary_test",
        "run_id": run_id,
        "created_at": NOW,
        "evidence_digest": evidence_digest,
        "paper_type": "method_paper",
        "trust_status": "usable_with_caveats",
        "review_status": "passed",
        "improvement_status": "not_needed",
        "trust_rationale": "正文页与结构化抽取结果可以相互核对。",
        "one_sentence_summary": "本文展示了一个可追溯的论文阅读流程。",
        "abstract_translation": "摘要说明该流程把正文证据与结构化结论连接起来。",
        "research_question": "如何生成能够追溯到原文页码的阅读笔记？",
        "method": (
            "This method extracts the paper and validates the evidence chain."
            if english_fallback
            else "方法先抽取正文，再对证据与结论执行结构化复核。"
        ),
        "experiments": "作者使用示例论文验证抽取、复核与渲染链路。",
        "ai4s_relevance": "该流程可用于材料与物理方向的论文归档。",
        "key_points": ["完整抽取", "证据定位", "复核门禁"],
        "contributions": ["把阅读结论与证据定位放在同一份笔记中。"],
        "limitations": ["抽取质量仍受原始 PDF 排版影响。"],
        "follow_up_keywords": ["evidence locator", "paper reading"],
        "evidence_summary": [
            {
                "claim": "该流程保留了结论到正文页的定位关系。",
                "evidence": [{"type": "text", "locator": locator, "summary": "正文页给出证据。"}],
                "confidence": "medium",
            }
        ],
        "note_labels": ["AI4S"],
    }
    if allowed_mixed:
        payload["method_overview"] = "结合 XPS depth profiling 与 MLFF/DFT 分析 Li-S 界面。"
    if semantic_mutation == "rejected":
        payload["trust_status"] = "rejected"
    elif semantic_mutation == "empty_required":
        payload["method"] = "   "
    elif semantic_mutation == "empty_claims":
        payload["evidence_summary"] = []
    elif semantic_mutation == "empty_claim_evidence":
        payload["evidence_summary"][0]["evidence"] = []
    return payload


def _make_review(
    run_dir: Path,
    *,
    run_id: str,
    evidence_path: Path,
    evidence_ref: dict[str, object],
    locator: str = "context.md page 1 section Methods table_candidate 1",
    english_fallback: bool = False,
    allowed_mixed: bool = False,
    semantic_mutation: str | None = None,
    noncanonical_summary: bool = False,
    noncanonical_validation: bool = False,
    mismatched_rendered_html: bool = False,
    review_checks: tuple[str, ...] = REVIEW_CHECKS,
) -> tuple[Path, dict[str, object], dict[str, bytes]]:
    package_id = "review-package_test"
    review_dir = run_dir / "reviews" / package_id
    summary = _summary(
        run_id=run_id,
        evidence_digest=str(evidence_ref["sha256"]),
        locator=locator,
        english_fallback=english_fallback,
        allowed_mixed=allowed_mixed,
        semantic_mutation=semantic_mutation,
    )
    summary_raw = _json(
        review_dir / "summary.json",
        summary,
        canonical=not noncanonical_summary,
    )
    review = {
        "schema_version": "paper_reader.review.v2",
        "review_id": "review_test",
        "run_id": run_id,
        "created_at": NOW,
        "summary_sha256": sha256_bytes(summary_raw),
        "evidence_digest": evidence_ref["sha256"],
        "review_status": "passed",
        "needs_improvement": False,
        "review_issues": [],
        "trust_status_recommendation": "usable_with_caveats",
        "improvement_requests": [],
    }
    review_raw = _json(review_dir / "review.json", review)
    evidence_raw = _write(review_dir / "evidence.json", evidence_path.read_bytes())
    note_md = (
        "# [Codex Summary] 测试论文 - 2026-07-10\n\n"
        "## 30 秒结论\n\n本文展示了可追溯的阅读流程。\n\n"
        + (
            "## 方法\n\nThis method extracts the paper and validates the evidence chain.\n"
            if english_fallback
            else "## 方法\n\n方法先抽取正文，再执行结构化复核。\n"
        )
        + (
            "\n结合 XPS depth profiling 与 MLFF/DFT 分析 Li-S 界面。\n"
            if allowed_mixed
            else ""
        )
    ).encode()
    note_html = _render_like_single(note_md)
    if mismatched_rendered_html:
        note_html = (
            "<h1>[Codex Summary] 测试论文 - 2026-07-10</h1>"
            "<p>这段 HTML 正文没有来自已封存的 Markdown。</p>\n"
        ).encode()
    _write(review_dir / "note.md", note_md)
    _write(review_dir / "note.html", note_html)
    validation = {
        "format": "paper_reader.review-validation.v2-internal",
        "run_id": run_id,
        "summary_sha256": sha256_bytes(summary_raw),
        "review_sha256": sha256_bytes(review_raw),
        "evidence_digest": evidence_ref["sha256"],
        "rendered_note_sha256": sha256_bytes(note_md),
        "rendered_html_sha256": sha256_bytes(note_html),
        "checks": list(review_checks),
        "blockers": [],
    }
    validation_raw = _json(
        review_dir / "validation.json",
        validation,
        canonical=not noncanonical_validation,
    )
    specs = {
        "summary.json": ("summary_snapshot", "application/json"),
        "review.json": ("review_snapshot", "application/json"),
        "evidence.json": ("evidence_manifest_snapshot", "application/json"),
        "validation.json": ("review_validation", "application/json"),
        "note.md": ("review_note_markdown", "text/markdown"),
        "note.html": ("review_note_html", "text/html"),
    }
    artifacts = [
        _foreign_ref(run_dir, review_dir / name, role, media)
        for name, (role, media) in specs.items()
    ]
    refs = {ref["role"]: ref for ref in artifacts}
    package = {
        "schema_version": "paper_reader.review-package.v2",
        "review_package_id": package_id,
        "run_id": run_id,
        "created_at": NOW,
        "summary": refs["summary_snapshot"],
        "review": refs["review_snapshot"],
        "evidence_manifest": refs["evidence_manifest_snapshot"],
        "summary_sha256": sha256_bytes(summary_raw),
        "review_sha256": sha256_bytes(review_raw),
        "evidence_digest": evidence_ref["sha256"],
        "artifacts": artifacts,
        "gate": _gate("passed", review_checks),
    }
    package_path = review_dir / "review-package.json"
    _json(package_path, package)
    return package_path, package, {
        "summary.json": summary_raw,
        "review.json": review_raw,
        "evidence.json": evidence_raw,
        "validation.json": validation_raw,
        "note.md": note_md,
        "note.html": note_html,
        "review-package.json": package_path.read_bytes(),
    }


def _make_candidate(
    run_dir: Path,
    *,
    run_snapshot: dict[str, object],
    source: dict[str, object],
    target: dict[str, object],
    evidence_path: Path,
    review_path: Path,
    review_snapshots: dict[str, bytes],
    source_snapshot: bytes,
    candidate_checks: tuple[str, ...],
    zotero_extra: dict[str, bytes] | None = None,
) -> tuple[Path, dict[str, object], dict[str, object]]:
    candidate_id = "candidate_test"
    candidate_dir = run_dir / "candidates" / candidate_id
    snapshots = {
        "run.json": canonical_json_bytes(run_snapshot),
        "source.json": source_snapshot,
        "evidence.json": evidence_path.read_bytes(),
        **review_snapshots,
    }
    snapshots.pop("review-package.json", None)
    snapshots["review-package.json"] = review_path.read_bytes()
    if zotero_extra:
        snapshots.update(zotero_extra)
    for name, raw in snapshots.items():
        _write(candidate_dir / name, raw)
    specs = {
        "run.json": ("run_snapshot", "application/json"),
        "source.json": ("source_snapshot", "application/json"),
        "evidence.json": ("evidence_manifest_snapshot", "application/json"),
        "summary.json": ("summary_snapshot", "application/json"),
        "review.json": ("review_snapshot", "application/json"),
        "review-package.json": ("review_package_snapshot", "application/json"),
        "validation.json": ("review_validation", "application/json"),
        "note.md": ("note_markdown", "text/markdown"),
        "note.html": ("note_html", "text/html"),
    }
    if zotero_extra:
        specs.update(
            {
                "discovery.raw.json": ("raw_discovery_bundle_snapshot", "application/json"),
                "parent.json": ("zotero_parent_snapshot", "application/json"),
                "children.json": ("zotero_children_snapshot", "application/json"),
            }
        )
    artifacts = [
        _foreign_ref(run_dir, candidate_dir / name, role, media)
        for name, (role, media) in specs.items()
    ]
    refs = {ref["role"]: ref for ref in artifacts}
    note_md = snapshots["note.md"]
    note_html = snapshots["note.html"].decode()
    note_title = (
        target["note_title"]
        if target["target_type"] == "zotero"
        else note_md.decode().splitlines()[0].removeprefix("# ")
    )
    candidate = {
        "schema_version": "paper_reader.candidate.v2",
        "candidate_id": candidate_id,
        "run_id": run_snapshot["run_id"],
        "created_at": NOW,
        "source": source,
        "target": target,
        "evidence_manifest": refs["evidence_manifest_snapshot"],
        "sealed_review": refs["review_package_snapshot"],
        "note_title": note_title,
        "tags": ["codex-summary", "paper-summary", "ai4s"],
        "content_sha256": (
            sha256_bytes(note_html.rstrip("\r\n").encode())
            if target["target_type"] == "zotero"
            else sha256_bytes(note_md)
        ),
        "content_length": (
            len(note_html.rstrip("\r\n")) if target["target_type"] == "zotero" else len(note_md)
        ),
        "artifacts": artifacts,
        "gate": _gate("write_ready", candidate_checks),
        "live_preflight": None,
    }
    if target["target_type"] == "zotero":
        candidate["live_preflight"] = {
            "preflight_id": "preflight_test",
            "captured_at": NOW,
            "parent_key": source["item_key"],
            "parent_fingerprint": source["parent_fingerprint"],
            "requested_note_title": note_title,
            "title_available": True,
            "matching_note_keys": [],
            "parent_snapshot": refs["zotero_parent_snapshot"],
            "children_snapshot": refs["zotero_children_snapshot"],
        }
    candidate_path = candidate_dir / "candidate.json"
    _json(candidate_path, candidate)
    return candidate_path, candidate, _foreign_ref(
        run_dir, candidate_path, "candidate", "application/json"
    )


def _local_fixture(
    tmp_path: Path,
    *,
    locator: str = "context.md page 1 section Methods table_candidate 1",
    english_fallback: bool = False,
    allowed_mixed: bool = False,
    semantic_mutation: str | None = None,
    noncanonical_summary: bool = False,
    noncanonical_validation: bool = False,
    candidate_checks: tuple[str, ...] = LOCAL_CANDIDATE_CHECKS,
    review_checks: tuple[str, ...] = REVIEW_CHECKS,
) -> BuiltWorkerFixture:
    pdf_path = tmp_path / "paper.pdf"
    _write(pdf_path, b"%PDF-1.7\nfixture\n")
    batch_source = _make_pdf_source(pdf_path)
    manifest = BatchManifest(
        schema_version="paper_reader_batch.manifest.v2",
        manifest_id=MANIFEST_ID,
        created_at=NOW,
        batch_title="local batch",
        source_summary=SourceSummary(source_type="pdf_paths", description="fixture"),
        items=[PdfManifestItem(item_id="001", source=batch_source)],
    )
    run_dir = tmp_path / "paper_analysis"
    run_id = "run_local"
    source = {
        "source_type": "local_pdf",
        "requested_path": str(pdf_path),
        "resolved_path": str(pdf_path),
        "sha256": batch_source.sha256,
        "size_bytes": batch_source.size_bytes,
        "device": batch_source.file_identity.device,
        "inode": batch_source.file_identity.inode,
    }
    source_path = run_dir / "source" / "source.json"
    source_raw = _json(source_path, source)
    source_ref = _foreign_ref(run_dir, source_path, "source_snapshot", "application/json")
    evidence_path, _evidence, evidence_ref = _make_evidence(
        run_dir, run_id=run_id, source_sha256=batch_source.sha256
    )
    review_path, package, review_snapshots = _make_review(
        run_dir,
        run_id=run_id,
        evidence_path=evidence_path,
        evidence_ref=evidence_ref,
        locator=locator,
        english_fallback=english_fallback,
        allowed_mixed=allowed_mixed,
        semantic_mutation=semantic_mutation,
        noncanonical_summary=noncanonical_summary,
        noncanonical_validation=noncanonical_validation,
        review_checks=review_checks,
    )
    review_ref = _foreign_ref(run_dir, review_path, "review_package", "application/json")
    target = {
        "target_type": "local",
        "resolved_path": str(tmp_path / "paper_note.md"),
        "parent_device": tmp_path.stat().st_dev,
        "parent_inode": tmp_path.stat().st_ino,
    }
    reviewed_run = {
        "schema_version": "paper_reader.run.v2",
        "run_id": run_id,
        "created_at": NOW,
        "source": source,
        "target": target,
        "status": "reviewed",
        "artifacts": [source_ref, evidence_ref, review_ref],
        "gate": package["gate"],
        "live_preflight": None,
    }
    candidate_path, candidate, candidate_ref = _make_candidate(
        run_dir,
        run_snapshot=reviewed_run,
        source=source,
        target=target,
        evidence_path=evidence_path,
        review_path=review_path,
        review_snapshots=review_snapshots,
        source_snapshot=source_raw,
        candidate_checks=candidate_checks,
    )
    note_bytes = (candidate_path.parent / "note.md").read_bytes()
    _write(Path(target["resolved_path"]), note_bytes)
    intent = {
        "format": "paper_reader.local-publication-intent.v2-internal",
        "run_id": run_id,
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate_ref["sha256"],
        "target_path": target["resolved_path"],
        "content_sha256": candidate["content_sha256"],
        "content_length": candidate["content_length"],
    }
    intent_path = run_dir / "publication-intent.json"
    intent_raw = _json(intent_path, intent)
    intent_ref = _foreign_ref(run_dir, intent_path, "local_publication_intent", "application/json")
    receipt = {
        "format": "paper_reader.local-receipt.v2-internal",
        "receipt_id": "local-receipt-candidate_test",
        "run_id": run_id,
        "candidate_path": "candidates/candidate_test/candidate.json",
        "candidate_digest": candidate_ref["sha256"],
        "intent_path": "publication-intent.json",
        "intent_sha256": sha256_bytes(intent_raw),
        "target_path": target["resolved_path"],
        "content_sha256": candidate["content_sha256"],
        "content_length": candidate["content_length"],
    }
    receipt_path = run_dir / "receipts" / "candidate_test.json"
    _json(receipt_path, receipt)
    receipt_ref = _foreign_ref(run_dir, receipt_path, "local_receipt", "application/json")
    final_run = {
        **reviewed_run,
        "status": "published",
        "artifacts": [source_ref, evidence_ref, review_ref, candidate_ref, intent_ref, receipt_ref],
        "gate": candidate["gate"],
    }
    run_path = run_dir / "run.json"
    _json(run_path, final_run)
    manifest_sha = canonical_sha256(manifest)
    result = WorkerResult(
        schema_version="paper_reader_batch.worker-result.v2",
        manifest_sha256=manifest_sha,
        item_id="001",
        worker_id="worker-1",
        claim_id=CLAIM_ID,
        attempt_id=ATTEMPT_ID,
        attempt_number=1,
        lease_token_sha256="a" * 64,
        status="succeeded",
        source=batch_source,
        paper_reader_run=_outer_ref(run_path, "paper_reader.run.v2", run_id),
        review_package=_outer_ref(
            review_path, "paper_reader.review-package.v2", "review-package_test"
        ),
        candidate=_outer_ref(candidate_path, "paper_reader.candidate.v2", "candidate_test"),
        local_publication=_outer_ref(
            receipt_path,
            "paper_reader.local-receipt.v2-internal",
            "local-receipt-candidate_test",
        ),
    )
    return BuiltWorkerFixture(
        manifest=manifest,
        result=result,
        run_dir=run_dir,
        run_path=run_path,
        candidate_path=candidate_path,
        source_path=source_path,
        evidence_path=evidence_path,
    )


def _fake_paper_reader_root(tmp_path: Path) -> Path:
    root = tmp_path / "paper-reader-root"
    (root / "src" / "paper_reader").mkdir(parents=True)
    (root / "references" / "schemas").mkdir(parents=True)
    (root / "SKILL.md").write_text("# paper_reader V2\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname="paper_reader"\nversion="2.0.0"\n[project.scripts]\n'
        'paper_reader="paper_reader.public_cli:app"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "src" / "paper_reader" / "public_cli.py").write_text(
        "app = object()\n",
        encoding="utf-8",
    )
    for name in [
        "paper_reader.run.v2.schema.json",
        "paper_reader.command-result.v2.schema.json",
        "paper_reader.review-package.v2.schema.json",
        "paper_reader.candidate.v2.schema.json",
    ]:
        (root / "references" / "schemas" / name).write_text("{}\n", encoding="utf-8")
    return root


def _prepared_then_claimed_worker_fixture(tmp_path: Path) -> PreparedWorkerFixture:
    built_worker = _local_fixture(tmp_path)
    source = json.loads(built_worker.source_path.read_text(encoding="utf-8"))
    prepared_run_dir = tmp_path / "paper_analysis_v2"
    prepared_source_path = prepared_run_dir / "source" / "source.json"
    source_raw = _json(prepared_source_path, source)
    source_ref = _foreign_ref(
        prepared_run_dir,
        prepared_source_path,
        "source_snapshot",
        "application/json",
    )
    run_id = "run_prepared_attempt"
    evidence_path, _evidence, evidence_ref = _make_evidence(
        prepared_run_dir,
        run_id=run_id,
        source_sha256=built_worker.manifest.items[0].source.sha256,
    )
    prepared_run = {
        "schema_version": "paper_reader.run.v2",
        "run_id": run_id,
        "created_at": NOW,
        "source": source,
        "target": {
            "target_type": "local",
            "resolved_path": str(tmp_path / "paper_note_v2.md"),
            "parent_device": tmp_path.stat().st_dev,
            "parent_inode": tmp_path.stat().st_ino,
        },
        "status": "prepared",
        "artifacts": [source_ref, evidence_ref],
        "gate": _gate("passed", ()),
        "live_preflight": None,
    }
    prepared_run_path = prepared_run_dir / "run.json"
    _json(prepared_run_path, prepared_run)
    assert sha256_bytes(source_raw) == source_ref["sha256"]

    manifest_path = tmp_path / "batch-manifest.json"
    _json(manifest_path, built_worker.manifest.model_dump(mode="json"))
    batch_skill_root = tmp_path / "batch-skill-root"
    batch_skill_root.mkdir()
    batch_run = tmp_path / "batch-run"
    initialize_run(
        manifest_path,
        request_id="44444444-4444-4444-8444-444444444444",
        skill_root=batch_skill_root,
        output=batch_run,
        initialized_at=NOW,
    )
    local_assignment = claim_local_prepare(
        batch_run,
        worker_id="preparer",
        request_id="55555555-5555-4555-8555-555555555555",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    view = load_run_view(batch_run)
    paper_reader_root = _fake_paper_reader_root(tmp_path)
    local_result = LocalPrepareResult(
        schema_version="paper_reader_batch.local-prepare-result.v2",
        manifest_sha256=view.manifest_sha256,
        item_id=local_assignment["item_id"],
        worker_id=local_assignment["worker_id"],
        claim_id=local_assignment["claim_id"],
        attempt_id=local_assignment["attempt_id"],
        attempt_number=local_assignment["attempt_number"],
        lease_token_sha256=sha256_bytes(local_assignment["lease_token"].encode()),
        status="prepared",
        source=view.manifest.items[0].source,
        paper_reader_root=paper_reader_root_identity(paper_reader_root),
        paper_reader_run_directory={
            "device": prepared_run_dir.stat().st_dev,
            "inode": prepared_run_dir.stat().st_ino,
        },
        paper_reader_run=_outer_ref(
            prepared_run_path,
            "paper_reader.run.v2",
            run_id,
        ),
        evidence=_outer_ref(
            evidence_path,
            "paper_reader.evidence.v2-internal",
            "evidence_test",
        ),
    )
    local_result_path = tmp_path / "local-prepare-result.json"
    local_result_raw = canonical_json_bytes(local_result)
    local_result_path.write_bytes(local_result_raw)
    local_finish = finish_local_prepare(
        batch_run,
        str(local_assignment["item_id"]),
        worker_id=str(local_assignment["worker_id"]),
        claim_id=str(local_assignment["claim_id"]),
        lease_token=str(local_assignment["lease_token"]),
        attempt_id=str(local_assignment["attempt_id"]),
        result_path=local_result_path,
        expected_root=paper_reader_root,
        request_id="66666666-6666-4666-8666-666666666666",
        now="2026-07-10T00:00:02Z",
    )
    worker_assignment = claim_worker(
        batch_run,
        worker_id="reader",
        request_id="77777777-7777-4777-8777-777777777777",
        now="2026-07-10T00:00:03Z",
    ).result["assignments"][0]
    return PreparedWorkerFixture(
        batch_run=batch_run,
        built_worker=built_worker,
        local_result=local_result,
        local_result_sha256=str(local_finish.result["result_sha256"]),
        worker_assignment=worker_assignment,
    )


def _zotero_fixture(
    tmp_path: Path,
    *,
    paper_title: str = "A Useful Paper & Result",
    raw_inventory_title: str | None = None,
    result_inventory_sha256: str | None = None,
    mismatched_rendered_html: bool = False,
) -> BuiltWorkerFixture:
    attachment_path = tmp_path / "zotero-paper.pdf"
    _write(attachment_path, b"%PDF-1.7\nzotero fixture\n")
    attachment_batch = _make_pdf_source(attachment_path)
    query = "Useful Paper" if paper_title == "A Useful Paper & Result" else paper_title
    manifest_source = ZoteroTitleSource(title=query)
    manifest = BatchManifest(
        schema_version="paper_reader_batch.manifest.v2",
        manifest_id=MANIFEST_ID,
        created_at=NOW,
        batch_title="zotero batch",
        source_summary=SourceSummary(source_type="zotero_titles", description="fixture"),
        items=[ZoteroTitleManifestItem(item_id="001", source=manifest_source)],
    )
    run_dir = tmp_path / "a-useful-paper-result"
    run_id = "run_zotero"
    item_key = "PARENT1"
    title = paper_title
    doi = "10.1000/example.doi"
    version = 17
    raw_inventory = [
        {
            "key": item_key,
            "version": version,
            "itemType": "journalArticle",
            "title": raw_inventory_title or title,
            "DOI": doi,
        }
    ]
    raw_selected = {
        "key": item_key,
        "version": version,
        "itemType": "journalArticle",
        "title": title,
        "DOI": doi,
        "attachments": [
            {
                "key": "ATTACH1",
                "version": 3,
                "itemType": "attachment",
                "title": "Full Text PDF",
                "contentType": "application/pdf",
                "path": str(attachment_path),
            }
        ],
        "notes": [],
    }
    raw_payload = {"search_results": raw_inventory, "selected_item": raw_selected}
    raw_path = run_dir / "source" / "discovery.raw.json"
    raw_bytes = _json(raw_path, raw_payload, canonical=False)
    normalized_inventory = [
        {
            "key": item_key,
            "title": title,
            "normalized_title": title.casefold(),
            "DOI": doi,
            "version": version,
        }
    ]
    normalized_attachment = raw_selected["attachments"][0]
    normalized = {
        "format": "paper_reader.zotero-source.v2-internal",
        "search_inventory": normalized_inventory,
        "selected_item": raw_selected,
        "selected_attachment": normalized_attachment,
    }
    normalized_path = run_dir / "source" / "source.json"
    normalized_bytes = _json(normalized_path, normalized)
    raw_ref = _foreign_ref(run_dir, raw_path, "raw_discovery_bundle", "application/json")
    normalized_ref = _foreign_ref(run_dir, normalized_path, "normalized_source", "application/json")
    fingerprint = _parent_fingerprint(key=item_key, title=title, doi=doi, version=version)
    attachment_source = {
        "source_type": "local_pdf",
        "requested_path": str(attachment_path),
        "resolved_path": str(attachment_path),
        "sha256": attachment_batch.sha256,
        "size_bytes": attachment_batch.size_bytes,
        "device": attachment_batch.file_identity.device,
        "inode": attachment_batch.file_identity.inode,
    }
    source = {
        "source_type": "zotero",
        "item_key": item_key,
        "title": title,
        "doi": doi,
        "parent_version": version,
        "parent_fingerprint": fingerprint,
        "raw_discovery_bundle": raw_ref,
        "normalized_source": normalized_ref,
        "attachment_key": "ATTACH1",
        "attachment": attachment_source,
    }
    evidence_path, _evidence, evidence_ref = _make_evidence(
        run_dir, run_id=run_id, source_sha256=attachment_batch.sha256
    )
    review_path, package, review_snapshots = _make_review(
        run_dir,
        run_id=run_id,
        evidence_path=evidence_path,
        evidence_ref=evidence_ref,
        mismatched_rendered_html=mismatched_rendered_html,
    )
    review_ref = _foreign_ref(run_dir, review_path, "review_package", "application/json")
    reviewed_run = {
        "schema_version": "paper_reader.run.v2",
        "run_id": run_id,
        "created_at": NOW,
        "source": source,
        "target": None,
        "status": "reviewed",
        "artifacts": [raw_ref, normalized_ref, evidence_ref, review_ref],
        "gate": package["gate"],
        "live_preflight": None,
    }
    note_title = f"[Codex Summary] {title} - 2026-07-10"
    sealed_md = review_snapshots["note.md"].decode()
    sealed_md_lines = sealed_md.splitlines(keepends=True)
    sealed_md_lines[0] = f"# {_escape_markdown_title(note_title)}\n"
    note_md = "".join(sealed_md_lines).encode()
    note_html = _render_like_single(note_md)
    review_snapshots = {**review_snapshots, "note.md": note_md, "note.html": note_html}
    parent_payload = {
        "key": item_key,
        "version": version,
        "data": {
            "key": item_key,
            "version": version,
            "itemType": "journalArticle",
            "title": title,
            "DOI": doi,
        },
    }
    children_payload = [
        {
            "key": "OTHERNOTE",
            "version": 1,
            "data": {
                "key": "OTHERNOTE",
                "version": 1,
                "itemType": "note",
                "parentItem": item_key,
                "note": "<h1>Another Note</h1><p>existing</p>",
            },
        }
    ]
    target = {
        "target_type": "zotero",
        "parent_key": item_key,
        "parent_fingerprint": fingerprint,
        "note_title": note_title,
    }
    candidate_path, candidate, candidate_ref = _make_candidate(
        run_dir,
        run_snapshot=reviewed_run,
        source=source,
        target=target,
        evidence_path=evidence_path,
        review_path=review_path,
        review_snapshots=review_snapshots,
        source_snapshot=normalized_bytes,
        candidate_checks=ZOTERO_CANDIDATE_CHECKS,
        zotero_extra={
            "discovery.raw.json": raw_bytes,
            "parent.json": canonical_json_bytes(parent_payload),
            "children.json": canonical_json_bytes(children_payload),
        },
    )
    final_run = {
        **reviewed_run,
        "target": target,
        "status": "candidate_built",
        "artifacts": [raw_ref, normalized_ref, evidence_ref, review_ref, candidate_ref],
        "gate": candidate["gate"],
        "live_preflight": candidate["live_preflight"],
    }
    run_path = run_dir / "run.json"
    _json(run_path, final_run)
    inventory_digest = sha256_bytes(canonical_json_bytes(raw_inventory))
    result_source = ZoteroTitleSource(
        title=query,
        resolved_item_key=item_key,
        inventory_sha256=result_inventory_sha256 or inventory_digest,
    )
    result = WorkerResult(
        schema_version="paper_reader_batch.worker-result.v2",
        manifest_sha256=canonical_sha256(manifest),
        item_id="001",
        worker_id="worker-1",
        claim_id=CLAIM_ID,
        attempt_id=ATTEMPT_ID,
        attempt_number=1,
        lease_token_sha256="a" * 64,
        status="succeeded",
        source=result_source,
        paper_reader_run=_outer_ref(run_path, "paper_reader.run.v2", run_id),
        review_package=_outer_ref(
            review_path, "paper_reader.review-package.v2", "review-package_test"
        ),
        candidate=_outer_ref(candidate_path, "paper_reader.candidate.v2", "candidate_test"),
    )
    return BuiltWorkerFixture(
        manifest=manifest,
        result=result,
        run_dir=run_dir,
        run_path=run_path,
        candidate_path=candidate_path,
        source_path=normalized_path,
        evidence_path=evidence_path,
    )


def test_worker_prompt_returns_exact_journal_bound_prepared_attempt(
    tmp_path: Path,
) -> None:
    prepared = _prepared_then_claimed_worker_fixture(tmp_path)
    assignment = prepared.worker_assignment

    prompt = worker_prompt(
        prepared.batch_run,
        str(assignment["item_id"]),
        worker_id=str(assignment["worker_id"]),
        claim_id=str(assignment["claim_id"]),
        lease_token=str(assignment["lease_token"]),
        attempt_id=str(assignment["attempt_id"]),
        now="2026-07-10T00:00:04Z",
    )

    assert prompt["local_prepare_result_sha256"] == prepared.local_result_sha256
    assert prompt["paper_reader_run"] == prepared.local_result.paper_reader_run.model_dump(
        mode="json"
    )
    assert prompt["evidence"] == prepared.local_result.evidence.model_dump(mode="json")


def test_worker_prompt_rejects_same_path_prepared_run_directory_replacement(
    tmp_path: Path,
) -> None:
    prepared = _prepared_then_claimed_worker_fixture(tmp_path)
    assignment = prepared.worker_assignment
    assert prepared.local_result.paper_reader_run is not None
    prepared_run_dir = Path(prepared.local_result.paper_reader_run.path).parent
    detached = tmp_path / "detached-prepared-run"
    prepared_run_dir.rename(detached)
    shutil.copytree(detached, prepared_run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        worker_prompt(
            prepared.batch_run,
            str(assignment["item_id"]),
            worker_id=str(assignment["worker_id"]),
            claim_id=str(assignment["claim_id"]),
            lease_token=str(assignment["lease_token"]),
            attempt_id=str(assignment["attempt_id"]),
            now="2026-07-10T00:00:04Z",
        )

    assert exc_info.value.code == "local_prepare_binding_mismatch"


@pytest.mark.parametrize(
    "schema_version",
    [
        None,
        "paper_reader_batch.local-prepare-result.v1",
        "paper_reader_batch.local-prepare-result.v3",
    ],
)
def test_worker_finish_rejects_unsupported_prepared_result_schema_before_transaction_lock(
    schema_version: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared_then_claimed_worker_fixture(tmp_path)
    assignment = prepared.worker_assignment
    prepared_path = (
        prepared.batch_run
        / "results"
        / "local-prepare"
        / f"{prepared.local_result_sha256}.json"
    )
    payload = json.loads(prepared_path.read_bytes())
    if schema_version is None:
        payload.pop("schema_version")
    else:
        payload["schema_version"] = schema_version
    prepared_path.write_bytes(canonical_json_bytes(payload))

    worker_result = prepared.built_worker.result.model_copy(
        update={
            "manifest_sha256": prepared.local_result.manifest_sha256,
            "worker_id": assignment["worker_id"],
            "claim_id": assignment["claim_id"],
            "attempt_id": assignment["attempt_id"],
            "attempt_number": assignment["attempt_number"],
            "lease_token_sha256": sha256_bytes(str(assignment["lease_token"]).encode()),
        }
    )
    worker_result_path = tmp_path / "worker-result-for-schema-preflight.json"
    worker_result_path.write_bytes(canonical_json_bytes(worker_result))

    def forbidden_transaction(*_args, **_kwargs):
        raise AssertionError("unsupported prepared result reached append_transaction")

    monkeypatch.setattr(worker_module, "append_transaction", forbidden_transaction)

    with pytest.raises(BatchRuntimeError) as exc_info:
        finish_worker(
            prepared.batch_run,
            str(assignment["item_id"]),
            worker_id=str(assignment["worker_id"]),
            claim_id=str(assignment["claim_id"]),
            lease_token=str(assignment["lease_token"]),
            attempt_id=str(assignment["attempt_id"]),
            result_path=worker_result_path,
            request_id="89898989-8989-4989-8989-898989898989",
            now="2026-07-10T00:00:04Z",
        )

    assert exc_info.value.code == "unsupported_run_schema"


def test_worker_finish_rejects_success_from_run_other_than_prepared_attempt(
    tmp_path: Path,
) -> None:
    prepared = _prepared_then_claimed_worker_fixture(tmp_path)
    assignment = prepared.worker_assignment
    worker_result = prepared.built_worker.result.model_copy(
        update={
            "manifest_sha256": load_run_view(prepared.batch_run).manifest_sha256,
            "worker_id": assignment["worker_id"],
            "claim_id": assignment["claim_id"],
            "attempt_id": assignment["attempt_id"],
            "attempt_number": assignment["attempt_number"],
            "lease_token_sha256": sha256_bytes(str(assignment["lease_token"]).encode()),
        }
    )
    worker_result_path = tmp_path / "worker-result-from-run-b.json"
    worker_result_path.write_bytes(canonical_json_bytes(worker_result))

    with pytest.raises(BatchRuntimeError) as exc_info:
        finish_worker(
            prepared.batch_run,
            str(assignment["item_id"]),
            worker_id=str(assignment["worker_id"]),
            claim_id=str(assignment["claim_id"]),
            lease_token=str(assignment["lease_token"]),
            attempt_id=str(assignment["attempt_id"]),
            result_path=worker_result_path,
            request_id="88888888-8888-4888-8888-888888888888",
            now="2026-07-10T00:00:04Z",
        )

    assert exc_info.value.code == "local_prepare_binding_mismatch"
    assert load_run_view(prepared.batch_run).state.items[0].worker_status == "claimed"


def test_run_recover_replays_worker_success_against_exact_prepared_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared_then_claimed_worker_fixture(tmp_path)
    assignment = prepared.worker_assignment
    worker_result = prepared.built_worker.result.model_copy(
        update={
            "manifest_sha256": load_run_view(prepared.batch_run).manifest_sha256,
            "worker_id": assignment["worker_id"],
            "claim_id": assignment["claim_id"],
            "attempt_id": assignment["attempt_id"],
            "attempt_number": assignment["attempt_number"],
            "lease_token_sha256": sha256_bytes(str(assignment["lease_token"]).encode()),
        }
    )
    worker_result_path = tmp_path / "worker-result-from-run-b-for-replay.json"
    worker_result_path.write_bytes(canonical_json_bytes(worker_result))

    # Simulate a worker.finished event written by the pre-fix runtime, whose
    # finish-time validator did not retain the prepared-attempt continuation.
    with monkeypatch.context() as finish_context:
        finish_context.setattr(
            worker_module,
            "validate_worker_result_artifacts",
            lambda *_args, **_kwargs: None,
        )
        finish_worker(
            prepared.batch_run,
            str(assignment["item_id"]),
            worker_id=str(assignment["worker_id"]),
            claim_id=str(assignment["claim_id"]),
            lease_token=str(assignment["lease_token"]),
            attempt_id=str(assignment["attempt_id"]),
            result_path=worker_result_path,
            request_id="89898989-8989-4989-8989-898989898989",
            now="2026-07-10T00:00:04Z",
        )

    before = {
        path.relative_to(prepared.batch_run).as_posix(): path.read_bytes()
        for path in prepared.batch_run.rglob("*")
        if path.is_file()
    }
    with pytest.raises(BatchRuntimeError) as exc_info:
        recover_run(
            prepared.batch_run,
            request_id="90909090-9090-4090-8090-909090909090",
            now="2026-07-10T00:00:05Z",
        )

    assert exc_info.value.code == "local_prepare_binding_mismatch"
    assert {
        path.relative_to(prepared.batch_run).as_posix(): path.read_bytes()
        for path in prepared.batch_run.rglob("*")
        if path.is_file()
    } == before


def test_worker_success_rejects_same_path_same_run_id_directory_replacement(
    tmp_path: Path,
) -> None:
    built = _local_fixture(tmp_path)
    assert built.result.paper_reader_run is not None
    run_payload = json.loads(built.run_path.read_text(encoding="utf-8"))
    evidence_ref = next(
        ref for ref in run_payload["artifacts"] if ref["role"] == "evidence_manifest"
    )
    evidence_path = built.run_dir / evidence_ref["path"]
    evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    run_metadata = built.run_dir.stat()
    prepared = LocalPrepareResult(
        schema_version="paper_reader_batch.local-prepare-result.v2",
        manifest_sha256=built.result.manifest_sha256,
        item_id=built.result.item_id,
        worker_id="preparer",
        claim_id="99999999-9999-4999-8999-999999999999",
        attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        attempt_number=1,
        lease_token_sha256="b" * 64,
        status="prepared",
        source=built.result.source,
        paper_reader_root=paper_reader_root_identity(_fake_paper_reader_root(tmp_path)),
        paper_reader_run_directory=FileIdentity(
            device=run_metadata.st_dev,
            inode=run_metadata.st_ino,
        ),
        paper_reader_run=built.result.paper_reader_run,
        evidence=_outer_ref(
            evidence_path,
            "paper_reader.evidence.v2-internal",
            str(evidence_payload["evidence_id"]),
        ),
    )
    legacy_payload = prepared.model_dump(mode="json")
    legacy_payload.pop("paper_reader_run_directory")
    with pytest.raises(ValidationError):
        LocalPrepareResult.model_validate(legacy_payload)
    assert validate_worker_result_artifacts(
        built.manifest,
        built.result,
        prepared_local_result=prepared,
    ) is None

    detached = tmp_path / "detached-final-run"
    built.run_dir.rename(detached)
    shutil.copytree(detached, built.run_dir)

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(
            built.manifest,
            built.result,
            prepared_local_result=prepared,
        )

    assert exc_info.value.code == "local_prepare_binding_mismatch"


def test_worker_validation_never_reads_replacement_run_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _local_fixture(tmp_path)
    assert built.result.paper_reader_run is not None
    run_payload = json.loads(built.run_path.read_text(encoding="utf-8"))
    evidence_ref = next(
        ref for ref in run_payload["artifacts"] if ref["role"] == "evidence_manifest"
    )
    evidence_path = built.run_dir / evidence_ref["path"]
    evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    run_metadata = built.run_dir.stat()
    prepared = LocalPrepareResult(
        schema_version="paper_reader_batch.local-prepare-result.v2",
        manifest_sha256=built.result.manifest_sha256,
        item_id=built.result.item_id,
        worker_id="preparer",
        claim_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        attempt_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        attempt_number=1,
        lease_token_sha256="d" * 64,
        status="prepared",
        source=built.result.source,
        paper_reader_root=paper_reader_root_identity(_fake_paper_reader_root(tmp_path)),
        paper_reader_run_directory=FileIdentity(
            device=run_metadata.st_dev,
            inode=run_metadata.st_ino,
        ),
        paper_reader_run=built.result.paper_reader_run,
        evidence=_outer_ref(
            evidence_path,
            "paper_reader.evidence.v2-internal",
            str(evidence_payload["evidence_id"]),
        ),
    )
    valid_replacement = tmp_path / "valid-replacement"
    shutil.copytree(built.run_dir, valid_replacement)
    missing_note = built.candidate_path.parent / "note.md"
    missing_note.rename(missing_note.with_name("note.md.missing"))
    detached_original = tmp_path / "detached-original"
    detached_replacement = tmp_path / "detached-replacement"
    original_artifact_read = artifact_module._read_artifact_bytes
    original_stat = artifact_module.os.stat
    swapped = False

    def swap_after_run_read(path: Path, *, code: str) -> bytes:
        nonlocal swapped
        raw = original_artifact_read(path, code=code)
        if Path(path) == built.run_path and not swapped:
            built.run_dir.rename(detached_original)
            valid_replacement.rename(built.run_dir)
            swapped = True
        return raw

    def restore_after_source_stat(path, *args, **kwargs):
        nonlocal swapped
        result = original_stat(path, *args, **kwargs)
        if Path(path) == Path(built.result.source.path) and swapped:
            built.run_dir.rename(detached_replacement)
            detached_original.rename(built.run_dir)
            swapped = False
        return result

    monkeypatch.setattr(artifact_module, "_read_artifact_bytes", swap_after_run_read)
    monkeypatch.setattr(artifact_module.os, "stat", restore_after_source_stat)

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(
            built.manifest,
            built.result,
            prepared_local_result=prepared,
        )
    assert exc_info.value.code == "artifact_closed_world_mismatch"


def test_worker_finish_rejects_same_run_candidate_using_other_than_prepared_evidence(
    tmp_path: Path,
) -> None:
    built = _local_fixture(tmp_path)
    final_run = json.loads(built.run_path.read_text(encoding="utf-8"))
    run_id = str(final_run["run_id"])
    manifest_item = built.manifest.items[0]
    prepared_evidence_path, _prepared_evidence, prepared_evidence_ref = _make_evidence(
        built.run_dir,
        run_id=run_id,
        source_sha256=manifest_item.source.sha256,
        evidence_id="evidence_prepared_attempt",
    )
    source_ref = next(
        ref for ref in final_run["artifacts"] if ref["role"] == "source_snapshot"
    )
    prepared_run = {
        **final_run,
        "status": "prepared",
        "artifacts": [source_ref, prepared_evidence_ref],
    }
    _json(built.run_path, prepared_run)

    manifest_path = tmp_path / "batch-manifest.json"
    _json(manifest_path, built.manifest.model_dump(mode="json"))
    batch_skill_root = tmp_path / "batch-skill-root"
    batch_skill_root.mkdir()
    batch_run = tmp_path / "batch-run"
    initialize_run(
        manifest_path,
        request_id="91919191-9191-4191-8191-919191919191",
        skill_root=batch_skill_root,
        output=batch_run,
        initialized_at=NOW,
    )
    local_assignment = claim_local_prepare(
        batch_run,
        worker_id="preparer",
        request_id="92929292-9292-4292-8292-929292929292",
        now="2026-07-10T00:00:01Z",
    ).result["assignments"][0]
    view = load_run_view(batch_run)
    paper_reader_root = _fake_paper_reader_root(tmp_path)
    local_result = LocalPrepareResult(
        schema_version="paper_reader_batch.local-prepare-result.v2",
        manifest_sha256=view.manifest_sha256,
        item_id=local_assignment["item_id"],
        worker_id=local_assignment["worker_id"],
        claim_id=local_assignment["claim_id"],
        attempt_id=local_assignment["attempt_id"],
        attempt_number=local_assignment["attempt_number"],
        lease_token_sha256=sha256_bytes(local_assignment["lease_token"].encode()),
        status="prepared",
        source=view.manifest.items[0].source,
        paper_reader_root=paper_reader_root_identity(paper_reader_root),
        paper_reader_run_directory={
            "device": built.run_dir.stat().st_dev,
            "inode": built.run_dir.stat().st_ino,
        },
        paper_reader_run=_outer_ref(built.run_path, "paper_reader.run.v2", run_id),
        evidence=_outer_ref(
            prepared_evidence_path,
            "paper_reader.evidence.v2-internal",
            "evidence_prepared_attempt",
        ),
    )
    local_result_path = tmp_path / "local-prepare-result.json"
    local_result_path.write_bytes(canonical_json_bytes(local_result))
    finish_local_prepare(
        batch_run,
        str(local_assignment["item_id"]),
        worker_id=str(local_assignment["worker_id"]),
        claim_id=str(local_assignment["claim_id"]),
        lease_token=str(local_assignment["lease_token"]),
        attempt_id=str(local_assignment["attempt_id"]),
        result_path=local_result_path,
        expected_root=paper_reader_root,
        request_id="93939393-9393-4393-8393-939393939393",
        now="2026-07-10T00:00:02Z",
    )

    built.rewrite_final_run(
        {
            **final_run,
            "artifacts": [*final_run["artifacts"], prepared_evidence_ref],
        }
    )
    worker_assignment = claim_worker(
        batch_run,
        worker_id="reader",
        request_id="94949494-9494-4494-8494-949494949494",
        now="2026-07-10T00:00:03Z",
    ).result["assignments"][0]
    worker_result = built.result.model_copy(
        update={
            "manifest_sha256": load_run_view(batch_run).manifest_sha256,
            "worker_id": worker_assignment["worker_id"],
            "claim_id": worker_assignment["claim_id"],
            "attempt_id": worker_assignment["attempt_id"],
            "attempt_number": worker_assignment["attempt_number"],
            "lease_token_sha256": sha256_bytes(
                str(worker_assignment["lease_token"]).encode()
            ),
        }
    )
    worker_result_path = tmp_path / "worker-result-other-evidence.json"
    worker_result_path.write_bytes(canonical_json_bytes(worker_result))

    with pytest.raises(BatchRuntimeError) as exc_info:
        finish_worker(
            batch_run,
            str(worker_assignment["item_id"]),
            worker_id=str(worker_assignment["worker_id"]),
            claim_id=str(worker_assignment["claim_id"]),
            lease_token=str(worker_assignment["lease_token"]),
            attempt_id=str(worker_assignment["attempt_id"]),
            result_path=worker_result_path,
            request_id="95959595-9595-4595-8595-959595959595",
            now="2026-07-10T00:00:04Z",
        )

    assert exc_info.value.code == "local_prepare_binding_mismatch"
    assert load_run_view(batch_run).state.items[0].worker_status == "claimed"


def test_accepts_real_local_and_zotero_success_artifact_shapes(tmp_path: Path) -> None:
    local = _local_fixture(tmp_path / "local")
    zotero = _zotero_fixture(tmp_path / "zotero")

    assert validate_worker_result_artifacts(local.manifest, local.result) is None
    assert validate_worker_result_artifacts(zotero.manifest, zotero.result) == "PARENT1"


@pytest.mark.parametrize(
    "fixture_options",
    [
        {"locator": "context.md page 2"},
        {"english_fallback": True},
        {"semantic_mutation": "rejected"},
    ],
)
def test_batch_trusts_hash_bound_single_seal_without_replaying_single_gate(
    fixture_options: dict[str, object],
    tmp_path: Path,
) -> None:
    built = _local_fixture(tmp_path, **fixture_options)

    assert validate_worker_result_artifacts(built.manifest, built.result) is None


def test_batch_source_does_not_copy_single_schema_renderer_or_gate() -> None:
    source_path = Path(artifact_module.__file__)
    source = source_path.read_text(encoding="utf-8")
    module = ast.parse(source)
    classes = {node.name for node in module.body if isinstance(node, ast.ClassDef)}
    functions = {node.name for node in module.body if isinstance(node, ast.FunctionDef)}
    imported_modules = {
        node.module
        for node in module.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in module.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert not classes.intersection(
        {
            "ForeignRun",
            "ForeignSummary",
            "ForeignReview",
            "ForeignReviewPackage",
            "ForeignCandidate",
            "ForeignEvidence",
        }
    )
    assert not functions.intersection(
        {
            "_render_note_html_like_single",
            "_expected_zotero_candidate_notes",
            "_expected_tags",
            "_looks_like_english_prose",
            "_rendered_markdown_has_english_prose",
            "_locator_is_member",
            "_validate_summary_evidence",
            "_validate_write_ready_summary_semantics",
        }
    )
    assert "markdown_it" not in imported_modules

    pyproject = source_path.parents[2] / "pyproject.toml"
    assert "markdown-it-py" not in pyproject.read_text(encoding="utf-8")


def test_batch_write_source_does_not_copy_single_lifecycle_schemas() -> None:
    source_path = Path(artifact_module.__file__).with_name("v2_write.py")
    module = ast.parse(source_path.read_text(encoding="utf-8"))
    copied = {
        node.name
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name.startswith("_Foreign")
    }

    assert not copied


@pytest.mark.parametrize(
    ("paper_title", "rendered_h1"),
    [
        (
            "A *B* Study",
            "<h1>[Codex Summary] A *B* Study - 2026-07-10</h1>",
        ),
        (
            "A `B` Study",
            "<h1>[Codex Summary] A `B` Study - 2026-07-10</h1>",
        ),
    ],
)
def test_zotero_candidate_accepts_single_markdown_title_rendering(
    paper_title: str,
    rendered_h1: str,
    tmp_path: Path,
) -> None:
    built = _zotero_fixture(tmp_path, paper_title=paper_title)

    assert validate_worker_result_artifacts(built.manifest, built.result) == "PARENT1"
    assert built.candidate_path.parent.joinpath("note.html").read_text().splitlines()[0] == rendered_h1


def test_trusts_sealed_validation_hashes_without_replaying_markdown_renderer(
    tmp_path: Path,
) -> None:
    built = _zotero_fixture(tmp_path, mismatched_rendered_html=True)

    assert validate_worker_result_artifacts(built.manifest, built.result) == "PARENT1"


@pytest.mark.parametrize("artifact", ["note.md", "note.html"])
def test_batch_does_not_replay_single_candidate_body_semantics(
    artifact: str,
    tmp_path: Path,
) -> None:
    built = _zotero_fixture(tmp_path)
    candidate = json.loads(built.candidate_path.read_text())
    title = str(candidate["note_title"])
    if artifact == "note.md":
        _rewrite_candidate_notes(
            built,
            note_md=(
                f"# {title}\n\n## 30 秒结论\n\n"
                "这段内容从未进入已封存的审阅包，却同步更新了候选引用与哈希。\n"
            ).encode(),
        )
    else:
        _rewrite_candidate_notes(
            built,
            note_html=(
                f"<h1>{escape(title, quote=False)}</h1>"
                "<h2>30 秒结论</h2><p>这段内容没有经过封存审阅。</p>"
            ).encode(),
        )

    assert validate_worker_result_artifacts(built.manifest, built.result) == "PARENT1"


def test_committed_validation_uses_immutable_candidate_not_mutable_root_run(tmp_path: Path) -> None:
    built = _zotero_fixture(tmp_path)
    changed = json.loads(built.run_path.read_text())
    changed["target"] = {
        **changed["target"],
        "note_title": "[Codex Summary] A Useful Paper & Result - 2026-07-10 (v2)",
    }
    # A committed batch result retains the old run.json digest while the single-paper
    # lifecycle is allowed to advance its mutable root manifest.
    _json(built.run_path, changed)

    with pytest.raises(BatchRuntimeError):
        validate_worker_result_artifacts(built.manifest, built.result)

    assert (
        validate_worker_result_artifacts(
            built.manifest,
            built.result,
            allow_mutable_run=True,
        )
        == "PARENT1"
    )


def test_committed_local_validation_ignores_external_source_and_target_after_finish(
    tmp_path: Path,
) -> None:
    built = _local_fixture(tmp_path)
    source = Path(built.manifest.items[0].source.path)
    target = Path(json.loads(built.candidate_path.read_text())["target"]["resolved_path"])
    source.unlink()
    target.unlink()

    with pytest.raises(BatchRuntimeError):
        validate_worker_result_artifacts(built.manifest, built.result)

    assert validate_worker_result_artifacts(
        built.manifest,
        built.result,
        allow_mutable_run=True,
    ) is None


def test_committed_local_validation_still_rejects_internal_source_snapshot_tamper(
    tmp_path: Path,
) -> None:
    built = _local_fixture(tmp_path)
    source = Path(built.manifest.items[0].source.path)
    target = Path(json.loads(built.candidate_path.read_text())["target"]["resolved_path"])
    source.unlink()
    target.unlink()
    built.source_path.write_bytes(b"{}")

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(
            built.manifest,
            built.result,
            allow_mutable_run=True,
        )

    assert exc_info.value.code in {"artifact_binding_mismatch", "source_binding_mismatch"}


def test_committed_root_must_retain_exact_immutable_review_and_candidate_refs(
    tmp_path: Path,
) -> None:
    built = _zotero_fixture(tmp_path)
    changed = json.loads(built.run_path.read_text())
    changed["artifacts"] = [
        ref for ref in changed["artifacts"] if ref["role"] not in {"review_package", "candidate"}
    ]
    _json(built.run_path, changed)

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(
            built.manifest,
            built.result,
            allow_mutable_run=True,
        )

    assert exc_info.value.code == "artifact_not_bound"


@pytest.mark.parametrize("missing_role", ["raw_discovery_bundle", "normalized_source"])
def test_committed_zotero_root_must_retain_exact_source_refs(
    missing_role: str,
    tmp_path: Path,
) -> None:
    built = _zotero_fixture(tmp_path)
    changed = json.loads(built.run_path.read_text())
    changed["artifacts"] = [
        ref for ref in changed["artifacts"] if ref["role"] != missing_role
    ]
    _json(built.run_path, changed)

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(
            built.manifest,
            built.result,
            allow_mutable_run=True,
        )

    assert exc_info.value.code == "source_binding_mismatch"


@pytest.mark.parametrize(
    "missing_role",
    [
        "source_snapshot",
        "evidence_manifest",
        "review_package",
        "candidate",
        "local_publication_intent",
        "local_receipt",
    ],
)
def test_committed_local_root_must_retain_every_owned_immutable_ref(
    missing_role: str,
    tmp_path: Path,
) -> None:
    built = _local_fixture(tmp_path)
    changed = json.loads(built.run_path.read_text())
    changed["artifacts"] = [
        ref for ref in changed["artifacts"] if ref["role"] != missing_role
    ]
    # The committed result deliberately retains the old run digest. Replay may
    # tolerate lifecycle changes to run.json, but never dropped ownership refs.
    _json(built.run_path, changed)

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(
            built.manifest,
            built.result,
            allow_mutable_run=True,
        )

    assert exc_info.value.code in {
        "artifact_not_bound",
        "evidence_binding_mismatch",
        "source_binding_mismatch",
        "local_publication_invalid",
    }


@pytest.mark.parametrize("kind", ["local", "zotero"])
def test_source_directory_is_an_exact_closed_world_bundle(kind: str, tmp_path: Path) -> None:
    built = _local_fixture(tmp_path) if kind == "local" else _zotero_fixture(tmp_path)
    _json(built.run_dir / "source" / "unexpected.json", {"unexpected": True})

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(built.manifest, built.result)

    assert exc_info.value.code == "artifact_closed_world_mismatch"


def test_foreign_absolute_paths_are_lexically_normalized_at_consumption_boundary() -> None:
    with pytest.raises(BatchRuntimeError) as exc_info:
        _require_normalized_absolute_path(
            "/tmp/../tmp/paper.pdf",
            code="source_binding_mismatch",
            label="paper_reader local source",
        )

    assert exc_info.value.code == "source_binding_mismatch"


@pytest.mark.parametrize("noncanonical", ["summary", "validation"])
def test_rejects_noncanonical_sealed_summary_review_validation(
    noncanonical: str,
    tmp_path: Path,
) -> None:
    built = _local_fixture(
        tmp_path,
        noncanonical_summary=noncanonical == "summary",
        noncanonical_validation=noncanonical == "validation",
    )

    with pytest.raises(BatchRuntimeError, match="canonical"):
        validate_worker_result_artifacts(built.manifest, built.result)


def test_accepts_single_reader_allowed_mixed_technical_phrases(tmp_path: Path) -> None:
    built = _local_fixture(tmp_path, allowed_mixed=True)

    assert validate_worker_result_artifacts(built.manifest, built.result) is None


def test_rejects_sealed_review_missing_consumer_required_proofs(tmp_path: Path) -> None:
    built = _local_fixture(tmp_path, review_checks=("summary_schema",))

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(built.manifest, built.result)

    assert exc_info.value.code == "review_not_sealed"


@pytest.mark.parametrize(
    "candidate_checks",
    [(), ("source_identity",), ("source_identity", "source_identity")],
)
def test_rejects_candidate_missing_or_repeating_consumer_required_proofs(
    candidate_checks: tuple[str, ...],
    tmp_path: Path,
) -> None:
    built = _local_fixture(tmp_path, candidate_checks=candidate_checks)

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(built.manifest, built.result)

    assert exc_info.value.code == "candidate_not_write_ready"


def test_normal_finish_revalidates_original_local_source_snapshot_closure(tmp_path: Path) -> None:
    built = _local_fixture(tmp_path)
    built.source_path.write_bytes(b"{}")

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(built.manifest, built.result)

    assert exc_info.value.code in {"artifact_binding_mismatch", "source_binding_mismatch"}


def test_rejects_receipt_and_intent_run_refs_with_wrong_size_or_media(tmp_path: Path) -> None:
    built = _local_fixture(tmp_path)
    run = json.loads(built.run_path.read_text())
    intent_ref = next(ref for ref in run["artifacts"] if ref["role"] == "local_publication_intent")
    receipt_ref = next(ref for ref in run["artifacts"] if ref["role"] == "local_receipt")
    intent_ref["size_bytes"] += 1
    receipt_ref["media_type"] = "text/plain"
    built.rewrite_final_run(run)

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(built.manifest, built.result)

    assert exc_info.value.code == "local_publication_invalid"


def test_zotero_title_inventory_provenance_is_canonical_raw_inventory_digest(
    tmp_path: Path,
) -> None:
    built = _zotero_fixture(tmp_path, result_inventory_sha256="f" * 64)

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(built.manifest, built.result)

    assert exc_info.value.code == "source_binding_mismatch"


def test_rejects_raw_inventory_that_does_not_normalize_to_source_inventory(
    tmp_path: Path,
) -> None:
    built = _zotero_fixture(tmp_path, raw_inventory_title="A Different Paper")

    with pytest.raises(BatchRuntimeError) as exc_info:
        validate_worker_result_artifacts(built.manifest, built.result)

    assert exc_info.value.code == "source_binding_mismatch"
