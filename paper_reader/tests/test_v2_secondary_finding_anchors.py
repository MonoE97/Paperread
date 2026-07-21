from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from pathlib import Path

import pytest

from paper_reader.secondary_projection import (
    SecondaryProjectionError,
    resolve_secondary_render_summary,
)
from paper_reader.evidence_manifest import load_bound_evidence
from paper_reader.storage import canonical_json_bytes

from test_capture_secondary_url import run_raw_strict_capture
from test_v2_review_package import (
    _blocker_codes,
    _invoke,
    _result_payload,
    _rewrite_current_secondary_plan,
    _write_summary_and_review,
)
from test_v2_zotero_prepare import (
    _rewrite_secondary_plan,
    _write_capture,
    _zotero_run,
)


SOURCE_URLS = (
    "https://mp.weixin.qq.com/s/anchor-one?scene=334",
    "https://mp.weixin.qq.com/s/anchor-two?scene=24",
)
DEFAULT_TEXT = "外部正文用于核对论文中的材料行为与工程边界。" * 20


def _prepared_secondary_run(
    tmp_path: Path,
    *,
    urls: tuple[str, ...] = (SOURCE_URLS[0],),
    legacy_policy: bool = False,
    text: str = DEFAULT_TEXT,
) -> tuple[Path, str, dict[str, str]]:
    run_dir, _ = _zotero_run(tmp_path, extra="\n".join(urls))
    if legacy_policy:
        plan_path = run_dir / "source" / "secondary-plan.json"
        plan = json.loads(plan_path.read_bytes())
        plan.pop("finding_anchor_policy")
        _rewrite_secondary_plan(run_dir, plan)

    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    temporary_digests: dict[str, str] = {}
    for index, url in enumerate(urls, start=1):
        source_id = f"secondary-{index:03d}"
        capture_path = _write_capture(
            capture_dir,
            binding_run_dir=run_dir,
            requested_url=url,
            source_id=source_id,
            text=text,
        )
        temporary_digests[source_id] = hashlib.sha256(capture_path.read_bytes()).hexdigest()

    result = _invoke(
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
    assert result.exit_code == 0, result.stderr
    return run_dir, _result_payload(result)["data"]["evidence_digest"], temporary_digests


def _bound_evidence(run_dir: Path):
    run = json.loads((run_dir / "run.json").read_bytes())
    evidence_ref_payload = next(
        item for item in run["artifacts"] if item["role"] == "evidence_manifest"
    )
    from paper_reader.v2_loader import load_v2_run

    return load_bound_evidence(load_v2_run(run_dir), evidence_ref_payload["sha256"])


def _capture_member(run_dir: Path, source_id: str) -> tuple[bytes, dict[str, object], str]:
    evidence = _bound_evidence(run_dir)
    expected_suffix = f"secondary/{source_id}.json"
    artifact = next(
        item
        for item in evidence.artifacts_by_role["secondary_capture"]
        if item.ref.path.endswith(expected_suffix)
    )
    inventory_artifact = evidence.artifacts_by_role["secondary_sources"][0]
    inventory = json.loads(inventory_artifact.raw_bytes)
    inventory_source = next(
        item for item in inventory["sources"] if item["source_id"] == source_id
    )
    raw_digest = hashlib.sha256(artifact.raw_bytes).hexdigest()
    assert artifact.ref.sha256 == inventory_source["capture_sha256"] == raw_digest
    return artifact.raw_bytes, json.loads(artifact.raw_bytes), raw_digest


def _anchor(
    run_dir: Path,
    source_id: str = "secondary-001",
    *,
    start: int = 0,
    end: int = 20,
) -> dict[str, object]:
    _raw, capture, capture_digest = _capture_member(run_dir, source_id)
    text = capture["text"]
    excerpt = text[start:end]
    return {
        "capture_sha256": capture_digest,
        "start_codepoint": start,
        "end_codepoint": end,
        "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }


def _used_assessment(
    anchor: dict[str, object] | None,
    *,
    source_id: str = "secondary-001",
    finding_count: int = 1,
) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    for index in range(finding_count):
        finding: dict[str, object] = {
            "relation": "extends",
            "target": "technical_details_item",
            "text": f"外部材料补充了论文工程边界的解释 {index + 1}。",
            "caveats": [],
        }
        if anchor is not None:
            finding["anchor"] = copy.deepcopy(anchor)
        findings.append(finding)
    return {
        "source_id": source_id,
        "status": "used",
        "reason": "该来源包含可与论文交叉核对的内容。",
        "findings": findings,
    }


def _write_anchor_summary(
    run_dir: Path,
    evidence_digest: str,
    assessments: list[dict[str, object]],
) -> None:
    _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={"secondary_cross_checks": assessments},
        auto_secondary_anchors=False,
    )


def test_legacy_policy_allows_unanchored_finding_but_forbids_anchor(
    tmp_path: Path,
) -> None:
    accepted_root = tmp_path / "accepted"
    accepted_root.mkdir()
    accepted_run, accepted_digest, _ = _prepared_secondary_run(
        accepted_root,
        legacy_policy=True,
    )
    _write_anchor_summary(
        accepted_run,
        accepted_digest,
        [_used_assessment(None)],
    )

    accepted = _invoke(["review", "validate", str(accepted_run)])

    assert accepted.exit_code == 0, accepted.stderr

    blocked_root = tmp_path / "blocked"
    blocked_root.mkdir()
    blocked_run, blocked_digest, _ = _prepared_secondary_run(
        blocked_root,
        legacy_policy=True,
    )
    _write_anchor_summary(
        blocked_run,
        blocked_digest,
        [_used_assessment(_anchor(blocked_run))],
    )

    blocked = _invoke(["review", "validate", str(blocked_run)])

    assert blocked.exit_code == 1
    assert "secondary_finding_anchor_not_allowed" in _blocker_codes(blocked)


def test_historical_legacy_plan_with_large_warning_inventory_still_prepares(
    tmp_path: Path,
) -> None:
    source_url = SOURCE_URLS[0]
    run_dir, _ = _zotero_run(tmp_path, extra=source_url)
    plan_path = run_dir / "source" / "secondary-plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan.pop("finding_anchor_policy")
    _rewrite_secondary_plan(run_dir, plan)

    warnings = [123, *(f"warning-{index}" for index in range(256))]
    for source_name in ("source.json", "discovery.raw.json"):
        source_path = run_dir / "source" / source_name
        source = json.loads(source_path.read_bytes())
        source["selected_item"]["_paper_reader"]["warnings"] = warnings
        source_path.write_bytes(canonical_json_bytes(source))
    _rewrite_current_secondary_plan(run_dir, extra=source_url)

    result = _invoke(
        ["run", "prepare", str(run_dir), "--figure-limit", "0"]
    )

    assert result.exit_code == 0, result.stderr


@pytest.mark.parametrize("legacy_policy", [False, True])
def test_explicit_null_anchor_is_rejected_by_summary_schema(
    legacy_policy: bool,
    tmp_path: Path,
) -> None:
    run_dir, evidence_digest, _ = _prepared_secondary_run(
        tmp_path,
        legacy_policy=legacy_policy,
    )
    assessment = _used_assessment(
        None if legacy_policy else _anchor(run_dir),
    )
    _write_anchor_summary(run_dir, evidence_digest, [assessment])
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_bytes())
    summary["secondary_cross_checks"][0]["findings"][0]["anchor"] = None
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "invalid_summary_schema" in _blocker_codes(result)


def test_current_policy_requires_every_used_finding_anchor_before_projection(
    tmp_path: Path,
) -> None:
    run_dir, evidence_digest, _ = _prepared_secondary_run(tmp_path)
    first = _anchor(run_dir)
    assessment = _used_assessment(first, finding_count=2)
    assessment["findings"][1].pop("anchor")
    _write_anchor_summary(run_dir, evidence_digest, [assessment])

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 1
    assert "secondary_finding_anchor_missing" in _blocker_codes(result)
    assert not (run_dir / "reviews").exists()


def test_current_policy_requires_assessment_for_every_eligible_source(
    tmp_path: Path,
) -> None:
    run_dir, evidence_digest, _ = _prepared_secondary_run(tmp_path)
    _write_anchor_summary(run_dir, evidence_digest, [])

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "secondary_cross_check_missing" in _blocker_codes(result)


def test_zero_eligible_current_policy_preserves_legacy_rendered_note_bytes(
    tmp_path: Path,
) -> None:
    rendered: list[tuple[bytes, bytes]] = []
    for name, legacy_policy in (("current", False), ("legacy", True)):
        root = tmp_path / name
        root.mkdir()
        run_dir, evidence_digest, _ = _prepared_secondary_run(
            root,
            urls=(),
            legacy_policy=legacy_policy,
        )
        _write_anchor_summary(run_dir, evidence_digest, [])

        sealed = _invoke(["review", "seal", str(run_dir)])

        assert sealed.exit_code == 0, sealed.stderr
        package_dir = Path(_result_payload(sealed)["data"]["review_package_dir"])
        rendered.append(
            (
                (package_dir / "note.md").read_bytes(),
                (package_dir / "note.html").read_bytes(),
            )
        )

    assert rendered[0] == rendered[1]


@pytest.mark.parametrize(
    "tamper",
    ["capture_digest", "excerpt_digest", "range", "other_source_digest"],
)
def test_current_policy_rejects_unbound_or_invalid_anchor(
    tamper: str,
    tmp_path: Path,
) -> None:
    urls = SOURCE_URLS if tamper == "other_source_digest" else (SOURCE_URLS[0],)
    run_dir, evidence_digest, _ = _prepared_secondary_run(tmp_path, urls=urls)
    anchor = _anchor(run_dir)
    if tamper == "capture_digest":
        anchor["capture_sha256"] = "0" * 64
    elif tamper == "excerpt_digest":
        anchor["excerpt_sha256"] = "0" * 64
    elif tamper == "range":
        _raw, capture, _digest = _capture_member(run_dir, "secondary-001")
        start = len(capture["text"]) + 1
        anchor["start_codepoint"] = start
        anchor["end_codepoint"] = start + 20
    else:
        anchor["capture_sha256"] = _anchor(run_dir, "secondary-002")["capture_sha256"]
    assessments = [_used_assessment(anchor)]
    if len(urls) == 2:
        assessments.append(_used_assessment(_anchor(run_dir, "secondary-002"), source_id="secondary-002"))
    _write_anchor_summary(run_dir, evidence_digest, assessments)

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "secondary_finding_anchor_invalid" in _blocker_codes(result)


def test_anchor_binds_immutable_evidence_member_not_temporary_capture_bytes(
    tmp_path: Path,
) -> None:
    run_dir, evidence_digest, temporary_digests = _prepared_secondary_run(tmp_path)
    member_anchor = _anchor(run_dir)
    assert temporary_digests["secondary-001"] != member_anchor["capture_sha256"]
    forged = copy.deepcopy(member_anchor)
    forged["capture_sha256"] = temporary_digests["secondary-001"]
    _write_anchor_summary(run_dir, evidence_digest, [_used_assessment(forged)])

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "secondary_finding_anchor_invalid" in _blocker_codes(result)


@pytest.mark.parametrize("normalization", ["unicode", "newline"])
def test_anchor_hash_never_normalizes_unicode_or_newlines(
    normalization: str,
    tmp_path: Path,
) -> None:
    text = ("中😀e\u0301\r\n材料边界用于精确核对。" * 20)
    run_dir, evidence_digest, _ = _prepared_secondary_run(tmp_path, text=text)
    anchor = _anchor(run_dir, start=0, end=40)
    _raw, capture, _digest = _capture_member(run_dir, "secondary-001")
    excerpt = capture["text"][0:40]
    normalized = (
        unicodedata.normalize("NFC", excerpt)
        if normalization == "unicode"
        else excerpt.replace("\r\n", "\n")
    )
    assert normalized != excerpt
    anchor["excerpt_sha256"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    _write_anchor_summary(run_dir, evidence_digest, [_used_assessment(anchor)])

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "secondary_finding_anchor_invalid" in _blocker_codes(result)


def test_anchor_uses_python_unicode_codepoint_offsets_and_stays_out_of_note(
    tmp_path: Path,
) -> None:
    text = ("中😀e\u0301材料边界核对。" * 30)
    run_dir, evidence_digest, _ = _prepared_secondary_run(tmp_path, text=text)
    _raw, capture, _digest = _capture_member(run_dir, "secondary-001")
    assert len(capture["text"]) == capture["text_length"]
    anchor = _anchor(run_dir, start=1, end=21)
    exact_excerpt = capture["text"][1:21]
    assert "😀" in exact_excerpt
    _write_anchor_summary(run_dir, evidence_digest, [_used_assessment(anchor)])

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 0, result.stderr
    package_dir = Path(_result_payload(result)["data"]["review_package_dir"])
    note = (package_dir / "note.md").read_text(encoding="utf-8")
    assert "外部交叉核对（补充）" in note
    assert exact_excerpt not in note
    assert anchor["capture_sha256"] not in note
    assert anchor["excerpt_sha256"] not in note


def test_anchor_rejects_utf16_offsets_for_astral_text(tmp_path: Path) -> None:
    text = ("中😀e\u0301材料边界核对。" * 30)
    run_dir, evidence_digest, _ = _prepared_secondary_run(tmp_path, text=text)
    codepoint_end = 20
    correct_excerpt = text[:codepoint_end]
    utf16_end = len(correct_excerpt.encode("utf-16-le")) // 2
    assert utf16_end != codepoint_end
    utf16_anchor = _anchor(run_dir, start=0, end=utf16_end)
    utf16_anchor["excerpt_sha256"] = hashlib.sha256(
        correct_excerpt.encode("utf-8")
    ).hexdigest()
    _write_anchor_summary(
        run_dir,
        evidence_digest,
        [_used_assessment(utf16_anchor)],
    )

    result = _invoke(["review", "validate", str(run_dir)])

    assert result.exit_code == 1
    assert "secondary_finding_anchor_invalid" in _blocker_codes(result)


def test_raw_cdp_capture_prepare_and_resolver_share_unicode_codepoint_offsets(
    tmp_path: Path,
) -> None:
    source_url = "https://93.184.216.34/article?scene=334"
    run_root = tmp_path / "reader"
    run_root.mkdir()
    run_dir, _ = _zotero_run(run_root, extra=source_url)
    capture_dir = tmp_path / "raw-captures"
    capture_dir.mkdir()
    capture_path = capture_dir / "secondary-001.json"

    captured, _raw_cdp = run_raw_strict_capture(
        tmp_path / "raw-cdp",
        mode="emoji",
        plan_path=run_dir / "source" / "secondary-plan.json",
        output=capture_path,
    )

    assert captured.returncode == 0, captured.stderr
    produced = json.loads(capture_path.read_bytes())
    expected_text = "中😀e\u0301" * 60
    assert produced["text"] == expected_text
    assert produced["text_length"] == len(expected_text)

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
    assert prepared.exit_code == 0, prepared.stderr
    evidence_digest = _result_payload(prepared)["data"]["evidence_digest"]
    _raw, immutable_capture, _digest = _capture_member(run_dir, "secondary-001")
    assert immutable_capture["text"] == expected_text

    codepoint_end = 20
    utf16_end = len(expected_text[:codepoint_end].encode("utf-16-le")) // 2
    assert utf16_end > codepoint_end
    utf16_anchor = _anchor(run_dir, start=0, end=utf16_end)
    utf16_anchor["excerpt_sha256"] = hashlib.sha256(
        expected_text[:codepoint_end].encode("utf-8")
    ).hexdigest()
    _write_anchor_summary(
        run_dir,
        evidence_digest,
        [_used_assessment(utf16_anchor)],
    )

    rejected = _invoke(["review", "validate", str(run_dir)])

    assert rejected.exit_code == 1
    assert "secondary_finding_anchor_invalid" in _blocker_codes(rejected)

    codepoint_anchor = _anchor(run_dir, start=0, end=codepoint_end)
    _write_anchor_summary(
        run_dir,
        evidence_digest,
        [_used_assessment(codepoint_anchor)],
    )

    accepted = _invoke(["review", "seal", str(run_dir)])

    assert accepted.exit_code == 0, accepted.stderr


@pytest.mark.parametrize("tamper", ["inventory", "capture"])
def test_resolver_rejects_inventory_or_capture_member_tamper(
    tamper: str,
    tmp_path: Path,
) -> None:
    run_dir, evidence_digest, _ = _prepared_secondary_run(tmp_path)
    anchor = _anchor(run_dir)
    summary, _ = _write_summary_and_review(
        run_dir,
        evidence_digest,
        summary_updates={"secondary_cross_checks": [_used_assessment(anchor)]},
        auto_secondary_anchors=False,
    )
    evidence = _bound_evidence(run_dir)
    from dataclasses import replace
    if tamper == "inventory":
        inventory = evidence.artifacts_by_role["secondary_sources"][0]
        tampered_inventory = json.loads(inventory.raw_bytes)
        tampered_inventory["sources"][0]["capture_sha256"] = "0" * 64
        tampered_artifact = replace(
            inventory,
            raw_bytes=json.dumps(
                tampered_inventory,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )
        evidence.artifacts_by_role["secondary_sources"] = (tampered_artifact,)
    else:
        capture = evidence.artifacts_by_role["secondary_capture"][0]
        evidence.artifacts_by_role["secondary_capture"] = (
            replace(capture, raw_bytes=capture.raw_bytes + b" "),
        )

    with pytest.raises(SecondaryProjectionError) as exc_info:
        resolve_secondary_render_summary(summary, evidence)

    assert exc_info.value.code == "secondary_evidence_invalid"


def test_local_run_rejects_secondary_anchor_before_review_publication(
    tmp_path: Path,
) -> None:
    from test_v2_review_package import _prepared_run

    run_dir, evidence_digest = _prepared_run(tmp_path)
    fake_anchor = {
        "capture_sha256": "a" * 64,
        "start_codepoint": 0,
        "end_codepoint": 20,
        "excerpt_sha256": "b" * 64,
    }
    _write_anchor_summary(
        run_dir,
        evidence_digest,
        [_used_assessment(fake_anchor)],
    )

    result = _invoke(["review", "seal", str(run_dir)])

    assert result.exit_code == 1
    assert "secondary_cross_check_not_allowed" in _blocker_codes(result)
    assert not (run_dir / "reviews").exists()
