from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

from paper_reader.storage import canonical_json_bytes


SCHEMA_VERSIONS = {
    "paper_reader.run.v2",
    "paper_reader.summary.v2",
    "paper_reader.review.v2",
    "paper_reader.review-package.v2",
    "paper_reader.candidate.v2",
    "paper_reader.write-authorization.v2",
    "paper_reader.verification.v2",
    "paper_reader.reconciliation.v2",
    "paper_reader.command-result.v2",
}


def _contracts_module():
    assert importlib.util.find_spec("paper_reader.contracts") is not None, "V2 contracts module is missing"
    return importlib.import_module("paper_reader.contracts")


def test_v2_contract_registry_contains_only_active_single_paper_schemas() -> None:
    contracts = _contracts_module()

    assert set(contracts.V2_SCHEMA_MODELS) == SCHEMA_VERSIONS


def test_every_v2_contract_is_strict_extra_forbid_and_frozen() -> None:
    contracts = _contracts_module()

    models = set(contracts.V2_SCHEMA_MODELS.values()) | set(contracts.V2_SUPPORT_MODELS)
    for model in models:
        assert model.model_config["strict"] is True, model.__name__
        assert model.model_config["extra"] == "forbid", model.__name__
        assert model.model_config["frozen"] is True, model.__name__


def test_artifact_reference_rejects_coercion_unknown_fields_and_unsafe_paths() -> None:
    contracts = _contracts_module()
    sha256 = "a" * 64

    with pytest.raises(contracts.ValidationError):
        contracts.ArtifactRef(role="context", path="evidence/context.md", sha256=sha256, size_bytes="12")
    with pytest.raises(contracts.ValidationError):
        contracts.ArtifactRef(
            role="context",
            path="evidence/context.md",
            sha256=sha256,
            size_bytes=12,
            surprise=True,
        )
    with pytest.raises(contracts.ValidationError):
        contracts.ArtifactRef(role="context", path="../context.md", sha256=sha256, size_bytes=12)


def test_command_result_rejects_an_unknown_or_unversioned_schema() -> None:
    contracts = _contracts_module()
    payload = {
        "schema_version": "paper_reader.command-result.v1",
        "command": "route",
        "ok": True,
        "code": "ok",
        "created_at": "2026-07-10T09:30:00Z",
        "data": {},
    }

    with pytest.raises(contracts.ValidationError):
        contracts.PaperReaderCommandResult.model_validate(payload)

    payload.pop("schema_version")
    with pytest.raises(contracts.ValidationError) as exc_info:
        contracts.PaperReaderCommandResult.model_validate(payload)
    assert ("schema_version",) in {error["loc"] for error in exc_info.value.errors()}


def test_schema_version_is_required_by_every_v2_model_and_checked_in_schema() -> None:
    contracts = _contracts_module()
    schema_dir = Path(__file__).parents[1] / "references" / "schemas"

    for version, model in contracts.V2_SCHEMA_MODELS.items():
        assert model.model_fields["schema_version"].is_required(), version
        with pytest.raises(contracts.ValidationError) as exc_info:
            model.model_validate({})
        assert ("schema_version",) in {error["loc"] for error in exc_info.value.errors()}, version

        checked_in = json.loads(
            (schema_dir / contracts.schema_filename(version)).read_text(encoding="utf-8")
        )
        assert "schema_version" in checked_in["required"], version


def test_core_contracts_fix_identity_time_path_target_artifact_gate_and_preflight_fields() -> None:
    contracts = _contracts_module()

    assert {
        "run_id",
        "created_at",
        "source",
        "target",
        "artifacts",
        "gate",
        "live_preflight",
    } <= set(contracts.PaperReaderRun.model_fields)
    assert {
        "candidate_id",
        "run_id",
        "created_at",
        "source",
        "target",
        "artifacts",
        "gate",
        "live_preflight",
    } <= set(contracts.PaperReaderCandidate.model_fields)
    assert {
        "authorization_id",
        "created_at",
        "expires_at",
        "candidate_digest",
        "target",
        "artifacts",
        "live_preflight",
        "external_claim_id",
        "write_attempt_id",
    } <= set(contracts.PaperReaderWriteAuthorization.model_fields)


def test_checked_in_json_schemas_match_pydantic_exports() -> None:
    contracts = _contracts_module()
    schema_dir = Path(__file__).parents[1] / "references" / "schemas"

    checked_in = {path.name for path in schema_dir.glob("*.schema.json")}
    expected = {contracts.schema_filename(version) for version in SCHEMA_VERSIONS}
    assert checked_in == expected

    for version, model in contracts.V2_SCHEMA_MODELS.items():
        schema_path = schema_dir / contracts.schema_filename(version)
        assert json.loads(schema_path.read_text(encoding="utf-8")) == model.model_json_schema(
            mode="validation"
        )


def _summary_hash_fixture_payload() -> dict[str, object]:
    return {
        "schema_version": "paper_reader.summary.v2",
        "summary_id": "summary_hash_fixture",
        "run_id": "run_hash_fixture",
        "created_at": "2026-07-15T00:00:00Z",
        "evidence_digest": "0" * 64,
        "paper_type": "method_paper",
        "trust_status": "usable_with_caveats",
        "review_status": "passed",
        "improvement_status": "not_needed",
        "trust_rationale": "可信度说明。",
        "one_sentence_summary": "一句话总结。",
        "abstract_translation": "摘要翻译。",
        "research_question": "研究问题？",
        "method": "研究方法。",
        "experiments": "实验设计。",
        "ai4s_relevance": "相关性。",
        "key_points": ["要点。"],
        "contributions": ["贡献。"],
        "limitations": ["局限。"],
        "follow_up_keywords": ["keyword"],
        "evidence_summary": [
            {
                "claim": "结论。",
                "evidence": [
                    {
                        "type": "text",
                        "locator": "context.md page 1",
                        "summary": "证据。",
                    }
                ],
                "confidence": "medium",
            }
        ],
    }


def test_empty_secondary_cross_checks_preserve_existing_v2_summary_canonical_hash() -> None:
    contracts = _contracts_module()
    payload = _summary_hash_fixture_payload()
    summary = contracts.PaperReaderSummary.model_validate_json(
        json.dumps(payload, ensure_ascii=False)
    )

    dumped = summary.model_dump(mode="json")
    assert "secondary_cross_checks" not in dumped
    assert hashlib.sha256(canonical_json_bytes(summary)).hexdigest() == (
        "947cc20ce7466e7befe4172346b8eed1dd4b33c97a88351615586810a057ad24"
    )


def test_legacy_unanchored_cross_check_preserves_canonical_summary_hash() -> None:
    contracts = _contracts_module()
    payload = _summary_hash_fixture_payload()
    payload["secondary_cross_checks"] = [
        {
            "source_id": "secondary-001",
            "status": "used",
            "reason": "该来源用于核对论文的技术判断。",
            "findings": [
                {
                    "relation": "supports",
                    "target": "technical_details_item",
                    "text": "外部材料与论文的技术判断方向一致。",
                    "caveats": ["仅用于交叉核对。"],
                }
            ],
        }
    ]
    summary = contracts.PaperReaderSummary.model_validate_json(
        json.dumps(payload, ensure_ascii=False)
    )

    dumped = summary.model_dump(mode="json")
    assert "anchor" not in dumped["secondary_cross_checks"][0]["findings"][0]
    assert hashlib.sha256(canonical_json_bytes(summary)).hexdigest() == (
        "fecc1c1c2a7c26d8b6f98316dea1f98d661266b89ee6621d8bf9a34e5252dff5"
    )


def test_secondary_text_anchor_is_strict_and_span_bounded() -> None:
    contracts = _contracts_module()
    valid = {
        "capture_sha256": "a" * 64,
        "start_codepoint": 7,
        "end_codepoint": 27,
        "excerpt_sha256": "b" * 64,
    }

    anchor = contracts.SecondaryTextAnchor.model_validate(valid)
    maximum = contracts.SecondaryTextAnchor.model_validate(
        {**valid, "end_codepoint": 2_007}
    )

    assert anchor.model_dump(mode="json") == valid
    assert maximum.end_codepoint - maximum.start_codepoint == 2_000
    invalid_payloads = [
        {**valid, "capture_sha256": "A" * 64},
        {**valid, "capture_sha256": "a" * 63},
        {**valid, "capture_sha256": 7},
        {**valid, "start_codepoint": True},
        {**valid, "start_codepoint": "7"},
        {**valid, "start_codepoint": -1},
        {**valid, "end_codepoint": 7},
        {**valid, "end_codepoint": 6},
        {**valid, "end_codepoint": 0},
        {**valid, "end_codepoint": 26},
        {**valid, "end_codepoint": 2_008},
        {**valid, "excerpt_sha256": "B" * 64},
        {**valid, "unknown": "forbidden"},
    ]
    for payload in invalid_payloads:
        with pytest.raises(contracts.ValidationError):
            contracts.SecondaryTextAnchor.model_validate(payload)


def test_secondary_finding_optional_anchor_preserves_legacy_canonical_bytes() -> None:
    contracts = _contracts_module()
    legacy_payload = {
        "relation": "supports",
        "target": "technical_details_item",
        "text": "外部材料与论文的技术判断方向一致。",
        "caveats": ("仅用于交叉核对。",),
    }

    legacy = contracts.SecondaryCrossCheckFinding.model_validate(legacy_payload)

    assert canonical_json_bytes(legacy) == (
        b'{"caveats":["\xe4\xbb\x85\xe7\x94\xa8\xe4\xba\x8e\xe4\xba\xa4\xe5\x8f\x89\xe6\xa0\xb8\xe5\xaf\xb9\xe3\x80\x82"],'
        b'"relation":"supports","target":"technical_details_item",'
        b'"text":"\xe5\xa4\x96\xe9\x83\xa8\xe6\x9d\x90\xe6\x96\x99\xe4\xb8\x8e\xe8\xae\xba\xe6\x96\x87\xe7\x9a\x84\xe6\x8a\x80\xe6\x9c\xaf\xe5\x88\xa4\xe6\x96\xad\xe6\x96\xb9\xe5\x90\x91\xe4\xb8\x80\xe8\x87\xb4\xe3\x80\x82"}'
    )
    assert "anchor" not in legacy.model_dump(mode="json")

    anchored = contracts.SecondaryCrossCheckFinding.model_validate(
        {
            **legacy_payload,
            "anchor": {
                "capture_sha256": "a" * 64,
                "start_codepoint": 0,
                "end_codepoint": 20,
                "excerpt_sha256": "b" * 64,
            },
        }
    )
    assert anchored.anchor is not None
    with pytest.raises(contracts.ValidationError):
        contracts.SecondaryCrossCheckFinding.model_validate(
            {**legacy_payload, "anchor": None}
        )
    with pytest.raises(contracts.ValidationError):
        contracts.SecondaryCrossCheckFinding.model_validate(
            {**legacy_payload, "anchor": [anchored.anchor.model_dump(mode="json")]}
        )

    anchor_schema = contracts.SecondaryCrossCheckFinding.model_json_schema(
        mode="validation"
    )["properties"]["anchor"]
    assert anchor_schema["$ref"] == "#/$defs/SecondaryTextAnchor"
    assert "anyOf" not in anchor_schema
    assert "default" not in anchor_schema
