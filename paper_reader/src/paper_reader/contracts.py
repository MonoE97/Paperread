from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from paper_reader.storage import safe_relative_artifact_path


def _validate_rfc3339_utc(value: str) -> str:
    if not value.endswith("Z"):
        raise ValueError("timestamp must use the UTC Z suffix")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp must be valid RFC3339 UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return value


def _validate_absolute_path(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("resolved path must be non-empty")
    if not value.startswith("/"):
        raise ValueError("resolved path must be absolute")
    return value


def _validate_secondary_cross_check_text(value: str) -> str:
    if value != value.strip() or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("secondary cross-check text must be a trimmed single line")
    if "http://" in value.lower() or "https://" in value.lower():
        raise ValueError("secondary cross-check text must not embed source URLs")
    return value


def _authorization_utc_instant(value: str) -> datetime:
    time_text = value[:-1]
    if "." in time_text:
        fractional = time_text.rsplit(".", 1)[1]
        if len(fractional) > 6:
            raise ValueError(
                "authorization timestamps must not exceed microsecond precision"
            )
    return datetime.fromisoformat(f"{time_text}+00:00")


Rfc3339Utc: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"),
    AfterValidator(_validate_rfc3339_utc),
]
Sha256: TypeAlias = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
PortableIdentifier: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
]
ArtifactPath: TypeAlias = Annotated[str, AfterValidator(safe_relative_artifact_path)]
AbsolutePath: TypeAlias = Annotated[str, AfterValidator(_validate_absolute_path)]
NonNegativeInt: TypeAlias = Annotated[int, Field(ge=0)]
PositiveInt: TypeAlias = Annotated[int, Field(gt=0)]
SecondaryCrossCheckText: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2_000),
    AfterValidator(_validate_secondary_cross_check_text),
]


class StrictContractModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ArtifactRef(StrictContractModel):
    role: Identifier
    path: ArtifactPath
    sha256: Sha256
    size_bytes: NonNegativeInt
    media_type: str | None = None


class LocalSourceIdentity(StrictContractModel):
    source_type: Literal["local_pdf"] = "local_pdf"
    requested_path: str
    resolved_path: AbsolutePath
    sha256: Sha256
    size_bytes: NonNegativeInt
    device: NonNegativeInt
    inode: NonNegativeInt


class ZoteroSourceIdentity(StrictContractModel):
    source_type: Literal["zotero"] = "zotero"
    item_key: Identifier
    title: str
    doi: str
    parent_version: NonNegativeInt
    parent_fingerprint: Sha256
    raw_discovery_bundle: ArtifactRef
    normalized_source: ArtifactRef
    attachment_key: Identifier
    attachment: LocalSourceIdentity


SourceIdentity: TypeAlias = Annotated[
    LocalSourceIdentity | ZoteroSourceIdentity,
    Field(discriminator="source_type"),
]


class LocalPublicationTarget(StrictContractModel):
    target_type: Literal["local"] = "local"
    resolved_path: AbsolutePath
    parent_device: NonNegativeInt
    parent_inode: NonNegativeInt


class ZoteroPublicationTarget(StrictContractModel):
    target_type: Literal["zotero"] = "zotero"
    parent_key: Identifier
    parent_fingerprint: Sha256
    note_title: str


PublicationTarget: TypeAlias = Annotated[
    LocalPublicationTarget | ZoteroPublicationTarget,
    Field(discriminator="target_type"),
]


class GateBlocker(StrictContractModel):
    code: Identifier
    message: str
    artifact_path: ArtifactPath | None = None


class GateState(StrictContractModel):
    status: Literal["not_evaluated", "blocked", "passed", "write_ready"]
    evaluated_at: Rfc3339Utc | None = None
    checks: tuple[Identifier, ...] = ()
    blockers: tuple[GateBlocker, ...] = ()


class LivePreflight(StrictContractModel):
    preflight_id: Identifier
    captured_at: Rfc3339Utc
    parent_key: Identifier
    parent_fingerprint: Sha256
    requested_note_title: str
    title_available: bool
    matching_note_keys: tuple[Identifier, ...]
    parent_snapshot: ArtifactRef
    children_snapshot: ArtifactRef


class MethodModule(StrictContractModel):
    name: str
    input: str
    target: str
    output: str
    role: str


class KeyFigure(StrictContractModel):
    figure_id: Identifier
    caption: str
    analysis: str | None = None
    why_it_matters: str | None = None
    why_it_matters_short: str | None = None
    image_quality: str | None = None
    evidence_level: str | None = None
    figure_quality_note: str | None = None


class EvidenceItem(StrictContractModel):
    type: str
    locator: str
    summary: str


class EvidenceClaim(StrictContractModel):
    claim: str
    evidence: tuple[EvidenceItem, ...]
    confidence: Literal["low", "medium", "high"]


class AuthorStatedLimitation(StrictContractModel):
    text: str
    source_type: Literal["author_stated"] = "author_stated"
    locator: str


class InferredLimitation(StrictContractModel):
    text: str
    source_type: Literal["inferred"] = "inferred"
    basis: str
    locator: str


class SecondaryTextAnchor(StrictContractModel):
    capture_sha256: Sha256
    start_codepoint: NonNegativeInt
    end_codepoint: PositiveInt
    excerpt_sha256: Sha256

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        span_length = self.end_codepoint - self.start_codepoint
        if not 20 <= span_length <= 2_000:
            raise ValueError(
                "secondary text anchor span must contain 20 to 2,000 code points"
            )
        return self


class SecondaryCrossCheckFinding(StrictContractModel):
    relation: Literal["supports", "extends", "questions", "conflicts"]
    target: Literal[
        "core_result_short_annotation",
        "main_risk_short_annotation",
        "technical_details_item",
        "inferred_limits_item",
        "applicability_limits_item",
    ]
    text: SecondaryCrossCheckText
    caveats: tuple[SecondaryCrossCheckText, ...] = ()
    anchor: SecondaryTextAnchor | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("anchor", mode="before")
    @classmethod
    def reject_explicit_null_anchor(cls, value: object) -> object:
        if value is None:
            raise ValueError("secondary cross-check anchor must be omitted instead of null")
        return value

    @model_validator(mode="after")
    def validate_caveats(self) -> Self:
        if len(self.caveats) > 3:
            raise ValueError("secondary cross-check finding accepts at most three caveats")
        return self


class SecondaryCrossCheck(StrictContractModel):
    source_id: Identifier
    status: Literal["used", "irrelevant", "unavailable"]
    reason: SecondaryCrossCheckText
    findings: tuple[SecondaryCrossCheckFinding, ...]

    @model_validator(mode="after")
    def validate_status_payload(self) -> Self:
        if self.status == "used" and not 1 <= len(self.findings) <= 3:
            raise ValueError("used secondary source requires one to three findings")
        if self.status != "used" and self.findings:
            raise ValueError("irrelevant or unavailable secondary source cannot contain findings")
        return self


class ReviewIssue(StrictContractModel):
    severity: Literal["low", "medium", "high", "blocker"]
    issue: str
    suggested_fix: str


class ImprovementNote(StrictContractModel):
    issue: str
    action: str
    source: str


class McpWriteEnvelope(StrictContractModel):
    action: Literal["create"] = "create"
    parentKey: Identifier
    content: str
    tags: tuple[str, ...]


class VerificationCheck(StrictContractModel):
    name: Identifier
    passed: bool
    expected: JsonValue | None = None
    actual: JsonValue | None = None
    message: str | None = None


class PaperReaderRun(StrictContractModel):
    schema_version: Literal["paper_reader.run.v2"]
    run_id: Identifier
    created_at: Rfc3339Utc
    source: SourceIdentity
    target: PublicationTarget | None
    status: Literal["initialized", "prepared", "reviewed", "candidate_built", "published", "blocked"]
    artifacts: tuple[ArtifactRef, ...] = ()
    gate: GateState
    live_preflight: LivePreflight | None = None


class PaperReaderSummary(StrictContractModel):
    schema_version: Literal["paper_reader.summary.v2"]
    summary_id: Identifier
    run_id: Identifier
    created_at: Rfc3339Utc
    evidence_digest: Sha256
    paper_type: Literal[
        "research_article",
        "review",
        "perspective",
        "benchmark",
        "method_paper",
        "dataset_paper",
        "theory_paper",
    ]
    trust_status: Literal["trusted", "usable_with_caveats", "needs_manual_review", "rejected"]
    review_status: Literal["not_reviewed", "passed", "passed_with_caveats", "failed"]
    improvement_status: Literal["not_needed", "needed", "completed"]
    trust_rationale: str
    one_sentence_summary: str
    abstract_translation: str
    research_question: str
    method: str
    experiments: str
    ai4s_relevance: str
    key_points: tuple[str, ...]
    contributions: tuple[str, ...]
    limitations: tuple[str, ...]
    follow_up_keywords: tuple[str, ...]
    evidence_summary: tuple[EvidenceClaim, ...]
    secondary_cross_checks: tuple[SecondaryCrossCheck, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    tldr: str | None = None
    research_object: str | None = None
    research_question_short: str | None = None
    core_method_short: str | None = None
    core_result_short: str | None = None
    main_risk_short: str | None = None
    reading_decision: str | None = None
    background_problem: str | None = None
    existing_gap: str | None = None
    paper_entry_point: str | None = None
    method_overview: str | None = None
    method_modules: tuple[MethodModule, ...] = ()
    workflow_steps: tuple[str, ...] = ()
    technical_details: tuple[str, ...] = ()
    key_figures: tuple[KeyFigure, ...] = ()
    author_stated_limitations: tuple[AuthorStatedLimitation, ...] = ()
    inferred_limits: tuple[InferredLimitation, ...] = ()
    applicability_limits: tuple[str, ...] = ()
    note_labels: tuple[str, ...] = ()
    review_issues: tuple[ReviewIssue, ...] = ()
    improvement_notes: tuple[ImprovementNote, ...] = ()


class PaperReaderReview(StrictContractModel):
    schema_version: Literal["paper_reader.review.v2"]
    review_id: Identifier
    run_id: Identifier
    created_at: Rfc3339Utc
    summary_sha256: Sha256
    evidence_digest: Sha256
    review_status: Literal["passed", "passed_with_caveats", "failed"]
    needs_improvement: bool
    review_issues: tuple[ReviewIssue, ...]
    trust_status_recommendation: Literal[
        "trusted", "usable_with_caveats", "needs_manual_review", "rejected"
    ]
    improvement_requests: tuple[str, ...]


class PaperReaderReviewPackage(StrictContractModel):
    schema_version: Literal["paper_reader.review-package.v2"]
    review_package_id: Identifier
    run_id: Identifier
    created_at: Rfc3339Utc
    summary: ArtifactRef
    review: ArtifactRef
    evidence_manifest: ArtifactRef
    summary_sha256: Sha256
    review_sha256: Sha256
    evidence_digest: Sha256
    artifacts: tuple[ArtifactRef, ...]
    gate: GateState


class PaperReaderCandidate(StrictContractModel):
    schema_version: Literal["paper_reader.candidate.v2"]
    candidate_id: Identifier
    run_id: Identifier
    created_at: Rfc3339Utc
    source: SourceIdentity
    target: PublicationTarget
    evidence_manifest: ArtifactRef
    sealed_review: ArtifactRef
    note_title: str
    tags: tuple[str, ...]
    content_sha256: Sha256
    content_length: NonNegativeInt
    artifacts: tuple[ArtifactRef, ...]
    gate: GateState
    live_preflight: LivePreflight | None = None


class PaperReaderWriteAuthorization(StrictContractModel):
    schema_version: Literal["paper_reader.write-authorization.v2"]
    authorization_id: Identifier
    run_id: Identifier
    created_at: Rfc3339Utc
    expires_at: Rfc3339Utc
    ttl_seconds: Annotated[int, Field(gt=0, le=300)]
    candidate: ArtifactRef
    candidate_digest: Sha256
    target: ZoteroPublicationTarget
    note_title: str
    tags: tuple[str, ...]
    content_html: str
    content_sha256: Sha256
    content_length: NonNegativeInt
    minimum_content_length: NonNegativeInt
    required_headings: tuple[str, ...]
    forbidden_headings: tuple[str, ...]
    nonce: str
    token_sha256: Sha256
    external_claim_id: Identifier
    write_attempt_id: Identifier
    mcp_envelope: McpWriteEnvelope
    artifacts: tuple[ArtifactRef, ...]
    live_preflight: LivePreflight
    gate: GateState

    @model_validator(mode="after")
    def _validate_exact_ttl_interval(self) -> Self:
        created_at = _authorization_utc_instant(self.created_at)
        expires_at = _authorization_utc_instant(self.expires_at)
        if expires_at - created_at != timedelta(seconds=self.ttl_seconds):
            raise ValueError(
                "expires_at must equal created_at + ttl_seconds exactly"
            )
        return self


class PaperReaderVerification(StrictContractModel):
    schema_version: Literal["paper_reader.verification.v2"]
    verification_id: Identifier
    run_id: Identifier
    created_at: Rfc3339Utc
    authorization: ArtifactRef
    authorization_digest: Sha256
    target: ZoteroPublicationTarget
    note_key: PortableIdentifier
    verified: bool
    content_sha256: Sha256
    content_length: NonNegativeInt
    checks: tuple[VerificationCheck, ...]
    note_snapshot: ArtifactRef
    checks_snapshot: ArtifactRef
    artifacts: tuple[ArtifactRef, ...]
    gate: GateState


class PaperReaderReconciliation(StrictContractModel):
    schema_version: Literal["paper_reader.reconciliation.v2"]
    reconciliation_id: Identifier
    run_id: Identifier
    created_at: Rfc3339Utc
    authorization: ArtifactRef
    authorization_digest: Sha256
    target: ZoteroPublicationTarget
    outcome: Literal["verified", "not_found", "ambiguous", "blocked"]
    match_count: NonNegativeInt
    matched_note_keys: tuple[PortableIdentifier, ...]
    children_snapshot: ArtifactRef
    verification: ArtifactRef | None = None
    retry_confirmation_required: bool
    artifacts: tuple[ArtifactRef, ...]
    gate: GateState


class PaperReaderCommandResult(StrictContractModel):
    schema_version: Literal["paper_reader.command-result.v2"]
    command: str
    ok: bool
    code: Identifier
    created_at: Rfc3339Utc
    message: str | None = None
    data: dict[str, JsonValue]


V2_SCHEMA_MODELS = {
    "paper_reader.run.v2": PaperReaderRun,
    "paper_reader.summary.v2": PaperReaderSummary,
    "paper_reader.review.v2": PaperReaderReview,
    "paper_reader.review-package.v2": PaperReaderReviewPackage,
    "paper_reader.candidate.v2": PaperReaderCandidate,
    "paper_reader.write-authorization.v2": PaperReaderWriteAuthorization,
    "paper_reader.verification.v2": PaperReaderVerification,
    "paper_reader.reconciliation.v2": PaperReaderReconciliation,
    "paper_reader.command-result.v2": PaperReaderCommandResult,
}

V2_SUPPORT_MODELS = (
    ArtifactRef,
    LocalSourceIdentity,
    ZoteroSourceIdentity,
    LocalPublicationTarget,
    ZoteroPublicationTarget,
    GateBlocker,
    GateState,
    LivePreflight,
    MethodModule,
    KeyFigure,
    EvidenceItem,
    EvidenceClaim,
    AuthorStatedLimitation,
    InferredLimitation,
    SecondaryTextAnchor,
    SecondaryCrossCheckFinding,
    SecondaryCrossCheck,
    ReviewIssue,
    ImprovementNote,
    McpWriteEnvelope,
    VerificationCheck,
)


def schema_filename(schema_version: str) -> str:
    if schema_version not in V2_SCHEMA_MODELS:
        raise ValueError(f"unknown V2 schema: {schema_version}")
    return f"{schema_version}.schema.json"


__all__ = [
    "ArtifactRef",
    "GateState",
    "LivePreflight",
    "LocalPublicationTarget",
    "LocalSourceIdentity",
    "PaperReaderCandidate",
    "PaperReaderCommandResult",
    "PaperReaderReconciliation",
    "PaperReaderReview",
    "PaperReaderReviewPackage",
    "PaperReaderRun",
    "PaperReaderSummary",
    "SecondaryCrossCheck",
    "SecondaryCrossCheckFinding",
    "SecondaryTextAnchor",
    "PaperReaderVerification",
    "PaperReaderWriteAuthorization",
    "PortableIdentifier",
    "V2_SCHEMA_MODELS",
    "V2_SUPPORT_MODELS",
    "ValidationError",
    "ZoteroPublicationTarget",
    "ZoteroSourceIdentity",
    "schema_filename",
]
