from __future__ import annotations

from datetime import datetime
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
from typing import Annotated, Literal, TypeAlias

from markdown_it import MarkdownIt
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from paper_reader_batch.v2_contracts import (
    ArtifactRef,
    BatchManifest,
    LocalPrepareResult,
    PdfManifestItem,
    PdfSource,
    SkillRootIdentity,
    WorkerResult,
    ZoteroItemManifestItem,
    ZoteroTitleManifestItem,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    canonical_json_bytes,
    canonical_sha256,
    list_directory,
    normalized_absolute_path,
    open_directory_fd,
    read_bytes,
    sha256_bytes,
)
from paper_reader_batch.v2_manifest import _pdf_source


class ForeignStrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


def _validate_foreign_rfc3339_utc(value: str) -> str:
    if not value.endswith("Z"):
        raise ValueError("timestamp must use the UTC Z suffix")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp must be valid RFC3339 UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return value


def _validate_foreign_absolute_path(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("resolved path must be non-empty")
    if not value.startswith("/"):
        raise ValueError("resolved path must be absolute")
    return value


def _validate_foreign_artifact_path(value: str) -> str:
    if not value or value == "." or "\\" in value or "\x00" in value:
        raise ValueError("artifact path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("artifact path must not be absolute or contain dot segments")
    if str(path) != value:
        raise ValueError("artifact path must be normalized")
    return value


# These aliases intentionally copy paper_reader.contracts rather than importing
# the sibling skill. The two skills remain independently installable while the
# foreign envelope parser enforces the exact public single-paper field contract.
ForeignRfc3339Utc: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"),
    AfterValidator(_validate_foreign_rfc3339_utc),
]
ForeignSha256: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
ForeignIdentifier: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
ForeignArtifactPath: TypeAlias = Annotated[
    str,
    AfterValidator(_validate_foreign_artifact_path),
]
ForeignAbsolutePath: TypeAlias = Annotated[
    str,
    AfterValidator(_validate_foreign_absolute_path),
]
ForeignNonNegativeInt: TypeAlias = Annotated[int, Field(ge=0)]


class ForeignArtifactRef(ForeignStrictModel):
    role: ForeignIdentifier
    path: ForeignArtifactPath
    sha256: ForeignSha256
    size_bytes: ForeignNonNegativeInt
    media_type: str | None = None


class ForeignLocalSource(ForeignStrictModel):
    source_type: Literal["local_pdf"] = "local_pdf"
    requested_path: str
    resolved_path: ForeignAbsolutePath
    sha256: ForeignSha256
    size_bytes: ForeignNonNegativeInt
    device: ForeignNonNegativeInt
    inode: ForeignNonNegativeInt


class ForeignZoteroSource(ForeignStrictModel):
    source_type: Literal["zotero"] = "zotero"
    item_key: ForeignIdentifier
    title: str
    doi: str
    parent_version: ForeignNonNegativeInt
    parent_fingerprint: ForeignSha256
    raw_discovery_bundle: ForeignArtifactRef
    normalized_source: ForeignArtifactRef
    attachment_key: ForeignIdentifier
    attachment: ForeignLocalSource


ForeignSource: TypeAlias = Annotated[
    ForeignLocalSource | ForeignZoteroSource,
    Field(discriminator="source_type"),
]


class ForeignLocalTarget(ForeignStrictModel):
    target_type: Literal["local"] = "local"
    resolved_path: ForeignAbsolutePath
    parent_device: ForeignNonNegativeInt


class ForeignZoteroTarget(ForeignStrictModel):
    target_type: Literal["zotero"] = "zotero"
    parent_key: ForeignIdentifier
    parent_fingerprint: ForeignSha256
    note_title: str


ForeignTarget: TypeAlias = Annotated[
    ForeignLocalTarget | ForeignZoteroTarget,
    Field(discriminator="target_type"),
]


class ForeignGateBlocker(ForeignStrictModel):
    code: ForeignIdentifier
    message: str
    artifact_path: ForeignArtifactPath | None = None


class ForeignGate(ForeignStrictModel):
    status: Literal["not_evaluated", "blocked", "passed", "write_ready"]
    evaluated_at: ForeignRfc3339Utc | None = None
    checks: tuple[ForeignIdentifier, ...] = ()
    blockers: tuple[ForeignGateBlocker, ...] = ()


class ForeignLivePreflight(ForeignStrictModel):
    preflight_id: ForeignIdentifier
    captured_at: ForeignRfc3339Utc
    parent_key: ForeignIdentifier
    parent_fingerprint: ForeignSha256
    requested_note_title: str
    title_available: bool
    matching_note_keys: tuple[ForeignIdentifier, ...]
    parent_snapshot: ForeignArtifactRef
    children_snapshot: ForeignArtifactRef


class ForeignRun(ForeignStrictModel):
    schema_version: Literal["paper_reader.run.v2"]
    run_id: ForeignIdentifier
    created_at: ForeignRfc3339Utc
    source: ForeignSource
    target: ForeignTarget | None
    status: Literal["initialized", "prepared", "reviewed", "candidate_built", "published", "blocked"]
    artifacts: tuple[ForeignArtifactRef, ...] = ()
    gate: ForeignGate
    live_preflight: ForeignLivePreflight | None = None


class ForeignReviewIssue(ForeignStrictModel):
    severity: Literal["low", "medium", "high", "blocker"]
    issue: str
    suggested_fix: str


class ForeignMethodModule(ForeignStrictModel):
    name: str
    input: str
    target: str
    output: str
    role: str


class ForeignKeyFigure(ForeignStrictModel):
    figure_id: ForeignIdentifier
    caption: str
    analysis: str | None = None
    why_it_matters: str | None = None
    why_it_matters_short: str | None = None
    image_quality: str | None = None
    evidence_level: str | None = None
    figure_quality_note: str | None = None


class ForeignEvidenceItem(ForeignStrictModel):
    type: str
    locator: str
    summary: str


class ForeignEvidenceClaim(ForeignStrictModel):
    claim: str
    evidence: tuple[ForeignEvidenceItem, ...]
    confidence: Literal["low", "medium", "high"]


class ForeignAuthorLimitation(ForeignStrictModel):
    text: str
    source_type: Literal["author_stated"] = "author_stated"
    locator: str


class ForeignInferredLimitation(ForeignStrictModel):
    text: str
    source_type: Literal["inferred"] = "inferred"
    basis: str
    locator: str


class ForeignImprovementNote(ForeignStrictModel):
    issue: str
    action: str
    source: str


class ForeignSummary(ForeignStrictModel):
    schema_version: Literal["paper_reader.summary.v2"]
    summary_id: ForeignIdentifier
    run_id: ForeignIdentifier
    created_at: ForeignRfc3339Utc
    evidence_digest: ForeignSha256
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
    evidence_summary: tuple[ForeignEvidenceClaim, ...]
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
    method_modules: tuple[ForeignMethodModule, ...] = ()
    workflow_steps: tuple[str, ...] = ()
    technical_details: tuple[str, ...] = ()
    key_figures: tuple[ForeignKeyFigure, ...] = ()
    author_stated_limitations: tuple[ForeignAuthorLimitation, ...] = ()
    inferred_limits: tuple[ForeignInferredLimitation, ...] = ()
    applicability_limits: tuple[str, ...] = ()
    note_labels: tuple[str, ...] = ()
    review_issues: tuple[ForeignReviewIssue, ...] = ()
    improvement_notes: tuple[ForeignImprovementNote, ...] = ()


class ForeignReview(ForeignStrictModel):
    schema_version: Literal["paper_reader.review.v2"]
    review_id: ForeignIdentifier
    run_id: ForeignIdentifier
    created_at: ForeignRfc3339Utc
    summary_sha256: ForeignSha256
    evidence_digest: ForeignSha256
    review_status: Literal["passed", "passed_with_caveats", "failed"]
    needs_improvement: bool
    review_issues: tuple[ForeignReviewIssue, ...]
    trust_status_recommendation: Literal[
        "trusted", "usable_with_caveats", "needs_manual_review", "rejected"
    ]
    improvement_requests: tuple[str, ...]


class ForeignReviewValidation(ForeignStrictModel):
    format: Literal["paper_reader.review-validation.v2-internal"]
    run_id: ForeignIdentifier
    summary_sha256: ForeignSha256
    review_sha256: ForeignSha256
    evidence_digest: ForeignSha256
    rendered_note_sha256: ForeignSha256
    rendered_html_sha256: ForeignSha256
    checks: tuple[ForeignIdentifier, ...]
    blockers: tuple[ForeignGateBlocker, ...]


class ForeignReviewPackage(ForeignStrictModel):
    schema_version: Literal["paper_reader.review-package.v2"]
    review_package_id: ForeignIdentifier
    run_id: ForeignIdentifier
    created_at: ForeignRfc3339Utc
    summary: ForeignArtifactRef
    review: ForeignArtifactRef
    evidence_manifest: ForeignArtifactRef
    summary_sha256: ForeignSha256
    review_sha256: ForeignSha256
    evidence_digest: ForeignSha256
    artifacts: tuple[ForeignArtifactRef, ...]
    gate: ForeignGate


class ForeignCandidate(ForeignStrictModel):
    schema_version: Literal["paper_reader.candidate.v2"]
    candidate_id: ForeignIdentifier
    run_id: ForeignIdentifier
    created_at: ForeignRfc3339Utc
    source: ForeignSource
    target: ForeignTarget
    evidence_manifest: ForeignArtifactRef
    sealed_review: ForeignArtifactRef
    note_title: str
    tags: tuple[str, ...]
    content_sha256: ForeignSha256
    content_length: ForeignNonNegativeInt
    artifacts: tuple[ForeignArtifactRef, ...]
    gate: ForeignGate
    live_preflight: ForeignLivePreflight | None = None


class EvidenceResourceCheck(ForeignStrictModel):
    name: str
    status: Literal["passed", "degraded", "blocked"]
    actual: int | float | str | bool | None
    limit: int | float | str | None
    message: str | None = None


class EvidenceSection(ForeignStrictModel):
    title: str
    start_page: ForeignNonNegativeInt
    end_page: ForeignNonNegativeInt


class EvidenceTable(ForeignStrictModel):
    index: ForeignNonNegativeInt
    page: ForeignNonNegativeInt
    section: str


class EvidenceFigure(ForeignStrictModel):
    figure_id: ForeignIdentifier
    page: ForeignNonNegativeInt
    artifact_path: ForeignArtifactPath


class ForeignEvidence(ForeignStrictModel):
    format: Literal["paper_reader.evidence.v2-internal"]
    evidence_id: ForeignIdentifier
    run_id: ForeignIdentifier
    created_at: ForeignRfc3339Utc
    source_sha256: ForeignSha256
    complete: bool
    degraded: bool
    preview_pages: ForeignNonNegativeInt | None
    files: tuple[ForeignArtifactRef, ...]
    pages: tuple[ForeignNonNegativeInt, ...]
    sections: tuple[EvidenceSection, ...]
    table_candidates: tuple[EvidenceTable, ...]
    figures: tuple[EvidenceFigure, ...]
    resource_checks: tuple[EvidenceResourceCheck, ...]


class ForeignLocalReceipt(ForeignStrictModel):
    format: Literal["paper_reader.local-receipt.v2-internal"]
    receipt_id: ForeignIdentifier
    run_id: ForeignIdentifier
    candidate_path: ForeignArtifactPath
    candidate_digest: ForeignSha256
    intent_path: ForeignArtifactPath
    intent_sha256: ForeignSha256
    target_path: ForeignAbsolutePath
    content_sha256: ForeignSha256
    content_length: ForeignNonNegativeInt


class ForeignLocalIntent(ForeignStrictModel):
    format: Literal["paper_reader.local-publication-intent.v2-internal"]
    run_id: ForeignIdentifier
    candidate_id: ForeignIdentifier
    candidate_digest: ForeignSha256
    target_path: ForeignAbsolutePath
    content_sha256: ForeignSha256
    content_length: ForeignNonNegativeInt


class _HeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current = ""
        self.parts: list[str] = []
        self.title = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "h1" and not self.title:
            self.current = "h1"
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.current:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == self.current:
            self.title = " ".join("".join(self.parts).split())
            self.current = ""


def _html_title(content: str) -> str:
    parser = _HeadingParser()
    parser.feed(content)
    parser.close()
    return parser.title


def _render_note_html_like_single(markdown: str) -> bytes:
    rendered = MarkdownIt("commonmark", {"html": False}).enable("table").render(markdown)
    return (rendered.strip() + "\n").encode("utf-8")


def _markdown_literal_note_title(note_title: str) -> str:
    prefix = "[Codex Summary] "
    fixed_prefix = prefix if note_title.startswith(prefix) else ""
    remainder = note_title[len(fixed_prefix) :]
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "&"):
        remainder = remainder.replace(character, f"\\{character}")
    return f"{fixed_prefix}{remainder}"


def _expected_zotero_candidate_notes(
    sealed_markdown: bytes,
    note_title: str,
) -> tuple[bytes, bytes]:
    if "\r" in note_title or "\n" in note_title:
        raise _invalid("candidate_binding_mismatch", "candidate note title must be one line")
    try:
        markdown = sealed_markdown.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid(
            "candidate_binding_mismatch",
            "sealed Markdown must be UTF-8 before candidate title rewrite",
            exc,
        )
    lines = markdown.splitlines(keepends=True)
    if not lines or not lines[0].startswith("# "):
        raise _invalid(
            "candidate_binding_mismatch",
            "sealed Markdown must start with the review H1",
        )
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n" if lines[0].endswith("\n") else ""
    lines[0] = f"# {_markdown_literal_note_title(note_title)}{newline}"
    expected_markdown = "".join(lines).encode("utf-8")
    return expected_markdown, _render_note_html_like_single(expected_markdown.decode("utf-8"))


def _expected_tags(labels: tuple[str, ...]) -> tuple[str, ...]:
    result = ["codex-summary", "paper-summary"]
    seen = set(result)
    for raw in labels:
        normalized = raw.strip().lower().replace("&", " and ")
        normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
        normalized = re.sub(r"_+", "_", normalized).strip("_")
        if not normalized or normalized in seen:
            continue
        result.append(normalized)
        seen.add(normalized)
        if len(result) == 6:
            break
    return tuple(result)


_REVIEW_CHECKS = (
    "summary_schema",
    "review_schema",
    "run_binding",
    "evidence_binding",
    "locator_membership",
    "resolved_render_chinese_prose",
)
_LOCAL_CANDIDATE_CHECKS = (
    "source_identity",
    "evidence_hashes",
    "sealed_review_hashes",
    "rendered_note_hash",
    "fixed_local_target",
)
_ZOTERO_CANDIDATE_CHECKS = (
    "source_identity",
    "evidence_hashes",
    "sealed_review_hashes",
    "parent_fingerprint",
    "live_title_availability",
    "canonical_html_binding",
)
_CANONICAL_CONTEXT_LOCATOR = re.compile(
    r"^context\.md page (?P<page>\d+)"
    r"(?: section (?P<section>[A-Za-z0-9][A-Za-z0-9 /&().,+:_-]*?)"
    r"(?: table_candidate (?P<table_candidate>\d+))?)?$"
)
_CANONICAL_FIGURE_LOCATOR = re.compile(
    r"^figure_context\.md (?P<figure_id>[A-Za-z0-9_.:-]+)$"
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_ENGLISH_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*\b")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_LATIN_SPAN_RE = re.compile(r"[^\u3400-\u9fff]+")
_LOCATOR_FRAGMENT_RE = re.compile(
    r"\b(?:context\.md page \d+|figure_context\.md [A-Za-z0-9_.:-]+|context\.md|figure_context\.md)\b"
    r"(?: table_candidate \d+)?"
)
_KNOWN_CONTEXT_SECTION_NAMES = (
    "Results and discussion",
    "Materials and methods",
    "Experimental section",
    "Computational methods",
    "Supporting information",
    "Introduction",
    "Background",
    "Methods",
    "Results",
    "Discussion",
    "Conclusions",
    "Conclusion",
    "Abstract",
)
_CONTEXT_SECTION_FRAGMENT_RE = re.compile(
    r"\bsection (?:"
    + "|".join(re.escape(name) for name in _KNOWN_CONTEXT_SECTION_NAMES)
    + r")\b",
    flags=re.IGNORECASE,
)
_ALLOWED_MIXED_ENGLISH_PHRASES = (
    "on-the-fly",
    "solid-state electrolyte",
    "all-solid-state",
    "sulfide SSE",
    "Li metal interface",
    "XPS depth profiling",
    "DC polarization",
    "post-mortem",
    "ex situ",
    "cycling 后",
)
_ENGLISH_FUNCTION_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is",
    "of", "or", "that", "the", "this", "to", "which", "with",
}
_UNIT_TOKENS = {
    "a", "c", "cm", "ev", "g", "h", "k", "kg", "mah", "mpa", "ms", "ps", "rh",
    "rpm", "s", "usd", "v", "vs",
}
_RENDERED_TEXT_FIELDS = (
    "one_sentence_summary",
    "research_object",
    "research_question_short",
    "core_method_short",
    "core_result_short",
    "main_risk_short",
    "tldr",
    "background_problem",
    "existing_gap",
    "paper_entry_point",
    "method_overview",
)
_RENDERED_TEXT_LIST_FIELDS = (
    "contributions", "technical_details", "limitations", "applicability_limits"
)


def _is_technical_token(token: str) -> bool:
    lower = token.lower()
    if lower in _UNIT_TOKENS or any(char.isdigit() for char in token):
        return True
    if re.fullmatch(r"[A-Z][a-z]?", token):
        return True
    parts = re.split(r"[-/]", token)
    if all(part.isupper() for part in parts):
        return True
    letters = _LATIN_LETTER_RE.findall(token)
    if len(letters) < 3 and lower not in _ENGLISH_FUNCTION_WORDS:
        return True
    return bool(re.fullmatch(r"(?:[A-Z][a-z]?)(?:[-/][A-Z][a-z]?)+", token))


def _strip_allowed_mixed_english_phrases(value: str) -> str:
    text = _LOCATOR_FRAGMENT_RE.sub(" ", value)
    text = _CONTEXT_SECTION_FRAGMENT_RE.sub(" ", text)
    for phrase in _ALLOWED_MIXED_ENGLISH_PHRASES:
        text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)
    return text


def _contains_allowed_mixed_english_phrase(value: str) -> bool:
    return any(
        re.search(re.escape(phrase), value, flags=re.IGNORECASE)
        for phrase in _ALLOWED_MIXED_ENGLISH_PHRASES
    )


def _english_prose_tokens(value: str) -> list[str]:
    text = _strip_allowed_mixed_english_phrases(value)
    return [
        token
        for token in _ENGLISH_TOKEN_RE.findall(text)
        if not _is_technical_token(token)
        and len(_LATIN_LETTER_RE.findall(token)) >= 2
        and any(char.islower() for char in token)
    ]


def _looks_like_english_prose(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    spans = _LATIN_SPAN_RE.findall(text) if _CJK_RE.search(text) else [text]
    for span in spans:
        tokens = _english_prose_tokens(span)
        if len(tokens) >= 3:
            return True
        if (
            len(tokens) >= 2
            and len(_LATIN_LETTER_RE.findall(_strip_allowed_mixed_english_phrases(span))) >= 10
        ):
            return True
    return _contains_allowed_mixed_english_phrase(text) and len(_english_prose_tokens(text)) == 1


def _iter_rendered_summary_text(summary: ForeignSummary):
    payload = summary.model_dump(mode="json")
    for field in _RENDERED_TEXT_FIELDS:
        yield payload.get(field)
    for field in _RENDERED_TEXT_LIST_FIELDS:
        yield from payload.get(field, [])
    yield from payload.get("workflow_steps", [])
    for module in payload.get("method_modules", []):
        for field in ("name", "input", "target", "output", "role"):
            yield module.get(field)
    for figure in payload.get("key_figures", []):
        for field in ("analysis", "why_it_matters", "why_it_matters_short"):
            yield figure.get(field)
        if not str(figure.get("analysis") or "").strip():
            yield figure.get("caption")
    for field in ("author_stated_limitations", "inferred_limits"):
        for item in payload.get(field, []):
            yield item.get("text")
            if field == "inferred_limits":
                yield item.get("basis")


def _rendered_markdown_has_english_prose(note: str) -> bool:
    for raw_line in note.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line == "---" or line.startswith("Tags:"):
            continue
        if line.startswith("|") and line.endswith("|"):
            values = [cell.strip() for cell in line.strip("|").split("|")]
            if values and all(re.fullmatch(r":?-{3,}:?", value) for value in values):
                continue
        else:
            values = [re.sub(r"^(?:[-*]|\d+\.)\s+", "", line)]
        if any(_looks_like_english_prose(value) for value in values):
            return True
    return False


def _locator_is_member(locator: str, evidence: ForeignEvidence) -> bool:
    figure = _CANONICAL_FIGURE_LOCATOR.fullmatch(locator)
    if figure is not None:
        figure_id = figure.group("figure_id")
        return any(item.figure_id == figure_id for item in evidence.figures)
    context = _CANONICAL_CONTEXT_LOCATOR.fullmatch(locator)
    if context is None:
        return False
    page = int(context.group("page"))
    if page not in evidence.pages:
        return False
    section = context.group("section")
    if section is None:
        return True
    if not any(
        item.title == section and item.start_page <= page <= item.end_page
        for item in evidence.sections
    ):
        return False
    table = context.group("table_candidate")
    return table is None or any(
        item.index == int(table) and item.page == page and item.section == section
        for item in evidence.table_candidates
    )


def _validate_summary_evidence(summary: ForeignSummary, evidence: ForeignEvidence) -> None:
    locators = [
        item.locator
        for claim in summary.evidence_summary
        for item in claim.evidence
    ]
    locators.extend(item.locator for item in summary.author_stated_limitations)
    locators.extend(item.locator for item in summary.inferred_limits)
    if any(not _locator_is_member(locator, evidence) for locator in locators):
        raise _invalid(
            "review_not_sealed",
            "sealed summary contains a noncanonical or nonmember evidence locator",
        )
    figure_ids = {item.figure_id for item in evidence.figures}
    if any(item.figure_id not in figure_ids for item in summary.key_figures):
        raise _invalid("review_not_sealed", "sealed summary cites a figure absent from evidence")


def _validate_write_ready_summary_semantics(summary: ForeignSummary) -> None:
    if summary.trust_status not in {"trusted", "usable_with_caveats"}:
        raise _invalid("review_not_sealed", "sealed summary trust status is not write-ready")
    required_text = (
        summary.trust_rationale,
        summary.one_sentence_summary,
        summary.abstract_translation,
        summary.research_question,
        summary.method,
        summary.experiments,
        summary.ai4s_relevance,
    )
    if any(not " ".join(value.split()) for value in required_text):
        raise _invalid("review_not_sealed", "sealed summary lacks required write-ready text")
    required_lists = (
        summary.key_points,
        summary.contributions,
        summary.limitations,
        summary.follow_up_keywords,
    )
    if any(not any(item.strip() for item in values) for values in required_lists):
        raise _invalid("review_not_sealed", "sealed summary lacks required write-ready list values")
    if not summary.evidence_summary:
        raise _invalid("review_not_sealed", "sealed summary must contain an evidence claim")
    for claim in summary.evidence_summary:
        if not claim.claim.strip() or not any(item.locator.strip() for item in claim.evidence):
            raise _invalid(
                "review_not_sealed",
                "each sealed summary claim must bind at least one evidence locator",
            )
    if summary.improvement_status == "needed":
        raise _invalid("review_not_sealed", "sealed summary still needs improvement")


def _require_ref_shape(
    ref: ForeignArtifactRef,
    *,
    role: str,
    path: str | None = None,
    media_type: str,
) -> None:
    if ref.role != role or ref.media_type != media_type or (path is not None and ref.path != path):
        raise _invalid("artifact_binding_mismatch", f"artifact ref shape is invalid for {role}")


class _VisibleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _visible_title(value: object) -> str:
    parser = _VisibleParser()
    parser.feed(unescape(str(value)))
    parser.close()
    return " ".join("".join(parser.parts).split())


def _parent_fingerprint(payload: object) -> tuple[str, str, str, int, str]:
    if not isinstance(payload, dict):
        raise _invalid("zotero_snapshot_invalid", "Zotero parent snapshot must be an object")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    key = str(payload.get("key") or data.get("key") or "").strip()
    title = _visible_title(data.get("title", ""))
    doi = str(data.get("DOI") or "").strip().casefold()
    version = payload.get("version", data.get("version", 0))
    if not key or not title or type(version) is not int or version < 0:
        raise _invalid("zotero_snapshot_invalid", "Zotero parent snapshot identity is invalid")
    fingerprint = canonical_sha256(
        {"key": key, "title": title.casefold(), "DOI": doi, "version": version}
    )
    return key, title, doi, version, fingerprint


def _json_no_nonfinite(raw: bytes, *, code: str) -> object:
    try:
        return json.loads(
            raw,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise _invalid(code, "foreign JSON artifact is invalid", exc)


def _raw_selected_item(value: object) -> dict[str, object]:
    raw = value
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("result"), dict)
        and isinstance(raw["result"].get("content"), list)
    ):
        raw = raw["result"]["content"]
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and raw[0].get("type") == "text":
        text = raw[0].get("text")
        if not isinstance(text, str):
            raise _invalid("source_binding_mismatch", "raw selected-item MCP text is invalid")
        try:
            raw = json.loads(
                text,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise _invalid("source_binding_mismatch", "raw selected-item MCP JSON is invalid", exc)
    if not isinstance(raw, dict):
        raise _invalid("source_binding_mismatch", "raw selected item must resolve to an object")
    return raw


def _normalized_inventory_entry(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid("source_binding_mismatch", f"{label} must be an object")
    key = str(value.get("key") or "").strip()
    title = _visible_title(value.get("title", ""))
    doi = str(value.get("DOI") or "").strip().casefold()
    version = value.get("version", 0)
    if not key or not title or type(version) is not int or version < 0:
        raise _invalid("source_binding_mismatch", f"{label} identity is invalid")
    return {
        "key": key,
        "title": title,
        "normalized_title": title.casefold(),
        "DOI": doi,
        "version": version,
    }


def _validate_zotero_source_snapshots(
    source: ForeignZoteroSource,
    raw_bytes: bytes,
    normalized_bytes: bytes,
) -> str:
    raw_payload = _json_no_nonfinite(raw_bytes, code="source_binding_mismatch")
    normalized_payload = _json_no_nonfinite(normalized_bytes, code="source_binding_mismatch")
    if canonical_json_bytes(normalized_payload) != normalized_bytes:
        raise _invalid("source_binding_mismatch", "normalized Zotero source is not canonical JSON")
    if (
        not isinstance(raw_payload, dict)
        or set(raw_payload) != {"search_results", "selected_item"}
        or not isinstance(normalized_payload, dict)
        or set(normalized_payload) != {"format", "search_inventory", "selected_item", "selected_attachment"}
        or normalized_payload.get("format") != "paper_reader.zotero-source.v2-internal"
    ):
        raise _invalid("source_binding_mismatch", "Zotero source snapshots have invalid structure")
    raw_inventory = raw_payload.get("search_results")
    if not isinstance(raw_inventory, list) or not raw_inventory:
        raise _invalid("source_binding_mismatch", "raw Zotero search inventory is empty or invalid")
    normalized_inventory = [
        _normalized_inventory_entry(item, label=f"search_results[{index}]")
        for index, item in enumerate(raw_inventory)
    ]
    keys = [str(item["key"]) for item in normalized_inventory]
    if len(set(keys)) != len(keys):
        raise _invalid("source_binding_mismatch", "raw Zotero search inventory repeats an item key")
    if normalized_payload.get("search_inventory") != normalized_inventory:
        raise _invalid(
            "source_binding_mismatch",
            "raw search_results do not normalize to normalized search_inventory",
        )
    raw_selected = _raw_selected_item(raw_payload.get("selected_item"))
    normalized_selected = normalized_payload.get("selected_item")
    normalized_attachment = normalized_payload.get("selected_attachment")
    if not isinstance(normalized_selected, dict) or not isinstance(normalized_attachment, dict):
        raise _invalid("source_binding_mismatch", "normalized Zotero selected item/attachment is invalid")
    raw_identity = _normalized_inventory_entry(raw_selected, label="raw selected_item")
    selected_identity = _normalized_inventory_entry(normalized_selected, label="normalized selected_item")
    if raw_identity != selected_identity or selected_identity not in normalized_inventory:
        raise _invalid(
            "source_binding_mismatch",
            "selected item identity is not the exact raw/normalized inventory member",
        )
    if sum(
        item["normalized_title"] == selected_identity["normalized_title"]
        for item in normalized_inventory
    ) != 1:
        raise _invalid("source_binding_mismatch", "selected normalized title is not unique in inventory")
    expected_identity = {
        "key": source.item_key,
        "title": source.title,
        "normalized_title": source.title.casefold(),
        "DOI": source.doi,
        "version": source.parent_version,
    }
    if selected_identity != expected_identity:
        raise _invalid("source_binding_mismatch", "normalized selected item differs from run source")
    selected_attachments = normalized_selected.get("attachments")
    if not isinstance(selected_attachments, list):
        raise _invalid("source_binding_mismatch", "normalized selected item attachments are invalid")
    matching_attachments = [
        item
        for item in selected_attachments
        if isinstance(item, dict) and str(item.get("key") or "") == source.attachment_key
    ]
    if (
        len(matching_attachments) != 1
        or matching_attachments[0] != normalized_attachment
        or str(normalized_attachment.get("key") or "") != source.attachment_key
        or str(normalized_attachment.get("path") or "") != source.attachment.resolved_path
    ):
        raise _invalid("source_binding_mismatch", "normalized selected attachment differs from run source")
    raw_attachments = raw_selected.get("attachments")
    raw_matches = [
        item
        for item in raw_attachments
        if isinstance(item, dict) and str(item.get("key") or "") == source.attachment_key
    ] if isinstance(raw_attachments, list) else []
    if len(raw_matches) != 1 or str(raw_matches[0].get("path") or "") != source.attachment.requested_path:
        raise _invalid("source_binding_mismatch", "raw selected attachment differs from run source")
    return sha256_bytes(canonical_json_bytes(raw_inventory))


def _invalid(code: str, message: str, exc: Exception | None = None) -> BatchRuntimeError:
    error = BatchRuntimeError(code, message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _require_normalized_absolute_path(value: str, *, code: str, label: str) -> Path:
    normalized = normalized_absolute_path(Path(value))
    if str(normalized) != value:
        raise _invalid(code, f"{label} must be one normalized absolute path")
    return normalized


def _relative_path(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or value != candidate.as_posix() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise _invalid("artifact_path_invalid", f"foreign artifact path is not canonical relative: {value}")
    return candidate


def _read_model(path: Path, model_type, *, code: str):
    raw = read_bytes(path, code=code)
    try:
        json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        model = model_type.model_validate_json(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        raise _invalid(code, f"strict foreign artifact validation failed: {path}", exc)
    if raw != canonical_json_bytes(model):
        raise _invalid(code, f"foreign artifact is not canonical JSON: {path}")
    return raw, model


def _read_envelope(
    ref: ArtifactRef,
    model_type,
    *,
    basename: str,
    schema: str,
    id_field: str,
    bind_bytes: bool = True,
):
    path = normalized_absolute_path(Path(ref.path))
    if str(path) != ref.path or path.name != basename:
        raise _invalid("artifact_path_invalid", f"artifact envelope path is not the required {basename}: {ref.path}")
    raw, model = _read_model(path, model_type, code="artifact_invalid")
    payload = model.model_dump(mode="json")
    if ref.schema_version != schema:
        raise _invalid("artifact_binding_mismatch", f"artifact envelope declares the wrong schema: {path}")
    if (
        (bind_bytes and (len(raw) != ref.size_bytes or sha256_bytes(raw) != ref.sha256))
        or payload.get("schema_version", payload.get("format")) != schema
        or payload.get(id_field) != ref.artifact_id
    ):
        raise _invalid("artifact_binding_mismatch", f"artifact envelope does not match bytes/identity: {path}")
    return path, raw, model


def _read_inner(run_dir: Path, ref: ForeignArtifactRef, *, model_type=None, code: str = "artifact_invalid"):
    relative = _relative_path(ref.path)
    path = run_dir.joinpath(*relative.parts)
    raw = read_bytes(path, code=code)
    if len(raw) != ref.size_bytes or sha256_bytes(raw) != ref.sha256:
        raise _invalid("artifact_binding_mismatch", f"foreign artifact reference hash/size mismatch: {ref.path}")
    if model_type is None:
        return path, raw, None
    try:
        json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        model = model_type.model_validate_json(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        raise _invalid(code, f"strict nested artifact validation failed: {ref.path}", exc)
    if raw != canonical_json_bytes(model):
        raise _invalid(code, f"nested artifact is not canonical JSON: {ref.path}")
    return path, raw, model


def _run_ref_for(
    run: ForeignRun,
    run_dir: Path,
    absolute_path: Path,
    envelope: ArtifactRef,
    role: str,
) -> ForeignArtifactRef:
    try:
        expected_relative = absolute_path.relative_to(run_dir).as_posix()
    except ValueError as exc:
        raise _invalid("artifact_not_bound", f"paper_reader run {role} path escapes run", exc)
    matches = [
        ref
        for ref in run.artifacts
        if ref.role == role
        and ref.path == expected_relative
        and ref.sha256 == envelope.sha256
        and ref.size_bytes == envelope.size_bytes
        and ref.media_type == "application/json"
    ]
    if len(matches) != 1:
        raise _invalid("artifact_not_bound", f"paper_reader run must bind exactly one {role} digest")
    expected = run_dir / _relative_path(matches[0].path)
    if expected != absolute_path:
        raise _invalid("artifact_not_bound", f"paper_reader run {role} path does not bind supplied artifact")
    return matches[0]


def _source_matches(manifest_item, source: ForeignSource, *, refingerprint: bool = True) -> str | None:
    expected = manifest_item.source
    if isinstance(manifest_item, PdfManifestItem):
        if not isinstance(source, ForeignLocalSource):
            raise _invalid("source_binding_mismatch", "PDF item requires a local paper_reader source")
        _require_normalized_absolute_path(
            source.resolved_path,
            code="source_binding_mismatch",
            label="paper_reader local source",
        )
        if (
            source.size_bytes <= 0
            or source.resolved_path != expected.path
            or source.sha256 != expected.sha256
            or source.size_bytes != expected.size_bytes
            or source.device != expected.file_identity.device
            or source.inode != expected.file_identity.inode
        ):
            raise _invalid("source_binding_mismatch", "paper_reader local source differs from manifest")
        if refingerprint:
            current = _pdf_source(Path(expected.path))
            if current != expected:
                raise _invalid("source_drift", f"PDF source changed before finish: {expected.path}")
        return None
    if not isinstance(source, ForeignZoteroSource):
        raise _invalid("source_binding_mismatch", "Zotero item requires a Zotero paper_reader source")
    _require_normalized_absolute_path(
        source.attachment.resolved_path,
        code="source_binding_mismatch",
        label="paper_reader Zotero attachment",
    )
    if source.attachment.size_bytes <= 0:
        raise _invalid("source_binding_mismatch", "paper_reader Zotero attachment must be non-empty")
    if isinstance(manifest_item, ZoteroItemManifestItem):
        if source.item_key != expected.item_key or source.title != expected.title:
            raise _invalid("source_binding_mismatch", "resolved Zotero source differs from manifest")
    elif isinstance(manifest_item, ZoteroTitleManifestItem):
        if expected.resolved_item_key is not None and source.item_key != expected.resolved_item_key:
            raise _invalid("source_binding_mismatch", "resolved Zotero key differs from manifest")
        if " ".join(expected.title.split()).casefold() not in " ".join(source.title.split()).casefold():
            raise _invalid("source_binding_mismatch", "resolved Zotero title does not match title query")
    if refingerprint:
        attachment = _pdf_source(Path(source.attachment.resolved_path))
        if (
            attachment.sha256 != source.attachment.sha256
            or attachment.size_bytes != source.attachment.size_bytes
            or attachment.file_identity.device != source.attachment.device
            or attachment.file_identity.inode != source.attachment.inode
        ):
            raise _invalid("source_drift", "Zotero attachment changed before finish")
    return source.item_key


def _validate_local_run_target(
    manifest_item: PdfManifestItem,
    run_path: Path,
    target: ForeignLocalTarget,
    *,
    check_parent_device: bool,
) -> None:
    source_path = Path(manifest_item.source.path)
    _require_normalized_absolute_path(
        target.resolved_path,
        code="local_prepare_invalid",
        label="paper_reader local target",
    )
    prefix = f"{source_path.stem}_analysis"
    run_name = run_path.parent.name
    if run_name == prefix:
        suffix = ""
    elif run_name.startswith(prefix + "_v") and run_name[len(prefix) + 2 :].isdigit():
        suffix = run_name[len(prefix) :]
    else:
        raise _invalid("local_prepare_invalid", "paper_reader local run directory does not match source/version")
    expected_target = source_path.parent / f"{source_path.stem}_note{suffix}.md"
    if target.resolved_path != str(expected_target):
        raise _invalid("local_prepare_invalid", "paper_reader local target does not match run/source version")
    if check_parent_device:
        try:
            parent_device = source_path.parent.stat().st_dev
        except OSError as exc:
            raise _invalid("source_drift", "local source parent is unavailable", exc)
        if target.parent_device != parent_device:
            raise _invalid("local_prepare_invalid", "paper_reader local target parent device changed")


def _walk_regular_files(root: Path) -> set[str]:
    found: set[str] = set()

    def walk(directory: Path, prefix: PurePosixPath) -> None:
        with open_directory_fd(directory, create=False) as (descriptor, _normalized):
            for name in sorted(os.listdir(descriptor)):
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                relative = prefix / name
                if stat.S_ISLNK(metadata.st_mode):
                    raise _invalid("artifact_closed_world_mismatch", f"foreign bundle contains symlink: {relative}")
                if stat.S_ISDIR(metadata.st_mode):
                    walk(directory / name, relative)
                elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    found.add(relative.as_posix())
                else:
                    raise _invalid("artifact_closed_world_mismatch", f"foreign bundle entry is unsafe: {relative}")

    walk(root, PurePosixPath())
    return found


def _validate_source_directory(run_dir: Path, source: ForeignSource) -> None:
    expected = (
        {"source.json"}
        if isinstance(source, ForeignLocalSource)
        else {"discovery.raw.json", "source.json"}
    )
    if _walk_regular_files(run_dir / "source") != expected:
        raise _invalid(
            "artifact_closed_world_mismatch",
            "paper_reader source directory is not its exact immutable closure",
        )


def _validate_evidence(run_dir: Path, evidence_path: Path, evidence: ForeignEvidence, source_sha256: str) -> None:
    if not evidence.complete or evidence.preview_pages is not None:
        raise _invalid("evidence_incomplete", "batch accepts only complete, non-preview evidence")
    if any(check.status == "blocked" for check in evidence.resource_checks):
        raise _invalid("evidence_incomplete", "complete evidence cannot contain blocked resource checks")
    if evidence.source_sha256 != source_sha256:
        raise _invalid("evidence_binding_mismatch", "evidence source digest differs from paper_reader source")
    roles = [ref.role for ref in evidence.files]
    for role in {"metadata", "extract", "context", "section_context", "secondary_sources"}:
        if roles.count(role) != 1:
            raise _invalid("evidence_binding_mismatch", f"evidence requires exactly one {role} artifact")
    pages = set(evidence.pages)
    if len(pages) != len(evidence.pages) or any(page <= 0 for page in pages):
        raise _invalid("evidence_binding_mismatch", "evidence pages must be unique positive integers")
    section_keys: set[tuple[str, int, int]] = set()
    for section in evidence.sections:
        key = (section.title, section.start_page, section.end_page)
        if (
            key in section_keys
            or section.start_page > section.end_page
            or section.start_page not in pages
            or section.end_page not in pages
        ):
            raise _invalid("evidence_binding_mismatch", "evidence section membership is invalid")
        section_keys.add(key)
    table_indices: set[int] = set()
    for table in evidence.table_candidates:
        if (
            table.index <= 0
            or table.index in table_indices
            or table.page not in pages
            or not any(
                section.title == table.section and section.start_page <= table.page <= section.end_page
                for section in evidence.sections
            )
        ):
            raise _invalid("evidence_binding_mismatch", "evidence table membership is invalid")
        table_indices.add(table.index)
    figure_paths = {ref.path for ref in evidence.files if ref.role == "figure_image"}
    figure_ids: set[str] = set()
    for figure in evidence.figures:
        if (
            figure.figure_id in figure_ids
            or figure.page not in pages
            or figure.artifact_path not in figure_paths
        ):
            raise _invalid("evidence_binding_mismatch", "evidence figure membership is invalid")
        figure_ids.add(figure.figure_id)
    expected_members: set[str] = set()
    evidence_dir = evidence_path.parent
    for ref in evidence.files:
        expected_media = {
            "metadata": "application/json",
            "extract": "application/json",
            "context": "text/markdown",
            "section_context": "text/markdown",
            "secondary_sources": "application/json",
            "figure_context": "text/markdown",
        }.get(ref.role)
        if expected_media is not None and ref.media_type != expected_media:
            raise _invalid("evidence_binding_mismatch", f"evidence {ref.role} media type is invalid")
        if ref.role == "figure_image" and not str(ref.media_type or "").startswith("image/"):
            raise _invalid("evidence_binding_mismatch", "evidence figure image media type is invalid")
        member_path, _raw, _model = _read_inner(run_dir, ref)
        try:
            relative = member_path.relative_to(evidence_dir).as_posix()
        except ValueError as exc:
            raise _invalid("evidence_binding_mismatch", "evidence member is outside its immutable bundle", exc)
        if relative == "evidence.json" or relative in expected_members:
            raise _invalid("evidence_binding_mismatch", "evidence manifest has duplicate/recursive member")
        expected_members.add(relative)
    actual = _walk_regular_files(evidence_dir)
    actual.discard("evidence.json")
    if actual != expected_members:
        raise _invalid("artifact_closed_world_mismatch", "evidence bundle membership differs from manifest")


def _validate_review_and_candidate(
    manifest_item,
    run_path: Path,
    run: ForeignRun,
    review_ref: ArtifactRef,
    candidate_ref: ArtifactRef,
    refingerprint: bool = True,
):
    run_dir = run_path.parent
    review_path, _review_raw, package = _read_envelope(
        review_ref,
        ForeignReviewPackage,
        basename="review-package.json",
        schema="paper_reader.review-package.v2",
        id_field="review_package_id",
    )
    candidate_path, _candidate_raw, candidate = _read_envelope(
        candidate_ref,
        ForeignCandidate,
        basename="candidate.json",
        schema="paper_reader.candidate.v2",
        id_field="candidate_id",
    )
    expected_review_path = run_dir / "reviews" / package.review_package_id / "review-package.json"
    expected_candidate_path = run_dir / "candidates" / candidate.candidate_id / "candidate.json"
    if review_path != expected_review_path or candidate_path != expected_candidate_path:
        raise _invalid("artifact_path_invalid", "review/candidate must live in the bound run id directory")
    review_names = {
        "summary.json": ("summary_snapshot", "application/json"),
        "review.json": ("review_snapshot", "application/json"),
        "evidence.json": ("evidence_manifest_snapshot", "application/json"),
        "validation.json": ("review_validation", "application/json"),
        "note.md": ("review_note_markdown", "text/markdown"),
        "note.html": ("review_note_html", "text/html"),
    }
    if _walk_regular_files(review_path.parent) != {*review_names, "review-package.json"}:
        raise _invalid("artifact_closed_world_mismatch", "sealed review directory is not the fixed seven-file closure")
    review_snapshots: dict[str, bytes] = {}
    if len(package.artifacts) != len(review_names):
        raise _invalid("review_not_sealed", "review package artifact count is not the fixed closure")
    for name, (role, media_type) in review_names.items():
        matches = [ref for ref in package.artifacts if ref.role == role]
        if len(matches) != 1:
            raise _invalid("review_not_sealed", f"review package must bind one {role}")
        _require_ref_shape(
            matches[0],
            role=role,
            path=(review_path.parent / name).relative_to(run_dir).as_posix(),
            media_type=media_type,
        )
        path, raw, _model = _read_inner(run_dir, matches[0])
        if path != review_path.parent / name:
            raise _invalid("review_not_sealed", f"review package {role} path is not fixed")
        review_snapshots[name] = raw
    by_role = {ref.role: ref for ref in package.artifacts}
    if (
        package.summary != by_role["summary_snapshot"]
        or package.review != by_role["review_snapshot"]
        or package.evidence_manifest != by_role["evidence_manifest_snapshot"]
    ):
        raise _invalid("review_not_sealed", "review package primary refs differ from closure refs")
    if package.run_id != run.run_id or candidate.run_id != run.run_id:
        raise _invalid("artifact_binding_mismatch", "review/candidate run id differs from paper_reader run")
    # The mutable root manifest may advance status/target/gate after a committed
    # batch result, but it must retain the exact immutable artifacts it owns.
    _run_ref_for(run, run_dir, review_path, review_ref, "review_package")
    _run_ref_for(run, run_dir, candidate_path, candidate_ref, "candidate")
    if (
        package.gate.status != "passed"
        or package.gate.blockers
        or package.gate.checks != _REVIEW_CHECKS
    ):
        raise _invalid("review_not_sealed", "review package must pass sealed Chinese-first validation")
    expected_candidate_checks = (
        _LOCAL_CANDIDATE_CHECKS
        if isinstance(candidate.target, ForeignLocalTarget)
        else _ZOTERO_CANDIDATE_CHECKS
    )
    if (
        candidate.gate.status != "write_ready"
        or candidate.gate.blockers
        or candidate.gate.checks != expected_candidate_checks
    ):
        raise _invalid("candidate_not_write_ready", "candidate gate must be write_ready with no blockers")
    for required_ref, label in [
        (package.summary, "summary"),
        (package.review, "review"),
        (package.evidence_manifest, "evidence_manifest"),
    ]:
        if required_ref not in package.artifacts:
            raise _invalid("review_not_sealed", f"review package does not bind its {label} artifact")
    for required_ref, label in [
        (candidate.sealed_review, "sealed_review"),
        (candidate.evidence_manifest, "evidence_manifest"),
    ]:
        if required_ref not in candidate.artifacts:
            raise _invalid("candidate_binding_mismatch", f"candidate does not bind its {label} artifact")
    try:
        summary = ForeignSummary.model_validate_json(review_snapshots["summary.json"])
        review_json = ForeignReview.model_validate_json(review_snapshots["review.json"])
        validation = ForeignReviewValidation.model_validate_json(review_snapshots["validation.json"])
    except ValidationError as exc:
        raise _invalid("review_invalid", "sealed summary/review/validation fails strict validation", exc)
    review_json_raw = review_snapshots["review.json"]
    summary_raw = review_snapshots["summary.json"]
    note_md = review_snapshots["note.md"]
    note_html = review_snapshots["note.html"]
    if (
        summary_raw != canonical_json_bytes(summary)
        or review_json_raw != canonical_json_bytes(review_json)
        or review_snapshots["validation.json"] != canonical_json_bytes(validation)
    ):
        raise _invalid("review_not_sealed", "sealed summary/review/validation must be canonical JSON")
    _validate_write_ready_summary_semantics(summary)
    if (
        summary.run_id != run.run_id
        or summary.evidence_digest != package.evidence_digest
        or summary.review_status not in {"passed", "passed_with_caveats"}
        or summary.review_status != review_json.review_status
        or summary.improvement_status == "needed"
        or review_json.run_id != run.run_id
        or review_json.review_status == "failed"
        or review_json.needs_improvement
        or review_json.summary_sha256 != package.summary_sha256
        or review_json.evidence_digest != package.evidence_digest
        or sha256_bytes(summary_raw) != package.summary_sha256
        or sha256_bytes(review_json_raw) != package.review_sha256
    ):
        raise _invalid("review_not_sealed", "sealed review content is failed, improvable, or hash-mismatched")
    if (
        validation.run_id != run.run_id
        or validation.summary_sha256 != package.summary_sha256
        or validation.review_sha256 != package.review_sha256
        or validation.evidence_digest != package.evidence_digest
        or validation.rendered_note_sha256 != sha256_bytes(note_md)
        or validation.rendered_html_sha256 != sha256_bytes(note_html)
        or validation.blockers
        or validation.checks != _REVIEW_CHECKS
        or package.gate.checks != _REVIEW_CHECKS
    ):
        raise _invalid("review_not_sealed", "sealed validation hashes/checks/blockers are inconsistent")
    try:
        rendered_text = note_md.decode("utf-8")
        note_html.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid("review_not_sealed", "sealed rendered notes must be UTF-8", exc)
    if note_html != _render_note_html_like_single(rendered_text):
        raise _invalid(
            "review_not_sealed",
            "sealed review HTML is not the canonical rendering of sealed Markdown",
        )
    if (
        not _CJK_RE.search(rendered_text)
        or any(_looks_like_english_prose(value) for value in _iter_rendered_summary_text(summary))
        or _rendered_markdown_has_english_prose(rendered_text)
    ):
        raise _invalid("review_not_sealed", "sealed rendered note does not satisfy Chinese-first prose")
    evidence_snapshot_raw = review_snapshots["evidence.json"]
    try:
        evidence = ForeignEvidence.model_validate_json(evidence_snapshot_raw)
    except ValidationError as exc:
        raise _invalid("evidence_invalid", "sealed evidence snapshot fails strict validation", exc)
    source_sha = run.source.sha256 if isinstance(run.source, ForeignLocalSource) else run.source.attachment.sha256
    if evidence.run_id != run.run_id or package.evidence_digest != package.evidence_manifest.sha256:
        raise _invalid("evidence_binding_mismatch", "sealed review evidence identity mismatch")
    _validate_summary_evidence(summary, evidence)
    candidate_names = {
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
    if isinstance(candidate.target, ForeignZoteroTarget):
        candidate_names.update(
            {
                "discovery.raw.json": ("raw_discovery_bundle_snapshot", "application/json"),
                "parent.json": ("zotero_parent_snapshot", "application/json"),
                "children.json": ("zotero_children_snapshot", "application/json"),
            }
        )
    if _walk_regular_files(candidate_path.parent) != {*candidate_names, "candidate.json"}:
        raise _invalid("artifact_closed_world_mismatch", "candidate directory is not its fixed immutable closure")
    if len(candidate.artifacts) != len(candidate_names):
        raise _invalid("candidate_binding_mismatch", "candidate artifact count is not the fixed closure")
    candidate_snapshots: dict[str, bytes] = {}
    candidate_refs: dict[str, ForeignArtifactRef] = {}
    for name, (role, media_type) in candidate_names.items():
        matches = [ref for ref in candidate.artifacts if ref.role == role]
        if len(matches) != 1:
            raise _invalid("candidate_binding_mismatch", f"candidate must bind one {role}")
        _require_ref_shape(
            matches[0],
            role=role,
            path=(candidate_path.parent / name).relative_to(run_dir).as_posix(),
            media_type=media_type,
        )
        path, raw, _model = _read_inner(run_dir, matches[0])
        if path != candidate_path.parent / name:
            raise _invalid("candidate_binding_mismatch", f"candidate {role} path is not fixed")
        candidate_snapshots[name] = raw
        candidate_refs[role] = matches[0]
    if (
        candidate.sealed_review != candidate_refs["review_package_snapshot"]
        or candidate.evidence_manifest != candidate_refs["evidence_manifest_snapshot"]
        or candidate_snapshots["review-package.json"] != _review_raw
        or candidate_snapshots["evidence.json"] != evidence_snapshot_raw
        or candidate_snapshots["summary.json"] != review_snapshots["summary.json"]
        or candidate_snapshots["review.json"] != review_snapshots["review.json"]
        or candidate_snapshots["validation.json"] != review_snapshots["validation.json"]
    ):
        raise _invalid("candidate_binding_mismatch", "candidate snapshots differ from sealed review package")
    try:
        run_snapshot = ForeignRun.model_validate_json(candidate_snapshots["run.json"])
        source_snapshot = (
            ForeignLocalSource.model_validate_json(candidate_snapshots["source.json"])
            if isinstance(candidate.source, ForeignLocalSource)
            else None
        )
    except ValidationError as exc:
        raise _invalid("candidate_binding_mismatch", "candidate run/source snapshot is invalid", exc)
    if (
        candidate_snapshots["run.json"] != canonical_json_bytes(run_snapshot)
        or run_snapshot.run_id != run.run_id
        or run_snapshot.source != candidate.source
        or run_snapshot.status != "reviewed"
        or run_snapshot.gate != package.gate
        or run_snapshot.live_preflight is not None
        or (
            isinstance(candidate.source, ForeignLocalSource)
            and (
                source_snapshot != candidate.source
                or candidate_snapshots["source.json"] != canonical_json_bytes(source_snapshot)
                or run_snapshot.target != candidate.target
            )
        )
        or (
            isinstance(candidate.source, ForeignZoteroSource)
            and run_snapshot.target is not None
        )
    ):
        raise _invalid("candidate_binding_mismatch", "candidate run/source snapshots do not bind source")
    _run_ref_for(run_snapshot, run_dir, review_path, review_ref, "review_package")
    evidence_refs = [
        ref
        for ref in run_snapshot.artifacts
        if ref.role == "evidence_manifest"
        and ref.sha256 == package.evidence_digest
        and ref.size_bytes == len(evidence_snapshot_raw)
        and ref.media_type == "application/json"
    ]
    if len(evidence_refs) != 1:
        raise _invalid("evidence_binding_mismatch", "candidate run snapshot must bind canonical evidence")
    evidence_path, evidence_raw, canonical_evidence = _read_inner(
        run_dir,
        evidence_refs[0],
        model_type=ForeignEvidence,
        code="evidence_invalid",
    )
    if evidence_raw != evidence_snapshot_raw or canonical_evidence != evidence:
        raise _invalid("evidence_binding_mismatch", "sealed evidence snapshot differs from canonical evidence")
    if [ref for ref in run.artifacts if ref == evidence_refs[0]] != [evidence_refs[0]]:
        raise _invalid(
            "evidence_binding_mismatch",
            "current paper_reader run must retain the exact immutable evidence ref",
        )
    _validate_evidence(run_dir, evidence_path, evidence, source_sha)
    if run.source != candidate.source:
        raise _invalid("candidate_binding_mismatch", "final paper_reader run source/target differs from candidate")
    if refingerprint:
        if (
            run.target != candidate.target
            or run.gate != candidate.gate
            or run.live_preflight != candidate.live_preflight
        ):
            raise _invalid("candidate_binding_mismatch", "current paper_reader run differs from candidate")
    if candidate.tags != _expected_tags(summary.note_labels):
        raise _invalid("candidate_binding_mismatch", "candidate tags differ from sealed summary labels")
    if (
        candidate.sealed_review.sha256 != review_ref.sha256
        or candidate.evidence_manifest.sha256 != package.evidence_manifest.sha256
    ):
        raise _invalid("candidate_binding_mismatch", "candidate does not bind supplied review/evidence")
    note_md_bytes = candidate_snapshots["note.md"]
    note_html_bytes = candidate_snapshots["note.html"]
    try:
        note_md_text = note_md_bytes.decode("utf-8")
        note_html_text = note_html_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid("candidate_binding_mismatch", "candidate note snapshots must be UTF-8", exc)
    if not _CJK_RE.search(note_md_text) or _rendered_markdown_has_english_prose(note_md_text):
        raise _invalid("candidate_binding_mismatch", "candidate Markdown violates Chinese-first prose")
    markdown_title = note_md_text.splitlines()[0].removeprefix("# ").strip() if note_md_text.splitlines() else ""
    if isinstance(candidate.target, ForeignLocalTarget):
        original_source_refs = [
            ref
            for ref in run_snapshot.artifacts
            if ref.role == "source_snapshot"
            and ref.path == "source/source.json"
            and ref.sha256 == sha256_bytes(candidate_snapshots["source.json"])
            and ref.size_bytes == len(candidate_snapshots["source.json"])
            and ref.media_type == "application/json"
        ]
        if len(original_source_refs) != 1:
            raise _invalid("source_binding_mismatch", "local run snapshot must bind exact source snapshot")
        if [ref for ref in run.artifacts if ref == original_source_refs[0]] != [
            original_source_refs[0]
        ]:
            raise _invalid(
                "source_binding_mismatch",
                "current local run must retain the exact immutable source snapshot ref",
            )
        _source_path, original_source_raw, original_source = _read_inner(
            run_dir,
            original_source_refs[0],
            model_type=ForeignLocalSource,
            code="source_binding_mismatch",
        )
        if original_source_raw != candidate_snapshots["source.json"] or original_source != candidate.source:
            raise _invalid("source_binding_mismatch", "original local source snapshot differs from candidate")
        if (
            sha256_bytes(note_md_bytes) != candidate.content_sha256
            or len(note_md_bytes) != candidate.content_length
            or markdown_title != candidate.note_title
            or note_md_bytes != review_snapshots["note.md"]
            or note_html_bytes != review_snapshots["note.html"]
        ):
            raise _invalid("candidate_binding_mismatch", "local candidate note Markdown binding is invalid")
    else:
        canonical_html = note_html_text.rstrip("\r\n")
        expected_note_md, expected_note_html = _expected_zotero_candidate_notes(
            review_snapshots["note.md"],
            candidate.note_title,
        )
        if (
            sha256_bytes(canonical_html.encode("utf-8")) != candidate.content_sha256
            or len(canonical_html) != candidate.content_length
            or note_md_bytes != expected_note_md
            or note_html_bytes != expected_note_html
        ):
            raise _invalid(
                "candidate_binding_mismatch",
                "Zotero candidate notes may differ from sealed review only by the exact H1 title rewrite",
            )
    resolved_key = _source_matches(manifest_item, candidate.source, refingerprint=refingerprint)
    if isinstance(manifest_item, PdfManifestItem):
        if not isinstance(candidate.target, ForeignLocalTarget):
            raise _invalid("candidate_binding_mismatch", "local PDF candidate must target a local publication")
        _validate_local_run_target(
            manifest_item,
            run_path,
            candidate.target,
            check_parent_device=refingerprint,
        )
    else:
        if not isinstance(candidate.target, ForeignZoteroTarget) or candidate.target.parent_key != resolved_key:
            raise _invalid("candidate_binding_mismatch", "Zotero candidate parent differs from resolved source")
        if not isinstance(candidate.source, ForeignZoteroSource) or candidate.live_preflight is None:
            raise _invalid("candidate_binding_mismatch", "Zotero candidate requires source and live preflight")
        live = candidate.live_preflight
        if (
            (refingerprint and run.live_preflight != live)
            or live.parent_key != resolved_key
            or live.parent_fingerprint != candidate.source.parent_fingerprint
            or candidate.target.parent_fingerprint != candidate.source.parent_fingerprint
            or live.requested_note_title != candidate.note_title
            or candidate.target.note_title != candidate.note_title
            or not live.title_available
            or live.matching_note_keys
            or live.parent_snapshot != candidate_refs["zotero_parent_snapshot"]
            or live.children_snapshot != candidate_refs["zotero_children_snapshot"]
        ):
            raise _invalid("candidate_binding_mismatch", "Zotero candidate live parent/title binding is invalid")
        try:
            parent_payload = json.loads(candidate_snapshots["parent.json"])
            children_payload = json.loads(candidate_snapshots["children.json"])
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _invalid("zotero_snapshot_invalid", "Zotero parent/children snapshot is invalid JSON", exc)
        if (
            canonical_json_bytes(parent_payload) != candidate_snapshots["parent.json"]
            or canonical_json_bytes(children_payload) != candidate_snapshots["children.json"]
            or not isinstance(children_payload, list)
        ):
            raise _invalid("zotero_snapshot_invalid", "Zotero parent/children snapshots are not canonical")
        parent_key, parent_title, parent_doi, parent_version, parent_digest = _parent_fingerprint(parent_payload)
        if (
            parent_key != candidate.source.item_key
            or parent_title != candidate.source.title
            or parent_doi != candidate.source.doi
            or parent_version != candidate.source.parent_version
            or parent_digest != candidate.source.parent_fingerprint
        ):
            raise _invalid("zotero_snapshot_invalid", "Zotero parent snapshot differs from source identity")
        exact_matches: list[str] = []
        for child in children_payload:
            if not isinstance(child, dict):
                raise _invalid("zotero_snapshot_invalid", "Zotero children snapshot has a non-object entry")
            child_data = child.get("data")
            if not isinstance(child_data, dict) or child_data.get("itemType") != "note":
                continue
            child_key = str(child.get("key") or child_data.get("key") or "").strip()
            if not child_key:
                raise _invalid("zotero_snapshot_invalid", "Zotero note child is missing its key")
            if str(child_data.get("parentItem") or "").strip() != resolved_key:
                raise _invalid("zotero_snapshot_invalid", "Zotero child belongs to another parent")
            if _html_title(str(child_data.get("note") or "")) == candidate.note_title:
                exact_matches.append(child_key)
        if exact_matches or tuple(exact_matches) != live.matching_note_keys:
            raise _invalid("candidate_binding_mismatch", "candidate title is no longer available in children snapshot")
        source_raw_ref = candidate.source.raw_discovery_bundle
        source_normalized_ref = candidate.source.normalized_source
        _require_ref_shape(
            source_raw_ref,
            role="raw_discovery_bundle",
            path="source/discovery.raw.json",
            media_type="application/json",
        )
        _require_ref_shape(
            source_normalized_ref,
            role="normalized_source",
            path="source/source.json",
            media_type="application/json",
        )
        if (
            [ref for ref in run_snapshot.artifacts if ref == source_raw_ref] != [source_raw_ref]
            or [ref for ref in run_snapshot.artifacts if ref == source_normalized_ref]
            != [source_normalized_ref]
            or [ref for ref in run.artifacts if ref == source_raw_ref] != [source_raw_ref]
            or [ref for ref in run.artifacts if ref == source_normalized_ref]
            != [source_normalized_ref]
            or candidate_snapshots["discovery.raw.json"] != _read_inner(
                run_dir, candidate_refs["raw_discovery_bundle_snapshot"]
            )[1]
        ):
            raise _invalid("source_binding_mismatch", "Zotero run/candidate source closure is invalid")
        raw_bytes = candidate_snapshots["discovery.raw.json"]
        normalized_bytes = candidate_snapshots["source.json"]
        if (
            sha256_bytes(raw_bytes) != source_raw_ref.sha256
            or len(raw_bytes) != source_raw_ref.size_bytes
            or sha256_bytes(normalized_bytes) != source_normalized_ref.sha256
            or len(normalized_bytes) != source_normalized_ref.size_bytes
        ):
            raise _invalid(
                "source_binding_mismatch",
                "candidate Zotero source snapshots do not match immutable source refs",
            )
        raw_path, original_raw, _raw_model = _read_inner(run_dir, source_raw_ref)
        normalized_path, original_normalized, _normalized_model = _read_inner(
            run_dir, source_normalized_ref
        )
        if (
            raw_path != run_dir / "source" / "discovery.raw.json"
            or normalized_path != run_dir / "source" / "source.json"
            or original_raw != raw_bytes
            or original_normalized != normalized_bytes
        ):
            raise _invalid("source_binding_mismatch", "original Zotero source differs from candidate")
        inventory_sha256 = _validate_zotero_source_snapshots(
            candidate.source,
            raw_bytes,
            normalized_bytes,
        )
        if (
            _html_title(note_html_text) != candidate.note_title
            or candidate.source.attachment.sha256 != source_sha
        ):
            raise _invalid("candidate_binding_mismatch", "Zotero rendered title/source attachment binding is invalid")
        return review_path, package, candidate_path, candidate, resolved_key, inventory_sha256
    return review_path, package, candidate_path, candidate, resolved_key, None


def validate_worker_result_artifacts(
    manifest: BatchManifest,
    result: WorkerResult,
    *,
    allow_mutable_run: bool = False,
) -> str | None:
    manifest_item = next((item for item in manifest.items if item.item_id == result.item_id), None)
    if manifest_item is None:
        raise _invalid("unknown_item", f"worker result references unknown item: {result.item_id}")
    if result.source.source_type != manifest_item.source.source_type:
        raise _invalid("source_binding_mismatch", "worker result source type differs from manifest")
    if isinstance(manifest_item, PdfManifestItem):
        if result.source != manifest_item.source:
            raise _invalid("source_binding_mismatch", "worker result PDF identity differs from manifest")
        if not allow_mutable_run:
            current = _pdf_source(Path(manifest_item.source.path))
            if current != manifest_item.source:
                raise _invalid("source_drift", "worker result PDF changed before finish")
    elif isinstance(manifest_item, ZoteroItemManifestItem):
        if result.source != manifest_item.source:
            raise _invalid("source_binding_mismatch", "worker result Zotero identity differs from manifest")
    if result.status != "succeeded":
        if result.source != manifest_item.source:
            raise _invalid("source_binding_mismatch", "failed worker result source differs from manifest")
        return None
    if isinstance(manifest_item, ZoteroTitleManifestItem) and (
        result.source.title != manifest_item.source.title
        or result.source.resolved_item_key is None
        or (
            manifest_item.source.resolved_item_key is not None
            and result.source.resolved_item_key != manifest_item.source.resolved_item_key
        )
    ):
        raise _invalid("source_binding_mismatch", "worker result title resolution differs from manifest")
    assert result.paper_reader_run and result.review_package and result.candidate
    run_path, _run_raw, run = _read_envelope(
        result.paper_reader_run,
        ForeignRun,
        basename="run.json",
        schema="paper_reader.run.v2",
        id_field="run_id",
        bind_bytes=not allow_mutable_run,
    )
    _validate_source_directory(run_path.parent, run.source)
    resolved_key = _source_matches(manifest_item, run.source, refingerprint=not allow_mutable_run)
    (
        _review_path,
        _package,
        candidate_path,
        candidate,
        candidate_key,
        candidate_inventory_sha256,
    ) = _validate_review_and_candidate(
        manifest_item,
        run_path,
        run,
        result.review_package,
        result.candidate,
        refingerprint=not allow_mutable_run,
    )
    if candidate_key != resolved_key:
        raise _invalid("candidate_binding_mismatch", "candidate and run resolve different sources")
    if isinstance(manifest_item, ZoteroTitleManifestItem):
        if (
            candidate_inventory_sha256 is None
            or result.source.inventory_sha256 != candidate_inventory_sha256
            or (
                manifest_item.source.inventory_sha256 is not None
                and manifest_item.source.inventory_sha256 != candidate_inventory_sha256
            )
        ):
            raise _invalid(
                "source_binding_mismatch",
                "Zotero title result inventory provenance differs from canonical raw search inventory",
            )
    if isinstance(manifest_item, PdfManifestItem):
        if (not allow_mutable_run and run.status != "published") or result.local_publication is None:
            raise _invalid("local_publication_invalid", "local worker success requires a published run and receipt")
        receipt_path, _receipt_raw, receipt = _read_envelope(
            result.local_publication,
            ForeignLocalReceipt,
            basename=f"{candidate.candidate_id}.json",
            schema="paper_reader.local-receipt.v2-internal",
            id_field="receipt_id",
        )
        if receipt_path.parent.name != "receipts":
            raise _invalid("local_publication_invalid", "local receipt must live in receipts/<candidate_id>.json")
        if receipt_path != run_path.parent / "receipts" / f"{candidate.candidate_id}.json":
            raise _invalid("local_publication_invalid", "local receipt belongs to another paper_reader run")
        expected_candidate_relative = candidate_path.relative_to(run_path.parent).as_posix()
        if (
            receipt.receipt_id != f"local-receipt-{candidate.candidate_id}"
            or receipt.run_id != run.run_id
            or receipt.candidate_path != expected_candidate_relative
            or receipt.candidate_digest != result.candidate.sha256
            or receipt.target_path != candidate.target.resolved_path
            or receipt.content_sha256 != candidate.content_sha256
            or receipt.content_length != candidate.content_length
        ):
            raise _invalid("local_publication_invalid", "local receipt does not bind candidate/publication")
        if receipt.intent_path != "publication-intent.json":
            raise _invalid("local_publication_invalid", "local receipt intent path is not canonical")
        _require_normalized_absolute_path(
            receipt.target_path,
            code="local_publication_invalid",
            label="local publication target",
        )
        intent_path = run_path.parent / receipt.intent_path
        intent_raw, intent = _read_model(intent_path, ForeignLocalIntent, code="local_publication_invalid")
        if (
            sha256_bytes(intent_raw) != receipt.intent_sha256
            or intent.run_id != run.run_id
            or intent.candidate_id != candidate.candidate_id
            or intent.candidate_digest != result.candidate.sha256
            or intent.target_path != receipt.target_path
            or intent.content_sha256 != receipt.content_sha256
            or intent.content_length != receipt.content_length
        ):
            raise _invalid("local_publication_invalid", "local publication intent differs from receipt/candidate")
        run_intent_refs = [ref for ref in run.artifacts if ref.role == "local_publication_intent"]
        run_receipt_refs = [ref for ref in run.artifacts if ref.role == "local_receipt"]
        if (
            len(run_intent_refs) != 1
            or len(run_receipt_refs) != 1
            or run_intent_refs[0].path != receipt.intent_path
            or run_intent_refs[0].sha256 != receipt.intent_sha256
            or run_intent_refs[0].size_bytes != len(intent_raw)
            or run_intent_refs[0].media_type != "application/json"
            or run_receipt_refs[0].path != receipt_path.relative_to(run_path.parent).as_posix()
            or run_receipt_refs[0].sha256 != result.local_publication.sha256
            or run_receipt_refs[0].size_bytes != result.local_publication.size_bytes
            or run_receipt_refs[0].media_type != "application/json"
        ):
            raise _invalid("local_publication_invalid", "published run does not bind exact intent/receipt refs")
        note_refs = [ref for ref in candidate.artifacts if ref.role == "note_markdown"]
        if len(note_refs) != 1:
            raise _invalid("local_publication_invalid", "local candidate does not bind one note Markdown")
        note_path, note_bytes, _model = _read_inner(run_path.parent, note_refs[0])
        if not allow_mutable_run:
            target_path = normalized_absolute_path(Path(receipt.target_path))
            published = read_bytes(target_path, code="local_publication_invalid")
            try:
                target_stat = os.stat(target_path, follow_symlinks=False)
                note_stat = os.stat(note_path, follow_symlinks=False)
                source_stat = os.stat(manifest_item.source.path, follow_symlinks=False)
            except OSError as exc:
                raise _invalid("local_publication_invalid", "local publication identity cannot be stat-ed", exc)
            if (
                published != note_bytes
                or len(published) != receipt.content_length
                or sha256_bytes(published) != receipt.content_sha256
                or (target_stat.st_dev, target_stat.st_ino) in {
                    (note_stat.st_dev, note_stat.st_ino),
                    (source_stat.st_dev, source_stat.st_ino),
                }
            ):
                raise _invalid("local_publication_invalid", "local published note bytes differ from receipt")
    elif not allow_mutable_run and run.status != "candidate_built":
        raise _invalid("candidate_binding_mismatch", "Zotero worker run must be candidate_built")
    if isinstance(manifest_item, ZoteroTitleManifestItem) and result.source.resolved_item_key != resolved_key:
        raise _invalid("source_binding_mismatch", "worker result resolved key differs from run/candidate")
    return resolved_key


def _tree_digest(root: Path, paths: list[Path]) -> str:
    records = []
    for path in sorted(paths, key=lambda value: value.relative_to(root).as_posix()):
        raw = read_bytes(path, code="paper_reader_root_invalid")
        records.append({"path": path.relative_to(root).as_posix(), "sha256": sha256_bytes(raw)})
    return canonical_sha256(records)


def paper_reader_root_identity(root: Path) -> SkillRootIdentity:
    normalized = normalized_absolute_path(root)
    with open_directory_fd(normalized, create=False):
        pass
    required = [normalized / "SKILL.md", normalized / "pyproject.toml", normalized / "uv.lock"]
    raw_required = [read_bytes(path, code="paper_reader_root_invalid") for path in required]
    try:
        pyproject = tomllib.loads(raw_required[1].decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _invalid("paper_reader_root_invalid", "paper_reader pyproject is invalid", exc)
    script = pyproject.get("project", {}).get("scripts", {}).get("paper_reader")
    if script != "paper_reader.public_cli:app":
        raise _invalid("paper_reader_root_invalid", "paper_reader CLI entrypoint is not the grouped V2 public app")
    runtime_root = normalized / "src"
    schema_root = normalized / "references" / "schemas"
    runtime_files = [path for path in runtime_root.rglob("*.py") if path.is_file()]
    schema_files = [path for path in schema_root.glob("*.schema.json") if path.is_file()]
    required_schemas = {
        "paper_reader.run.v2.schema.json",
        "paper_reader.command-result.v2.schema.json",
        "paper_reader.review-package.v2.schema.json",
        "paper_reader.candidate.v2.schema.json",
    }
    if not runtime_files or not required_schemas.issubset({path.name for path in schema_files}):
        raise _invalid("paper_reader_root_invalid", "paper_reader root lacks V2 runtime/schema capability")
    return SkillRootIdentity(
        path=str(normalized),
        skill_md_sha256=sha256_bytes(raw_required[0]),
        pyproject_sha256=sha256_bytes(raw_required[1]),
        uv_lock_sha256=sha256_bytes(raw_required[2]),
        runtime_sha256=_tree_digest(normalized, runtime_files),
        schemas_sha256=_tree_digest(normalized, schema_files),
    )


def validate_local_prepare_result_artifacts(
    manifest: BatchManifest,
    result: LocalPrepareResult,
    *,
    expected_root: Path | None = None,
    allow_mutable_run: bool = False,
) -> None:
    manifest_item = next((item for item in manifest.items if item.item_id == result.item_id), None)
    if not isinstance(manifest_item, PdfManifestItem):
        raise _invalid("unknown_item", "local prepare result must reference a PDF manifest item")
    if result.source != manifest_item.source or (
        not allow_mutable_run and _pdf_source(Path(result.source.path)) != result.source
    ):
        raise _invalid("source_drift", "local prepare source identity changed")
    if not allow_mutable_run:
        root = normalized_absolute_path(expected_root or Path(result.paper_reader_root.path))
        if result.paper_reader_root != paper_reader_root_identity(root):
            raise _invalid("paper_reader_root_drift", "paper_reader root identity differs from result")
    if result.status != "prepared":
        return
    assert result.paper_reader_run and result.evidence
    run_path, _run_raw, run = _read_envelope(
        result.paper_reader_run,
        ForeignRun,
        basename="run.json",
        schema="paper_reader.run.v2",
        id_field="run_id",
        bind_bytes=not allow_mutable_run,
    )
    _validate_source_directory(run_path.parent, run.source)
    if not isinstance(run.target, ForeignLocalTarget) or (
        not allow_mutable_run
        and (run.status != "prepared" or run.gate.status == "blocked" or run.gate.blockers)
    ):
        raise _invalid("local_prepare_invalid", "local prepare run must be prepared with its fixed local target")
    _source_matches(manifest_item, run.source, refingerprint=not allow_mutable_run)
    _validate_local_run_target(
        manifest_item,
        run_path,
        run.target,
        check_parent_device=not allow_mutable_run,
    )
    evidence_path, _evidence_raw, evidence = _read_envelope(
        result.evidence,
        ForeignEvidence,
        basename="evidence.json",
        schema="paper_reader.evidence.v2-internal",
        id_field="evidence_id",
    )
    if evidence_path != run_path.parent / "evidence" / evidence.evidence_id / "evidence.json":
        raise _invalid("evidence_binding_mismatch", "local prepare evidence path is outside the bound run bundle")
    if evidence.run_id != run.run_id:
        raise _invalid("evidence_binding_mismatch", "local prepare evidence run id differs")
    _run_ref_for(run, run_path.parent, evidence_path, result.evidence, "evidence_manifest")
    _validate_evidence(run_path.parent, evidence_path, evidence, manifest_item.source.sha256)


__all__ = [
    "paper_reader_root_identity",
    "validate_local_prepare_result_artifacts",
    "validate_worker_result_artifacts",
]
