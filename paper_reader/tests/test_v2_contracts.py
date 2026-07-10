from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import pytest


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
