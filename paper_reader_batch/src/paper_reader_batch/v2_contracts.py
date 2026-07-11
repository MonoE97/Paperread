from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)


MANIFEST_SCHEMA_VERSION = "paper_reader_batch.manifest.v2"
STATE_SCHEMA_VERSION = "paper_reader_batch.state.v2"
EVENT_SCHEMA_VERSION = "paper_reader_batch.event.v2"
WORKER_RESULT_SCHEMA_VERSION = "paper_reader_batch.worker-result.v2"
LOCAL_PREPARE_RESULT_SCHEMA_VERSION = "paper_reader_batch.local-prepare-result.v2"
WRITE_RESULT_SCHEMA_VERSION = "paper_reader_batch.write-result.v2"
RECONCILIATION_SCHEMA_VERSION = "paper_reader_batch.reconciliation.v2"
REPORT_SCHEMA_VERSION = "paper_reader_batch.report.v2"
COMMAND_RESULT_SCHEMA_VERSION = "paper_reader_batch.command-result.v2"
LOCAL_PREPARE_COORDINATION_UUID_NAME = "paper_reader_batch.local_prepare.coordination_reserved.v2"

PAPER_READER_RUN_SCHEMA_VERSION = "paper_reader.run.v2"
PAPER_READER_REVIEW_PACKAGE_SCHEMA_VERSION = "paper_reader.review-package.v2"
PAPER_READER_CANDIDATE_SCHEMA_VERSION = "paper_reader.candidate.v2"
PAPER_READER_VERIFICATION_SCHEMA_VERSION = "paper_reader.verification.v2"
PAPER_READER_AUTHORIZATION_SCHEMA_VERSION = "paper_reader.write-authorization.v2"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _canonical_uuid(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("must be a syntactically valid UUID") from exc
    if str(parsed) != value.lower():
        raise ValueError("must use canonical hyphenated UUID form")
    return value.lower()


def _absolute_normalized_path(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain NUL bytes")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("must be an absolute path")
    if ".." in path.parts:
        raise ValueError("must not contain parent traversal")
    if value != os.path.normpath(value):
        raise ValueError("must use one lexically normalized absolute path")
    return value


def _valid_rfc3339_utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("must be a valid RFC3339 UTC timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("must be UTC")
    return value


NonEmptyString = Annotated[str, StringConstraints(strict=True, min_length=1)]
ItemId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"),
]
UuidString = Annotated[str, StringConstraints(strict=True), AfterValidator(_canonical_uuid)]
Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Rfc3339Utc = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$",
    ),
    AfterValidator(_valid_rfc3339_utc),
]
AbsolutePath = Annotated[str, StringConstraints(strict=True, min_length=1), AfterValidator(_absolute_normalized_path)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
LeaseToken = Annotated[str, StringConstraints(strict=True, min_length=32, max_length=256)]


class FileIdentity(StrictModel):
    device: NonNegativeInt
    inode: PositiveInt


class PdfSource(StrictModel):
    source_type: Literal["pdf_path"] = "pdf_path"
    path: AbsolutePath
    size_bytes: PositiveInt
    sha256: Sha256
    file_identity: FileIdentity


class ZoteroItemSource(StrictModel):
    source_type: Literal["zotero_item"] = "zotero_item"
    item_key: NonEmptyString
    title: NonEmptyString
    inventory_sha256: Sha256
    collection_key: NonEmptyString | None = None


class ZoteroTitleSource(StrictModel):
    source_type: Literal["zotero_title"] = "zotero_title"
    title: NonEmptyString
    resolved_item_key: NonEmptyString | None = None
    inventory_sha256: Sha256 | None = None


SourceIdentity: TypeAlias = Annotated[
    PdfSource | ZoteroItemSource | ZoteroTitleSource,
    Field(discriminator="source_type"),
]


class SourceSummary(StrictModel):
    source_type: Literal["pdf_folder", "pdf_paths", "zotero_titles", "zotero_collection"]
    description: NonEmptyString
    source_sha256: Sha256 | None = None
    collection_key: NonEmptyString | None = None
    collection_name: NonEmptyString | None = None


class PdfManifestItem(StrictModel):
    item_id: ItemId
    input_type: Literal["pdf_path"] = "pdf_path"
    source: PdfSource
    expected_output: Literal["local_note"] = "local_note"


class ZoteroItemManifestItem(StrictModel):
    item_id: ItemId
    input_type: Literal["zotero_item"] = "zotero_item"
    source: ZoteroItemSource
    expected_output: Literal["zotero_note_candidate"] = "zotero_note_candidate"


class ZoteroTitleManifestItem(StrictModel):
    item_id: ItemId
    input_type: Literal["zotero_title"] = "zotero_title"
    source: ZoteroTitleSource
    expected_output: Literal["zotero_note_candidate"] = "zotero_note_candidate"


ManifestItem: TypeAlias = Annotated[
    PdfManifestItem | ZoteroItemManifestItem | ZoteroTitleManifestItem,
    Field(discriminator="input_type"),
]


class BatchManifest(StrictModel):
    schema_version: Literal[MANIFEST_SCHEMA_VERSION]
    manifest_id: UuidString
    created_at: Rfc3339Utc
    batch_title: NonEmptyString
    default_concurrency: Annotated[int, Field(strict=True, ge=1, le=32)] = 3
    write_policy: Literal["zotero_write", "prepare_only"] = "zotero_write"
    source_summary: SourceSummary
    items: Annotated[list[ManifestItem], Field(min_length=1)]

    @model_validator(mode="after")
    def reject_duplicate_identities(self) -> "BatchManifest":
        item_ids: set[str] = set()
        pdf_paths: set[str] = set()
        file_ids: set[tuple[int, int]] = set()
        zotero_keys: set[str] = set()
        for item in self.items:
            if item.item_id in item_ids:
                raise ValueError(f"duplicate item_id: {item.item_id}")
            item_ids.add(item.item_id)
            if isinstance(item, PdfManifestItem):
                identity = (item.source.file_identity.device, item.source.file_identity.inode)
                if item.source.path in pdf_paths:
                    raise ValueError(f"duplicate normalized PDF path: {item.source.path}")
                if identity in file_ids:
                    raise ValueError(f"duplicate PDF file identity: {identity}")
                pdf_paths.add(item.source.path)
                file_ids.add(identity)
            elif isinstance(item, ZoteroItemManifestItem):
                if item.source.item_key in zotero_keys:
                    raise ValueError(f"duplicate resolved Zotero item key: {item.source.item_key}")
                zotero_keys.add(item.source.item_key)
            elif item.source.resolved_item_key is not None:
                if item.source.resolved_item_key in zotero_keys:
                    raise ValueError(f"duplicate resolved Zotero item key: {item.source.resolved_item_key}")
                zotero_keys.add(item.source.resolved_item_key)
        return self


class ArtifactRef(StrictModel):
    path: AbsolutePath
    size_bytes: PositiveInt
    sha256: Sha256
    schema_version: NonEmptyString
    artifact_id: NonEmptyString


class SkillRootIdentity(StrictModel):
    path: AbsolutePath
    skill_md_sha256: Sha256
    pyproject_sha256: Sha256
    uv_lock_sha256: Sha256
    runtime_sha256: Sha256
    schemas_sha256: Sha256


class LeaseState(StrictModel):
    lane: Literal["worker", "local_prepare"]
    actor_id: NonEmptyString
    claim_id: UuidString
    attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    issued_at: Rfc3339Utc
    expires_at: Rfc3339Utc


class WriteLeaseState(StrictModel):
    writer_id: NonEmptyString
    claim_id: UuidString
    write_attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    issued_at: Rfc3339Utc
    expires_at: Rfc3339Utc
    candidate_sha256: Sha256


class StateItem(StrictModel):
    item_id: ItemId
    input_type: Literal["pdf_path", "zotero_item", "zotero_title"]
    expected_output: Literal["local_note", "zotero_note_candidate"]
    worker_status: Literal["queued", "claimed", "succeeded", "failed", "blocked"] = "queued"
    worker_attempt_count: NonNegativeInt = 0
    worker_lease: LeaseState | None = None
    worker_result_sha256: Sha256 | None = None
    worker_last_actor_id: NonEmptyString | None = None
    worker_last_claim_id: UuidString | None = None
    worker_last_attempt_id: UuidString | None = None
    worker_last_lease_token_sha256: Sha256 | None = None
    worker_pending_attempt_id: UuidString | None = None
    worker_failure_code: NonEmptyString | None = None
    worker_failure_message: NonEmptyString | None = None
    local_prepare_status: Literal["not_applicable", "queued", "claimed", "prepared", "failed", "blocked"]
    local_prepare_attempt_count: NonNegativeInt = 0
    local_prepare_lease: LeaseState | None = None
    local_prepare_result_sha256: Sha256 | None = None
    local_prepare_last_actor_id: NonEmptyString | None = None
    local_prepare_last_claim_id: UuidString | None = None
    local_prepare_last_attempt_id: UuidString | None = None
    local_prepare_last_lease_token_sha256: Sha256 | None = None
    local_prepare_coordination_request_id: UuidString | None = None
    local_prepare_coordination_fingerprint: NonEmptyString | None = None
    local_prepare_coordination_device: NonNegativeInt | None = None
    local_prepare_coordination_inode: PositiveInt | None = None
    local_prepare_failure_code: NonEmptyString | None = None
    local_prepare_failure_message: NonEmptyString | None = None
    resolved_zotero_item_key: NonEmptyString | None = None
    write_status: Literal[
        "not_applicable",
        "awaiting_candidate",
        "queued",
        "claimed",
        "started",
        "prepared_only",
        "written",
        "uncertain",
        "retry_confirmation_required",
        "blocked",
    ]
    write_attempt_count: NonNegativeInt = 0
    write_lease: WriteLeaseState | None = None
    write_last_writer_id: NonEmptyString | None = None
    write_last_claim_id: UuidString | None = None
    write_last_attempt_id: UuidString | None = None
    write_last_lease_token_sha256: Sha256 | None = None
    write_pending_attempt_id: UuidString | None = None
    candidate_sha256: Sha256 | None = None
    write_started_event_sha256: Sha256 | None = None
    authorization_sha256: Sha256 | None = None
    authorization_nonce_sha256: Sha256 | None = None
    external_claim_id: UuidString | None = None
    write_last_authorization_sha256: Sha256 | None = None
    write_last_authorization_nonce_sha256: Sha256 | None = None
    write_last_external_claim_id: UuidString | None = None
    write_result_sha256: Sha256 | None = None
    reconciliation_sha256: Sha256 | None = None
    write_failure_code: NonEmptyString | None = None
    write_failure_message: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_lane_state(self) -> "StateItem":
        if (self.worker_status == "claimed") != (self.worker_lease is not None):
            raise ValueError("worker claimed state and lease must be present together")
        if (self.local_prepare_status == "claimed") != (self.local_prepare_lease is not None):
            raise ValueError("local prepare claimed state and lease must be present together")
        if self.worker_lease is not None:
            if self.worker_lease.lane != "worker" or self.worker_lease.attempt_number != self.worker_attempt_count:
                raise ValueError("worker lease lane/attempt must match worker state")
        if self.local_prepare_lease is not None:
            if (
                self.local_prepare_lease.lane != "local_prepare"
                or self.local_prepare_lease.attempt_number != self.local_prepare_attempt_count
            ):
                raise ValueError("local prepare lease lane/attempt must match local state")
        write_active = self.write_status in {"claimed", "started"}
        if write_active != (self.write_lease is not None):
            raise ValueError("write claimed/started state and active lease must be present together")
        write_last_identity = (
            self.write_last_writer_id,
            self.write_last_claim_id,
            self.write_last_attempt_id,
            self.write_last_lease_token_sha256,
        )
        if any(value is not None for value in write_last_identity) != all(
            value is not None for value in write_last_identity
        ):
            raise ValueError("write last identity must be all present or all absent")
        if self.write_lease is not None:
            if (
                self.write_lease.attempt_number != self.write_attempt_count
                or self.write_lease.writer_id != self.write_last_writer_id
                or self.write_lease.claim_id != self.write_last_claim_id
                or self.write_lease.write_attempt_id != self.write_last_attempt_id
                or self.write_lease.lease_token_sha256 != self.write_last_lease_token_sha256
                or self.write_lease.candidate_sha256 != self.candidate_sha256
            ):
                raise ValueError("write lease identity/candidate must match write state")
        if self.write_pending_attempt_id is not None:
            if self.write_status != "queued" or self.write_lease is not None:
                raise ValueError("pending write attempt requires queued state without an active lease")
            if self.write_pending_attempt_id == self.write_last_attempt_id:
                raise ValueError("pending write attempt must be new")
        active_authorization = (
            self.authorization_sha256,
            self.authorization_nonce_sha256,
            self.external_claim_id,
            self.write_started_event_sha256,
        )
        if any(value is not None for value in active_authorization) != all(
            value is not None for value in active_authorization
        ):
            raise ValueError("active write authorization binding must be all present or all absent")
        last_authorization = (
            self.write_last_authorization_sha256,
            self.write_last_authorization_nonce_sha256,
            self.write_last_external_claim_id,
        )
        if any(value is not None for value in last_authorization) != all(
            value is not None for value in last_authorization
        ):
            raise ValueError("last write authorization binding must be all present or all absent")
        if self.authorization_sha256 is not None:
            if (
                self.authorization_sha256 != self.write_last_authorization_sha256
                or self.authorization_nonce_sha256 != self.write_last_authorization_nonce_sha256
                or self.external_claim_id != self.write_last_external_claim_id
                or self.external_claim_id != self.write_last_claim_id
            ):
                raise ValueError("active write authorization must match the current write claim")
        if self.write_status == "started" and self.authorization_sha256 is None:
            raise ValueError("started write state requires an active authorization binding")
        write_failure = self.write_failure_code is not None or self.write_failure_message is not None
        if self.write_result_sha256 is not None and self.reconciliation_sha256 is not None:
            raise ValueError("write result and reconciliation result are mutually exclusive")
        if self.write_status in {
            "not_applicable",
            "awaiting_candidate",
            "queued",
            "claimed",
            "prepared_only",
        }:
            if (
                self.authorization_sha256 is not None
                or self.write_result_sha256 is not None
                or self.reconciliation_sha256 is not None
                or write_failure
            ):
                raise ValueError("pre-start write state forbids active authorization and terminal artifacts")
        elif self.write_status == "started":
            if self.write_result_sha256 is not None or self.reconciliation_sha256 is not None or write_failure:
                raise ValueError("started write state forbids terminal artifacts and failure")
        elif self.write_status == "uncertain":
            if (
                self.authorization_sha256 is None
                or self.write_result_sha256 is not None
                or self.reconciliation_sha256 is not None
                or not write_failure
            ):
                raise ValueError("uncertain write state requires authorization and typed failure only")
        elif self.write_status in {"retry_confirmation_required", "blocked"}:
            if (
                self.authorization_sha256 is None
                or self.write_result_sha256 is not None
                or self.reconciliation_sha256 is None
                or not write_failure
            ):
                raise ValueError("reconciled write attention state requires authorization, result, and failure")
        elif self.write_status == "written":
            if (
                self.authorization_sha256 is None
                or (self.write_result_sha256 is None) == (self.reconciliation_sha256 is None)
                or write_failure
            ):
                raise ValueError("written write state requires authorization and exactly one successful result")
        worker_failure = self.worker_failure_code is not None or self.worker_failure_message is not None
        local_failure = self.local_prepare_failure_code is not None or self.local_prepare_failure_message is not None
        coordination_binding = (
            self.local_prepare_coordination_request_id,
            self.local_prepare_coordination_fingerprint,
            self.local_prepare_coordination_device,
            self.local_prepare_coordination_inode,
        )
        if any(value is not None for value in coordination_binding) != all(
            value is not None for value in coordination_binding
        ):
            raise ValueError("local prepare coordination binding must be all present or all absent")
        if all(value is not None for value in coordination_binding) and self.local_prepare_last_attempt_id is None:
            raise ValueError("local prepare coordination binding requires an authoritative attempt")
        if self.worker_status in {"failed", "blocked"}:
            if not worker_failure or self.worker_result_sha256 is None:
                raise ValueError("failed/blocked worker state requires result and typed worker failure")
        elif worker_failure:
            raise ValueError("worker failure fields require failed/blocked worker status")
        if self.local_prepare_status in {"failed", "blocked"}:
            if not local_failure or self.local_prepare_result_sha256 is None:
                raise ValueError("failed/blocked local state requires result and typed local failure")
        elif local_failure:
            raise ValueError("local failure fields require failed/blocked local status")
        if self.worker_status == "succeeded" and (self.worker_result_sha256 is None or self.candidate_sha256 is None):
            raise ValueError("successful worker state requires a result and candidate digest")
        if (
            self.local_prepare_status == "prepared"
            and self.local_prepare_result_sha256 is None
            and self.worker_status != "succeeded"
        ):
            raise ValueError("prepared local state requires a local result or completed worker result")
        return self


class BatchState(StrictModel):
    schema_version: Literal[STATE_SCHEMA_VERSION]
    manifest_id: UuidString
    manifest_sha256: Sha256
    lease_secret_sha256: Sha256
    initialized_at: Rfc3339Utc
    updated_at: Rfc3339Utc
    next_sequence: PositiveInt
    latest_event_sha256: Sha256
    batch_status: Literal[
        "corrupt",
        "write_uncertain",
        "running",
        "needs_attention",
        "awaiting_write",
        "ready",
        "succeeded",
    ]
    items: Annotated[list[StateItem], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_cross_item_write_identities(self) -> "BatchState":
        for field in (
            "write_last_claim_id",
            "write_last_attempt_id",
            "write_last_lease_token_sha256",
            "write_last_authorization_sha256",
            "write_last_authorization_nonce_sha256",
        ):
            values = [getattr(item, field) for item in self.items if getattr(item, field) is not None]
            if len(values) != len(set(values)):
                raise ValueError(f"{field} must be unique across batch items")
        pending = [
            item.write_pending_attempt_id
            for item in self.items
            if item.write_pending_attempt_id is not None
        ]
        last_attempts = {
            item.write_last_attempt_id
            for item in self.items
            if item.write_last_attempt_id is not None
        }
        if len(pending) != len(set(pending)) or set(pending) & last_attempts:
            raise ValueError("pending write attempt ids must be globally new and unique")
        return self


class ClaimAssignment(StrictModel):
    item_id: ItemId
    lane: Literal["worker", "local_prepare"]
    actor_id: NonEmptyString
    claim_id: UuidString
    attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    issued_at: Rfc3339Utc
    expires_at: Rfc3339Utc
    source: SourceIdentity


class RunInitializedData(StrictModel):
    kind: Literal["run.initialized"] = "run.initialized"
    manifest_id: UuidString
    initialized_at: Rfc3339Utc
    lease_secret_sha256: Sha256


class ClaimedData(StrictModel):
    kind: Literal["worker.claimed", "local_prepare.claimed"]
    assignments: Annotated[list[ClaimAssignment], Field(min_length=1)]


class LocalPrepareCoordinationReservedData(StrictModel):
    kind: Literal["local_prepare.coordination_reserved"] = "local_prepare.coordination_reserved"
    item_id: ItemId
    actor_id: NonEmptyString
    claim_id: UuidString
    attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    coordinator_request_id: UuidString
    coordinator_request_fingerprint: NonEmptyString
    request_dir_device: NonNegativeInt
    request_dir_inode: PositiveInt


class LeaseMutationData(StrictModel):
    kind: Literal[
        "worker.renewed",
        "worker.released",
        "worker.lease_expired",
        "local_prepare.renewed",
        "local_prepare.released",
        "local_prepare.lease_expired",
    ]
    item_id: ItemId
    actor_id: NonEmptyString
    claim_id: UuidString
    attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    issued_at: Rfc3339Utc | None = None
    expires_at: Rfc3339Utc | None = None


class FinishedData(StrictModel):
    kind: Literal["worker.finished", "local_prepare.finished"]
    item_id: ItemId
    actor_id: NonEmptyString
    claim_id: UuidString
    attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    status: Literal["succeeded", "failed", "blocked", "prepared"]
    result_sha256: Sha256
    resolved_zotero_item_key: NonEmptyString | None = None
    candidate_sha256: Sha256 | None = None
    failure_code: NonEmptyString | None = None
    failure_message: NonEmptyString | None = None


class RetriedData(StrictModel):
    kind: Literal["worker.retried"] = "worker.retried"
    item_id: ItemId
    previous_actor_id: NonEmptyString
    previous_claim_id: UuidString
    previous_attempt_id: UuidString
    previous_attempt_number: PositiveInt
    previous_lease_token_sha256: Sha256
    next_attempt_id: UuidString
    next_attempt_number: PositiveInt


class RecoveredLease(StrictModel):
    item_id: ItemId
    lane: Literal["worker", "local_prepare"]
    actor_id: NonEmptyString
    claim_id: UuidString
    attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    expires_at: Rfc3339Utc


class ResumedLocalPrepareLease(StrictModel):
    item_id: ItemId
    lane: Literal["local_prepare"] = "local_prepare"
    actor_id: NonEmptyString
    claim_id: UuidString
    attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    previous_expires_at: Rfc3339Utc
    issued_at: Rfc3339Utc
    expires_at: Rfc3339Utc


class RunRecoveredData(StrictModel):
    kind: Literal["run.recovered"] = "run.recovered"
    expired_worker_leases: list[RecoveredLease] = Field(default_factory=list)
    expired_local_prepare_leases: list[RecoveredLease] = Field(default_factory=list)
    resumed_local_prepare_leases: list[ResumedLocalPrepareLease] = Field(default_factory=list)
    snapshot_repaired: bool


class WriteClaimedData(StrictModel):
    kind: Literal["write.claimed"] = "write.claimed"
    item_id: ItemId
    writer_id: NonEmptyString
    claim_id: UuidString
    write_attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    issued_at: Rfc3339Utc
    expires_at: Rfc3339Utc
    candidate_sha256: Sha256


class WriteLeaseMutationData(StrictModel):
    kind: Literal["write.renewed", "write.released", "write.lease_expired"]
    item_id: ItemId
    writer_id: NonEmptyString
    claim_id: UuidString
    write_attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    candidate_sha256: Sha256
    issued_at: Rfc3339Utc | None = None
    expires_at: Rfc3339Utc | None = None


class WriteStartedData(StrictModel):
    kind: Literal["write.started"] = "write.started"
    item_id: ItemId
    writer_id: NonEmptyString
    claim_id: UuidString
    write_attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    candidate_sha256: Sha256
    authorization_sha256: Sha256
    authorization_nonce_sha256: Sha256
    external_claim_id: UuidString
    started_at: Rfc3339Utc


class WriteWrittenData(StrictModel):
    kind: Literal["write.written"] = "write.written"
    item_id: ItemId
    writer_id: NonEmptyString
    claim_id: UuidString
    write_attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    candidate_sha256: Sha256
    authorization_sha256: Sha256
    result_sha256: Sha256
    note_key: NonEmptyString
    parent_key: NonEmptyString
    canonical_html_sha256: Sha256


class WriteUncertainData(StrictModel):
    kind: Literal["write.marked_uncertain", "write.lease_expired_uncertain"]
    item_id: ItemId
    writer_id: NonEmptyString
    claim_id: UuidString
    write_attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    candidate_sha256: Sha256
    authorization_sha256: Sha256
    reason: NonEmptyString


class WriteReconciledData(StrictModel):
    kind: Literal["write.reconciled"] = "write.reconciled"
    item_id: ItemId
    writer_id: NonEmptyString
    claim_id: UuidString
    write_attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    candidate_sha256: Sha256
    authorization_sha256: Sha256
    reconciliation_sha256: Sha256
    outcome: Literal["verified", "not_found", "ambiguous", "blocked"]


class WriteRetriedData(StrictModel):
    kind: Literal["write.retried"] = "write.retried"
    item_id: ItemId
    previous_writer_id: NonEmptyString
    previous_claim_id: UuidString
    previous_write_attempt_id: UuidString
    previous_attempt_number: PositiveInt
    previous_lease_token_sha256: Sha256
    candidate_sha256: Sha256
    authorization_sha256: Sha256
    previous_authorization_nonce_sha256: Sha256
    previous_external_claim_id: UuidString
    reconciliation_sha256: Sha256
    acknowledged_no_match: bool
    next_write_attempt_id: UuidString
    next_attempt_number: PositiveInt


EventData: TypeAlias = Annotated[
    RunInitializedData
    | ClaimedData
    | LocalPrepareCoordinationReservedData
    | LeaseMutationData
    | FinishedData
    | RetriedData
    | RunRecoveredData
    | WriteClaimedData
    | WriteLeaseMutationData
    | WriteStartedData
    | WriteWrittenData
    | WriteUncertainData
    | WriteReconciledData
    | WriteRetriedData,
    Field(discriminator="kind"),
]


class EventCommandResultSnapshot(StrictModel):
    schema_version: Literal[COMMAND_RESULT_SCHEMA_VERSION]
    command: NonEmptyString
    request_id: UuidString
    replayed: Literal[False] = False
    ok: Literal[True] = True
    semantic_result_sha256: Sha256
    error: Literal[None] = None


class BatchEvent(StrictModel):
    schema_version: Literal[EVENT_SCHEMA_VERSION]
    sequence: PositiveInt
    event_id: UuidString
    occurred_at: Rfc3339Utc
    request_id: UuidString
    command: NonEmptyString
    request_fingerprint: Sha256
    manifest_sha256: Sha256
    previous_event_sha256: Sha256 | None
    data: EventData
    command_result: EventCommandResultSnapshot
    event_sha256: Sha256


class ResultError(StrictModel):
    code: NonEmptyString
    message: NonEmptyString


class WorkerResult(StrictModel):
    schema_version: Literal[WORKER_RESULT_SCHEMA_VERSION]
    manifest_sha256: Sha256
    item_id: ItemId
    worker_id: NonEmptyString
    claim_id: UuidString
    attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    status: Literal["succeeded", "failed", "blocked"]
    source: SourceIdentity
    paper_reader_run: ArtifactRef | None = None
    review_package: ArtifactRef | None = None
    candidate: ArtifactRef | None = None
    local_publication: ArtifactRef | None = None
    error: ResultError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "WorkerResult":
        if self.status == "succeeded":
            if self.error is not None or self.paper_reader_run is None or self.review_package is None or self.candidate is None:
                raise ValueError("succeeded worker result requires run, sealed review package, and candidate")
            if isinstance(self.source, PdfSource) and self.local_publication is None:
                raise ValueError("local PDF success requires a local publication")
            if not isinstance(self.source, PdfSource) and self.local_publication is not None:
                raise ValueError("Zotero success cannot contain a local publication")
        else:
            if self.error is None:
                raise ValueError("failed or blocked worker result requires error")
            if any(
                artifact is not None
                for artifact in (self.paper_reader_run, self.review_package, self.candidate, self.local_publication)
            ):
                raise ValueError("failed or blocked worker result forbids success artifacts")
        return self


class LocalPrepareResult(StrictModel):
    schema_version: Literal[LOCAL_PREPARE_RESULT_SCHEMA_VERSION]
    manifest_sha256: Sha256
    item_id: ItemId
    worker_id: NonEmptyString
    claim_id: UuidString
    attempt_id: UuidString
    attempt_number: PositiveInt
    lease_token_sha256: Sha256
    status: Literal["prepared", "failed", "blocked"]
    source: PdfSource
    paper_reader_root: SkillRootIdentity
    paper_reader_run: ArtifactRef | None = None
    evidence: ArtifactRef | None = None
    error: ResultError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "LocalPrepareResult":
        if self.status == "prepared":
            if self.error is not None or self.paper_reader_run is None or self.evidence is None:
                raise ValueError("prepared result requires run and evidence")
        else:
            if self.error is None:
                raise ValueError("failed or blocked local prepare result requires error")
            if self.paper_reader_run is not None or self.evidence is not None:
                raise ValueError("failed or blocked local prepare result forbids success artifacts")
        return self


class WriteResult(StrictModel):
    schema_version: Literal[WRITE_RESULT_SCHEMA_VERSION]
    manifest_sha256: Sha256
    item_id: ItemId
    writer_id: NonEmptyString
    claim_id: UuidString
    write_attempt_id: UuidString
    lease_token_sha256: Sha256
    started_event_sha256: Sha256
    candidate_sha256: Sha256
    authorization_sha256: Sha256
    authorization_nonce_sha256: Sha256
    external_claim_id: UuidString
    note_key: NonEmptyString
    parent_key: NonEmptyString
    canonical_html_sha256: Sha256
    verification: ArtifactRef
    status: Literal["written"] = "written"

    @model_validator(mode="after")
    def validate_write_closure(self) -> "WriteResult":
        if self.external_claim_id != self.claim_id:
            raise ValueError("external_claim_id must equal the consumed batch claim_id")
        if self.verification.schema_version != "paper_reader.verification.v2":
            raise ValueError("verification must use paper_reader.verification.v2")
        return self


class ReconciliationResult(StrictModel):
    schema_version: Literal[RECONCILIATION_SCHEMA_VERSION]
    manifest_sha256: Sha256
    item_id: ItemId
    writer_id: NonEmptyString
    claim_id: UuidString
    lease_token_sha256: Sha256
    write_attempt_id: UuidString
    candidate_sha256: Sha256
    authorization_sha256: Sha256
    readback_sha256: Sha256
    parent_key: NonEmptyString
    exact_title: NonEmptyString
    canonical_html_sha256: Sha256
    matched_note_keys: list[NonEmptyString]
    match_count: NonNegativeInt
    outcome: Literal["verified", "not_found", "ambiguous", "blocked"]
    verification: ArtifactRef | None = None
    matched_note_key: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_match_outcome(self) -> "ReconciliationResult":
        if self.match_count != len(self.matched_note_keys):
            raise ValueError("match_count must equal len(matched_note_keys)")
        if len(set(self.matched_note_keys)) != self.match_count:
            raise ValueError("matched_note_keys must contain distinct exact matches")
        if self.outcome == "not_found":
            if self.match_count != 0 or self.matched_note_key is not None or self.verification is not None:
                raise ValueError("not_found requires zero matches and no selected note or verification")
        elif self.outcome == "ambiguous":
            if self.match_count <= 1 or self.matched_note_key is not None or self.verification is not None:
                raise ValueError("ambiguous requires multiple matches and no selected note or verification")
        elif self.outcome in {"verified", "blocked"}:
            if (
                self.match_count != 1
                or self.matched_note_key != self.matched_note_keys[0]
                or self.verification is None
            ):
                raise ValueError(
                    f"{self.outcome} requires one matching selected note and a full verification artifact"
                )
            if self.verification.schema_version != "paper_reader.verification.v2":
                raise ValueError("reconciliation verification must use paper_reader.verification.v2")
        return self


class ReportItem(StrictModel):
    item_id: ItemId
    input_type: Literal["pdf_path", "zotero_item", "zotero_title"]
    status: Literal["queued", "claimed", "succeeded", "failed", "blocked", "prepared"]
    write_status: Literal[
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
    ]
    thirty_second_takeaway: str
    takeaway_source_type: str
    takeaway_source_path: AbsolutePath | None = None
    takeaway_source_sha256: Sha256 | None = None
    failure_code: str
    failure_message: str


class BatchReport(StrictModel):
    schema_version: Literal[REPORT_SCHEMA_VERSION]
    manifest_id: UuidString
    manifest_sha256: Sha256
    generated_at: Rfc3339Utc
    report_generation_id: Sha256
    report_markdown_sha256: Sha256
    batch_status: Literal[
        "corrupt",
        "write_uncertain",
        "running",
        "needs_attention",
        "awaiting_write",
        "ready",
        "succeeded",
    ]
    write_policy: Literal["zotero_write", "prepare_only"]
    effective_write_policy: Literal["zotero_write", "prepare_only", "local_only"]
    items: Annotated[list[ReportItem], Field(min_length=1)]


class CommandError(StrictModel):
    code: NonEmptyString
    message: NonEmptyString
    details: dict[str, JsonValue] = Field(default_factory=dict)


class CommandResult(StrictModel):
    schema_version: Literal[COMMAND_RESULT_SCHEMA_VERSION]
    command: NonEmptyString
    request_id: UuidString | None
    replayed: bool
    ok: bool
    result: dict[str, JsonValue] | None
    error: CommandError | None

    @model_validator(mode="after")
    def result_matches_status(self) -> "CommandResult":
        if self.ok and (self.error is not None or self.result is None):
            raise ValueError("successful command result requires result and forbids error")
        if not self.ok and (self.error is None or self.result is not None):
            raise ValueError("failed command result requires error and forbids result")
        return self


ACTIVE_CONTRACT_MODELS: dict[str, type[StrictModel]] = {
    MANIFEST_SCHEMA_VERSION: BatchManifest,
    STATE_SCHEMA_VERSION: BatchState,
    EVENT_SCHEMA_VERSION: BatchEvent,
    WORKER_RESULT_SCHEMA_VERSION: WorkerResult,
    LOCAL_PREPARE_RESULT_SCHEMA_VERSION: LocalPrepareResult,
    WRITE_RESULT_SCHEMA_VERSION: WriteResult,
    RECONCILIATION_SCHEMA_VERSION: ReconciliationResult,
    REPORT_SCHEMA_VERSION: BatchReport,
    COMMAND_RESULT_SCHEMA_VERSION: CommandResult,
}


def export_contract_schemas() -> dict[str, dict[str, JsonValue]]:
    return {
        schema_version: model.model_json_schema(mode="validation")
        for schema_version, model in ACTIVE_CONTRACT_MODELS.items()
    }


def schema_filename(schema_version: str) -> str:
    if schema_version not in ACTIVE_CONTRACT_MODELS:
        raise ValueError(f"unknown active contract: {schema_version}")
    return f"{schema_version}.schema.json"
