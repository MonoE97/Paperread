from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from paper_reader.evidence_manifest import EvidenceFigureMember, EvidenceResourceCheck
from paper_reader.figures import (
    FigureCandidateLimitError,
    FigurePixelLimitError,
    extract_figures,
)
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.storage import (
    DirectoryAnchorLike,
    atomic_write_bytes,
    canonical_json_bytes,
    remove_anchored_file,
    remove_anchored_tree,
)
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


def _display_pdf_path(
    value: str,
    *,
    source_path: Path,
    verified_source_path: Path | None,
) -> str:
    if verified_source_path is None:
        return value
    aliases = {str(verified_source_path)}
    if verified_source_path.name.isdigit():
        aliases.update(
            {
                f"/dev/fd/{verified_source_path.name}",
                f"/proc/self/fd/{verified_source_path.name}",
            }
        )
    rendered = value
    for alias in sorted(aliases, key=len, reverse=True):
        rendered = rendered.replace(alias, str(source_path))
    return rendered


def _preallocation_resource_check(exc: Exception) -> EvidenceResourceCheck | None:
    if isinstance(exc, FigureCandidateLimitError):
        return EvidenceResourceCheck(
            name="figure_candidate_count",
            status="degraded",
            actual=exc.actual,
            limit=exc.limit,
            message="figure candidate count exceeds the V2 cap",
        )
    if isinstance(exc, FigurePixelLimitError):
        resource_name = (
            "figure_pixels_total"
            if getattr(exc, "resource_name", "") == "figure_pixels_total"
            else "figure_pixels_each"
        )
        message = (
            "selected figure images exceed the V2 aggregate pixel cap"
            if resource_name == "figure_pixels_total"
            else "one or more figure images exceed the V2 per-image pixel cap"
        )
        return EvidenceResourceCheck(
            name=resource_name,
            status="degraded",
            actual=exc.actual,
            limit=exc.limit,
            message=message,
        )
    return None


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
    verified_source_path: Path | None = None,
    staging: Path,
    staging_anchor: DirectoryAnchorLike,
    future_dir: Path,
    run_dir: Path,
    figure_limit: int,
    preview_pages: int | None,
    complete: bool,
    allow_network_source: bool = False,
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
        with tempfile.TemporaryDirectory(prefix="paper-reader-figures-") as scratch_text:
            scratch = Path(scratch_text).resolve(strict=True)
            scratch_figures = scratch / "figures"
            figures_payload = extract_figures(
                source_path,
                output_dir=scratch_figures,
                top_k=figure_limit,
                max_pages=preview_pages,
                item_details=None,
                allow_network_source=allow_network_source,
                max_candidates=V2_RESOURCE_POLICY.figure_max_candidates,
                _verified_pdf_path=verified_source_path,
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
            figures_root = scratch_figures.resolve(strict=True)
            for item in selected:
                if not isinstance(item, dict):
                    raise ValueError("selected figure must be an object")
                image_path = Path(str(item.get("image_path", ""))).resolve(strict=True)
                if not image_path.is_relative_to(figures_root):
                    raise ValueError(f"figure image escapes extraction scratch: {image_path}")
                before = os.lstat(image_path)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or stat.S_ISLNK(before.st_mode)
                    or before.st_nlink != 1
                ):
                    raise ValueError(f"figure image is not a single-link regular file: {image_path}")
                image_bytes = image_path.read_bytes()
                after = os.lstat(image_path)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                    before.st_nlink,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                    after.st_nlink,
                ) or len(image_bytes) != before.st_size:
                    raise ValueError(f"figure image changed while copied: {image_path}")
                artifact_size = item.get("artifact_size_bytes")
                artifact_sha256 = item.get("artifact_sha256")
                if artifact_size is not None or artifact_sha256 is not None:
                    if (
                        type(artifact_size) is not int
                        or artifact_size < 0
                        or type(artifact_sha256) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None
                    ):
                        raise ValueError(
                            f"figure artifact binding is invalid: {image_path}"
                        )
                    if (
                        before.st_size != artifact_size
                        or len(image_bytes) != artifact_size
                        or hashlib.sha256(image_bytes).hexdigest() != artifact_sha256
                    ):
                        raise ValueError(
                            f"figure artifact does not match its bound size/hash: {image_path}"
                        )
                relative_image = image_path.relative_to(figures_root)
                staged_image = staging / "figures" / relative_image
                atomic_write_bytes(
                    staged_image,
                    image_bytes,
                    anchor=staging_anchor,
                )
                future_image = future_dir / "figures" / relative_image
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
                artifacts.append((staged_image, future_image, "figure_image", "image/png"))

            normalized_payload = dict(figures_payload)
            normalized_payload["selected_figures"] = normalized_selected
            figures_json = staging / "figures.json"
            figure_context = staging / "figure_context.md"
            atomic_write_bytes(
                figures_json,
                canonical_json_bytes(normalized_payload),
                anchor=staging_anchor,
            )
            atomic_write_bytes(
                figure_context,
                build_figure_context_markdown(normalized_payload).encode("utf-8"),
                anchor=staging_anchor,
            )
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
        remove_anchored_tree(staging_anchor, staging / "figures")
        remove_anchored_file(staging_anchor, staging / "figures.json")
        remove_anchored_file(staging_anchor, staging / "figure_context.md")
        display_message = _display_pdf_path(
            str(exc),
            source_path=source_path,
            verified_source_path=verified_source_path,
        )
        if not complete:
            raise IncompleteFigureEvidenceError(display_message) from exc
        if isinstance(exc, FigureResourceLimitError):
            resource_checks = (exc.check,)
        else:
            preallocation_check = _preallocation_resource_check(exc)
            resource_checks = (preallocation_check,) if preallocation_check is not None else ()
        return PreparedFigures(
            artifacts=(),
            members=(),
            degraded=True,
            extraction_check=EvidenceResourceCheck(
                name="figure_extraction",
                status="degraded",
                actual=0,
                limit=figure_limit,
                message=f"{type(exc).__name__}: {display_message}",
            ),
            resource_checks=resource_checks,
        )


__all__ = [
    "ArtifactSpec",
    "IncompleteFigureEvidenceError",
    "PreparedFigures",
    "prepare_figure_artifacts",
]
