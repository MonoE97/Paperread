from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markdown_it import MarkdownIt

REQUIRED_SECTIONS = [
    "0. 速读决策",
    "1. 论文核心",
    "2. 方法怎么做",
    "3. 结果是否站得住",
    "4. 图表导读",
    "5. 局限、适用边界与潜在 gap",
    "6. 可迁移启发",
    "7. 术语与概念卡片",
    "8. 后续检索关键词",
    "9. 元数据",
    "10. 证据链附录",
    "11. 补充优化记录",
]

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
FIXED_NOTE_LABELS = ["codex-summary", "paper-summary"]
MAX_INFERRED_NOTE_LABELS = 4
VALID_PAPER_TYPES = {
    "research_article",
    "review",
    "perspective",
    "benchmark",
    "method_paper",
    "dataset_paper",
    "theory_paper",
    "unknown",
}
VALID_TRUST_STATUSES = {"trusted", "usable_with_caveats", "metadata_only", "needs_manual_review"}
VALID_REVIEW_STATUSES = {"not_reviewed", "passed", "passed_with_caveats", "failed"}
VALID_IMPROVEMENT_STATUSES = {"not_needed", "needed", "completed", "blocked"}
VALID_READING_DECISIONS = {"strongly_recommended", "recommended", "skim_only", "not_priority", "unknown"}
VALID_EVIDENCE_LEVELS = {"high", "medium", "low", "text_only", "caption_only", "image_unverified", "unknown"}
VALID_IMAGE_QUALITIES = {"good", "ok", "poor", "image_too_small", "caption_only", "unknown"}
WRITE_READY_REVIEW_STATUSES = {"passed", "passed_with_caveats"}
PAPER_TYPE_DISPLAY_LABELS = {
    "research_article": "研究论文",
    "review": "综述",
    "perspective": "观点 / 展望",
    "benchmark": "基准测试论文",
    "method_paper": "方法论文",
    "dataset_paper": "数据集论文",
    "theory_paper": "理论论文",
    "unknown": "unknown",
}
TRUST_STATUS_DISPLAY_LABELS = {
    "trusted": "可信",
    "usable_with_caveats": "可用但需注意限制",
    "metadata_only": "仅元数据可用",
    "needs_manual_review": "需要人工复核",
}
READING_DECISION_DISPLAY_LABELS = {
    "strongly_recommended": "强烈建议精读",
    "recommended": "建议阅读",
    "skim_only": "只需略读",
    "not_priority": "暂非优先",
    "unknown": "unknown",
}
REQUIRED_WRITE_READY_TEXT_FIELDS = {
    "one_sentence_summary": "one_sentence_summary is required",
    "abstract_translation": "abstract_translation is required",
    "research_question": "research_question is required",
    "method": "method is required",
    "experiments": "experiments is required",
    "ai4s_relevance": "ai4s_relevance is required",
}
REQUIRED_WRITE_READY_LIST_FIELDS = {
    "key_points": "key_points must contain at least one item",
    "contributions": "contributions must contain at least one item",
    "limitations": "limitations must contain at least one item",
    "follow_up_keywords": "follow_up_keywords must contain at least one item",
}


def safe_choice(value: Any, allowed: set[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def display_choice(value: str, display_labels: dict[str, str]) -> str:
    label = display_labels.get(value, "unknown")
    if value == "unknown" or label == "unknown":
        return "unknown"
    return f"{label} ({value})"


def safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def clean_string_list(value: Any) -> list[str]:
    items = safe_list(value)
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text:
            cleaned.append(text)
    return cleaned


def clean_method_modules(value: Any) -> list[dict[str, str]]:
    items = safe_list(value)
    cleaned: list[dict[str, str]] = []
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        name = safe_text(item.get("name"))
        if name == "unknown":
            continue
        cleaned.append(
            {
                "name": name,
                "input": safe_text(item.get("input")),
                "target": safe_text(item.get("target")),
                "output": safe_text(item.get("output")),
                "role": safe_text(item.get("role")),
            }
        )
    return cleaned


def clean_key_results_table(value: Any) -> list[dict[str, str]]:
    items = safe_list(value)
    cleaned: list[dict[str, str]] = []
    for item in items[:12]:
        if not isinstance(item, dict):
            continue
        result = safe_text(item.get("result"))
        if result == "unknown":
            continue
        cleaned.append(
            {
                "result": result,
                "value": safe_text(item.get("value")),
                "meaning": safe_text(item.get("meaning")),
            }
        )
    return cleaned


def clean_recommendations(value: Any, *, label_keys: tuple[str, ...], limit: int = 5) -> list[dict[str, str]]:
    items = safe_list(value)
    cleaned: list[dict[str, str]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        label = ""
        for key in label_keys:
            label = optional_text(item.get(key))
            if label:
                break
        reason = fallback_text(item.get("reason"), item.get("result"), "")
        locator = optional_text(item.get("locator"))
        if label and reason:
            cleaned.append({"label": label, "reason": reason, "locator": locator})
    return cleaned


def clean_result_evidence_notes(value: Any) -> list[dict[str, str]]:
    items = safe_list(value)
    cleaned: list[dict[str, str]] = []
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        result = optional_text(item.get("result"))
        if not result:
            continue
        cleaned.append(
            {
                "result": result,
                "evidence": optional_text(item.get("evidence")),
                "locator": optional_text(item.get("locator")),
                "confidence": safe_text(item.get("confidence"), "unknown"),
            }
        )
    return cleaned


def clean_limitation_objects(value: Any, *, expected_source_type: str | None = None) -> list[dict[str, str]]:
    items = safe_list(value)
    cleaned: list[dict[str, str]] = []
    for item in items[:8]:
        if isinstance(item, str):
            text = safe_text(item)
            if text != "unknown":
                cleaned.append(
                    {
                        "text": text,
                        "basis": "",
                        "locator": "",
                        "source_type": expected_source_type or "",
                        "uncertainty": "",
                    }
                )
            continue
        if not isinstance(item, dict):
            continue
        text = optional_text(item.get("text"))
        if not text:
            continue
        cleaned.append(
            {
                "text": text,
                "basis": optional_text(item.get("basis")),
                "locator": optional_text(item.get("locator")),
                "source_type": optional_text(item.get("source_type")) or (expected_source_type or ""),
                "uncertainty": optional_text(item.get("uncertainty")),
            }
        )
    return cleaned


def clean_concept_cards(value: Any) -> list[dict[str, Any]]:
    items = safe_list(value)
    cleaned: list[dict[str, Any]] = []
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        term = safe_text(item.get("term"))
        if term == "unknown":
            continue
        cleaned.append(
            {
                "term": term,
                "short_definition": safe_text(item.get("short_definition")),
                "role_in_paper": safe_text(item.get("role_in_paper")),
                "related_keywords": clean_string_list(item.get("related_keywords", [])),
            }
        )
    return cleaned


def flatten_inline_markdown_text(value: str) -> str:
    parts = []
    for line in value.splitlines():
        text = re.sub(r"[ \t]+", " ", line.strip())
        if text:
            parts.append(text)
    return " ".join(parts)


def clean_required_text(value: Any) -> str:
    return flatten_inline_markdown_text(value) if isinstance(value, str) else ""


def safe_text(value: Any, default: str = "unknown") -> str:
    if not isinstance(value, str):
        return default
    text = flatten_inline_markdown_text(value)
    return text if text else default


def scalar_text(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return flatten_inline_markdown_text(value) or default
    if isinstance(value, (int, float)):
        return str(value)
    return default


def markdown_table_cell(value: Any) -> str:
    text = scalar_text(value) if not isinstance(value, str) else flatten_inline_markdown_text(value)
    return text.replace("|", r"\|")


def optional_text(value: Any) -> str:
    return flatten_inline_markdown_text(value) if isinstance(value, str) else ""


def fallback_text(primary: Any, fallback: Any, default: str = "unknown") -> str:
    primary_text = flatten_inline_markdown_text(primary) if isinstance(primary, str) else ""
    if primary_text:
        return primary_text
    fallback_text_value = flatten_inline_markdown_text(fallback) if isinstance(fallback, str) else ""
    if fallback_text_value:
        return fallback_text_value
    return default


def clean_workflow_steps(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    items = clean_string_list(value)
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def _has_text(value: Any) -> bool:
    return bool(clean_required_text(value))


def _is_write_ready_evidence_locator(value: Any) -> bool:
    locator = clean_required_text(value)
    return bool(re.match(r"^(?:context\.md|figure_context\.md)(?:$|[\s:#])", locator))


def format_evidence_line(locator: str, summary: str) -> str:
    locator = flatten_inline_markdown_text(locator)
    summary = flatten_inline_markdown_text(summary)
    if locator and summary:
        return f"{locator}: {summary}"
    return locator or summary


def clean_evidence_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    items = summary.get("evidence_summary", [])
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        claim = flatten_inline_markdown_text(str(item.get("claim", "")))
        if not claim:
            continue
        evidence_items = item.get("evidence", [])
        if not isinstance(evidence_items, list):
            evidence_items = []
        cleaned_evidence = []
        for evidence in evidence_items[:3]:
            if not isinstance(evidence, dict):
                continue
            locator = str(evidence.get("locator", "")).strip()
            evidence_summary = str(evidence.get("summary", "")).strip()
            evidence_type = str(evidence.get("type", "")).strip() or "text"
            if locator or evidence_summary:
                cleaned_evidence.append(
                    {
                        "type": evidence_type,
                        "locator": locator,
                        "summary": evidence_summary,
                        "line": format_evidence_line(locator, evidence_summary),
                    }
                )
        cleaned.append(
            {
                "claim": claim,
                "evidence": cleaned_evidence,
                "confidence": str(item.get("confidence", "")).strip() or "unknown",
            }
        )
    return cleaned


def validate_write_ready_evidence(summary: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    items = summary.get("evidence_summary", [])
    if not isinstance(items, list):
        return ["evidence_summary must contain at least one claim"]

    valid_claim_count = 0
    for index, item in enumerate(items[:5], start=1):
        if not isinstance(item, dict):
            continue
        if not _has_text(item.get("claim")):
            errors.append(f"evidence_summary[{index}] claim is required")
            continue

        valid_claim_count += 1
        evidence_items = item.get("evidence", [])
        has_locator = False
        if isinstance(evidence_items, list):
            for evidence in evidence_items[:3]:
                if isinstance(evidence, dict) and _is_write_ready_evidence_locator(evidence.get("locator")):
                    has_locator = True
                    break
        if not has_locator:
            errors.append(f"evidence_summary[{index}] must include at least one evidence locator")

    if valid_claim_count == 0:
        errors.insert(0, "evidence_summary must contain at least one claim")
    return errors


def normalize_figure_image_quality(item: dict[str, Any]) -> str:
    visual_quality = item.get("visual_quality")
    if isinstance(visual_quality, dict):
        warnings = visual_quality.get("warnings", [])
        if isinstance(warnings, list):
            for warning in warnings:
                if isinstance(warning, str) and warning in VALID_IMAGE_QUALITIES:
                    return warning

    image_quality = item.get("image_quality")
    if isinstance(image_quality, str) and image_quality in VALID_IMAGE_QUALITIES:
        return image_quality

    if isinstance(visual_quality, dict):
        status = visual_quality.get("status")
        if isinstance(status, str) and status in VALID_IMAGE_QUALITIES:
            return status

    return "unknown"


def extract_figure_display_label(caption: str, *, fallback_index: int) -> str:
    """Return a human-facing figure label without exposing extraction IDs."""
    match = re.search(
        r"\b(?P<prefix>fig(?:ure)?\.?|scheme)\s*(?P<number>[A-Za-z]?\d+(?:\s*[-–]\s*[A-Za-z]?\d+)?[A-Za-z]?)",
        caption,
        flags=re.IGNORECASE,
    )
    if not match:
        return f"Figure {fallback_index}"

    prefix = match.group("prefix").lower().rstrip(".")
    number = re.sub(r"\s*[-–]\s*", "-", match.group("number").strip())
    label_prefix = "Scheme" if prefix == "scheme" else "Figure"
    return f"{label_prefix} {number}"


def clean_key_figures(summary: dict[str, Any]) -> list[dict[str, Any]]:
    items = safe_list(summary.get("key_figures", []))
    cleaned: list[dict[str, Any]] = []
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        fallback_index = len(cleaned) + 1
        image_quality = normalize_figure_image_quality(item)
        caption = str(item.get("caption", "")).strip()
        cleaned.append(
            {
                "figure_id": str(item.get("figure_id", "")).strip(),
                "display_label": extract_figure_display_label(caption, fallback_index=fallback_index),
                "caption": caption,
                "page": item.get("page", ""),
                "priority_score": item.get("priority_score", ""),
                "why_it_matters": str(item.get("why_it_matters", "")).strip(),
                "title_short": optional_text(item.get("title_short")),
                "why_it_matters_short": fallback_text(item.get("why_it_matters_short"), item.get("why_it_matters")),
                "evidence_level": safe_choice(item.get("evidence_level"), VALID_EVIDENCE_LEVELS, "unknown"),
                "image_quality": image_quality,
                "figure_quality_note": fallback_text(item.get("figure_quality_note"), image_quality),
                "analysis": str(item.get("analysis", "")).strip(),
            }
        )
    return cleaned


def clean_issue_list(summary: dict[str, Any]) -> list[dict[str, str]]:
    items = summary.get("review_issues", [])
    if not isinstance(items, list):
        return []
    cleaned = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue", "")).strip()
        if not issue:
            continue
        cleaned.append(
            {
                "severity": str(item.get("severity", "")).strip() or "medium",
                "issue": issue,
                "suggested_fix": str(item.get("suggested_fix", "")).strip(),
            }
        )
    return cleaned


def infer_main_risk_short(summary: dict[str, Any], review_issues: list[dict[str, str]]) -> str:
    main_risk_short = optional_text(summary.get("main_risk_short"))
    if main_risk_short:
        return main_risk_short

    extraction_warnings = clean_string_list(summary.get("extraction_warnings", []))
    if extraction_warnings:
        return extraction_warnings[0]

    if review_issues:
        severity_rank = {"high": 0, "medium": 1, "low": 2}
        highest_issue = min(
            review_issues,
            key=lambda item: severity_rank.get(item.get("severity", "medium"), severity_rank["medium"]),
        )
        return highest_issue.get("issue") or "none"

    return "none"


def clean_improvement_notes(summary: dict[str, Any]) -> list[dict[str, str]]:
    items = summary.get("improvement_notes", [])
    if not isinstance(items, list):
        return []
    cleaned = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue", "")).strip()
        action = str(item.get("action", "")).strip()
        if not issue and not action:
            continue
        cleaned.append(
            {
                "issue": issue,
                "action": action,
                "source": str(item.get("source", "")).strip(),
            }
        )
    return cleaned


def validate_trusted_summary(summary: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    paper_type = safe_choice(summary.get("paper_type"), VALID_PAPER_TYPES, "unknown")
    if paper_type == "unknown":
        errors.append("paper_type must be a known paper type")

    trust_status = safe_choice(summary.get("trust_status"), VALID_TRUST_STATUSES, "needs_manual_review")
    if trust_status in {"metadata_only", "needs_manual_review"}:
        errors.append("trust_status is not write-ready")

    review_status = safe_choice(summary.get("review_status"), VALID_REVIEW_STATUSES, "not_reviewed")
    if review_status not in WRITE_READY_REVIEW_STATUSES:
        errors.append("review_status must be passed or passed_with_caveats")

    if not clean_required_text(summary.get("trust_rationale", "")):
        errors.append("trust_rationale is required")

    for field_name, error_message in REQUIRED_WRITE_READY_TEXT_FIELDS.items():
        value = clean_required_text(summary.get(field_name, ""))
        if not value:
            errors.append(error_message)

    for field_name, error_message in REQUIRED_WRITE_READY_LIST_FIELDS.items():
        if not clean_string_list(summary.get(field_name, [])):
            errors.append(error_message)

    errors.extend(validate_write_ready_evidence(summary))

    improvement_status = safe_choice(
        summary.get("improvement_status"),
        VALID_IMPROVEMENT_STATUSES,
        "needed",
    )
    if improvement_status in {"needed", "blocked"}:
        errors.append("improvement_status must not be needed or blocked for write-through")

    return errors


def normalize_note_label(value: Any) -> str | None:
    """Return an English-key style label suitable for note rendering."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or None


def build_note_labels(summary: dict[str, Any]) -> list[str]:
    """Build fixed Zotero labels plus a small set of normalized paper labels."""
    labels = list(FIXED_NOTE_LABELS)
    seen = set(labels)
    raw_labels = summary.get("note_labels", [])
    if not isinstance(raw_labels, list):
        raw_labels = []
    for raw_label in raw_labels:
        label = normalize_note_label(raw_label)
        if label is None or label in seen:
            continue
        labels.append(label)
        seen.add(label)
        if len(labels) - len(FIXED_NOTE_LABELS) >= MAX_INFERRED_NOTE_LABELS:
            break
    return labels


def build_note_title(
    metadata: dict[str, Any],
    generated_date: str,
    *,
    version_suffix: str = "",
) -> str:
    """Build the Zotero child-note title used by the Markdown template."""
    title = str(metadata.get("title", "")).strip()
    return f"[Codex Summary] {title} - {generated_date}{version_suffix}"


def next_same_day_version_suffix(
    existing_titles: list[str],
    *,
    paper_title: str,
    generated_date: str,
) -> str:
    """Return the next same-day title suffix without overwriting old notes."""
    base = f"[Codex Summary] {paper_title} - {generated_date}"
    used_versions: set[int] = set()
    pattern = re.compile(rf"^{re.escape(base)} \(v(?P<version>\d+)\)$")
    for title in existing_titles:
        if title == base:
            used_versions.add(1)
            continue
        match = pattern.match(title)
        if match:
            used_versions.add(int(match.group("version")))
    version = 1
    while version in used_versions:
        version += 1
    return "" if version == 1 else f" (v{version})"


def render_note(
    metadata: dict[str, Any],
    summary: dict[str, Any],
    generated_date: str | None = None,
    *,
    version_suffix: str = "",
) -> str:
    """Render a Zotero/Better Notes friendly Markdown note."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["table_cell"] = markdown_table_cell
    template = env.get_template("zotero_note.md.j2")
    resolved_date = generated_date or date.today().isoformat()
    review_issues = clean_issue_list(summary)
    extraction_warnings = clean_string_list(summary.get("extraction_warnings", []))
    paper_type = safe_choice(summary.get("paper_type"), VALID_PAPER_TYPES, "unknown")
    trust_status = safe_choice(summary.get("trust_status"), VALID_TRUST_STATUSES, "usable_with_caveats")
    reading_decision = safe_choice(summary.get("reading_decision"), VALID_READING_DECISIONS, "unknown")
    context = {
        "note_title": build_note_title(metadata, resolved_date, version_suffix=version_suffix),
        "generated_date": resolved_date,
        "key": metadata.get("key", ""),
        "title": metadata.get("title", ""),
        "creators": metadata.get("creators", ""),
        "date": metadata.get("date", ""),
        "doi": metadata.get("DOI", ""),
        "url": metadata.get("url", ""),
        "zotero_url": metadata.get("zoteroUrl", ""),
        "quality_score": scalar_text(summary.get("quality_score")),
        "paper_type": display_choice(paper_type, PAPER_TYPE_DISPLAY_LABELS),
        "trust_status": display_choice(trust_status, TRUST_STATUS_DISPLAY_LABELS),
        "trust_rationale": safe_text(summary.get("trust_rationale"), "未提供可信度判断依据。"),
        "review_status": safe_choice(summary.get("review_status"), VALID_REVIEW_STATUSES, "not_reviewed"),
        "review_issues": review_issues,
        "evidence_summary": clean_evidence_summary(summary),
        "improvement_status": safe_choice(
            summary.get("improvement_status"), VALID_IMPROVEMENT_STATUSES, "not_needed"
        ),
        "improvement_notes": clean_improvement_notes(summary),
        "one_sentence_summary": safe_text(summary.get("one_sentence_summary"), ""),
        "abstract_translation": safe_text(summary.get("abstract_translation"), ""),
        "key_points": clean_string_list(summary.get("key_points", [])),
        "research_question": safe_text(summary.get("research_question"), ""),
        "method": safe_text(summary.get("method"), ""),
        "figure_overview": safe_text(summary.get("figure_overview"), ""),
        "key_figures": clean_key_figures(summary),
        "experiments": safe_text(summary.get("experiments"), ""),
        "contributions": clean_string_list(summary.get("contributions", [])),
        "limitations": clean_string_list(summary.get("limitations", [])),
        "ai4s_relevance": safe_text(summary.get("ai4s_relevance"), ""),
        "follow_up_keywords": clean_string_list(summary.get("follow_up_keywords", [])),
        "extraction_warnings": extraction_warnings,
        "note_labels": build_note_labels(summary),
        "research_object": safe_text(summary.get("research_object")),
        "research_question_short": fallback_text(summary.get("research_question_short"), summary.get("research_question")),
        "core_method_short": fallback_text(summary.get("core_method_short"), summary.get("method")),
        "core_result_short": fallback_text(summary.get("core_result_short"), summary.get("one_sentence_summary")),
        "relevance_to_user": safe_text(summary.get("relevance_to_user")),
        "reading_decision": display_choice(reading_decision, READING_DECISION_DISPLAY_LABELS),
        "main_risk_short": infer_main_risk_short(summary, review_issues),
        "tldr": optional_text(summary.get("tldr")),
        "background_problem": safe_text(summary.get("background_problem")),
        "existing_gap": safe_text(summary.get("existing_gap")),
        "paper_entry_point": safe_text(summary.get("paper_entry_point")),
        "method_overview": fallback_text(summary.get("method_overview"), summary.get("method")),
        "method_modules": clean_method_modules(summary.get("method_modules", [])),
        "workflow_steps": clean_workflow_steps(summary.get("workflow_steps", "")),
        "technical_details": clean_string_list(summary.get("technical_details", [])),
        "key_results_table": clean_key_results_table(summary.get("key_results_table", [])),
        "recommended_sections": clean_recommendations(
            summary.get("recommended_sections", []), label_keys=("section",)
        ),
        "recommended_figures": clean_recommendations(
            summary.get("recommended_figures", []), label_keys=("figure_id",)
        ),
        "baseline_or_comparison": clean_recommendations(
            summary.get("baseline_or_comparison", []), label_keys=("target",), limit=8
        ),
        "result_evidence_notes": clean_result_evidence_notes(summary.get("result_evidence_notes", [])),
        "evidence_quality_summary": optional_text(summary.get("evidence_quality_summary")),
        "applicability_limits": clean_string_list(summary.get("applicability_limits", [])),
        "author_stated_limitations": clean_limitation_objects(
            summary.get("author_stated_limitations", []),
            expected_source_type="author_stated",
        ),
        "inferred_limits": clean_limitation_objects(
            summary.get("inferred_limits", []),
            expected_source_type="inferred",
        ),
        "potential_gaps": clean_limitation_objects(summary.get("potential_gaps", [])),
        "transferable_insight": fallback_text(summary.get("transferable_insight"), summary.get("ai4s_relevance")),
        "workflow_lessons": clean_string_list(summary.get("workflow_lessons", [])),
        "follow_up_questions": clean_string_list(summary.get("follow_up_questions", [])),
        "concept_cards": clean_concept_cards(summary.get("concept_cards", [])),
    }
    return template.render(**context).strip() + "\n"


def render_note_html(note: str) -> str:
    """Convert rendered Markdown into Zotero-ready HTML with table support."""
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    return parser.render(note).strip() + "\n"


def validate_note(note: str) -> list[str]:
    """Return validation errors for a rendered note."""
    errors: list[str] = []
    for section in REQUIRED_SECTIONS:
        if f"## {section}" not in note:
            errors.append(f"missing_section: {section}")
    if "[Codex Summary]" not in note:
        errors.append("missing_codex_summary_title")
    if "Tags: codex-summary, paper-summary" not in note:
        errors.append("missing_tags")
    return errors
