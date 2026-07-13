import json
import ast
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from paper_reader_batch.v2_contracts import (
    ACTIVE_CONTRACT_MODELS,
    COMMAND_RESULT_SCHEMA_VERSION,
    ArtifactRef,
    BatchEvent,
    BatchState,
    CommandResult,
    EventCommandResultSnapshot,
    LocalPrepareResult,
    PdfSource,
    RecoveredUncertainWrite,
    ReconciliationResult,
    Rfc3339Utc,
    ReportItem,
    SkillRootIdentity,
    StateItem,
    WriteResult,
    export_contract_schemas,
    local_prepare_result_canonical_payload,
    schema_filename,
)


BATCH_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = BATCH_ROOT / "references" / "schemas"

EXPECTED_CONTRACTS = {
    "paper_reader_batch.manifest.v2",
    "paper_reader_batch.state.v2",
    "paper_reader_batch.event.v2",
    "paper_reader_batch.worker-result.v2",
    "paper_reader_batch.local-prepare-result.v2",
    "paper_reader_batch.write-result.v2",
    "paper_reader_batch.reconciliation.v2",
    "paper_reader_batch.report.v2",
    "paper_reader_batch.command-result.v2",
}


def test_active_contract_registry_is_exact_and_strict() -> None:
    assert set(ACTIVE_CONTRACT_MODELS) == EXPECTED_CONTRACTS

    for schema_version, model in ACTIVE_CONTRACT_MODELS.items():
        schema = model.model_json_schema()
        assert model.model_config["strict"] is True
        assert model.model_config["extra"] == "forbid"
        assert schema["additionalProperties"] is False
        assert schema["properties"]["schema_version"]["const"] == schema_version
        assert "schema_version" in schema["required"]
        with pytest.raises(ValidationError) as exc_info:
            model.model_validate({})
        assert ("schema_version",) in {error["loc"] for error in exc_info.value.errors()}


def test_state_item_source_has_no_silently_overwritten_annotations() -> None:
    source = (BATCH_ROOT / "src" / "paper_reader_batch" / "v2_contracts.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    state_item = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "StateItem")
    names = [
        node.target.id
        for node in state_item.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    ("model", "payload", "path_field"),
    [
        (
            PdfSource,
            {
                "path": "/tmp/paper\x00.pdf",
                "size_bytes": 1,
                "sha256": "a" * 64,
                "file_identity": {"device": 0, "inode": 1},
            },
            "path",
        ),
        (
            ArtifactRef,
            {
                "path": "/tmp/artifact\x00.json",
                "size_bytes": 1,
                "sha256": "b" * 64,
                "schema_version": "paper_reader.candidate.v2",
                "artifact_id": "candidate-1",
            },
            "path",
        ),
        (
            SkillRootIdentity,
            {
                "path": "/tmp/skill\x00-root",
                "skill_md_sha256": "c" * 64,
                "pyproject_sha256": "d" * 64,
                "uv_lock_sha256": "e" * 64,
                "runtime_sha256": "f" * 64,
                "schemas_sha256": "0" * 64,
            },
            "path",
        ),
        (
            ReportItem,
            {
                "item_id": "001",
                "input_type": "pdf_path",
                "status": "prepared",
                "write_status": "not_applicable",
                "thirty_second_takeaway": "结论",
                "takeaway_source_type": "local_note",
                "takeaway_source_path": "/tmp/note\x00.md",
                "takeaway_source_sha256": "1" * 64,
                "failure_code": "",
                "failure_message": "",
            },
            "takeaway_source_path",
        ),
    ],
)
def test_every_absolute_path_contract_field_rejects_nul_at_model_boundary(
    model: type,
    payload: dict,
    path_field: str,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(payload)

    assert (path_field,) in {error["loc"] for error in exc_info.value.errors()}


def test_checked_in_contract_schemas_match_export() -> None:
    exported = export_contract_schemas()

    assert set(exported) == EXPECTED_CONTRACTS
    for schema_version, schema in exported.items():
        path = SCHEMA_ROOT / schema_filename(schema_version)
        assert json.loads(path.read_text(encoding="utf-8")) == schema


def test_prepared_local_result_preserves_legacy_v2_bytes_and_accepts_new_identity() -> None:
    payload = {
        "schema_version": "paper_reader_batch.local-prepare-result.v2",
        "manifest_sha256": "a" * 64,
        "item_id": "001",
        "worker_id": "preparer",
        "claim_id": "11111111-1111-4111-8111-111111111111",
        "attempt_id": "22222222-2222-4222-8222-222222222222",
        "attempt_number": 1,
        "lease_token_sha256": "b" * 64,
        "status": "prepared",
        "source": {
            "source_type": "pdf_path",
            "path": "/tmp/paper.pdf",
            "size_bytes": 1,
            "sha256": "c" * 64,
            "file_identity": {"device": 1, "inode": 2},
        },
        "paper_reader_root": {
            "path": "/tmp/paper-reader",
            "skill_md_sha256": "d" * 64,
            "pyproject_sha256": "e" * 64,
            "uv_lock_sha256": "f" * 64,
            "runtime_sha256": "0" * 64,
            "schemas_sha256": "1" * 64,
        },
        "paper_reader_run": {
            "path": "/tmp/paper_analysis/run.json",
            "size_bytes": 1,
            "sha256": "2" * 64,
            "schema_version": "paper_reader.run.v2",
            "artifact_id": "run_test",
        },
        "evidence": {
            "path": "/tmp/paper_analysis/evidence/evidence_test/evidence.json",
            "size_bytes": 1,
            "sha256": "3" * 64,
            "schema_version": "paper_reader.evidence.v2-internal",
            "artifact_id": "evidence_test",
        },
        "error": None,
    }

    legacy = LocalPrepareResult.model_validate(payload)
    assert legacy.paper_reader_run_directory is None
    assert local_prepare_result_canonical_payload(legacy) == payload

    payload["paper_reader_run_directory"] = {"device": 4, "inode": 5}
    parsed = LocalPrepareResult.model_validate(payload)
    assert parsed.paper_reader_run_directory.model_dump(mode="json") == {
        "device": 4,
        "inode": 5,
    }


def test_recover_receipt_schema_binds_the_complete_uncertain_write_identity() -> None:
    event_schema = BatchEvent.model_json_schema()
    recovered_schema = event_schema["$defs"]["RecoveredUncertainWrite"]
    run_recovered_schema = event_schema["$defs"]["RunRecoveredData"]

    assert RecoveredUncertainWrite.model_config["strict"] is True
    assert recovered_schema["additionalProperties"] is False
    assert set(recovered_schema["required"]) == {
        "item_id",
        "writer_id",
        "claim_id",
        "write_attempt_id",
        "attempt_number",
        "lease_token_sha256",
        "candidate_sha256",
        "authorization_id",
        "authorization_path",
        "authorization_sha256",
        "authorization_nonce_sha256",
        "external_claim_id",
        "write_started_event_sha256",
    }
    assert "reconciliation_write" in run_recovered_schema["required"]
    assert run_recovered_schema["properties"]["reconciliation_write"]["anyOf"] == [
        {"$ref": "#/$defs/RecoveredUncertainWrite"},
        {"type": "null"},
    ]


def test_command_result_forbids_extra_fields_and_wrong_version() -> None:
    valid = {
        "schema_version": COMMAND_RESULT_SCHEMA_VERSION,
        "command": "run.status",
        "request_id": None,
        "replayed": False,
        "ok": True,
        "result": {"batch_status": "ready"},
        "error": None,
    }
    assert CommandResult.model_validate(valid).ok is True

    with pytest.raises(ValidationError, match="extra_forbidden"):
        CommandResult.model_validate({**valid, "unexpected": True})

    with pytest.raises(ValidationError, match="literal_error"):
        CommandResult.model_validate({**valid, "schema_version": "paper_reader_batch.command-result.v1"})


def test_reconciliation_result_requires_distinct_exact_matches() -> None:
    payload = {
        "schema_version": "paper_reader_batch.reconciliation.v2",
        "manifest_sha256": "1" * 64,
        "item_id": "001",
        "writer_id": "writer",
        "claim_id": "11111111-1111-4111-8111-111111111111",
        "lease_token_sha256": "2" * 64,
        "write_attempt_id": "22222222-2222-4222-8222-222222222222",
        "candidate_sha256": "3" * 64,
        "authorization_sha256": "4" * 64,
        "readback_sha256": "5" * 64,
        "parent_key": "PARENT",
        "exact_title": "[Codex Summary] Paper",
        "canonical_html_sha256": "6" * 64,
        "matched_note_keys": ["NOTE1", "NOTE1"],
        "match_count": 2,
        "outcome": "ambiguous",
        "verification": None,
        "matched_note_key": None,
    }

    with pytest.raises(ValidationError, match="distinct"):
        ReconciliationResult.model_validate(payload)


def test_write_result_contract_closes_external_claim_and_verification_schema() -> None:
    payload = {
        "schema_version": "paper_reader_batch.write-result.v2",
        "manifest_sha256": "1" * 64,
        "item_id": "001",
        "writer_id": "writer",
        "claim_id": "11111111-1111-4111-8111-111111111111",
        "write_attempt_id": "22222222-2222-4222-8222-222222222222",
        "lease_token_sha256": "2" * 64,
        "started_event_sha256": "3" * 64,
        "candidate_sha256": "4" * 64,
        "authorization_sha256": "5" * 64,
        "authorization_nonce_sha256": "6" * 64,
        "external_claim_id": "11111111-1111-4111-8111-111111111111",
        "note_key": "NOTE1",
        "parent_key": "PARENT",
        "canonical_html_sha256": "7" * 64,
        "verification": {
            "path": "/tmp/verification.json",
            "size_bytes": 2,
            "sha256": "8" * 64,
            "schema_version": "paper_reader.verification.v2",
            "artifact_id": "verification_NOTE1",
        },
        "status": "written",
    }
    assert WriteResult.model_validate(payload).external_claim_id == payload["claim_id"]

    with pytest.raises(ValidationError, match="external_claim_id"):
        WriteResult.model_validate(
            {**payload, "external_claim_id": "33333333-3333-4333-8333-333333333333"}
        )
    with pytest.raises(ValidationError, match="verification"):
        WriteResult.model_validate(
            {
                **payload,
                "verification": {
                    **payload["verification"],
                    "schema_version": "paper_reader.verification.v99",
                },
            }
        )


def test_exported_event_and_state_contracts_are_final_for_task6_write_lane() -> None:
    event_schema_text = json.dumps(BatchEvent.model_json_schema(), sort_keys=True)
    for event_type in [
        "write.claimed",
        "write.renewed",
        "write.released",
        "write.started",
        "write.written",
        "write.marked_uncertain",
        "write.lease_expired_uncertain",
        "write.reconciled",
        "write.retried",
    ]:
        assert event_type in event_schema_text

    for field in [
        "write_lease",
        "write_attempt_count",
        "write_started_event_sha256",
        "authorization_sha256",
        "authorization_nonce_sha256",
        "external_claim_id",
        "write_result_sha256",
        "reconciliation_sha256",
    ]:
        assert field in StateItem.model_fields

    assert "lease_secret_sha256" in BatchState.model_fields
    assert "lease_secret_sha256" in BatchEvent.model_json_schema()["$defs"]["RunInitializedData"]["required"]


def test_reconciliation_and_report_use_final_strict_write_identities() -> None:
    required = set(ReconciliationResult.model_json_schema()["required"])
    for field in [
        "writer_id",
        "claim_id",
        "lease_token_sha256",
        "write_attempt_id",
        "candidate_sha256",
        "authorization_sha256",
        "parent_key",
        "exact_title",
        "canonical_html_sha256",
        "matched_note_keys",
    ]:
        assert field in required

    report_schema = ReportItem.model_json_schema()
    assert set(report_schema["properties"]["write_status"]["enum"]) == {
        "not_applicable",
        "awaiting_candidate",
        "queued",
        "claimed",
        "started",
        "written",
        "uncertain",
        "retry_confirmation_required",
        "blocked",
        "prepared_only",
    }


def test_event_embeds_a_strict_redacted_command_result_snapshot() -> None:
    snapshot = {
        "schema_version": COMMAND_RESULT_SCHEMA_VERSION,
        "command": "worker.claim",
        "request_id": "11111111-1111-4111-8111-111111111111",
        "replayed": False,
        "ok": True,
        "semantic_result_sha256": "a" * 64,
        "error": None,
    }
    assert EventCommandResultSnapshot.model_validate(snapshot).ok is True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EventCommandResultSnapshot.model_validate({**snapshot, "lease_token": "secret"})

    event_schema_text = json.dumps(BatchEvent.model_json_schema(), sort_keys=True)
    assert "EventCommandResultSnapshot" in event_schema_text
    assert '"command_result": {"$ref"' in event_schema_text


def test_reconciliation_outcome_invariants_reject_inconsistent_matches() -> None:
    base = {
        "schema_version": "paper_reader_batch.reconciliation.v2",
        "manifest_sha256": "a" * 64,
        "item_id": "001",
        "writer_id": "writer-1",
        "claim_id": "11111111-1111-4111-8111-111111111111",
        "lease_token_sha256": "b" * 64,
        "write_attempt_id": "22222222-2222-4222-8222-222222222222",
        "candidate_sha256": "c" * 64,
        "authorization_sha256": "d" * 64,
        "readback_sha256": "e" * 64,
        "parent_key": "PARENT",
        "exact_title": "Exact title",
        "canonical_html_sha256": "f" * 64,
        "matched_note_keys": [],
        "match_count": 1,
        "outcome": "not_found",
        "verification": None,
        "matched_note_key": None,
    }
    with pytest.raises(ValidationError, match="match_count"):
        ReconciliationResult.model_validate(base)

    verification = {
        "path": "/tmp/verification.json",
        "size_bytes": 1,
        "sha256": "9" * 64,
        "schema_version": "paper_reader.verification.v2",
        "artifact_id": "verification-1",
    }
    verified = {
        **base,
        "matched_note_keys": ["NOTE1"],
        "match_count": 1,
        "outcome": "verified",
        "verification": verification,
        "matched_note_key": "NOTE1",
    }
    assert ReconciliationResult.model_validate(verified).outcome == "verified"

    with pytest.raises(ValidationError, match="ambiguous"):
        ReconciliationResult.model_validate(
            {
                **verified,
                "matched_note_keys": ["NOTE1", "NOTE2"],
                "match_count": 2,
                "outcome": "ambiguous",
            }
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-99-99T00:00:00Z",
        "2026-07-10T25:00:00Z",
        "2026-07-10T00:00:00+00:00",
    ],
)
def test_rfc3339_utc_rejects_invalid_calendar_time_and_non_z_suffix(timestamp: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Rfc3339Utc).validate_python(timestamp)
