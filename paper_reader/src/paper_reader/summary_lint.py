from __future__ import annotations

import re
from typing import Any

from paper_reader.evidence import SECONDARY_EVIDENCE_PREFIXES, is_canonical_trusted_locator

LOW_QUALITY_IMAGE_VALUES = {"poor", "image_too_small", "caption_only"}
CJK_RE = re.compile(r"[\u3400-\u9fff]")
ENGLISH_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*\b")
LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
LATIN_SPAN_RE = re.compile(r"[^\u3400-\u9fff]+")
KNOWN_CONTEXT_SECTION_NAMES = (
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
CONTEXT_SECTION_FRAGMENT_RE = re.compile(
    r"\bsection (?:" + "|".join(re.escape(name) for name in KNOWN_CONTEXT_SECTION_NAMES) + r")\b",
    flags=re.IGNORECASE,
)
LOCATOR_FRAGMENT_RE = re.compile(
    r"\b(?:context\.md page \d+|figure_context\.md [A-Za-z0-9_.:-]+|context\.md|figure_context\.md)\b"
    r"(?: table_candidate \d+)?",
)
CHEMICAL_SYMBOL_RE = re.compile(r"^[A-Z][a-z]?$")
CHEMICAL_SYMBOL_SEQUENCE_RE = re.compile(r"^(?:[A-Z][a-z]?)(?:[-/][A-Z][a-z]?)+$")
ENGLISH_FUNCTION_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "or",
    "that",
    "the",
    "this",
    "to",
    "which",
    "with",
}
UNIT_TOKENS = {
    "a",
    "c",
    "cm",
    "ev",
    "g",
    "h",
    "k",
    "kg",
    "mah",
    "mpa",
    "ms",
    "ps",
    "rh",
    "rpm",
    "s",
    "usd",
    "v",
    "vs",
}
ALLOWED_MIXED_ENGLISH_PHRASES = (
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
RENDERED_TEXT_FIELDS = (
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
RENDERED_TEXT_LIST_FIELDS = ("contributions", "technical_details", "limitations", "applicability_limits")
RENDERED_METHOD_MODULE_FIELDS = ("name", "input", "target", "output", "role")
RENDERED_FIGURE_FIELDS = ("analysis", "why_it_matters", "why_it_matters_short")

def _strip_allowed_mixed_english_phrases(value: str) -> str:
    text = LOCATOR_FRAGMENT_RE.sub(" ", value)
    text = CONTEXT_SECTION_FRAGMENT_RE.sub(" ", text)
    for phrase in ALLOWED_MIXED_ENGLISH_PHRASES:
        text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)
    return text


def _contains_allowed_mixed_english_phrase(value: str) -> bool:
    return any(re.search(re.escape(phrase), value, flags=re.IGNORECASE) for phrase in ALLOWED_MIXED_ENGLISH_PHRASES)


def _is_technical_token(token: str) -> bool:
    lower_token = token.lower()
    if lower_token in UNIT_TOKENS:
        return True
    if any(char.isdigit() for char in token):
        return True
    if CHEMICAL_SYMBOL_RE.fullmatch(token):
        return True
    parts = re.split(r"[-/]", token)
    if all(part.isupper() for part in parts):
        return True
    latin_letters = LATIN_LETTER_RE.findall(token)
    if len(latin_letters) < 3 and lower_token not in ENGLISH_FUNCTION_WORDS:
        return True
    return bool(CHEMICAL_SYMBOL_SEQUENCE_RE.fullmatch(token))


def _english_prose_tokens(value: str) -> list[str]:
    text = _strip_allowed_mixed_english_phrases(value)
    tokens: list[str] = []
    for token in ENGLISH_TOKEN_RE.findall(text):
        if _is_technical_token(token):
            continue
        if len(LATIN_LETTER_RE.findall(token)) < 2:
            continue
        if any(char.islower() for char in token):
            tokens.append(token)
    return tokens


def _english_prose_token_count(value: str) -> int:
    return len(_english_prose_tokens(value))


def _span_looks_like_english_prose(value: str) -> bool:
    prose_tokens = _english_prose_tokens(value)
    if len(prose_tokens) >= 3:
        return True
    if len(prose_tokens) == 1 and _contains_allowed_mixed_english_phrase(value):
        return True
    if len(prose_tokens) < 2:
        return False
    latin_letter_count = len(LATIN_LETTER_RE.findall(_strip_allowed_mixed_english_phrases(value)))
    return latin_letter_count >= 10


def _looks_like_english_prose(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if not CJK_RE.search(text):
        return _span_looks_like_english_prose(text)
    if any(_span_looks_like_english_prose(span) for span in LATIN_SPAN_RE.findall(text)):
        return True
    return _contains_allowed_mixed_english_phrase(text) and len(_english_prose_tokens(text)) == 1


def _preview(value: str, *, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", value.strip())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _iter_rendered_text_values(summary: dict[str, Any]):
    for field_name in RENDERED_TEXT_FIELDS:
        yield field_name, summary.get(field_name)

    workflow_steps = summary.get("workflow_steps")
    if isinstance(workflow_steps, list):
        for index, item in enumerate(workflow_steps):
            yield f"workflow_steps[{index}]", item
    else:
        yield "workflow_steps", workflow_steps

    for field_name in RENDERED_TEXT_LIST_FIELDS:
        items = summary.get(field_name, [])
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            yield f"{field_name}[{index}]", item

    method_modules = summary.get("method_modules", [])
    if isinstance(method_modules, list):
        for index, item in enumerate(method_modules):
            if not isinstance(item, dict):
                continue
            for key in RENDERED_METHOD_MODULE_FIELDS:
                yield f"method_modules[{index}].{key}", item.get(key)

    key_figures = summary.get("key_figures", [])
    if isinstance(key_figures, list):
        for index, item in enumerate(key_figures):
            if not isinstance(item, dict):
                continue
            for key in RENDERED_FIGURE_FIELDS:
                yield f"key_figures[{index}].{key}", item.get(key)
            if not str(item.get("analysis", "")).strip():
                yield f"key_figures[{index}].caption", item.get("caption")

    for field_name in ("author_stated_limitations", "inferred_limits"):
        items = summary.get(field_name, [])
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if isinstance(item, str):
                yield f"{field_name}[{index}]", item
            elif isinstance(item, dict):
                yield f"{field_name}[{index}].text", item.get("text")
                if field_name == "inferred_limits":
                    yield f"{field_name}[{index}].basis", item.get("basis")


def lint_summary(summary: dict[str, Any]) -> list[dict[str, str]]:
    """Return non-fatal summary issues that should be fixed before write-through."""
    issues: list[dict[str, str]] = []

    workflow_steps = summary.get("workflow_steps")
    if isinstance(workflow_steps, str) and "\n" not in workflow_steps and re.search(r"\b1\..*\b2\.", workflow_steps):
        issues.append(
            {
                "code": "workflow_steps_single_line_numbered_list",
                "message": "workflow_steps looks like a numbered list but has no line breaks",
            }
        )

    for claim_index, claim in enumerate(summary.get("evidence_summary", []) or []):
        if not isinstance(claim, dict):
            continue
        for evidence_index, evidence in enumerate(claim.get("evidence", []) or []):
            if not isinstance(evidence, dict):
                continue
            locator = str(evidence.get("locator", ""))
            if locator.startswith(SECONDARY_EVIDENCE_PREFIXES):
                issues.append(
                    {
                        "code": "secondary_context_used_as_evidence",
                        "message": f"evidence_summary[{claim_index}].evidence[{evidence_index}] cites secondary context",
                    }
                )
            elif locator and not is_canonical_trusted_locator(locator):
                issues.append(
                    {
                        "code": "malformed_trusted_evidence_locator",
                        "message": (
                            f"evidence_summary[{claim_index}].evidence[{evidence_index}] "
                            "has malformed trusted locator"
                        ),
                    }
                )

    for index, item in enumerate(summary.get("author_stated_limitations", []) or []):
        if isinstance(item, dict) and item.get("source_type") not in {"author_stated", None, ""}:
            issues.append(
                {
                    "code": "author_stated_limitation_source_type_invalid",
                    "message": f"author_stated_limitations[{index}] source_type must be author_stated",
                }
            )

    for index, item in enumerate(summary.get("inferred_limits", []) or []):
        if isinstance(item, dict) and item.get("source_type") not in {"inferred", None, ""}:
            issues.append(
                {
                    "code": "inferred_limit_source_type_invalid",
                    "message": f"inferred_limits[{index}] source_type must be inferred",
                }
            )

    for index, figure in enumerate(summary.get("key_figures", []) or []):
        if not isinstance(figure, dict):
            continue
        image_quality = str(figure.get("image_quality", ""))
        figure_quality_note = str(figure.get("figure_quality_note", "")).strip()
        if image_quality in LOW_QUALITY_IMAGE_VALUES and not figure_quality_note:
            issues.append(
                {
                    "code": "low_quality_figure_missing_quality_note",
                    "message": f"key_figures[{index}] has {image_quality} without figure_quality_note",
                }
            )

    for field_path, value in _iter_rendered_text_values(summary):
        if _looks_like_english_prose(value):
            issues.append(
                {
                    "code": "rendered_note_field_english_prose",
                    "message": (
                        f"{field_path} should use Chinese prose unless it is a proper noun/key: "
                        f"{_preview(str(value))}"
                    ),
                }
            )

    return issues
