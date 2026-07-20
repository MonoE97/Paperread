from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from html import unescape
from typing import Literal
from urllib.parse import quote

from pydantic import ValidationError

from paper_reader.contracts import (
    Identifier,
    NonNegativeInt,
    PaperReaderSummary,
    SecondaryCrossCheckFinding,
    Sha256,
)
from paper_reader.evidence_manifest import BoundEvidence, VerifiedEvidenceArtifact
from paper_reader.note import clean_issue_list, infer_main_risk_short
from paper_reader.secondary_evidence import (
    CAPTURE_DESCRIPTION_MAX_CHARS,
    CAPTURE_PUBLISHED_AT_MAX_CHARS,
    CAPTURE_PUBLISHER_MAX_CHARS,
    CAPTURE_TEXT_MAX_CHARS,
    CAPTURE_TEXT_MIN_CHARS,
    CAPTURE_TITLE_MAX_CHARS,
    INVENTORY_FORMAT,
    SecondaryCapture,
    SecondaryPlan,
    SecondaryPlanSource,
    StrictSecondaryModel,
)
from paper_reader.secondary_sources import USAGE_BOUNDARY, is_unsafe_secondary_url
from paper_reader.storage import canonical_json_bytes


CJK_RE = re.compile(r"[\u3400-\u9fff]")
RELATION_TARGETS = {
    "supports": {
        "core_result_short_annotation",
        "technical_details_item",
    },
    "extends": {
        "technical_details_item",
        "applicability_limits_item",
    },
    "questions": {
        "main_risk_short_annotation",
        "inferred_limits_item",
        "applicability_limits_item",
    },
    "conflicts": {
        "main_risk_short_annotation",
        "inferred_limits_item",
    },
}
TABLE_TARGETS = {
    "core_result_short_annotation",
    "main_risk_short_annotation",
}


class SecondaryInventorySource(SecondaryPlanSource):
    capture_status: Literal["rejected", "not_attempted", "captured", "unavailable"]
    capture_path: str | None
    capture_sha256: Sha256 | None


class SecondaryInventory(StrictSecondaryModel):
    format: Literal["paper_reader.secondary-sources.v2-internal"]
    run_id: Identifier
    item_key: Identifier
    title: str
    source_snapshot_sha256: Sha256
    secondary_plan_sha256: Sha256
    usage_boundary: Literal["cross-check only; must not be cited in evidence_summary"]
    eligible_source_count: NonNegativeInt
    captured_source_count: NonNegativeInt
    sources: tuple[SecondaryInventorySource, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SecondarySourceView:
    planned: SecondaryPlanSource
    capture_status: str
    capture: SecondaryCapture | None


class SecondaryProjectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _one_artifact(evidence: BoundEvidence, role: str) -> VerifiedEvidenceArtifact | None:
    artifacts = evidence.artifacts_by_role.get(role, ())
    if not artifacts:
        return None
    if len(artifacts) != 1:
        raise SecondaryProjectionError(
            "secondary_evidence_invalid",
            f"evidence must bind at most one {role} artifact",
        )
    return artifacts[0]


def _load_secondary_views(
    evidence: BoundEvidence,
) -> tuple[tuple[SecondarySourceView, ...] | None, bool]:
    inventory_artifact = _one_artifact(evidence, "secondary_sources")
    if inventory_artifact is None:  # pragma: no cover - evidence loader requires it
        raise SecondaryProjectionError(
            "secondary_evidence_invalid",
            "evidence has no secondary source inventory",
        )
    try:
        raw_inventory = json.loads(inventory_artifact.raw_bytes)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SecondaryProjectionError(
            "secondary_evidence_invalid",
            "secondary source inventory is not valid JSON",
        ) from exc
    if not isinstance(raw_inventory, dict):
        raise SecondaryProjectionError(
            "secondary_evidence_invalid",
            "secondary source inventory must be an object",
        )
    if raw_inventory.get("format") != INVENTORY_FORMAT:
        return None, False

    plan_artifact = _one_artifact(evidence, "secondary_plan")
    if plan_artifact is None:
        raise SecondaryProjectionError(
            "secondary_evidence_invalid",
            "versioned secondary source inventory has no plan snapshot",
        )
    try:
        inventory = SecondaryInventory.model_validate_json(inventory_artifact.raw_bytes)
        plan = SecondaryPlan.model_validate_json(plan_artifact.raw_bytes)
    except ValidationError as exc:
        raise SecondaryProjectionError(
            "secondary_evidence_invalid",
            f"secondary evidence failed strict validation: {exc}",
        ) from exc
    if (
        canonical_json_bytes(inventory) != inventory_artifact.raw_bytes
        or canonical_json_bytes(plan) != plan_artifact.raw_bytes
        or inventory.format != INVENTORY_FORMAT
        or inventory.run_id != evidence.manifest.run_id
        or inventory.item_key != plan.item_key
        or inventory.source_snapshot_sha256 != plan.source_snapshot_sha256
        or inventory.secondary_plan_sha256
        != hashlib.sha256(plan_artifact.raw_bytes).hexdigest()
        or inventory.usage_boundary != plan.usage_boundary
        or inventory.usage_boundary != USAGE_BOUNDARY
        or inventory.eligible_source_count != plan.eligible_source_count
        or len(inventory.sources) != len(plan.sources)
    ):
        raise SecondaryProjectionError(
            "secondary_evidence_invalid",
            "secondary inventory and plan snapshots are inconsistent",
        )

    captures_by_relative_path: dict[str, VerifiedEvidenceArtifact] = {}
    manifest_dir = evidence.manifest_path.parent
    for artifact in evidence.artifacts_by_role.get("secondary_capture", ()):
        relative = artifact.path.relative_to(manifest_dir).as_posix()
        if relative in captures_by_relative_path:
            raise SecondaryProjectionError(
                "secondary_evidence_invalid",
                f"duplicate secondary capture path: {relative}",
            )
        captures_by_relative_path[relative] = artifact

    views: list[SecondarySourceView] = []
    captured_count = 0
    used_capture_paths: set[str] = set()
    for planned, item in zip(plan.sources, inventory.sources, strict=True):
        if planned.eligibility == "eligible" and is_unsafe_secondary_url(planned.url):
            raise SecondaryProjectionError(
                "secondary_evidence_invalid",
                f"eligible plan URL is not public HTTP(S): {planned.source_id}",
            )
        planned_payload = planned.model_dump(mode="json")
        inventory_plan_payload = {
            key: value
            for key, value in item.model_dump(mode="json").items()
            if key not in {"capture_status", "capture_path", "capture_sha256"}
        }
        if inventory_plan_payload != planned_payload:
            raise SecondaryProjectionError(
                "secondary_evidence_invalid",
                f"secondary inventory source differs from plan: {planned.source_id}",
            )
        capture: SecondaryCapture | None = None
        if planned.eligibility == "rejected":
            if (
                item.capture_status != "rejected"
                or item.capture_path is not None
                or item.capture_sha256 is not None
            ):
                raise SecondaryProjectionError(
                    "secondary_evidence_invalid",
                    f"rejected source has capture state: {planned.source_id}",
                )
        elif item.capture_status == "not_attempted":
            if item.capture_path is not None or item.capture_sha256 is not None:
                raise SecondaryProjectionError(
                    "secondary_evidence_invalid",
                    f"not-attempted source binds a capture: {planned.source_id}",
                )
        elif item.capture_status in {"captured", "unavailable"}:
            expected_path = f"secondary/{planned.source_id}.json"
            if item.capture_path != expected_path or not item.capture_sha256:
                raise SecondaryProjectionError(
                    "secondary_evidence_invalid",
                    f"captured source has an invalid path or digest: {planned.source_id}",
                )
            capture_artifact = captures_by_relative_path.get(expected_path)
            if (
                capture_artifact is None
                or capture_artifact.ref.sha256 != item.capture_sha256
                or hashlib.sha256(capture_artifact.raw_bytes).hexdigest()
                != item.capture_sha256
            ):
                raise SecondaryProjectionError(
                    "secondary_evidence_invalid",
                    f"capture artifact binding failed: {planned.source_id}",
                )
            try:
                capture = SecondaryCapture.model_validate_json(capture_artifact.raw_bytes)
            except ValidationError as exc:
                raise SecondaryProjectionError(
                    "secondary_evidence_invalid",
                    f"capture failed strict validation: {planned.source_id}: {exc}",
                ) from exc
            if (
                canonical_json_bytes(capture) != capture_artifact.raw_bytes
                or capture.run_id != inventory.run_id
                or capture.item_key != inventory.item_key
                or capture.source_snapshot_sha256 != inventory.source_snapshot_sha256
                or capture.secondary_plan_sha256 != inventory.secondary_plan_sha256
                or capture.source_id != planned.source_id
                or capture.requested_url != planned.url
                or capture.status != item.capture_status
                or capture.text_length != len(capture.text)
                or hashlib.sha256(capture.text.encode("utf-8")).hexdigest()
                != capture.text_sha256
            ):
                raise SecondaryProjectionError(
                    "secondary_evidence_invalid",
                    f"capture content does not match its inventory: {planned.source_id}",
                )
            if (
                len(capture.title) > CAPTURE_TITLE_MAX_CHARS
                or len(capture.publisher) > CAPTURE_PUBLISHER_MAX_CHARS
                or len(capture.published_at) > CAPTURE_PUBLISHED_AT_MAX_CHARS
                or len(capture.description) > CAPTURE_DESCRIPTION_MAX_CHARS
            ):
                raise SecondaryProjectionError(
                    "secondary_evidence_invalid",
                    f"capture metadata exceeds strict limits: {planned.source_id}",
                )
            if capture.status == "captured" and (
                not CAPTURE_TEXT_MIN_CHARS
                <= capture.text_length
                <= CAPTURE_TEXT_MAX_CHARS
                or not capture.title.strip()
                or is_unsafe_secondary_url(capture.final_url)
            ):
                raise SecondaryProjectionError(
                    "secondary_evidence_invalid",
                    f"captured source is not usable: {planned.source_id}",
                )
            if capture.status == "unavailable" and (
                capture.text
                or capture.text_length != 0
                or not capture.warnings
            ):
                raise SecondaryProjectionError(
                    "secondary_evidence_invalid",
                    f"unavailable source has captured content: {planned.source_id}",
                )
            used_capture_paths.add(expected_path)
            if capture.status == "captured":
                captured_count += 1
        else:
            raise SecondaryProjectionError(
                "secondary_evidence_invalid",
                f"unknown capture status for {planned.source_id}: {item.capture_status}",
            )
        views.append(
            SecondarySourceView(
                planned=planned,
                capture_status=item.capture_status,
                capture=capture,
            )
        )
    if (
        used_capture_paths != set(captures_by_relative_path)
        or captured_count != inventory.captured_source_count
    ):
        raise SecondaryProjectionError(
            "secondary_evidence_invalid",
            "secondary capture inventory is not closed-world consistent",
        )
    return tuple(views), True


def _require_chinese(value: str, *, field: str) -> None:
    if not CJK_RE.search(value):
        raise SecondaryProjectionError(
            "secondary_cross_check_not_chinese",
            f"{field} must use Chinese-first prose",
        )


def _validate_finding_source_separation(
    finding: SecondaryCrossCheckFinding,
    *,
    view: SecondarySourceView,
) -> None:
    values = (finding.text, *finding.caveats)
    for index, value in enumerate(values):
        _require_chinese(
            value,
            field=(
                f"secondary finding for {view.planned.source_id}"
                if index == 0
                else f"secondary caveat for {view.planned.source_id}"
            ),
        )
        forbidden = [view.planned.url]
        if view.capture is not None:
            forbidden.extend((view.capture.title, view.capture.publisher))
        if any(needle and needle in value for needle in forbidden):
            raise SecondaryProjectionError(
                "secondary_cross_check_source_metadata_embedded",
                f"finding embeds source metadata instead of using evidence: {view.planned.source_id}",
            )


def _validate_assessments(
    summary: PaperReaderSummary,
    views: tuple[SecondarySourceView, ...] | None,
    *,
    rich_inventory: bool,
) -> tuple[SecondarySourceView, ...]:
    assessments = summary.secondary_cross_checks
    if not rich_inventory or views is None:
        if assessments:
            raise SecondaryProjectionError(
                "secondary_cross_check_not_allowed",
                "secondary cross-checks require plan-bound Zotero evidence",
            )
        return ()

    eligible = tuple(view for view in views if view.planned.eligibility == "eligible")
    expected_ids = tuple(view.planned.source_id for view in eligible)
    observed_ids = tuple(item.source_id for item in assessments)
    if len(set(observed_ids)) != len(observed_ids):
        raise SecondaryProjectionError(
            "secondary_cross_check_mismatch",
            "secondary source assessments contain duplicate source ids",
        )
    if expected_ids and not observed_ids:
        raise SecondaryProjectionError(
            "secondary_cross_check_missing",
            "every eligible secondary source requires one assessment",
        )
    if observed_ids != expected_ids:
        raise SecondaryProjectionError(
            "secondary_cross_check_mismatch",
            "secondary source assessments do not exactly match eligible plan order",
        )

    table_counts = {target: 0 for target in TABLE_TARGETS}
    for assessment, view in zip(assessments, eligible, strict=True):
        _require_chinese(
            assessment.reason,
            field=f"secondary assessment reason for {assessment.source_id}",
        )
        if assessment.status in {"used", "irrelevant"}:
            if view.capture_status != "captured" or view.capture is None:
                raise SecondaryProjectionError(
                    "secondary_cross_check_status_mismatch",
                    f"{assessment.status} assessment requires a captured source: {assessment.source_id}",
                )
        elif view.capture_status not in {"unavailable", "not_attempted"}:
            raise SecondaryProjectionError(
                "secondary_cross_check_status_mismatch",
                f"unavailable assessment conflicts with captured evidence: {assessment.source_id}",
            )
        for finding in assessment.findings:
            if finding.target not in RELATION_TARGETS[finding.relation]:
                raise SecondaryProjectionError(
                    "secondary_cross_check_target_invalid",
                    f"{finding.relation} cannot target {finding.target}",
                )
            if finding.target in table_counts:
                table_counts[finding.target] += 1
                if table_counts[finding.target] > 2:
                    raise SecondaryProjectionError(
                        "secondary_cross_check_table_limit",
                        f"table target {finding.target} accepts at most two annotations",
                    )
            _validate_finding_source_separation(finding, view=view)
    return eligible


def _escape_markdown_inline(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for marker in ("`", "*", "_", "[", "]", "<", ">", "|"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped


def _one_line(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _markdown_url(value: str) -> str:
    return quote(
        value,
        safe=":/?#[]@!$&'*+,;=%~-._",
    )


def _source_link(view: SecondarySourceView, *, unavailable: bool = False) -> str:
    title = "来源"
    if not unavailable and view.capture is not None and view.capture.title.strip():
        title = _one_line(view.capture.title)
    return f"[{_escape_markdown_inline(title)}]({_markdown_url(view.planned.url)})"


def _annotation(finding: SecondaryCrossCheckFinding, view: SecondarySourceView) -> str:
    detail = _escape_markdown_inline(finding.text)
    if finding.caveats:
        caveats = "；".join(_escape_markdown_inline(item) for item in finding.caveats)
        detail = f"{detail}；注意：{caveats}"
    return f"外部交叉核对（补充）：{detail}（{_source_link(view)}）"


def _iter_text_values(
    value: object,
    *,
    path: str = "summary",
) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_text_values(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_text_values(item, path=f"{path}[{index}]")


def _reject_unstructured_plan_urls(
    summary: PaperReaderSummary,
    plan_sources: tuple[SecondarySourceView, ...],
) -> None:
    raw_payload = summary.model_dump(
        mode="json",
        exclude={"secondary_cross_checks"},
    )
    planned_url_variants = {
        variant
        for view in plan_sources
        for variant in (view.planned.url, _markdown_url(view.planned.url))
    }
    for path, value in _iter_text_values(raw_payload):
        decoded_value = unescape(value)
        if any(url in decoded_value for url in planned_url_variants):
            raise SecondaryProjectionError(
                "secondary_cross_check_projection_bypass",
                f"{path} embeds a plan-bound secondary URL outside secondary_cross_checks",
            )


def resolve_secondary_render_summary(
    summary: PaperReaderSummary,
    evidence: BoundEvidence,
) -> dict[str, object]:
    views, rich_inventory = _load_secondary_views(evidence)
    eligible = _validate_assessments(
        summary,
        views,
        rich_inventory=rich_inventory,
    )
    if rich_inventory and views is not None:
        _reject_unstructured_plan_urls(
            summary,
            tuple(
                view
                for view in views
                if view.planned.rejection_reason != "primary_source"
            ),
        )
    payload = summary.model_dump(mode="json")
    if not eligible:
        return payload

    view_by_id = {view.planned.source_id: view for view in eligible}
    technical_details = list(payload.get("technical_details", []))
    applicability_limits = list(payload.get("applicability_limits", []))
    inferred_limits = list(payload.get("inferred_limits", []))
    projected_inferred_limits: list[dict[str, str]] = []
    projected_inferred_count = 0
    table_annotations: dict[str, list[str]] = {target: [] for target in TABLE_TARGETS}
    unavailable_views: list[SecondarySourceView] = []
    trusted_source_links: list[str] = []

    for assessment in summary.secondary_cross_checks:
        view = view_by_id[assessment.source_id]
        if assessment.status == "unavailable":
            unavailable_views.append(view)
            continue
        if assessment.status == "irrelevant":
            continue
        for finding in assessment.findings:
            source_link = _source_link(view)
            if source_link not in trusted_source_links:
                trusted_source_links.append(source_link)
            rendered = _annotation(finding, view)
            if finding.target in table_annotations:
                table_annotations[finding.target].append(rendered)
            elif finding.target == "technical_details_item":
                technical_details.append(rendered)
            elif finding.target == "applicability_limits_item":
                applicability_limits.append(rendered)
            elif finding.target == "inferred_limits_item":
                projected_inferred_count += 1
                projected_inferred_limits.append(
                    {
                        "text": rendered,
                        "source_type": "inferred",
                        "basis": "外部材料交叉核对，不作为论文原文证据",
                        "locator": "",
                    }
                )

    core_annotations = table_annotations["core_result_short_annotation"]
    if core_annotations:
        base = str(payload.get("core_result_short") or payload["one_sentence_summary"])
        payload["core_result_short"] = "；".join((base, *core_annotations))
    risk_annotations = table_annotations["main_risk_short_annotation"]
    if risk_annotations:
        base = infer_main_risk_short(payload, clean_issue_list(payload))
        payload["main_risk_short"] = "；".join((base, *risk_annotations))
    if unavailable_views:
        for view in unavailable_views:
            source_link = _source_link(view, unavailable=True)
            if source_link not in trusted_source_links:
                trusted_source_links.append(source_link)
        links = "、".join(_source_link(view, unavailable=True) for view in unavailable_views)
        applicability_limits.append(
            "外部交叉核对未完整完成：以下链接无法读取，未纳入上述判断"
            f"（{links}）。"
        )
    payload["technical_details"] = technical_details
    payload["applicability_limits"] = applicability_limits
    payload["inferred_limits"] = (
        [*inferred_limits[:8], *projected_inferred_limits]
        if projected_inferred_limits
        else inferred_limits
    )
    if projected_inferred_count:
        payload["_secondary_projected_inferred_count"] = projected_inferred_count
    if trusted_source_links:
        payload["_secondary_trusted_source_links"] = trusted_source_links
    return payload


__all__ = [
    "SecondaryProjectionError",
    "resolve_secondary_render_summary",
]
