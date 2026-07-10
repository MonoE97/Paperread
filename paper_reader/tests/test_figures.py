from pathlib import Path
import base64

import fitz
import pytest

from paper_reader.figures import (
    _detect_captions,
    assess_image_quality,
    classify_figure_evidence_tier,
    extract_figures,
)
from paper_reader.workflow import build_figure_context_markdown


def _selected(payload: dict) -> list[dict]:
    return payload["selected_figures"]


def test_classify_figure_evidence_tier_marks_source_pdf_as_pixel_verified() -> None:
    figure = {
        "source": "pdf-figure",
        "caption_confidence": 0.9,
        "visual_quality": {"status": "ok", "warnings": []},
    }

    assert classify_figure_evidence_tier(figure)["tier"] == "pixel_verified"


def test_classify_figure_evidence_tier_marks_embedded_low_caption_as_text_grounded() -> None:
    figure = {
        "source": "embedded-image",
        "caption_confidence": 0.56,
        "visual_quality": {"status": "ok", "warnings": []},
    }

    result = classify_figure_evidence_tier(figure)

    assert result["tier"] == "caption_text_grounded"
    assert "embedded-image" in result["reason"]


def test_classify_figure_evidence_tier_marks_tiny_image_not_usable() -> None:
    figure = {
        "source": "embedded-image",
        "caption_confidence": 0.2,
        "visual_quality": {"status": "poor", "warnings": ["image_too_small"]},
    }

    result = classify_figure_evidence_tier(figure)

    assert result["tier"] == "not_usable"
    assert "image_too_small" in result["reason"]


def make_captioned_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=400, height=640)

    page.insert_textbox(
        fitz.Rect(40, 30, 360, 70),
        "Introductory body text that should not be captured in the figure crop.",
        fontsize=11,
    )
    page.draw_rect(fitz.Rect(60, 100, 240, 190), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
    page.insert_text(
        (60, 215),
        "Figure 1. Proposed pipeline for the full workflow.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_ranking_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=760)

    page.draw_rect(fitz.Rect(40, 40, 260, 150), color=(0, 0, 0), fill=(0.9, 0.7, 0.7))
    page.insert_text(
        (40, 175),
        "Figure 1. Qualitative examples from three cases.",
        fontsize=12,
    )

    page.draw_rect(fitz.Rect(40, 240, 260, 350), color=(0, 0, 0), fill=(0.7, 0.9, 0.7))
    page.insert_text(
        (40, 375),
        "Figure 2. Overview of the pipeline and workflow framework.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_charge_response_like_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=560, height=720)
    page.draw_rect(fitz.Rect(40, 40, 510, 210), color=(0, 0, 0), fill=(0.96, 0.96, 0.96))
    page.insert_textbox(
        fitz.Rect(40, 230, 510, 270),
        "Figure 1. Overview results from representative configurations.",
        fontsize=12,
    )
    page.draw_rect(fitz.Rect(40, 300, 510, 470), color=(0, 0, 0), fill=(0.96, 0.96, 0.96))
    page.insert_textbox(
        fitz.Rect(40, 490, 510, 530),
        "Figure 2. Comparison results from repeated simulations.",
        fontsize=12,
    )
    page.draw_rect(fitz.Rect(55, 545, 245, 650), color=(0, 0, 0), fill=(0.95, 0.95, 0.95))
    page.draw_rect(fitz.Rect(285, 545, 475, 650), color=(0, 0, 0), fill=(0.95, 0.95, 0.95))
    for x0 in (80, 310):
        page.draw_line(fitz.Point(x0, 625), fitz.Point(x0 + 135, 565), color=(0.2, 0.2, 0.8), width=2)
        page.draw_line(fitz.Point(x0, 625), fitz.Point(x0 + 135, 600), color=(0.8, 0.2, 0.2), width=2)
    page.insert_textbox(
        fitz.Rect(55, 665, 475, 715),
        "Figure 3. (a) Response of charge density averaged over the xy plane and (b) the cell potential-dependent differential capacitance.",
        fontsize=12,
    )
    doc.save(path)
    doc.close()


def make_ion_distribution_like_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=560, height=820)
    page.draw_rect(fitz.Rect(40, 40, 510, 210), color=(0, 0, 0), fill=(0.96, 0.96, 0.96))
    page.insert_textbox(
        fitz.Rect(40, 230, 510, 270),
        "Figure 1. Overview results from representative configurations.",
        fontsize=12,
    )
    page.draw_rect(fitz.Rect(40, 300, 510, 470), color=(0, 0, 0), fill=(0.96, 0.96, 0.96))
    page.insert_textbox(
        fitz.Rect(40, 490, 510, 530),
        "Figure 2. Comparison results from repeated simulations.",
        fontsize=12,
    )
    panel_rects = [
        fitz.Rect(45, 550, 235, 625),
        fitz.Rect(285, 550, 475, 625),
        fitz.Rect(45, 640, 235, 715),
        fitz.Rect(285, 640, 475, 715),
    ]
    for rect in panel_rects:
        page.draw_rect(rect, color=(0, 0, 0), fill=(0.96, 0.96, 0.96))
        page.draw_line(
            fitz.Point(rect.x0 + 20, rect.y1 - 20),
            fitz.Point(rect.x1 - 20, rect.y0 + 25),
            color=(0.1, 0.5, 0.1),
            width=2,
        )
        page.draw_line(
            fitz.Point(rect.x0 + 20, rect.y1 - 30),
            fitz.Point(rect.x1 - 20, rect.y0 + 60),
            color=(0.7, 0.2, 0.2),
            width=2,
        )
    page.insert_textbox(
        fitz.Rect(45, 725, 475, 805),
        "Figure 4. Concentration distributions and PMFs of cations (Na+) and anions (Cl-) as a function of the distance to the electrode.",
        fontsize=12,
    )
    doc.save(path)
    doc.close()


def make_multi_panel_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=320)

    page.draw_rect(fitz.Rect(40, 40, 150, 120), color=(0, 0, 0), fill=(0.9, 0.7, 0.7))
    page.draw_rect(fitz.Rect(190, 45, 320, 130), color=(0, 0, 0), fill=(0.7, 0.9, 0.7))
    page.insert_text(
        (40, 160),
        "Figure 3. Multi-panel pipeline overview.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_stacked_figure_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=420)

    page.draw_rect(fitz.Rect(80, 40, 240, 100), color=(0, 0, 0), fill=(0.9, 0.8, 0.7))
    page.draw_rect(fitz.Rect(80, 130, 240, 210), color=(0, 0, 0), fill=(0.7, 0.8, 0.9))
    page.insert_text(
        (80, 250),
        "Figure 7. Two-layer stacked result.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_stacked_figures_with_individual_captions_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=320)

    page.draw_rect(fitz.Rect(80, 40, 240, 100), color=(0, 0, 0), fill=(0.9, 0.8, 0.7))
    page.insert_text(
        (80, 118),
        "Figure 1. Top stacked figure.",
        fontsize=12,
    )

    page.draw_rect(fitz.Rect(80, 132, 240, 192), color=(0, 0, 0), fill=(0.7, 0.8, 0.9))
    page.insert_text(
        (80, 210),
        "Figure 2. Bottom stacked figure.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_raster_image_pdf(path: Path) -> None:
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
    )
    doc = fitz.open()
    page = doc.new_page(width=320, height=260)
    page.insert_image(fitz.Rect(70, 60, 210, 150), stream=png_bytes)
    page.insert_text(
        (70, 180),
        "Fig. 4. Raster result comparison.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_raster_image_with_far_caption_pdf(path: Path) -> None:
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
    )
    doc = fitz.open()
    page = doc.new_page(width=360, height=420)
    page.insert_image(fitz.Rect(70, 60, 210, 150), stream=png_bytes)
    page.insert_text(
        (70, 320),
        "Figure 12. Caption is too far away to backfill.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_embedded_image_with_multiple_nearby_captions_pdf(path: Path) -> None:
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
    )
    doc = fitz.open()
    page = doc.new_page(width=500, height=260)
    page.insert_image(fitz.Rect(110, 50, 390, 140), stream=png_bytes)
    page.insert_textbox(
        fitz.Rect(90, 165, 240, 205),
        "Figure 1. Left caption.",
        fontsize=12,
    )
    page.insert_textbox(
        fitz.Rect(260, 165, 410, 205),
        "Figure 2. Right caption.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_mixed_deterministic_and_captionless_embedded_pdf(path: Path) -> None:
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
    )
    doc = fitz.open()
    page = doc.new_page(width=520, height=280)
    page.draw_rect(fitz.Rect(40, 50, 200, 140), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
    page.insert_textbox(
        fitz.Rect(40, 165, 220, 205),
        "Figure 3. Left deterministic figure.",
        fontsize=12,
    )
    page.insert_image(fitz.Rect(300, 50, 460, 140), stream=png_bytes)

    doc.save(path)
    doc.close()


def make_claimed_caption_above_embedded_image_pdf(path: Path) -> None:
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
    )
    doc = fitz.open()
    page = doc.new_page(width=420, height=360)
    page.draw_rect(fitz.Rect(80, 40, 260, 120), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
    page.insert_textbox(
        fitz.Rect(80, 140, 320, 170),
        "Figure 8. Upper deterministic figure.",
        fontsize=12,
    )
    page.insert_image(fitz.Rect(80, 190, 260, 270), stream=png_bytes)

    doc.save(path)
    doc.close()


def make_embedded_images_sharing_single_nearby_caption_pdf(path: Path) -> None:
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
    )
    doc = fitz.open()
    page = doc.new_page(width=420, height=360)
    page.insert_image(fitz.Rect(100, 40, 280, 110), stream=png_bytes)
    page.insert_text(
        (100, 150),
        "Figure 11. Shared caption.",
        fontsize=12,
    )
    page.insert_image(fitz.Rect(100, 180, 280, 250), stream=png_bytes)

    doc.save(path)
    doc.close()


def make_fig_without_period_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=320, height=260)
    page.draw_rect(fitz.Rect(50, 60, 210, 150), color=(0, 0, 0), fill=(0.8, 0.8, 0.6))
    page.insert_text(
        (50, 180),
        "Fig 5. Raster result comparison without period.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_side_by_side_captions_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=500, height=260)
    page.insert_textbox(
        fitz.Rect(40, 160, 220, 200),
        "Figure 1. Left caption.",
        fontsize=12,
    )
    page.insert_textbox(
        fitz.Rect(280, 160, 460, 200),
        "Figure 2. Right caption.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_side_by_side_figures_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=500, height=320)
    page.draw_rect(fitz.Rect(40, 60, 200, 150), color=(0, 0, 0), fill=(0.9, 0.7, 0.7))
    page.draw_rect(fitz.Rect(280, 60, 440, 150), color=(0, 0, 0), fill=(0.7, 0.9, 0.7))
    page.insert_textbox(
        fitz.Rect(40, 180, 220, 220),
        "Figure 1. Left figure.",
        fontsize=12,
    )
    page.insert_textbox(
        fitz.Rect(280, 180, 460, 220),
        "Figure 2. Right figure.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_caption_above_figure_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=320)
    page.insert_text(
        (60, 70),
        "Figure 10. Caption appears above the figure.",
        fontsize=12,
    )
    page.draw_rect(fitz.Rect(60, 100, 250, 190), color=(0, 0, 0), fill=(0.8, 0.9, 0.7))

    doc.save(path)
    doc.close()


def make_caption_followed_by_body_text_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=320)
    page.insert_textbox(
        fitz.Rect(40, 150, 360, 190),
        "Figure 6. Caption line only.\nThis is ordinary body text in the same block.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_wrapped_label_only_caption_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=320)
    page.insert_textbox(
        fitz.Rect(40, 150, 360, 210),
        "Figure 1.\nWrapped caption description continues here.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_wrapped_label_only_caption_without_separator_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=320)
    page.insert_textbox(
        fitz.Rect(40, 150, 360, 210),
        "Figure 1\nWrapped caption description continues here.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_label_only_caption_followed_by_body_text_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=320)
    page.insert_textbox(
        fitz.Rect(40, 150, 360, 210),
        "Figure 1.\nThis is ordinary body text in the same block.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_split_block_wrapped_caption_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=320)
    page.insert_textbox(
        fitz.Rect(40, 150, 120, 176),
        "Figure 1.",
        fontsize=12,
    )
    page.insert_textbox(
        fitz.Rect(64, 176, 360, 208),
        "Wrapped caption description continues here.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_body_text_figure_reference_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=320)
    page.draw_rect(fitz.Rect(60, 40, 240, 130), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
    page.insert_textbox(
        fitz.Rect(60, 160, 360, 200),
        "Figure 1 shows the proposed pipeline for the full workflow.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_wrapped_uppercase_caption_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=320)
    page.insert_textbox(
        fitz.Rect(40, 150, 360, 210),
        "Figure 1. Overview of the method\nSEM images show morphology.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_separator_line_artifact_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=360)
    page.draw_rect(fitz.Rect(70, 70, 260, 160), color=(0, 0, 0), fill=(0.8, 0.9, 0.7))
    page.draw_rect(fitz.Rect(70, 188, 260, 190), color=(0, 0, 0), fill=(0, 0, 0))
    page.insert_text(
        (70, 220),
        "Figure 8. Separator lines should not win the crop anchor.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_low_confidence_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=360, height=260)
    page.draw_rect(fitz.Rect(60, 80, 220, 98), color=(0, 0, 0), fill=(0.7, 0.8, 0.9))
    page.insert_text(
        (60, 130),
        "Figure 9. Thin strip candidate needs fallback.",
        fontsize=12,
    )

    doc.save(path)
    doc.close()


def make_embedded_image_only_pdf(path: Path) -> None:
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
    )
    doc = fitz.open()
    page = doc.new_page(width=360, height=260)
    page.insert_image(fitz.Rect(40, 40, 300, 220), stream=png_bytes)
    doc.save(path)
    doc.close()


def render_quality_gate_image(path: Path, width: int, height: int, kind: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)

    if kind == "tiny":
        page.draw_rect(
            fitz.Rect(4, 4, width - 4, height - 4),
            color=(0, 0, 0),
            fill=(0.8, 0.8, 0.8),
        )
    elif kind == "article-header":
        page.insert_text((24, 32), "Article", fontsize=18)
    elif kind == "formula-strip":
        page.insert_text(
            (64, height / 2),
            "E = E0 + kBT ln(c/c0)",
            fontsize=16,
        )
    elif kind == "plot":
        plot_area = fitz.Rect(54, 42, width - 36, height - 42)
        page.draw_line(plot_area.bl, plot_area.tl, color=(0, 0, 0), width=1.5)
        page.draw_line(plot_area.bl, plot_area.br, color=(0, 0, 0), width=1.5)
        points = [
            fitz.Point(plot_area.x0 + 12, plot_area.y1 - 24),
            fitz.Point(plot_area.x0 + 78, plot_area.y1 - 92),
            fitz.Point(plot_area.x0 + 148, plot_area.y1 - 66),
            fitz.Point(plot_area.x0 + 224, plot_area.y0 + 48),
            fitz.Point(plot_area.x1 - 18, plot_area.y0 + 30),
        ]
        for start, end in zip(points, points[1:]):
            page.draw_line(start, end, color=(0.1, 0.2, 0.8), width=3)
        for point in points:
            page.draw_circle(point, 4, color=(0.8, 0.1, 0.1), fill=(0.8, 0.1, 0.1))
        page.insert_text((plot_area.x0 - 28, plot_area.y0 + 18), "y", fontsize=12)
        page.insert_text((plot_area.x1 - 16, plot_area.y1 + 24), "x", fontsize=12)
    elif kind == "transparent-plot":
        plot_area = fitz.Rect(54, 42, width - 36, height - 42)
        page.draw_line(plot_area.bl, plot_area.tl, color=(0, 0, 0), width=3)
        page.draw_line(plot_area.bl, plot_area.br, color=(0, 0, 0), width=3)
        points = [
            fitz.Point(plot_area.x0 + 12, plot_area.y1 - 24),
            fitz.Point(plot_area.x0 + 96, plot_area.y1 - 116),
            fitz.Point(plot_area.x0 + 190, plot_area.y1 - 72),
            fitz.Point(plot_area.x0 + 284, plot_area.y0 + 54),
            fitz.Point(plot_area.x1 - 18, plot_area.y0 + 30),
        ]
        for start, end in zip(points, points[1:]):
            page.draw_line(start, end, color=(0, 0, 0), width=5)
    else:
        raise ValueError(f"unknown image kind: {kind}")

    pixmap = page.get_pixmap(alpha=kind == "transparent-plot")
    pixmap.save(path)
    doc.close()


def test_assess_image_quality_flags_tiny_image(tmp_path: Path) -> None:
    image_path = tmp_path / "tiny.png"
    render_quality_gate_image(image_path, width=40, height=40, kind="tiny")

    quality = assess_image_quality(image_path)

    assert quality["status"] == "poor"
    assert "image_too_small" in quality["warnings"]
    assert quality["width"] == 40
    assert quality["height"] == 40


def test_assess_image_quality_flags_normal_size_text_only_header(tmp_path: Path) -> None:
    image_path = tmp_path / "article-header.png"
    render_quality_gate_image(image_path, width=480, height=260, kind="article-header")

    quality = assess_image_quality(image_path)

    assert quality["status"] == "poor"
    assert quality["warnings"] == ["image_content_area_too_sparse"]
    assert quality["content_bbox_area_ratio"] < 0.12


def test_assess_image_quality_flags_formula_strip(tmp_path: Path) -> None:
    image_path = tmp_path / "formula-strip.png"
    render_quality_gate_image(image_path, width=520, height=240, kind="formula-strip")

    quality = assess_image_quality(image_path)

    assert quality["status"] == "poor"
    assert quality["warnings"] == ["image_content_area_too_sparse"]
    assert quality["content_bbox_area_ratio"] < 0.12


def test_assess_image_quality_accepts_normal_plot_like_image(tmp_path: Path) -> None:
    image_path = tmp_path / "plot.png"
    render_quality_gate_image(image_path, width=520, height=320, kind="plot")

    quality = assess_image_quality(image_path)

    assert quality["status"] == "ok"
    assert quality["warnings"] == []
    assert quality["content_bbox_area_ratio"] >= 0.12


def test_assess_image_quality_accepts_transparent_black_plot(tmp_path: Path) -> None:
    image_path = tmp_path / "transparent-plot.png"
    render_quality_gate_image(image_path, width=520, height=320, kind="transparent-plot")

    quality = assess_image_quality(image_path)

    assert quality["status"] == "ok"
    assert "image_low_information" not in quality["warnings"]
    assert quality["warnings"] == []


def test_assess_image_quality_flags_unreadable_image(tmp_path: Path) -> None:
    image_path = tmp_path / "corrupt.png"
    image_path.write_bytes(b"not a png")

    quality = assess_image_quality(image_path)

    assert quality["status"] == "poor"
    assert quality["warnings"] == ["image_unreadable"]


def test_extract_figures_uses_tight_graphic_crop_and_1_based_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "figures.pdf"
    output_dir = tmp_path / "images"
    make_captioned_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir)
    figures = _selected(payload)

    assert len(figures) == 1

    by_caption = {figure["caption"]: figure for figure in figures}
    first = by_caption["Figure 1. Proposed pipeline for the full workflow."]
    assert first["page"] == 1
    assert first["bbox"] == [60.0, 100.0, 240.0, 190.0]
    assert first["bbox"][3] <= first["caption_bbox"][1]
    assert first["bbox"][1] > 70.0
    assert first["area"] == 16200.0
    assert first["source"] == "deterministic-pdf"
    assert Path(first["image_path"]).exists()


def test_extract_figures_ranks_pipeline_then_table_then_qualitative(tmp_path: Path) -> None:
    pdf_path = tmp_path / "ranking.pdf"
    output_dir = tmp_path / "images"
    make_ranking_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir)
    figures = _selected(payload)

    captions = [figure["caption"] for figure in figures]

    assert captions == [
        "Figure 2. Overview of the pipeline and workflow framework.",
        "Figure 1. Qualitative examples from three cases.",
    ]
    assert figures[0]["page"] == 1
    assert figures[0]["priority_score"] > figures[1]["priority_score"]


def test_extract_figures_prefers_charge_response_plot_with_caption(tmp_path: Path) -> None:
    pdf_path = tmp_path / "charge-response.pdf"
    output_dir = tmp_path / "images"
    make_charge_response_like_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir, top_k=2)

    captions = [figure["caption"] for figure in payload["selected_figures"]]
    assert any("Figure 3." in caption for caption in captions)


def test_extract_figures_prefers_ion_distribution_plot_with_caption(tmp_path: Path) -> None:
    pdf_path = tmp_path / "ion-distribution.pdf"
    output_dir = tmp_path / "images"
    make_ion_distribution_like_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir, top_k=2)

    captions = [figure["caption"] for figure in payload["selected_figures"]]
    assert any("Figure 4." in caption for caption in captions)


def test_extract_figures_unions_multi_panel_regions(tmp_path: Path) -> None:
    pdf_path = tmp_path / "multi-panel.pdf"
    output_dir = tmp_path / "images"
    make_multi_panel_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir)
    figures = _selected(payload)

    assert len(figures) == 1
    figure = figures[0]
    assert figure["caption"] == "Figure 3. Multi-panel pipeline overview."
    assert figure["bbox"] == [40.0, 40.0, 320.0, 130.0]
    assert figure["area"] == 25200.0
    assert Path(figure["image_path"]).exists()


def test_extract_figures_unions_stacked_regions_for_single_caption(tmp_path: Path) -> None:
    pdf_path = tmp_path / "stacked-figure.pdf"
    output_dir = tmp_path / "images"
    make_stacked_figure_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir)
    figures = _selected(payload)

    assert len(figures) == 1
    figure = figures[0]
    assert figure["caption"] == "Figure 7. Two-layer stacked result."
    assert figure["bbox"] == [80.0, 40.0, 240.0, 210.0]
    assert Path(figure["image_path"]).exists()


def test_extract_figures_keeps_stacked_figures_with_separate_captions_separate(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "stacked-figures-separate-captions.pdf"
    output_dir = tmp_path / "images"
    make_stacked_figures_with_individual_captions_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir)
    figures = _selected(payload)

    assert [figure["caption"] for figure in figures] == [
        "Figure 1. Top stacked figure.",
        "Figure 2. Bottom stacked figure.",
    ]
    assert [figure["bbox"] for figure in figures] == [
        [80.0, 40.0, 240.0, 100.0],
        [80.0, 132.0, 240.0, 192.0],
    ]


def test_extract_figures_detects_raster_image_regions(tmp_path: Path) -> None:
    pdf_path = tmp_path / "raster.pdf"
    output_dir = tmp_path / "images"
    make_raster_image_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir)
    figures = _selected(payload)

    assert len(figures) == 1
    figure = figures[0]
    assert figure["caption"] == "Fig. 4. Raster result comparison."
    assert figure["caption_confidence"] > 0.0
    assert figure["caption_confidence"] < 0.95
    assert figure["bbox"] == [70.0, 60.0, 210.0, 150.0]
    assert figure["page"] == 1
    assert figure["source"] == "embedded-image"
    assert Path(figure["image_path"]).exists()


def test_extract_figures_backfills_caption_for_captionless_embedded_image_from_same_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "raster-caption-backfill.pdf"
    output_dir = tmp_path / "images"
    make_raster_image_pdf(pdf_path)
    monkeypatch.setattr(
        "paper_reader.figures._detect_graphic_regions",
        lambda page: [],
    )

    payload = extract_figures(pdf_path, output_dir)

    assert payload["candidate_count"] == 1
    figure = _selected(payload)[0]
    assert figure["source"] == "embedded-image"
    assert figure["caption"] == "Fig. 4. Raster result comparison."
    assert figure["caption_confidence"] > 0.0
    assert figure["caption_confidence"] < 0.95
    assert figure["page"] == 1


def test_extract_figures_keeps_embedded_image_regions_as_late_supplement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "raster-supplement.pdf"
    output_dir = tmp_path / "images"
    make_raster_image_pdf(pdf_path)
    monkeypatch.setattr(
        "paper_reader.figures._detect_graphic_regions",
        lambda page: [],
    )

    payload = extract_figures(pdf_path, output_dir, top_k=2)

    assert payload["candidate_count"] == 1
    figure = _selected(payload)[0]
    assert figure["source"] == "embedded-image"
    assert figure["caption"] == "Fig. 4. Raster result comparison."
    assert figure["caption_confidence"] > 0.0
    assert figure["page"] == 1


def test_build_figure_context_markdown_includes_caption_confidence() -> None:
    markdown = build_figure_context_markdown(
        {
            "pdf_path": "/tmp/paper.pdf",
            "candidate_count": 1,
            "warnings": [],
            "source_attempts": [],
            "selected_figures": [
                {
                    "figure_id": "p1-f1",
                    "caption": "Figure 1. Example.",
                    "caption_confidence": 0.72,
                    "page": 1,
                    "source": "embedded-image",
                    "image_path": "/tmp/figure.png",
                    "priority_score": 0.05,
                    "needs_fallback": False,
                    "visual_quality": {"status": "ok", "warnings": []},
                }
            ],
        }
    )

    assert "- Caption Confidence: 0.72" in markdown
    assert '- Visual Quality: {"status": "ok", "warnings": []}' in markdown


def test_extract_figures_does_not_backfill_when_multiple_same_page_captions_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "ambiguous-captions.pdf"
    output_dir = tmp_path / "images"
    make_embedded_image_with_multiple_nearby_captions_pdf(pdf_path)
    monkeypatch.setattr(
        "paper_reader.figures._detect_graphic_regions",
        lambda page: [],
    )

    payload = extract_figures(pdf_path, output_dir)

    assert payload["candidate_count"] == 1
    figure = _selected(payload)[0]
    assert figure["source"] == "embedded-image"
    assert figure["caption"] == ""
    assert figure["caption_confidence"] == 0.0


def test_extract_figures_keeps_embedded_image_when_deterministic_neighbor_has_caption(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "mixed-deterministic-embedded.pdf"
    output_dir = tmp_path / "images"
    make_mixed_deterministic_and_captionless_embedded_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir, top_k=3)

    figures = _selected(payload)
    assert len(figures) == 2
    assert [figure["source"] for figure in figures] == [
        "deterministic-pdf",
        "embedded-image",
    ]
    assert figures[0]["caption"] == "Figure 3. Left deterministic figure."
    assert figures[1]["caption"] == ""
    assert figures[1]["caption_confidence"] == 0.0


def test_extract_figures_does_not_backfill_embedded_image_with_caption_claimed_by_deterministic_figure(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "claimed-caption-above-embedded.pdf"
    output_dir = tmp_path / "images"
    make_claimed_caption_above_embedded_image_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir, top_k=3)

    figures = _selected(payload)
    assert len(figures) == 2
    assert [figure["source"] for figure in figures] == [
        "deterministic-pdf",
        "embedded-image",
    ]
    assert figures[0]["caption"] == "Figure 8. Upper deterministic figure."
    assert figures[1]["caption"] == ""
    assert figures[1]["caption_confidence"] == 0.0


def test_extract_figures_claims_backfilled_caption_only_once_across_embedded_supplements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "shared-caption-embedded-supplements.pdf"
    output_dir = tmp_path / "images"
    make_embedded_images_sharing_single_nearby_caption_pdf(pdf_path)
    monkeypatch.setattr(
        "paper_reader.figures._detect_graphic_regions",
        lambda page: [],
    )

    payload = extract_figures(pdf_path, output_dir, top_k=3)

    figures = _selected(payload)
    assert len(figures) == 2
    assert [figure["caption"] for figure in figures] == [
        "Figure 11. Shared caption.",
        "",
    ]
    assert figures[0]["caption_confidence"] > 0.0
    assert figures[1]["caption_confidence"] == 0.0


def test_extract_figures_does_not_backfill_from_far_caption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "far-caption.pdf"
    output_dir = tmp_path / "images"
    make_raster_image_with_far_caption_pdf(pdf_path)
    monkeypatch.setattr(
        "paper_reader.figures._detect_graphic_regions",
        lambda page: [],
    )

    payload = extract_figures(pdf_path, output_dir)

    assert payload["candidate_count"] == 1
    figure = _selected(payload)[0]
    assert figure["source"] == "embedded-image"
    assert figure["caption"] == ""
    assert figure["caption_confidence"] == 0.0


def test_extract_figures_accepts_fig_without_period(tmp_path: Path) -> None:
    pdf_path = tmp_path / "fig-no-period.pdf"
    output_dir = tmp_path / "images"
    make_fig_without_period_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir)
    figures = _selected(payload)

    assert len(figures) == 1
    figure = figures[0]
    assert figure["caption"] == "Fig 5. Raster result comparison without period."
    assert figure["bbox"] == [50.0, 60.0, 210.0, 150.0]
    assert figure["page"] == 1
    assert Path(figure["image_path"]).exists()


def test_extract_figures_splits_side_by_side_captions_on_same_row(tmp_path: Path) -> None:
    pdf_path = tmp_path / "side-by-side-captions.pdf"
    make_side_by_side_captions_pdf(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        captions = _detect_captions(doc[0])
    finally:
        doc.close()

    assert [caption["caption"] for caption in captions] == [
        "Figure 1. Left caption.",
        "Figure 2. Right caption.",
    ]


def test_detect_captions_does_not_swallow_body_text_in_same_block(tmp_path: Path) -> None:
    pdf_path = tmp_path / "caption-followed-by-body.pdf"
    make_caption_followed_by_body_text_pdf(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        captions = _detect_captions(doc[0])
    finally:
        doc.close()

    assert [caption["caption"] for caption in captions] == [
        "Figure 6. Caption line only.",
    ]


def test_detect_captions_continues_wrapped_label_only_caption_line(tmp_path: Path) -> None:
    pdf_path = tmp_path / "wrapped-label-only-caption.pdf"
    make_wrapped_label_only_caption_pdf(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        captions = _detect_captions(doc[0])
    finally:
        doc.close()

    assert [caption["caption"] for caption in captions] == [
        "Figure 1. Wrapped caption description continues here.",
    ]


def test_detect_captions_continues_wrapped_label_only_caption_without_separator(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "wrapped-label-only-caption-without-separator.pdf"
    make_wrapped_label_only_caption_without_separator_pdf(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        captions = _detect_captions(doc[0])
    finally:
        doc.close()

    assert [caption["caption"] for caption in captions] == [
        "Figure 1 Wrapped caption description continues here.",
    ]


def test_detect_captions_does_not_merge_label_only_caption_with_body_text(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "label-only-caption-followed-by-body.pdf"
    make_label_only_caption_followed_by_body_text_pdf(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        captions = _detect_captions(doc[0])
    finally:
        doc.close()

    assert [caption["caption"] for caption in captions] == [
        "Figure 1.",
    ]


def test_detect_captions_continues_wrapped_caption_across_split_blocks(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "split-block-wrapped-caption.pdf"
    make_split_block_wrapped_caption_pdf(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        captions = _detect_captions(doc[0])
    finally:
        doc.close()

    assert [caption["caption"] for caption in captions] == [
        "Figure 1. Wrapped caption description continues here.",
    ]


def test_detect_captions_ignores_body_text_figure_references(tmp_path: Path) -> None:
    pdf_path = tmp_path / "body-text-figure-reference.pdf"
    output_dir = tmp_path / "images"
    make_body_text_figure_reference_pdf(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        captions = _detect_captions(doc[0])
    finally:
        doc.close()

    payload = extract_figures(pdf_path, output_dir)

    assert captions == []
    assert _selected(payload) == []
    assert payload["candidate_count"] == 0


def test_detect_captions_continues_wrapped_caption_with_uppercase_line(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "wrapped-uppercase-caption.pdf"
    make_wrapped_uppercase_caption_pdf(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        captions = _detect_captions(doc[0])
    finally:
        doc.close()

    assert [caption["caption"] for caption in captions] == [
        "Figure 1. Overview of the method SEM images show morphology.",
    ]


def test_extract_figures_does_not_merge_side_by_side_figures_into_one_bbox(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "side-by-side-figures.pdf"
    output_dir = tmp_path / "images"
    make_side_by_side_figures_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir)
    figures = _selected(payload)

    assert [figure["caption"] for figure in figures] == [
        "Figure 1. Left figure.",
        "Figure 2. Right figure.",
    ]
    assert [figure["bbox"] for figure in figures] == [
        [40.0, 60.0, 200.0, 150.0],
        [280.0, 60.0, 440.0, 150.0],
    ]


def test_extract_figures_ignores_separator_line_artifacts(tmp_path: Path) -> None:
    pdf_path = tmp_path / "separator-line-artifact.pdf"
    output_dir = tmp_path / "images"
    make_separator_line_artifact_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir)
    figure = _selected(payload)[0]

    assert figure["bbox"] == [70.0, 70.0, 260.0, 160.0]
    assert figure["extraction_confidence"] > 0.5
    assert figure["needs_fallback"] is False


def test_extract_figures_supports_caption_above_layouts(tmp_path: Path) -> None:
    pdf_path = tmp_path / "caption-above.pdf"
    output_dir = tmp_path / "images"
    make_caption_above_figure_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir)
    figures = _selected(payload)

    assert len(figures) == 1
    figure = figures[0]
    assert figure["caption"] == "Figure 10. Caption appears above the figure."
    assert figure["bbox"] == [60.0, 100.0, 250.0, 190.0]
    assert figure["bbox"][1] >= figure["caption_bbox"][3]
    assert Path(figure["image_path"]).suffix.lower() == ".png"


def test_extract_figures_surfaces_low_confidence_geometry(tmp_path: Path) -> None:
    pdf_path = tmp_path / "low-confidence.pdf"
    output_dir = tmp_path / "images"
    make_low_confidence_pdf(pdf_path)

    payload = extract_figures(pdf_path, output_dir)
    figure = _selected(payload)[0]

    assert figure["caption"] == "Figure 9. Thin strip candidate needs fallback."
    assert figure["needs_fallback"] is True
    assert figure["extraction_confidence"] < 0.5
    assert figure["fallback_reason"] == "low_confidence_geometry"
    assert figure["visual_quality"]["status"] == "poor"
    assert "image_too_small" in figure["visual_quality"]["warnings"]
    assert f"figure_visual_quality:{figure['figure_id']}:image_too_small" in payload["warnings"]


def test_extract_figures_ranks_source_figures_above_embedded_image_supplements_when_scores_tie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "embedded-only.pdf"
    output_dir = tmp_path / "images"
    make_embedded_image_only_pdf(pdf_path)
    source_root = tmp_path / "unused-source"
    source_root.mkdir()

    source_image_path = tmp_path / "source-priority.png"
    source_image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
        )
    )

    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.resolve_arxiv_id",
        lambda details, pdf_path=None: "2402.12345",
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.download_arxiv_source",
        lambda arxiv_id, workdir: source_root,
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.collect_source_figures",
        lambda source_root, output_dir: [
            {
                "rel_path": "figure-source.png",
                "image_path": str(source_image_path),
                "source": "arxiv-source",
                "media_type": "image",
            }
        ],
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.render_source_figure_pdfs",
        lambda source_figures, output_dir: [
            {
                "rel_path": "figure-source.pdf",
                "image_path": str(source_image_path),
                "source": "pdf-figure",
                "caption": "",
            }
        ],
    )

    payload = extract_figures(
        pdf_path,
        output_dir,
        item_details={"url": "https://arxiv.org/abs/2402.12345"},
        top_k=2,
    )

    assert [figure["source"] for figure in _selected(payload)] == [
        "pdf-figure",
        "embedded-image",
    ]


def test_extract_figures_normalizes_source_provenance_and_uses_1_based_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "embedded-only.pdf"
    output_dir = tmp_path / "images"
    make_embedded_image_only_pdf(pdf_path)
    source_root = tmp_path / "source-root"
    source_root.mkdir()

    image_path = tmp_path / "source-image.png"
    pdf_figure_path = tmp_path / "source-figure.pdf"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
        )
    )

    source_doc = fitz.open()
    source_doc.new_page(width=120, height=80)
    source_doc.save(pdf_figure_path)
    source_doc.close()

    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.resolve_arxiv_id",
        lambda details, pdf_path=None: "2402.12345",
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.download_arxiv_source",
        lambda arxiv_id, workdir: source_root,
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.collect_source_figures",
        lambda source_root, output_dir: [
            {
                "rel_path": "figure-source.png",
                "image_path": str(image_path),
                "source": "tex-figure",
                "media_type": "image",
            }
        ],
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.render_source_figure_pdfs",
        lambda source_figures, output_dir: [
            {
                "rel_path": "figure-source.pdf",
                "image_path": str(pdf_figure_path),
                "source": "rendered-pdf",
                "media_type": "pdf",
            }
        ],
    )

    payload = extract_figures(
        pdf_path,
        output_dir,
        item_details={"url": "https://arxiv.org/abs/2402.12345"},
        top_k=3,
    )

    assert [figure["page"] for figure in _selected(payload)] == [1, 1]
    assert {figure["source"] for figure in _selected(payload)} == {
        "pdf-figure",
        "embedded-image",
    }


def test_extract_figures_keeps_selected_source_figure_paths_raster_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "embedded-only.pdf"
    output_dir = tmp_path / "images"
    make_embedded_image_only_pdf(pdf_path)
    source_root = tmp_path / "source-root"
    source_root.mkdir()

    pdf_figure_path = tmp_path / "figure-source.pdf"
    rendered_png_path = tmp_path / "figure-source.png"
    source_doc = fitz.open()
    source_doc.new_page(width=120, height=80)
    source_doc.save(pdf_figure_path)
    source_doc.close()
    with fitz.open(pdf_figure_path) as rendered_doc:
        rendered_doc.load_page(0).get_pixmap().save(rendered_png_path)

    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.resolve_arxiv_id",
        lambda details, pdf_path=None: "2402.12345",
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.download_arxiv_source",
        lambda arxiv_id, workdir: source_root,
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.collect_source_figures",
        lambda source_root, output_dir: [
            {
                "rel_path": "figures/figure-source.pdf",
                "image_path": str(pdf_figure_path),
                "source": "arxiv-source",
                "media_type": "pdf",
            }
        ],
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.render_source_figure_pdfs",
        lambda source_figures, output_dir: [
            {
                "rel_path": "figures/figure-source.pdf",
                "image_path": str(rendered_png_path),
                "source": "pdf-figure",
                "media_type": "image",
            }
        ],
    )

    payload = extract_figures(
        pdf_path,
        output_dir,
        item_details={"url": "https://arxiv.org/abs/2402.12345"},
        top_k=1,
    )

    figure = _selected(payload)[0]
    assert figure["source"] == "pdf-figure"
    assert Path(figure["image_path"]) == rendered_png_path
    assert Path(figure["image_path"]).suffix.lower() == ".png"


def test_extract_figures_dedupes_same_caption_across_source_and_pdf_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "captioned.pdf"
    output_dir = tmp_path / "images"
    make_captioned_pdf(pdf_path)
    source_root = tmp_path / "source-root"
    source_root.mkdir()

    source_image_path = tmp_path / "source-figure.png"
    source_image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
        )
    )

    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.resolve_arxiv_id",
        lambda details, pdf_path=None: "2402.12345",
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.download_arxiv_source",
        lambda arxiv_id, workdir: source_root,
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.collect_source_figures",
        lambda source_root, output_dir: [
            {
                "rel_path": "figures/figure-source.png",
                "image_path": str(source_image_path),
                "source": "arxiv-source",
                "media_type": "image",
                "caption": "Figure 1. Proposed pipeline for the full workflow.",
                "page": 1,
            }
        ],
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.render_source_figure_pdfs",
        lambda source_figures, output_dir: [],
    )

    payload = extract_figures(
        pdf_path,
        output_dir,
        item_details={"url": "https://arxiv.org/abs/2402.12345"},
        top_k=3,
    )

    figures = _selected(payload)
    same_caption = [
        figure
        for figure in figures
        if figure["caption"] == "Figure 1. Proposed pipeline for the full workflow."
    ]
    assert len(same_caption) == 1
    assert same_caption[0]["source"] == "arxiv-source"


def test_extract_figures_surfaces_arxiv_download_failures_as_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "embedded-only.pdf"
    output_dir = tmp_path / "images"
    make_embedded_image_only_pdf(pdf_path)

    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.resolve_arxiv_id",
        lambda details, pdf_path=None: "2402.12345",
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.download_arxiv_source",
        lambda arxiv_id, workdir: None,
    )

    payload = extract_figures(
        pdf_path,
        output_dir,
        item_details={"url": "https://arxiv.org/abs/2402.12345"},
        top_k=2,
    )

    assert payload["source_attempts"] == [
        {"stage": "resolve", "status": "resolved", "arxiv_id": "2402.12345"},
        {"stage": "download", "status": "download_failed", "arxiv_id": "2402.12345"},
    ]
    assert "arxiv_source_download_failed" in payload["warnings"]


def test_extract_figures_ranks_source_model_image_above_source_stats_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "embedded-only.pdf"
    output_dir = tmp_path / "images"
    make_embedded_image_only_pdf(pdf_path)
    source_root = tmp_path / "source-root"
    source_root.mkdir()

    model_image_path = tmp_path / "crystalgrw_model_new.png"
    stats_image_path = tmp_path / "alexmp20_stats.png"
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
    )
    model_image_path.write_bytes(png_bytes)
    stats_image_path.write_bytes(png_bytes)

    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.resolve_arxiv_id",
        lambda details, pdf_path=None: "2501.08998",
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.download_arxiv_source",
        lambda arxiv_id, workdir: source_root,
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.collect_source_figures",
        lambda source_root, output_dir: [
            {
                "rel_path": "crystalgrw_model_new.png",
                "image_path": str(model_image_path),
                "source": "arxiv-source",
                "media_type": "image",
            },
            {
                "rel_path": "alexmp20_stats.png",
                "image_path": str(stats_image_path),
                "source": "arxiv-source",
                "media_type": "image",
            },
        ],
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.render_source_figure_pdfs",
        lambda source_figures, output_dir: [],
    )

    payload = extract_figures(
        pdf_path,
        output_dir,
        item_details={"url": "https://arxiv.org/abs/2501.08998"},
        top_k=3,
    )

    captions = [figure["caption"] for figure in _selected(payload)]
    assert "crystalgrw model new" in captions
    assert "alexmp20 stats" in captions
    assert captions.index("crystalgrw model new") < captions.index("alexmp20 stats")


def test_extract_figures_keeps_generic_unlabeled_source_images_behind_embedded_supplements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "embedded-only.pdf"
    output_dir = tmp_path / "images"
    make_embedded_image_only_pdf(pdf_path)
    source_root = tmp_path / "source-root"
    source_root.mkdir()

    source_image_path = tmp_path / "generic-source.png"
    source_image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
        )
    )

    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.resolve_arxiv_id",
        lambda details, pdf_path=None: "2501.08998",
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.download_arxiv_source",
        lambda arxiv_id, workdir: source_root,
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.collect_source_figures",
        lambda source_root, output_dir: [
            {
                "rel_path": "pg_alexmp20_gs05.png",
                "image_path": str(source_image_path),
                "source": "arxiv-source",
                "media_type": "image",
            }
        ],
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.render_source_figure_pdfs",
        lambda source_figures, output_dir: [],
    )

    payload = extract_figures(
        pdf_path,
        output_dir,
        item_details={"url": "https://arxiv.org/abs/2501.08998"},
        top_k=2,
    )

    assert [figure["source"] for figure in _selected(payload)] == [
        "embedded-image",
        "arxiv-source",
    ]


def test_extract_figures_does_not_let_low_value_source_stats_dominate_real_pdf_figure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "captioned.pdf"
    output_dir = tmp_path / "images"
    make_captioned_pdf(pdf_path)
    source_root = tmp_path / "source-root"
    source_root.mkdir()

    stats_image_path = tmp_path / "alexmp20_stats.png"
    stats_image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7c87sAAAAASUVORK5CYII="
        )
    )

    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.resolve_arxiv_id",
        lambda details, pdf_path=None: "2501.08998",
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.download_arxiv_source",
        lambda arxiv_id, workdir: source_root,
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.collect_source_figures",
        lambda source_root, output_dir: [
            {
                "rel_path": "alexmp20_stats.png",
                "image_path": str(stats_image_path),
                "source": "arxiv-source",
                "media_type": "image",
            }
        ],
    )
    monkeypatch.setattr(
        "paper_reader.figures.arxiv_source.render_source_figure_pdfs",
        lambda source_figures, output_dir: [],
    )

    payload = extract_figures(
        pdf_path,
        output_dir,
        item_details={"url": "https://arxiv.org/abs/2501.08998"},
        top_k=2,
    )

    captions = [figure["caption"] for figure in _selected(payload)]
    assert captions[0] == "Figure 1. Proposed pipeline for the full workflow."


def test_pdf_candidate_cap_is_checked_before_any_rasterization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paper_reader.figures as figures

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"placeholder")
    page = type("FakePage", (), {"rect": fitz.Rect(0, 0, 400, 600)})()

    class FakeDocument:
        page_count = 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def load_page(self, index: int):
            assert index == 0
            return page

    captions = [
        {"caption": f"Figure {index}.", "rect": fitz.Rect(10, 10, 100, 30)}
        for index in range(201)
    ]
    monkeypatch.setattr(figures.fitz, "open", lambda _path: FakeDocument())
    monkeypatch.setattr(figures, "_detect_captions", lambda _page: captions)
    monkeypatch.setattr(figures, "_detect_graphic_regions", lambda _page: [fitz.Rect(10, 40, 200, 200)])
    monkeypatch.setattr(figures, "_detect_embedded_image_regions", lambda _page: [])
    monkeypatch.setattr(
        figures,
        "_select_owned_bbox",
        lambda *args, **kwargs: fitz.Rect(10, 40, 200, 200),
    )

    def forbidden_raster(*args, **kwargs):
        pytest.fail("candidate cap was checked only after rasterization")

    monkeypatch.setattr(figures, "_rasterize_figure", forbidden_raster)

    with pytest.raises(figures.FigureCandidateLimitError) as exc_info:
        figures._extract_pdf_candidates(
            pdf_path,
            tmp_path / "figures",
            max_pages=None,
            max_candidates=200,
        )

    assert exc_info.value.actual == 201
    assert exc_info.value.limit == 200
