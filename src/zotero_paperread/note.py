from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

REQUIRED_SECTIONS = [
    "元数据",
    "可信度与证据",
    "核心结论",
    "摘要翻译",
    "关键要点",
    "研究问题",
    "方法拆解",
    "关键图片总览",
    "实验与证据",
    "主要贡献",
    "局限与风险",
    "AI+物理/材料启发",
    "后续关键词",
    "抽取告警",
    "本文标签",
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
WRITE_READY_REVIEW_STATUSES = {"passed", "passed_with_caveats"}
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


def flatten_inline_markdown_text(value: str) -> str:
    parts = []
    for line in value.splitlines():
        text = re.sub(r"[ \t]+", " ", line.strip())
        if text:
            parts.append(text)
    return " ".join(parts)


def clean_required_text(value: Any) -> str:
    return flatten_inline_markdown_text(value) if isinstance(value, str) else ""


def _has_text(value: Any) -> bool:
    return bool(clean_required_text(value))


def format_evidence_line(locator: str, summary: str) -> str:
    locator = flatten_inline_markdown_text(locator)
    summary = flatten_inline_markdown_text(summary)
    if locator and summary:
        details = f"{locator}; {summary}"
    else:
        details = locator or summary
    return f"  - 证据: {details}"


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
                if isinstance(evidence, dict) and _has_text(evidence.get("locator")):
                    has_locator = True
                    break
        if not has_locator:
            errors.append(f"evidence_summary[{index}] must include at least one evidence locator")

    if valid_claim_count == 0:
        errors.insert(0, "evidence_summary must contain at least one claim")
    return errors


def clean_key_figures(summary: dict[str, Any]) -> list[dict[str, Any]]:
    items = safe_list(summary.get("key_figures", []))
    cleaned: list[dict[str, Any]] = []
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        cleaned.append(
            {
                "figure_id": str(item.get("figure_id", "")).strip(),
                "caption": str(item.get("caption", "")).strip(),
                "page": item.get("page", ""),
                "priority_score": item.get("priority_score", ""),
                "why_it_matters": str(item.get("why_it_matters", "")).strip(),
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
    template = env.get_template("zotero_note.md.j2")
    resolved_date = generated_date or date.today().isoformat()
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
        "quality_score": summary.get("quality_score", ""),
        "paper_type": safe_choice(summary.get("paper_type"), VALID_PAPER_TYPES, "unknown"),
        "trust_status": safe_choice(summary.get("trust_status"), VALID_TRUST_STATUSES, "usable_with_caveats"),
        "trust_rationale": summary.get("trust_rationale", "") or "未提供可信度判断依据。",
        "review_status": safe_choice(summary.get("review_status"), VALID_REVIEW_STATUSES, "not_reviewed"),
        "review_issues": clean_issue_list(summary),
        "evidence_summary": clean_evidence_summary(summary),
        "improvement_status": safe_choice(
            summary.get("improvement_status"), VALID_IMPROVEMENT_STATUSES, "not_needed"
        ),
        "improvement_notes": clean_improvement_notes(summary),
        "one_sentence_summary": summary.get("one_sentence_summary", ""),
        "abstract_translation": summary.get("abstract_translation", ""),
        "key_points": clean_string_list(summary.get("key_points", [])),
        "research_question": summary.get("research_question", ""),
        "method": summary.get("method", ""),
        "figure_overview": summary.get("figure_overview", ""),
        "key_figures": clean_key_figures(summary),
        "experiments": summary.get("experiments", ""),
        "contributions": clean_string_list(summary.get("contributions", [])),
        "limitations": clean_string_list(summary.get("limitations", [])),
        "ai4s_relevance": summary.get("ai4s_relevance", ""),
        "follow_up_keywords": clean_string_list(summary.get("follow_up_keywords", [])),
        "extraction_warnings": clean_string_list(summary.get("extraction_warnings", [])),
        "note_labels": build_note_labels(summary),
    }
    return template.render(**context).strip() + "\n"


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
