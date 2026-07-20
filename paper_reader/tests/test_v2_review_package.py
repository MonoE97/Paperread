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
    ReviewIssue,
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


def _prepared_zotero_secondary_run(
    tmp_path: Path,
    *,
    urls: tuple[str, ...],
    captured: bool = True,
    capture_title: str = "外部解读文章",
) -> tuple[Path, str]:
    from test_v2_zotero_prepare import _write_capture, _zotero_run

    run_dir, _pdf_path = _zotero_run(tmp_path, extra="\n".join(urls))
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    if captured:
        for index, url in enumerate(urls, start=1):
            _write_capture(
                capture_dir,
                binding_run_dir=run_dir,
                requested_url=url,
                source_id=f"secondary-{index:03d}",
                title=capture_title,
            )
    prepared = _invoke(
        [
            "run",
            "prepare",
            str(run_dir),
            "--figure-limit",
            "0",
            "--secondary-capture-dir",
            str(capture_dir),
        ]
    )
    payload = _result_payload(prepared)
    assert prepared.exit_code == 0, prepared.stderr
    return run_dir, payload["data"]["evidence_digest"]


def _write_summary_and_review(
    run_dir: Path,
    evidence_digest: str,
    *,
    locator: str = "context.md page 1",
    method: str = "方法先抽取正文，再对证据与结论执行结构化复核。",
    review_status: str = "passed",
    summary_updates: dict[str, object] | None = None,
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
    summary_payload.update(summary_updates or {})
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


def test_review_seal_projects_secondary_findings_into_allowed_existing_fields_only(
    tmp_path: Path,
) -> None:
    source_url = "https://mp.weixin.qq.com/s/example?scene=334"
    run_dir, evidence_digest = _prepared_zotero_secondary_run(
        tmp_path,
        urls=(source_url,),
    )
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={
            "secondary_cross_checks": [
                {
                    "source_id": "secondary-001",
                    "status": "used",
                    "reason": "该解读提供了可核对的实验语境。",
                    "findings": [
                        {
                            "relation": "supports",
                            "target": "core_result_short_annotation",
                            "text": "外部解读与论文关于压力影响形变的方向一致。",
                            "caveats": ["外部表述经过简化，仍以论文数据为准。"],
                        },
                        {
                            "relation": "extends",
                            "target": "technical_details_item",
                            "text": "解读补充说明了该结果对电池堆叠条件的工程含义。",
                            "caveats": [],
                        },
                        {
                            "relation": "questions",
                            "target": "inferred_limits_item",
                            "text": "解读未给出跨材料体系复现所需的完整条件。",
                            "caveats": [],
                        },
                    ],
                }
            ]
        },
    )

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    package_dir = Path(_result_payload(result)["data"]["review_package_dir"])
    note = (package_dir / "note.md").read_text(encoding="utf-8")
    expected_annotation = "外部交叉核对（补充）"
    assert note.count(expected_annotation) == 3
    assert note.count(f"]({source_url})") == 3
    thirty_second = next(line for line in note.splitlines() if line.startswith("| 30 秒结论 |"))
    assert source_url not in thirty_second
    evidence_lines = [line for line in note.splitlines() if "context.md page" in line]
    assert all(source_url not in line for line in evidence_lines)
    assert "## 6" not in note
    assert [line for line in note.splitlines() if line.startswith("## ")] == [
        "## 0. 阅读结论",
        "## 1. 速读信息",
        "## 2. 论文主张",
        "## 3. 方法与设计",
        "## 4. 图表导读",
        "## 5. 边界与机会",
    ]
    template = Path(__file__).parents[1] / "templates" / "zotero_note.md.j2"
    assert hashlib.sha256(template.read_bytes()).hexdigest() == (
        "510daa4cd394b841cfba2aa2718acd2a8faacadf340b3d279050d025aeeaaee3"
    )


def test_review_seal_keeps_projected_inferred_finding_after_legacy_display_cap(
    tmp_path: Path,
) -> None:
    source_url = "https://mp.weixin.qq.com/s/example?scene=334"
    run_dir, evidence_digest = _prepared_zotero_secondary_run(
        tmp_path,
        urls=(source_url,),
    )
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={
            "inferred_limits": [
                {
                    "text": f"论文内部推断限制 {index}。",
                    "source_type": "inferred",
                    "basis": "基于论文实验边界。",
                    "locator": "context.md page 1",
                }
                for index in range(9)
            ],
            "secondary_cross_checks": [
                {
                    "source_id": "secondary-001",
                    "status": "used",
                    "reason": "外部解读暴露了额外适用边界。",
                    "findings": [
                        {
                            "relation": "questions",
                            "target": "inferred_limits_item",
                            "text": "外部解读未说明长周期压力波动的影响。",
                            "caveats": [],
                        }
                    ],
                }
            ],
        },
    )

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    package_dir = Path(_result_payload(result)["data"]["review_package_dir"])
    note = (package_dir / "note.md").read_text(encoding="utf-8")
    assert "外部解读未说明长周期压力波动的影响" in note
    assert note.count("论文内部推断限制") == 8


def test_review_seal_flattens_and_escapes_secondary_source_title(
    tmp_path: Path,
) -> None:
    source_url = "https://mp.weixin.qq.com/s/example?scene=334"
    run_dir, evidence_digest = _prepared_zotero_secondary_run(
        tmp_path,
        urls=(source_url,),
        capture_title="外部|[解读]\n## 6. 注入",
    )
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={
            "secondary_cross_checks": [
                {
                    "source_id": "secondary-001",
                    "status": "used",
                    "reason": "该来源可用于补充技术语境。",
                    "findings": [
                        {
                            "relation": "extends",
                            "target": "technical_details_item",
                            "text": "该来源补充了工程|压力控制的解释。",
                            "caveats": [],
                        }
                    ],
                }
            ]
        },
    )

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    package_dir = Path(_result_payload(result)["data"]["review_package_dir"])
    note = (package_dir / "note.md").read_text(encoding="utf-8")
    assert "\n## 6. 注入" not in note
    assert "该来源补充了工程\\|压力控制的解释" in note
    assert "[外部\\|\\[解读\\] ## 6. 注入]" in note


def test_review_seal_allows_english_secondary_source_title_as_metadata(
    tmp_path: Path,
) -> None:
    source_url = "https://example.com/external-guide"
    run_dir, evidence_digest = _prepared_zotero_secondary_run(
        tmp_path,
        urls=(source_url,),
        capture_title="External Engineering Guide",
    )
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={
            "secondary_cross_checks": [
                {
                    "source_id": "secondary-001",
                    "status": "used",
                    "reason": "该来源可用于补充工程语境。",
                    "findings": [
                        {
                            "relation": "extends",
                            "target": "technical_details_item",
                            "text": "该来源补充了堆叠压力控制的实践语境。",
                            "caveats": [],
                        }
                    ],
                }
            ]
        },
    )

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    package_dir = Path(_result_payload(result)["data"]["review_package_dir"])
    note = (package_dir / "note.md").read_text(encoding="utf-8")
    assert f"[External Engineering Guide]({source_url})" in note


def test_review_seal_allows_pipe_in_english_secondary_title_inside_table(
    tmp_path: Path,
) -> None:
    source_url = "https://example.com/external-guide"
    run_dir, evidence_digest = _prepared_zotero_secondary_run(
        tmp_path,
        urls=(source_url,),
        capture_title="External | Engineering Guide",
    )
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={
            "secondary_cross_checks": [
                {
                    "source_id": "secondary-001",
                    "status": "used",
                    "reason": "该来源可用于核对核心结果。",
                    "findings": [
                        {
                            "relation": "supports",
                            "target": "core_result_short_annotation",
                            "text": "该来源给出了与核心结果一致的解释。",
                            "caveats": [],
                        }
                    ],
                }
            ]
        },
    )

    result = _invoke(["review", "seal", str(run_dir)])

    result_payload = _result_payload(result)
    assert result.exit_code == 0, (result.stderr, result_payload["data"]["blockers"])
    package_dir = Path(_result_payload(result)["data"]["review_package_dir"])
    note = (package_dir / "note.md").read_text(encoding="utf-8")
    assert f"[External \\\\| Engineering Guide]({source_url})" in note


@pytest.mark.parametrize(
    "raw_summary_update",
    [
        {
            "one_sentence_summary": (
                "本文结论参考了[外部来源](https://example.com/external-guide)。"
            )
        },
        {
            "technical_details": [
                "额外工程说明见[外部来源](https://example.com/external-guide)。"
            ]
        },
    ],
)
def test_review_validate_blocks_plan_url_outside_structured_secondary_assessment(
    raw_summary_update: dict[str, object],
    tmp_path: Path,
) -> None:
    source_url = "https://example.com/external-guide"
    run_dir, evidence_digest = _prepared_zotero_secondary_run(
        tmp_path,
        urls=(source_url,),
    )
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={
            **raw_summary_update,
            "secondary_cross_checks": [
                {
                    "source_id": "secondary-001",
                    "status": "used",
                    "reason": "该来源可用于补充工程语境。",
                    "findings": [
                        {
                            "relation": "extends",
                            "target": "technical_details_item",
                            "text": "该来源补充了堆叠压力控制的实践语境。",
                            "caveats": [],
                        }
                    ],
                }
            ],
        },
    )

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "secondary_cross_check_projection_bypass" in _blocker_codes(result)


def test_review_validate_blocks_rejected_plan_url_outside_secondary_pipeline(
    tmp_path: Path,
) -> None:
    source_url = "http://127.0.0.1/private"
    run_dir, evidence_digest = _prepared_zotero_secondary_run(
        tmp_path,
        urls=(source_url,),
        captured=False,
    )
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={
            "one_sentence_summary": f"本文结论还参考了[未绑定来源]({source_url})。"
        },
    )

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "secondary_cross_check_projection_bypass" in _blocker_codes(result)


def test_review_validate_blocks_untrusted_english_link_label_in_raw_summary(
    tmp_path: Path,
) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={
            "technical_details": [
                "中文引导（[This external article completely contradicts the reported mechanism]"
                "(https://example.org)）"
            ]
        },
    )

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "rendered_note_field_english_prose" in _blocker_codes(result)


def test_review_seal_projects_unavailable_secondary_source_into_existing_boundary_list(
    tmp_path: Path,
) -> None:
    source_url = "https://mp.weixin.qq.com/s/unavailable?scene=334"
    run_dir, evidence_digest = _prepared_zotero_secondary_run(
        tmp_path,
        urls=(source_url,),
        captured=False,
    )
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={
            "secondary_cross_checks": [
                {
                    "source_id": "secondary-001",
                    "status": "unavailable",
                    "reason": "页面无法读取，不能形成内容判断。",
                    "findings": [],
                }
            ]
        },
    )

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    package_dir = Path(_result_payload(result)["data"]["review_package_dir"])
    note = (package_dir / "note.md").read_text(encoding="utf-8")
    failure = "外部交叉核对未完整完成：以下链接无法读取，未纳入上述判断"
    assert note.count(failure) == 1
    boundary_section = note.split("### 适用机会与边界", 1)[1]
    assert source_url in boundary_section
    assert source_url not in note.split("### 适用机会与边界", 1)[0]


def test_zotero_without_extra_links_keeps_existing_note_rendering_unchanged(
    tmp_path: Path,
) -> None:
    from paper_reader.note import render_note, render_note_html

    run_dir, evidence_digest = _prepared_zotero_secondary_run(tmp_path, urls=())
    summary, _review = _write_summary_and_review(run_dir, evidence_digest)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    evidence_ref = next(
        item for item in run["artifacts"] if item["role"] == "evidence_manifest"
    )
    evidence = json.loads(
        (run_dir / evidence_ref["path"]).read_text(encoding="utf-8")
    )
    metadata_ref = next(item for item in evidence["files"] if item["role"] == "metadata")
    metadata = json.loads(
        (run_dir / metadata_ref["path"]).read_text(encoding="utf-8")
    )
    expected_note = render_note(
        metadata,
        summary.model_dump(mode="json"),
        generated_date=run["created_at"][:10],
    )

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    package_dir = Path(_result_payload(result)["data"]["review_package_dir"])
    assert (package_dir / "note.md").read_text(encoding="utf-8") == expected_note
    assert (package_dir / "note.html").read_text(encoding="utf-8") == render_note_html(
        expected_note
    )
    assert "外部交叉核对" not in expected_note


@pytest.mark.parametrize(
    ("summary_updates", "expected_code"),
    [
        ({}, "secondary_cross_check_missing"),
        (
            {
                "secondary_cross_checks": [
                    {
                        "source_id": "secondary-999",
                        "status": "used",
                        "reason": "来源身份不匹配。",
                        "findings": [
                            {
                                "relation": "supports",
                                "target": "core_result_short_annotation",
                                "text": "该内容与论文结果一致。",
                                "caveats": [],
                            }
                        ],
                    }
                ]
            },
            "secondary_cross_check_mismatch",
        ),
        (
            {
                "secondary_cross_checks": [
                    {
                        "source_id": "secondary-001",
                        "status": "used",
                        "reason": "尝试把冲突写入论文核心结果。",
                        "findings": [
                            {
                                "relation": "conflicts",
                                "target": "core_result_short_annotation",
                                "text": "外部说法与论文结果存在冲突。",
                                "caveats": [],
                            }
                        ],
                    }
                ]
            },
            "secondary_cross_check_target_invalid",
        ),
        (
            {
                "secondary_cross_checks": [
                    {
                        "source_id": "secondary-001",
                        "status": "used",
                        "reason": "同一表格字段的标注数量超过上限。",
                        "findings": [
                            {
                                "relation": "supports",
                                "target": "core_result_short_annotation",
                                "text": "第一条一致性判断。",
                                "caveats": [],
                            },
                            {
                                "relation": "supports",
                                "target": "core_result_short_annotation",
                                "text": "第二条一致性判断。",
                                "caveats": [],
                            },
                            {
                                "relation": "supports",
                                "target": "core_result_short_annotation",
                                "text": "第三条一致性判断。",
                                "caveats": [],
                            },
                        ],
                    }
                ]
            },
            "secondary_cross_check_table_limit",
        ),
    ],
)
def test_review_validate_blocks_incomplete_or_misbound_secondary_assessments(
    summary_updates: dict[str, object],
    expected_code: str,
    tmp_path: Path,
) -> None:
    source_url = "https://mp.weixin.qq.com/s/example?scene=334"
    run_dir, evidence_digest = _prepared_zotero_secondary_run(
        tmp_path,
        urls=(source_url,),
    )
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates=summary_updates,
    )

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert expected_code in _blocker_codes(result)


def test_review_validate_rejects_secondary_assessment_for_local_pdf(
    tmp_path: Path,
) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={
            "secondary_cross_checks": [
                {
                    "source_id": "secondary-001",
                    "status": "unavailable",
                    "reason": "本地 PDF 不应进入外部链接流程。",
                    "findings": [],
                }
            ]
        },
    )

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "secondary_cross_check_not_allowed" in _blocker_codes(result)


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


def test_review_validate_blocks_blocker_severity_review_issue(tmp_path: Path) -> None:
    run_dir, evidence_digest = _prepared_run(tmp_path)
    _summary, review = _write_summary_and_review(run_dir, evidence_digest)
    review = review.model_copy(
        update={
            "review_issues": (
                ReviewIssue(
                    severity="blocker",
                    issue="关键结论缺少可核对的正文证据。",
                    suggested_fix="补充对应正文页证据后重新复核。",
                ),
            )
        }
    )
    (run_dir / "review.json").write_bytes(canonical_json_bytes(review))

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "review_issue_blocker" in _blocker_codes(result)


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

    def validate_then_overwrite(run_path: Path, **kwargs):
        validation = original_validate(run_path, **kwargs)
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
    original_cas = review_package.cas_update_run
    failed = False

    def fail_once(loaded, value, **kwargs):
        nonlocal failed
        if loaded.manifest_path.name == "run.json" and not failed:
            failed = True
            raise OSError("injected failure after review package publication")
        return original_cas(loaded, value, **kwargs)

    monkeypatch.setattr(review_package, "cas_update_run", fail_once)

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
