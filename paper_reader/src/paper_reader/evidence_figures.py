from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from paper_reader.evidence_manifest import EvidenceFigureMember, EvidenceResourceCheck
from paper_reader.figures import extract_figures
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.storage import canonical_json_bytes
from paper_reader.workflow import build_figure_context_markdown


ArtifactSpec = tuple[Path, Path, str, str]


@dataclass(frozen=True, slots=True)
class PreparedFigures:
    artifacts: tuple[ArtifactSpec, ...]
    members: tuple[EvidenceFigureMember, ...]
    degraded: bool
    extraction_check: EvidenceResourceCheck
    resource_checks: tuple[EvidenceResourceCheck, ...]


class IncompleteFigureEvidenceError(ValueError):
    pass


class FigureResourceLimitError(ValueError):
    def __init__(self, check: EvidenceResourceCheck) -> None:
        super().__init__(check.message or check.name)
        self.check = check


def _resource_checks(
    figures_payload: dict,
    selected: list[dict],
) -> tuple[EvidenceResourceCheck, ...]:
    candidate_count = int(figures_payload.get("candidate_count", 0))
    candidate_check = EvidenceResourceCheck(
        name="figure_candidate_count",
        status="passed",
        actual=candidate_count,
        limit=V2_RESOURCE_POLICY.figure_max_candidates,
    )
    if candidate_count > V2_RESOURCE_POLICY.figure_max_candidates:
        raise FigureResourceLimitError(
            candidate_check.model_copy(
                update={
                    "status": "degraded",
                    "message": "figure candidate count exceeds the V2 cap",
                }
            )
        )

    pixels: list[int] = []
    total_bytes = 0
    for item in selected:
        quality = item.get("visual_quality")
        if not isinstance(quality, dict):
            raise ValueError("selected figure visual_quality must be an object")
        width = quality.get("width")
        height = quality.get("height")
        if not isinstance(width, int) or isinstance(width, bool) or width < 0:
            raise ValueError("selected figure visual_quality.width must be a non-negative integer")
        if not isinstance(height, int) or isinstance(height, bool) or height < 0:
            raise ValueError("selected figure visual_quality.height must be a non-negative integer")
        pixels.append(width * height)
        total_bytes += Path(str(item.get("image_path", ""))).stat().st_size

    max_pixels = max(pixels, default=0)
    each_check = EvidenceResourceCheck(
        name="figure_pixels_each",
        status="passed",
        actual=max_pixels,
        limit=V2_RESOURCE_POLICY.figure_max_pixels_each,
    )
    if max_pixels > V2_RESOURCE_POLICY.figure_max_pixels_each:
        raise FigureResourceLimitError(
            each_check.model_copy(
                update={
                    "status": "degraded",
                    "message": "one or more figure images exceed the V2 per-image pixel cap",
                }
            )
        )

    total_pixels = sum(pixels)
    pixels_total_check = EvidenceResourceCheck(
        name="figure_pixels_total",
        status="passed",
        actual=total_pixels,
        limit=V2_RESOURCE_POLICY.figure_max_pixels_total,
    )
    if total_pixels > V2_RESOURCE_POLICY.figure_max_pixels_total:
        raise FigureResourceLimitError(
            pixels_total_check.model_copy(
                update={
                    "status": "degraded",
                    "message": "selected figure images exceed the V2 aggregate pixel cap",
                }
            )
        )

    bytes_check = EvidenceResourceCheck(
        name="figure_bytes_total",
        status="passed",
        actual=total_bytes,
        limit=V2_RESOURCE_POLICY.figure_max_bytes_total,
    )
    if total_bytes > V2_RESOURCE_POLICY.figure_max_bytes_total:
        raise FigureResourceLimitError(
            bytes_check.model_copy(
                update={
                    "status": "degraded",
                    "message": "selected figure images exceed the V2 aggregate byte cap",
                }
            )
        )
    return candidate_check, each_check, pixels_total_check, bytes_check


def prepare_figure_artifacts(
    *,
    source_path: Path,
    staging: Path,
    future_dir: Path,
    run_dir: Path,
    figure_limit: int,
    preview_pages: int | None,
    complete: bool,
) -> PreparedFigures:
    if figure_limit == 0:
        return PreparedFigures(
            artifacts=(),
            members=(),
            degraded=False,
            extraction_check=EvidenceResourceCheck(
                name="figure_extraction",
                status="passed",
                actual=0,
                limit=0,
                message="disabled",
            ),
            resource_checks=(),
        )

    try:
        figures_payload = extract_figures(
            source_path,
            output_dir=staging / "figures",
            top_k=figure_limit,
            max_pages=preview_pages,
            item_details=None,
            allow_network_source=False,
        )
        selected = figures_payload.get("selected_figures", [])
        if not isinstance(selected, list):
            raise ValueError("selected_figures must be a list")
        if len(selected) > figure_limit:
            raise ValueError("selected figure count exceeds the requested figure limit")
        resource_checks = _resource_checks(figures_payload, selected)
        artifacts: list[ArtifactSpec] = []
        members: list[EvidenceFigureMember] = []
        normalized_selected: list[dict] = []
        figures_root = (staging / "figures").resolve(strict=True)
        for item in selected:
            if not isinstance(item, dict):
                raise ValueError("selected figure must be an object")
            image_path = Path(str(item.get("image_path", ""))).resolve(strict=True)
            if not image_path.is_relative_to(figures_root):
                raise ValueError(f"figure image escapes evidence staging: {image_path}")
            relative_image = image_path.relative_to(staging)
            future_image = future_dir / relative_image
            normalized = dict(item)
            normalized["image_path"] = future_image.relative_to(run_dir).as_posix()
            normalized_selected.append(normalized)
            members.append(
                EvidenceFigureMember(
                    figure_id=str(item.get("figure_id", "")),
                    page=int(item.get("page", 0)),
                    artifact_path=future_image.relative_to(run_dir).as_posix(),
                )
            )
            artifacts.append((image_path, future_image, "figure_image", "image/png"))

        normalized_payload = dict(figures_payload)
        normalized_payload["selected_figures"] = normalized_selected
        figures_json = staging / "figures.json"
        figure_context = staging / "figure_context.md"
        figures_json.write_bytes(canonical_json_bytes(normalized_payload))
        figure_context.write_text(build_figure_context_markdown(normalized_payload), encoding="utf-8")
        artifacts.extend(
            [
                (figures_json, future_dir / "figures.json", "figures", "application/json"),
                (
                    figure_context,
                    future_dir / "figure_context.md",
                    "figure_context",
                    "text/markdown",
                ),
            ]
        )
        return PreparedFigures(
            artifacts=tuple(artifacts),
            members=tuple(members),
            degraded=False,
            extraction_check=EvidenceResourceCheck(
                name="figure_extraction",
                status="passed",
                actual=len(normalized_selected),
                limit=figure_limit,
            ),
            resource_checks=resource_checks,
        )
    except Exception as exc:
        shutil.rmtree(staging / "figures", ignore_errors=True)
        if not complete:
            raise IncompleteFigureEvidenceError(str(exc)) from exc
        resource_checks = (exc.check,) if isinstance(exc, FigureResourceLimitError) else ()
        return PreparedFigures(
            artifacts=(),
            members=(),
            degraded=True,
            extraction_check=EvidenceResourceCheck(
                name="figure_extraction",
                status="degraded",
                actual=0,
                limit=figure_limit,
                message=f"{type(exc).__name__}: {exc}",
            ),
            resource_checks=resource_checks,
        )


__all__ = [
    "ArtifactSpec",
    "IncompleteFigureEvidenceError",
    "PreparedFigures",
    "prepare_figure_artifacts",
]
