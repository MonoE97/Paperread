from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

import fitz

from zotero_paperread import arxiv_source

CAPTION_PATTERN = re.compile(
    r"^\s*(figure|fig\.?|scheme)\s+([A-Za-z0-9]+)(?:\s*[\.:_-]\s*|\s*$)",
    re.IGNORECASE,
)

CAPTION_SCORE_PATTERNS = (
    (re.compile(r"\bpipeline\b"), 4.0),
    (re.compile(r"\bworkflow\b"), 3.0),
    (re.compile(r"\bframework\b"), 3.0),
    (re.compile(r"\boverview\b"), 2.0),
    (re.compile(r"\bcomparison\b"), 3.0),
    (re.compile(r"\bquantitative\b"), 3.0),
    (re.compile(r"\bablation\b"), 3.0),
    (re.compile(r"\bresults?\b"), 2.0),
)

SCIENTIFIC_FIGURE_SCORE_PATTERNS = (
    (re.compile(r"\bcapacitance\b"), 3.0),
    (re.compile(r"\bcharge (density|response)\b"), 3.0),
    (re.compile(r"\bconcentration distributions?\b"), 2.5),
    (re.compile(r"\bpmfs?\b"), 2.5),
    (re.compile(r"\bions?\b|\bcations?\b|\banions?\b"), 1.0),
)

CAPTION_PENALTY_PATTERNS = (
    (re.compile(r"\bqualitative\b"), -1.0),
    (re.compile(r"\bexamples?\b"), -0.5),
)
SOURCE_FILENAME_SCORE_PATTERNS = (
    (re.compile(r"\bmodel\b"), 4.0),
    (re.compile(r"\barchitecture\b"), 4.0),
    (re.compile(r"\bpipeline\b"), 4.0),
    (re.compile(r"\bworkflow\b"), 3.0),
    (re.compile(r"\bframework\b"), 3.0),
    (re.compile(r"\boverview\b"), 2.5),
    (re.compile(r"\bresults?\b"), 2.0),
    (re.compile(r"\bnovel\b"), 1.0),
)
SOURCE_FILENAME_PENALTY_PATTERNS = (
    (re.compile(r"\bstats?\b"), -3.0),
    (re.compile(r"\brmsd\b"), -2.5),
    (re.compile(r"\bforce\b"), -2.0),
    (re.compile(r"\bhf\b"), -1.5),
)

MAX_DIRECTIONAL_GAP = 160.0
CAPTION_BACKFILL_MAX_DIRECTIONAL_GAP = 72.0
CAPTION_BACKFILL_MIN_HORIZONTAL_OVERLAP_RATIO = 0.5
UNION_GAP_TOLERANCE = 24.0
STACKED_REGION_GAP_TOLERANCE = 48.0
CAPTION_BUFFER = 8.0
MIN_REGION_SIDE = 4.0
MAX_DEGENERATE_ASPECT = 30.0
LOW_CONFIDENCE_THRESHOLD = 0.5
QUALITY_MIN_WIDTH = 80
QUALITY_MIN_HEIGHT = 80
QUALITY_MIN_AREA = 10000
QUALITY_LOW_INFORMATION_RATIO = 0.001
QUALITY_LOW_INFORMATION_PIXELS = 64
QUALITY_SPARSE_CONTENT_BBOX_RATIO = 0.12
QUALITY_BACKGROUND_DELTA = 24
QUALITY_ALPHA_THRESHOLD = 8
QUALITY_ALPHA_DELTA = 24
CAPTION_CONTINUATION_START = re.compile(r"^\s*[\(\[]?[A-Za-z0-9]")
LABEL_ONLY_CAPTION_CONTINUATION_START = re.compile(r"^\s*[\(\[]?[A-Za-z0-9]")
CAPTION_TERMINAL_PUNCTUATION = (".", "!", "?", ":")
CAPTION_LABEL_ONLY_PATTERN = re.compile(
    r"^\s*(figure|fig\.?|scheme)\s+[A-Za-z0-9]+(?:\s*[\.:_-])?\s*$",
    re.IGNORECASE,
)
OBVIOUS_BODY_TEXT_START = re.compile(
    r"^\s*(this|these|we|our|here|it|in\s+this)\b",
    re.IGNORECASE,
)
SOURCE_PRIORITY = {
    "ocr-fallback": 5,
    "pdf-figure": 4,
    "arxiv-source": 3,
    "deterministic-pdf": 2,
    "embedded-image": 1,
}
ALLOWED_SOURCES = frozenset(SOURCE_PRIORITY)
FigureSource = Literal[
    "arxiv-source",
    "pdf-figure",
    "deterministic-pdf",
    "embedded-image",
    "ocr-fallback",
]


class FigureExtraction(TypedDict):
    figure_id: str
    caption: str
    caption_confidence: float
    caption_bbox: list[float]
    bbox: list[float]
    page: int
    area: float
    image_path: str
    priority_score: float
    source: FigureSource
    extraction_strategy: str
    extraction_confidence: float
    fallback_reason: str | None
    needs_fallback: bool
    visual_quality: NotRequired[dict[str, Any]]
    evidence_tier: NotRequired[str]
    evidence_tier_reason: NotRequired[str]


class CaptionBlock(TypedDict):
    caption: str
    rect: fitz.Rect


class CaptionBackfill(TypedDict):
    block: CaptionBlock
    confidence: float
    caption_index: int | None


class TextLine(TypedDict):
    text: str
    rect: fitz.Rect
    block_index: int
    line_index: int


def classify_figure_evidence_tier(figure: dict[str, Any]) -> dict[str, str]:
    """Classify whether figure analysis can rely on pixels, caption/text, or neither."""
    source = str(figure.get("source", ""))
    caption_confidence = float(figure.get("caption_confidence") or 0.0)
    visual_quality = figure.get("visual_quality") if isinstance(figure.get("visual_quality"), dict) else {}
    warnings = visual_quality.get("warnings", []) if isinstance(visual_quality, dict) else []
    warning_text = ",".join(str(item) for item in warnings)

    if any(item in {"image_too_small", "image_low_information", "image_unreadable"} for item in warnings):
        return {"tier": "not_usable", "reason": f"visual quality warning: {warning_text}"}

    if source in {"pdf-figure", "arxiv-source", "deterministic-pdf"} and caption_confidence >= 0.75:
        return {
            "tier": "pixel_verified",
            "reason": "source and caption confidence are strong enough for visual cross-checking",
        }

    if caption_confidence > 0 or source == "embedded-image":
        return {
            "tier": "caption_text_grounded",
            "reason": f"{source or 'unknown source'} requires text/caption-grounded analysis",
        }

    return {"tier": "not_usable", "reason": "missing caption and weak source provenance"}


def extract_figures(
    pdf_path: Path,
    output_dir: Path,
    top_k: int = 4,
    max_pages: int | None = None,
    *,
    arxiv_id: str | None = None,
    item_details: dict[str, Any] | None = None,
    enable_ocr_fallback: bool = False,
) -> dict[str, Any]:
    resolved = Path(pdf_path).expanduser()
    if not resolved.exists():
        raise FileNotFoundError(f"PDF not found: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"PDF path is not a file: {resolved}")

    output_root = Path(output_dir).expanduser()
    output_root.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    source_attempts: list[dict[str, Any]] = []
    resolved_arxiv_id = arxiv_id or arxiv_source.resolve_arxiv_id(
        item_details or {},
        pdf_path=resolved,
    )

    source_candidates = _collect_source_candidates(
        resolved_arxiv_id,
        output_root,
        source_attempts,
        warnings,
    )
    pdf_candidates = _extract_pdf_candidates(
        pdf_path=resolved,
        output_root=output_root,
        max_pages=max_pages,
    )

    ranking_pool = _dedupe_candidates(source_candidates + pdf_candidates)
    ranking_pool.sort(key=_ranking_key)

    selected = ranking_pool[: max(top_k, 0)]
    for item in selected:
        quality = assess_image_quality(Path(item["image_path"]))
        item["visual_quality"] = quality
        tier = classify_figure_evidence_tier(item)
        item["evidence_tier"] = tier["tier"]
        item["evidence_tier_reason"] = tier["reason"]
        for warning in quality["warnings"]:
            item["needs_fallback"] = True
            item["fallback_reason"] = item.get("fallback_reason") or "visual_quality"
            warnings.append(f"figure_visual_quality:{item['figure_id']}:{warning}")

    if enable_ocr_fallback and any(item["needs_fallback"] for item in selected):
        if not ocr_fallback_available():
            warnings.append("ocr_fallback_unavailable")

    return {
        "arxiv_id": resolved_arxiv_id,
        "pdf_path": str(resolved),
        "candidate_count": len(ranking_pool),
        "selected_figures": selected,
        "source_attempts": source_attempts,
        "warnings": _dedupe_strings(warnings),
    }


def ocr_fallback_available() -> bool:
    return False


def assess_image_quality(image_path: Path) -> dict[str, Any]:
    """Assess whether a rendered figure crop contains enough visual content."""
    try:
        pixmap = _load_quality_pixmap(Path(image_path).expanduser())
    except Exception:
        return {
            "status": "poor",
            "warnings": ["image_unreadable"],
            "width": 0,
            "height": 0,
            "content_ratio": 0.0,
            "content_bbox_area_ratio": 0.0,
            "content_pixels": 0,
            "content_bbox": [],
        }

    width = int(pixmap.width)
    height = int(pixmap.height)
    image_area = max(width * height, 0)
    x0, y0, x1, y1, content_pixels = _content_pixel_bbox(pixmap)

    if content_pixels > 0:
        content_width = max(x1 - x0 + 1, 0)
        content_height = max(y1 - y0 + 1, 0)
        content_bbox_area = content_width * content_height
        content_bbox = [x0, y0, x1, y1]
    else:
        content_bbox_area = 0
        content_bbox = []

    content_ratio = content_pixels / image_area if image_area > 0 else 0.0
    content_bbox_area_ratio = content_bbox_area / image_area if image_area > 0 else 0.0

    warnings: list[str] = []
    if width < QUALITY_MIN_WIDTH or height < QUALITY_MIN_HEIGHT or image_area < QUALITY_MIN_AREA:
        warnings.append("image_too_small")
    if (
        content_pixels < QUALITY_LOW_INFORMATION_PIXELS
        or content_ratio < QUALITY_LOW_INFORMATION_RATIO
    ):
        warnings.append("image_low_information")
    if (
        "image_too_small" not in warnings
        and "image_low_information" not in warnings
        and content_bbox_area_ratio < QUALITY_SPARSE_CONTENT_BBOX_RATIO
    ):
        warnings.append("image_content_area_too_sparse")

    warnings = _dedupe_strings(warnings)
    return {
        "status": "poor" if warnings else "ok",
        "warnings": warnings,
        "width": width,
        "height": height,
        "content_ratio": round(content_ratio, 6),
        "content_bbox_area_ratio": round(content_bbox_area_ratio, 6),
        "content_pixels": content_pixels,
        "content_bbox": content_bbox,
    }


def _load_quality_pixmap(image_path: Path) -> fitz.Pixmap:
    if image_path.suffix.lower() == ".pdf":
        with fitz.open(image_path) as doc:
            if doc.page_count < 1:
                raise ValueError("PDF has no pages")
            return doc.load_page(0).get_pixmap(alpha=False)
    return fitz.Pixmap(str(image_path))


def _content_pixel_bbox(pixmap: fitz.Pixmap) -> tuple[int, int, int, int, int]:
    width = int(pixmap.width)
    height = int(pixmap.height)
    if width <= 0 or height <= 0:
        return (0, 0, 0, 0, 0)

    stride = int(pixmap.n)
    has_alpha = bool(pixmap.alpha and stride > 1)
    color_components = stride - 1 if has_alpha else stride
    if stride <= 0 or color_components <= 0:
        return (0, 0, 0, 0, 0)

    samples = pixmap.samples
    background = _background_pixel(samples, width, height, stride)
    x0 = width
    y0 = height
    x1 = -1
    y1 = -1
    count = 0

    for y in range(height):
        row_offset = y * width * stride
        for x in range(width):
            offset = row_offset + (x * stride)
            if not _is_content_pixel(samples, offset, color_components, background, has_alpha):
                continue
            count += 1
            x0 = min(x0, x)
            y0 = min(y0, y)
            x1 = max(x1, x)
            y1 = max(y1, y)

    if count == 0:
        return (0, 0, 0, 0, 0)
    return (x0, y0, x1, y1, count)


def _background_pixel(
    samples: bytes,
    width: int,
    height: int,
    stride: int,
) -> tuple[int, ...]:
    positions = (
        (0, 0),
        (max(width - 1, 0), 0),
        (0, max(height - 1, 0)),
        (max(width - 1, 0), max(height - 1, 0)),
    )
    pixels = []
    for x, y in positions:
        offset = ((y * width) + x) * stride
        pixels.append(_pixel_tuple(samples, offset, stride))
    return tuple(sorted(values)[len(values) // 2] for values in zip(*pixels))


def _pixel_tuple(samples: bytes, offset: int, color_components: int) -> tuple[int, ...]:
    return tuple(int(value) for value in samples[offset : offset + color_components])


def _is_content_pixel(
    samples: bytes,
    offset: int,
    color_components: int,
    background: tuple[int, ...],
    has_alpha: bool,
) -> bool:
    if has_alpha:
        alpha = int(samples[offset + color_components])
        if alpha <= QUALITY_ALPHA_THRESHOLD:
            return False
        background_alpha = background[color_components]
        if abs(alpha - background_alpha) > QUALITY_ALPHA_DELTA:
            return True
    return (
        _pixel_delta(samples, offset, color_components, background[:color_components])
        > QUALITY_BACKGROUND_DELTA
    )


def _pixel_delta(
    samples: bytes,
    offset: int,
    color_components: int,
    background: tuple[int, ...],
) -> int:
    pixel = samples[offset : offset + color_components]
    return max(abs(int(value) - background[index]) for index, value in enumerate(pixel))


def _collect_source_candidates(
    arxiv_id: str | None,
    output_root: Path,
    source_attempts: list[dict[str, Any]],
    warnings: list[str],
) -> list[FigureExtraction]:
    if arxiv_id is None:
        source_attempts.append(
            {"stage": "resolve", "status": "skipped", "reason": "no_arxiv_id"}
        )
        return []

    source_attempts.append(
        {"stage": "resolve", "status": "resolved", "arxiv_id": arxiv_id}
    )

    try:
        source_root = arxiv_source.download_arxiv_source(
            arxiv_id,
            output_root / "arxiv-source",
        )
    except Exception:
        source_attempts.append(
            {"stage": "download", "status": "error", "arxiv_id": arxiv_id}
        )
        warnings.append("arxiv_source_download_failed")
        return []

    if source_root is None:
        source_attempts.append(
            {"stage": "download", "status": "download_failed", "arxiv_id": arxiv_id}
        )
        warnings.append("arxiv_source_download_failed")
        return []

    source_path = Path(source_root).expanduser()
    if not source_path.exists():
        source_attempts.append(
            {
                "stage": "download",
                "status": "missing_path",
                "arxiv_id": arxiv_id,
                "path": str(source_path),
            }
        )
        warnings.append("arxiv_source_path_missing")
        return []

    source_attempts.append(
        {
            "stage": "download",
            "status": "available",
            "arxiv_id": arxiv_id,
            "path": str(source_path),
        }
    )

    try:
        source_figures = arxiv_source.collect_source_figures(
            source_path,
            output_root / "source-figures",
        )
        rendered_pdf_figures = arxiv_source.render_source_figure_pdfs(
            source_figures,
            output_root / "source-rendered",
        )
    except Exception:
        source_attempts.append({"stage": "collect", "status": "error"})
        warnings.append("arxiv_source_collect_failed")
        return []

    source_attempts.append(
        {
            "stage": "collect",
            "status": "available",
            "raw_count": len(source_figures),
            "rendered_count": len(rendered_pdf_figures),
        }
    )

    candidates = [
        *_source_entries_to_candidates(source_figures),
        *_source_entries_to_candidates(rendered_pdf_figures),
    ]
    return _dedupe_source_candidates(candidates)


def _source_entries_to_candidates(entries: list[dict[str, Any]]) -> list[FigureExtraction]:
    candidates: list[FigureExtraction] = []
    for index, entry in enumerate(entries, start=1):
        media_type = str(entry.get("media_type", "image"))
        source = _normalize_source(str(entry.get("source", "arxiv-source")), media_type)

        image_path = Path(str(entry["image_path"]))
        if not image_path.exists():
            continue

        width, height = _image_dimensions(image_path)
        bbox = fitz.Rect(0, 0, width, height)
        caption = _source_caption(entry)
        priority = _source_priority_score(source, entry, caption, bbox)

        candidates.append(
            FigureExtraction(
                figure_id=f"source-{index}-{image_path.stem}",
                caption=caption,
                caption_confidence=_source_caption_confidence(entry, caption),
                caption_bbox=_rect_to_list(bbox),
                bbox=_rect_to_list(bbox),
                page=_normalize_page(entry.get("page")),
                area=round(float(bbox.get_area()), 2),
                image_path=str(image_path),
                priority_score=round(priority, 4),
                source=source,
                extraction_strategy="deterministic",
                extraction_confidence=0.98,
                fallback_reason=None,
                needs_fallback=False,
            )
        )
    return candidates


def _source_priority_score(
    source: FigureSource,
    entry: dict[str, Any],
    caption: str,
    bbox: fitz.Rect,
) -> float:
    explicit_caption = entry.get("caption")
    if isinstance(explicit_caption, str) and explicit_caption.strip():
        return round(_priority_score(caption, bbox), 4)

    label = caption.lower()
    score = 0.05 if source == "pdf-figure" else 0.0
    for pattern, weight in SOURCE_FILENAME_SCORE_PATTERNS:
        if pattern.search(label):
            score += weight
    for pattern, weight in SOURCE_FILENAME_PENALTY_PATTERNS:
        if pattern.search(label):
            score += weight

    if source == "pdf-figure":
        score += min(bbox.get_area() / 1000000.0, 0.05)
    elif score > 0:
        score += min(bbox.get_area() / 2000000.0, 0.03)
    return round(score, 4)


def _dedupe_source_candidates(
    candidates: list[FigureExtraction],
) -> list[FigureExtraction]:
    deduped: dict[str, FigureExtraction] = {}
    for candidate in candidates:
        key = Path(candidate["image_path"]).stem
        current = deduped.get(key)
        if current is None or _prefer_source_candidate(candidate, current):
            deduped[key] = candidate
    return list(deduped.values())


def _dedupe_candidates(
    candidates: list[FigureExtraction],
) -> list[FigureExtraction]:
    deduped: dict[tuple[Any, ...], FigureExtraction] = {}
    for candidate in candidates:
        key = _candidate_identity_key(candidate)
        current = deduped.get(key)
        if current is None or _prefer_candidate(candidate, current):
            deduped[key] = candidate
    return list(deduped.values())


def _extract_pdf_candidates(
    pdf_path: Path,
    output_root: Path,
    max_pages: int | None,
) -> list[FigureExtraction]:
    with fitz.open(pdf_path) as doc:
        page_limit = doc.page_count if max_pages is None else min(doc.page_count, max_pages)
        results: list[FigureExtraction] = []
        for page_index in range(page_limit):
            page = doc.load_page(page_index)
            captions = _detect_captions(page)
            vector_regions = _detect_graphic_regions(page)
            image_regions = _detect_embedded_image_regions(page)
            used_regions: list[fitz.Rect] = []
            claimed_caption_indices: set[int] = set()

            for caption_index, caption in enumerate(captions):
                bbox = _select_owned_bbox(
                    vector_regions,
                    captions,
                    caption_index,
                    page.rect,
                )
                if bbox is None:
                    continue
                used_regions.append(bbox)
                claimed_caption_indices.add(caption_index)
                results.append(
                    _rasterize_figure(
                        page=page,
                        page_number=page_index + 1,
                        figure_index=caption_index + 1,
                        caption=caption,
                        bbox=bbox,
                        output_root=output_root,
                        source="deterministic-pdf",
                    )
                )

            results.extend(
                _supplement_embedded_images(
                    page=page,
                    page_number=page_index + 1,
                    captions=captions,
                    image_regions=image_regions,
                    claimed_caption_indices=claimed_caption_indices,
                    used_regions=used_regions,
                    output_root=output_root,
                )
            )

    return results


def _supplement_embedded_images(
    page: fitz.Page,
    page_number: int,
    captions: list[CaptionBlock],
    image_regions: list[fitz.Rect],
    claimed_caption_indices: set[int],
    used_regions: list[fitz.Rect],
    output_root: Path,
) -> list[FigureExtraction]:
    supplements: list[FigureExtraction] = []
    supplement_index = 1
    for region in image_regions:
        if any(_rect_overlap_ratio(region, used) > 0.8 for used in used_regions):
            continue
        backfill = _backfill_caption_for_region(
            region,
            captions,
            claimed_caption_indices=claimed_caption_indices,
        )
        supplements.append(
            _rasterize_figure(
                page=page,
                page_number=page_number,
                figure_index=1000 + supplement_index,
                caption=backfill["block"],
                bbox=region,
                output_root=output_root,
                source="embedded-image",
                caption_confidence=backfill["confidence"],
            )
        )
        if backfill["caption_index"] is not None:
            claimed_caption_indices.add(backfill["caption_index"])
        supplement_index += 1
    return supplements


def _detect_captions(page: fitz.Page) -> list[CaptionBlock]:
    captions: list[CaptionBlock] = []
    lines = _text_lines(page)
    line_index = 0
    while line_index < len(lines):
        line = lines[line_index]
        line_text = line["text"]
        if CAPTION_PATTERN.match(line_text) is None:
            line_index += 1
            continue

        caption_lines = [line_text]
        caption_rect = fitz.Rect(line["rect"])
        next_index = line_index + 1

        while next_index < len(lines):
            next_line = lines[next_index]
            next_text = next_line["text"]
            next_rect = next_line["rect"]
            if CAPTION_PATTERN.match(next_text) is not None:
                break
            if not _is_vertical_caption_neighbor(caption_rect, next_rect):
                break
            if not _same_caption_neighborhood(caption_rect, next_rect):
                break
            if not _is_strong_caption_continuation(caption_lines[-1], next_text):
                break

            caption_lines.append(next_text)
            caption_rect.include_rect(next_rect)
            next_index += 1

        captions.append(
            CaptionBlock(
                caption=" ".join(caption_lines),
                rect=caption_rect,
            )
        )
        line_index = next_index

    captions.sort(key=lambda item: item["rect"].y0)
    return captions


def _detect_graphic_regions(page: fitz.Page) -> list[fitz.Rect]:
    regions: list[fitz.Rect] = []

    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is None:
            continue
        candidate = fitz.Rect(rect)
        if _reject_graphic_region(candidate):
            continue
        regions.append(candidate)

    return _dedupe_regions(regions)


def _detect_embedded_image_regions(page: fitz.Page) -> list[fitz.Rect]:
    regions: list[fitz.Rect] = []

    for image in page.get_images(full=True):
        xref = image[0]
        for rect in page.get_image_rects(xref, transform=False):
            candidate = fitz.Rect(rect)
            if _reject_graphic_region(candidate):
                continue
            regions.append(candidate)

    return _dedupe_regions(regions)


def _reject_graphic_region(candidate: fitz.Rect) -> bool:
    if candidate.is_empty or candidate.get_area() <= 1:
        return True
    width = max(candidate.width, 0.0)
    height = max(candidate.height, 0.0)
    min_side = min(width, height)
    max_side = max(width, height)
    if min_side < MIN_REGION_SIDE:
        return True
    aspect = max_side / max(min_side, 1.0)
    return aspect >= MAX_DEGENERATE_ASPECT and min_side <= CAPTION_BUFFER


def _select_owned_bbox(
    graphic_regions: list[fitz.Rect],
    captions: list[CaptionBlock],
    caption_index: int,
    page_rect: fitz.Rect,
) -> fitz.Rect | None:
    x0, x1 = _ownership_bounds(captions, caption_index, page_rect)
    for direction in ("up", "down"):
        nearby = _nearby_regions(graphic_regions, captions, caption_index, direction)
        if not nearby:
            continue

        anchor_gap = min(item[0] for item in nearby)
        owned_regions = [
            region
            for gap, region in nearby
            if gap <= anchor_gap + UNION_GAP_TOLERANCE
            and _region_center_x(region) >= x0
            and _region_center_x(region) <= x1
        ]
        if not owned_regions:
            continue
        return _expand_owned_regions(
            graphic_regions,
            owned_regions,
            captions,
            x0,
            x1,
        )
    return None


def _nearby_regions(
    graphic_regions: list[fitz.Rect],
    captions: list[CaptionBlock],
    caption_index: int,
    direction: Literal["up", "down"],
) -> list[tuple[float, fitz.Rect]]:
    caption_rect = captions[caption_index]["rect"]
    matches: list[tuple[float, fitz.Rect]] = []

    for region in graphic_regions:
        gap = _directional_gap(region, caption_rect, direction)
        if gap < 0 or gap > MAX_DIRECTIONAL_GAP:
            continue
        if _intersects_other_caption(region, captions, caption_index):
            continue
        matches.append((gap, region))

    matches.sort(
        key=lambda item: (
            item[0],
            -_horizontal_overlap(caption_rect, item[1]),
            -item[1].get_area(),
        )
    )
    return matches


def _directional_gap(
    region: fitz.Rect,
    caption_rect: fitz.Rect,
    direction: Literal["up", "down"],
) -> float:
    if direction == "up":
        return caption_rect.y0 - region.y1
    return region.y0 - caption_rect.y1


def _rasterize_figure(
    page: fitz.Page,
    page_number: int,
    figure_index: int,
    caption: CaptionBlock,
    bbox: fitz.Rect,
    output_root: Path,
    source: str,
    caption_confidence: float | None = None,
) -> FigureExtraction:
    image_path = output_root / f"figure-p{page_number}-{figure_index}.png"
    pixmap = page.get_pixmap(clip=bbox, dpi=144, alpha=False)
    pixmap.save(image_path)

    confidence, fallback_reason = _geometry_confidence(bbox)
    priority_score = _priority_score(caption["caption"], bbox)
    if source == "embedded-image" and caption["caption"] == "":
        priority_score = 0.05

    return FigureExtraction(
        figure_id=f"p{page_number}-f{figure_index}",
        caption=caption["caption"],
        caption_confidence=_caption_confidence(source, caption, caption_confidence),
        caption_bbox=_rect_to_list(caption["rect"]),
        bbox=_rect_to_list(bbox),
        page=max(page_number, 1),
        area=round(float(bbox.get_area()), 2),
        image_path=str(image_path),
        priority_score=round(priority_score, 4),
        source=_normalize_source(source),
        extraction_strategy="deterministic",
        extraction_confidence=confidence,
        fallback_reason=fallback_reason,
        needs_fallback=confidence < LOW_CONFIDENCE_THRESHOLD,
    )


def _geometry_confidence(bbox: fitz.Rect) -> tuple[float, str | None]:
    width = max(float(bbox.width), 0.0)
    height = max(float(bbox.height), 0.0)
    area = float(bbox.get_area())
    min_side = min(width, height)
    max_side = max(width, height)
    aspect = max_side / max(min_side, 1.0)

    if area < 4000 or min_side < 24 or aspect > 10:
        return (0.35, "low_confidence_geometry")
    score = 0.6 + min(area / 50000.0, 0.35)
    return (round(min(score, 0.99), 4), None)


def _horizontal_overlap(left: fitz.Rect, right: fitz.Rect) -> float:
    return max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))


def _is_strong_caption_continuation(current_text: str, next_text: str) -> bool:
    is_label_only = CAPTION_LABEL_ONLY_PATTERN.match(current_text) is not None
    if OBVIOUS_BODY_TEXT_START.match(next_text) is not None:
        return False
    if (
        current_text.rstrip().endswith(CAPTION_TERMINAL_PUNCTUATION)
        and not is_label_only
    ):
        return False
    if is_label_only:
        return LABEL_ONLY_CAPTION_CONTINUATION_START.match(next_text) is not None
    return CAPTION_CONTINUATION_START.match(next_text) is not None


def _intersects_other_caption(
    region: fitz.Rect,
    captions: list[CaptionBlock],
    caption_index: int,
) -> bool:
    expanded = fitz.Rect(region)
    expanded.y0 -= CAPTION_BUFFER
    expanded.y1 += CAPTION_BUFFER
    for index, caption in enumerate(captions):
        if index == caption_index:
            continue
        if expanded.intersects(caption["rect"]):
            return True
    return False


def _dedupe_regions(regions: list[fitz.Rect]) -> list[fitz.Rect]:
    deduped: list[fitz.Rect] = []
    seen: set[tuple[float, float, float, float]] = set()
    for region in regions:
        key = (
            round(float(region.x0), 2),
            round(float(region.y0), 2),
            round(float(region.x1), 2),
            round(float(region.y1), 2),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(region)
    return deduped


def _dedupe_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _line_text(line: dict) -> str:
    spans = line.get("spans", [])
    text = "".join(str(span.get("text", "")) for span in spans)
    return " ".join(text.split())


def _text_lines(page: fitz.Page) -> list[TextLine]:
    text_dict = page.get_text("dict")
    lines: list[TextLine] = []
    for block_index, block in enumerate(text_dict.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            text = _line_text(line)
            if not text:
                continue
            lines.append(
                TextLine(
                    text=text,
                    rect=fitz.Rect(line["bbox"]),
                    block_index=block_index,
                    line_index=line_index,
                )
            )

    lines.sort(
        key=lambda item: (
            item["rect"].y0,
            item["rect"].x0,
            item["block_index"],
            item["line_index"],
        )
    )
    return lines


def _is_vertical_caption_neighbor(current_rect: fitz.Rect, next_rect: fitz.Rect) -> bool:
    gap = _directional_gap(next_rect, current_rect, "down")
    max_gap = max(
        CAPTION_BUFFER,
        min(current_rect.height, next_rect.height) * 0.6,
    )
    return gap >= 0 and gap <= max_gap


def _same_caption_neighborhood(current_rect: fitz.Rect, next_rect: fitz.Rect) -> bool:
    if _horizontal_overlap(current_rect, next_rect) > 0:
        return True
    return abs(current_rect.x0 - next_rect.x0) <= CAPTION_BUFFER * 4


def _ownership_bounds(
    captions: list[CaptionBlock],
    caption_index: int,
    page_rect: fitz.Rect,
) -> tuple[float, float]:
    target = captions[caption_index]["rect"]
    row_captions = sorted(
        (
            caption["rect"]
            for caption in captions
            if _same_caption_row(target, caption["rect"])
        ),
        key=lambda rect: (rect.x0, rect.y0),
    )
    row_index = next(index for index, rect in enumerate(row_captions) if rect == target)

    left = page_rect.x0
    right = page_rect.x1
    if row_index > 0:
        left_neighbor = row_captions[row_index - 1]
        left = (_region_center_x(left_neighbor) + _region_center_x(target)) / 2.0
    if row_index + 1 < len(row_captions):
        right_neighbor = row_captions[row_index + 1]
        right = (_region_center_x(target) + _region_center_x(right_neighbor)) / 2.0
    return left, right


def _same_caption_row(left: fitz.Rect, right: fitz.Rect) -> bool:
    return max(left.y0, right.y0) <= min(left.y1, right.y1)


def _region_center_x(region: fitz.Rect) -> float:
    return (region.x0 + region.x1) / 2.0


def _expand_owned_regions(
    graphic_regions: list[fitz.Rect],
    owned_regions: list[fitz.Rect],
    captions: list[CaptionBlock],
    x0: float,
    x1: float,
) -> fitz.Rect:
    expanded = list(owned_regions)
    merged = _union_rects(expanded)

    while True:
        next_region = None
        for region in graphic_regions:
            if region in expanded:
                continue
            if _region_center_x(region) < x0 or _region_center_x(region) > x1:
                continue
            if _horizontal_overlap(merged, region) <= 0:
                continue
            if _vertical_separation(merged, region) > STACKED_REGION_GAP_TOLERANCE:
                continue
            if _has_intervening_caption(merged, region, captions):
                continue
            next_region = region
            break

        if next_region is None:
            return merged

        expanded.append(next_region)
        merged.include_rect(next_region)


def _vertical_separation(top: fitz.Rect, bottom: fitz.Rect) -> float:
    if top.y1 < bottom.y0:
        return bottom.y0 - top.y1
    if bottom.y1 < top.y0:
        return top.y0 - bottom.y1
    return 0.0


def _has_intervening_caption(
    merged: fitz.Rect,
    region: fitz.Rect,
    captions: list[CaptionBlock],
) -> bool:
    overlap = _horizontal_overlap(merged, region)
    if overlap <= 0:
        return False

    upper, lower = sorted((merged, region), key=lambda rect: rect.y0)
    corridor = fitz.Rect(
        max(upper.x0, lower.x0),
        upper.y1 - CAPTION_BUFFER,
        min(upper.x1, lower.x1),
        lower.y0 + CAPTION_BUFFER,
    )
    if corridor.is_empty:
        return False

    for caption in captions:
        if corridor.intersects(caption["rect"]):
            return True
    return False


def _union_rects(regions: list[fitz.Rect]) -> fitz.Rect:
    merged = fitz.Rect(regions[0])
    for region in regions[1:]:
        merged.include_rect(region)
    return merged


def _priority_score(caption: str, bbox: fitz.Rect) -> float:
    lowered = caption.lower()
    score = 0.0

    for pattern, weight in CAPTION_SCORE_PATTERNS:
        if pattern.search(lowered):
            score += weight

    score += _scientific_caption_score(lowered)

    for pattern, weight in CAPTION_PENALTY_PATTERNS:
        if pattern.search(lowered):
            score += weight

    score += min(bbox.get_area() / 100000.0, 2.0)
    return round(score, 4)


def _scientific_caption_score(caption: str) -> float:
    return sum(weight for pattern, weight in SCIENTIFIC_FIGURE_SCORE_PATTERNS if pattern.search(caption))


def _normalize_page(value: Any) -> int:
    if isinstance(value, int):
        return max(value, 1)
    if isinstance(value, str):
        try:
            return max(int(value), 1)
        except ValueError:
            return 1
    return 1


def _normalize_source(source: str, media_type: str | None = None) -> FigureSource:
    if media_type == "pdf":
        return "pdf-figure"
    if source in ALLOWED_SOURCES:
        return source  # type: ignore[return-value]
    return "arxiv-source"


def _prefer_source_candidate(
    candidate: FigureExtraction,
    current: FigureExtraction,
) -> bool:
    candidate_is_raster = _is_raster_safe_path(candidate["image_path"])
    current_is_raster = _is_raster_safe_path(current["image_path"])
    if candidate_is_raster != current_is_raster:
        return candidate_is_raster

    candidate_key = _ranking_key(candidate)
    current_key = _ranking_key(current)
    if candidate_key != current_key:
        return candidate_key < current_key
    return False


def _prefer_candidate(
    candidate: FigureExtraction,
    current: FigureExtraction,
) -> bool:
    candidate_source_priority = SOURCE_PRIORITY.get(candidate["source"], 0)
    current_source_priority = SOURCE_PRIORITY.get(current["source"], 0)
    if candidate_source_priority != current_source_priority:
        return candidate_source_priority > current_source_priority

    candidate_is_raster = _is_raster_safe_path(candidate["image_path"])
    current_is_raster = _is_raster_safe_path(current["image_path"])
    if candidate_is_raster != current_is_raster:
        return candidate_is_raster

    candidate_key = _ranking_key(candidate)
    current_key = _ranking_key(current)
    if candidate_key != current_key:
        return candidate_key < current_key
    return False


def _is_raster_safe_path(image_path: str) -> bool:
    return Path(image_path).suffix.lower() != ".pdf"


def _candidate_identity_key(item: FigureExtraction) -> tuple[Any, ...]:
    caption_key = _normalized_caption_key(item["caption"])
    if caption_key is not None:
        return ("caption", item["page"], caption_key)
    bbox = item["bbox"]
    return (
        "geometry",
        item["page"],
        round(float(bbox[0]), 1),
        round(float(bbox[1]), 1),
        round(float(bbox[2]), 1),
        round(float(bbox[3]), 1),
    )


def _normalized_caption_key(caption: str) -> str | None:
    normalized = " ".join(caption.lower().split())
    if normalized == "":
        return None
    normalized = re.sub(r"[^\w]+", " ", normalized).strip()
    return normalized or None


def _ranking_key(item: FigureExtraction) -> tuple[float, float, float, float]:
    return (
        -item["priority_score"],
        -SOURCE_PRIORITY.get(item["source"], 0),
        item["page"],
        item["caption_bbox"][1],
    )


def _image_dimensions(path: Path) -> tuple[float, float]:
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as doc:
            page = doc.load_page(0)
            rect = page.rect
            return float(rect.width), float(rect.height)
    pixmap = fitz.Pixmap(str(path))
    return float(pixmap.width), float(pixmap.height)


def _source_caption(entry: dict[str, Any]) -> str:
    caption = entry.get("caption")
    if isinstance(caption, str):
        return caption.strip()
    rel_path = entry.get("rel_path")
    if isinstance(rel_path, str):
        return Path(rel_path).stem.replace("_", " ")
    return ""


def _source_caption_confidence(entry: dict[str, Any], caption: str) -> float:
    if caption == "":
        return 0.0
    explicit_caption = entry.get("caption")
    if isinstance(explicit_caption, str) and explicit_caption.strip():
        return 0.98
    return 0.2


def _backfill_caption_for_region(
    region: fitz.Rect,
    captions: list[CaptionBlock],
    *,
    claimed_caption_indices: set[int] | None = None,
) -> CaptionBackfill:
    if not captions:
        return CaptionBackfill(
            block=CaptionBlock(caption="", rect=region),
            confidence=0.0,
            caption_index=None,
        )

    matches = [
        _caption_backfill_match(region, caption_index, caption)
        for caption_index, caption in enumerate(captions)
        if claimed_caption_indices is None or caption_index not in claimed_caption_indices
    ]
    valid_matches = [match for match in matches if match is not None]
    if len(valid_matches) != 1:
        return CaptionBackfill(
            block=CaptionBlock(caption="", rect=region),
            confidence=0.0,
            caption_index=None,
        )

    matched = valid_matches[0]
    return CaptionBackfill(
        block=CaptionBlock(
            caption=matched["caption"]["caption"],
            rect=fitz.Rect(matched["caption"]["rect"]),
        ),
        confidence=matched["confidence"],
        caption_index=matched["caption_index"],
    )


def _caption_backfill_match(
    region: fitz.Rect,
    caption_index: int,
    caption: CaptionBlock,
) -> dict[str, Any] | None:
    direction_gap = _caption_region_directional_gap(caption["rect"], region)
    if direction_gap is None or direction_gap > CAPTION_BACKFILL_MAX_DIRECTIONAL_GAP:
        return None

    overlap_ratio = _caption_region_horizontal_overlap_ratio(caption["rect"], region)
    if overlap_ratio < CAPTION_BACKFILL_MIN_HORIZONTAL_OVERLAP_RATIO:
        return None

    gap_score = 1.0 - (direction_gap / CAPTION_BACKFILL_MAX_DIRECTIONAL_GAP)
    confidence = round(0.2 + (0.2 * overlap_ratio) + (0.2 * gap_score), 4)
    return {
        "caption": caption,
        "caption_index": caption_index,
        "confidence": confidence,
    }


def _caption_region_directional_gap(
    caption_rect: fitz.Rect,
    region: fitz.Rect,
) -> float | None:
    if caption_rect.y1 <= region.y0:
        return region.y0 - caption_rect.y1
    if region.y1 <= caption_rect.y0:
        return caption_rect.y0 - region.y1
    return None


def _caption_region_horizontal_overlap_ratio(
    caption_rect: fitz.Rect,
    region: fitz.Rect,
) -> float:
    overlap = _horizontal_overlap(caption_rect, region)
    min_width = min(caption_rect.width, region.width)
    if min_width <= 0:
        return 0.0
    return overlap / min_width


def _caption_region_distance(caption_rect: fitz.Rect, region: fitz.Rect) -> float:
    directional_gap = _caption_region_directional_gap(caption_rect, region)
    if directional_gap is None:
        return 0.0
    return directional_gap


def _caption_confidence(
    source: str,
    caption: CaptionBlock,
    explicit_confidence: float | None = None,
) -> float:
    if explicit_confidence is not None:
        return explicit_confidence
    if caption["caption"] == "":
        return 0.0
    if source == "embedded-image":
        return 0.0
    return 0.95


def _rect_overlap_ratio(left: fitz.Rect, right: fitz.Rect) -> float:
    intersection = fitz.Rect(left)
    intersection.intersect(right)
    if intersection.is_empty or left.get_area() <= 0:
        return 0.0
    return float(intersection.get_area()) / float(left.get_area())


def _rect_to_list(rect: fitz.Rect) -> list[float]:
    return [
        round(float(rect.x0), 2),
        round(float(rect.y0), 2),
        round(float(rect.x1), 2),
        round(float(rect.y1), 2),
    ]
