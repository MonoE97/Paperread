from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

REQUIRED_SECTIONS = [
    "元数据",
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


def render_note(metadata: dict[str, Any], summary: dict[str, Any], generated_date: str | None = None) -> str:
    """Render a Zotero/Better Notes friendly Markdown note."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("zotero_note.md.j2")
    context = {
        "generated_date": generated_date or date.today().isoformat(),
        "key": metadata.get("key", ""),
        "title": metadata.get("title", ""),
        "creators": metadata.get("creators", ""),
        "date": metadata.get("date", ""),
        "doi": metadata.get("DOI", ""),
        "url": metadata.get("url", ""),
        "zotero_url": metadata.get("zoteroUrl", ""),
        "quality_score": summary.get("quality_score", ""),
        "one_sentence_summary": summary.get("one_sentence_summary", ""),
        "abstract_translation": summary.get("abstract_translation", ""),
        "key_points": summary.get("key_points", []),
        "research_question": summary.get("research_question", ""),
        "method": summary.get("method", ""),
        "figure_overview": summary.get("figure_overview", ""),
        "key_figures": summary.get("key_figures", []),
        "experiments": summary.get("experiments", ""),
        "contributions": summary.get("contributions", []),
        "limitations": summary.get("limitations", []),
        "ai4s_relevance": summary.get("ai4s_relevance", ""),
        "follow_up_keywords": summary.get("follow_up_keywords", []),
        "extraction_warnings": summary.get("extraction_warnings", []),
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
