from __future__ import annotations

import math
import re
import shutil
import tarfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

import fitz

from paper_reader.resource_policy import V2_RESOURCE_POLICY

ARXIV_ID_PATTERN = re.compile(
    r"(?<!\d)(?:arxiv:)?(?P<id>(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}))(?:v\d+)?(?!\d)",
    re.IGNORECASE,
)
SOURCE_DIR_NAMES = ("pics", "figures", "fig", "images", "img")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
PDF_SUFFIXES = {".pdf"}
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = V2_RESOURCE_POLICY.arxiv_timeout_seconds
DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class FigureCandidateLimitError(ValueError):
    def __init__(self, *, actual: int, limit: int) -> None:
        super().__init__(f"figure candidate count {actual} exceeds {limit}")
        self.actual = actual
        self.limit = limit


class FigurePixelLimitError(ValueError):
    def __init__(self, *, actual: int, limit: int) -> None:
        super().__init__(f"figure pixels {actual} exceeds {limit}")
        self.actual = actual
        self.limit = limit


def resolve_arxiv_id(details: dict[str, Any], pdf_path: Path | None = None) -> str | None:
    for key in ("url", "archiveLocation", "extra"):
        arxiv_id = _extract_arxiv_id(details.get(key))
        if arxiv_id is not None:
            return arxiv_id

    attachments = details.get("attachments", [])
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            arxiv_id = _extract_arxiv_id(attachment.get("filename"))
            if arxiv_id is not None:
                return arxiv_id

    if pdf_path is not None:
        return _extract_arxiv_id(pdf_path.name)
    return None


def download_arxiv_source(
    arxiv_id: str,
    workdir: Path,
    *,
    timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
) -> Path | None:
    destination_root = Path(workdir).expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    cache_name = _cache_name_for_arxiv_id(arxiv_id)
    source_root = destination_root / cache_name
    if source_root.exists():
        return source_root

    archive_path = destination_root / f"{cache_name}.tar.gz"
    temp_root = destination_root / f".{cache_name}.tmp"
    url = f"https://arxiv.org/e-print/{arxiv_id}"
    effective_timeout = min(float(timeout_seconds), V2_RESOURCE_POLICY.arxiv_timeout_seconds)

    if temp_root.exists():
        shutil.rmtree(temp_root)

    try:
        with urllib.request.urlopen(url, timeout=effective_timeout) as response:
            _write_bounded_response(
                response,
                archive_path,
                max_bytes=V2_RESOURCE_POLICY.arxiv_compressed_max_bytes,
            )
        extract_source_package(archive_path, temp_root)
        temp_root.replace(source_root)
        return source_root
    except FigureCandidateLimitError:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    except (OSError, tarfile.TarError, urllib.error.URLError, ValueError):
        shutil.rmtree(temp_root, ignore_errors=True)
        return None
    finally:
        archive_path.unlink(missing_ok=True)


def extract_source_package(archive_path: Path, output_dir: Path) -> Path:
    archive = Path(archive_path).expanduser().resolve()
    if archive.stat().st_size > V2_RESOURCE_POLICY.arxiv_compressed_max_bytes:
        raise ValueError("arXiv source compressed size exceeds the V2 cap")
    destination = Path(output_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)

    written_paths: list[Path] = []
    try:
        with tarfile.open(archive) as package:
            members = package.getmembers()
            _validate_archive_members(members)
            expanded_bytes = 0
            for member in members:
                relative_path = _safe_member_path(member)
                target_path = destination / relative_path
                if member.isdir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue

                target_path.parent.mkdir(parents=True, exist_ok=True)
                extracted = package.extractfile(member)
                if extracted is None:
                    raise ValueError(f"unsafe tar member: {member.name}")
                written_paths.append(target_path)
                with target_path.open("wb") as handle:
                    while chunk := extracted.read(DOWNLOAD_CHUNK_BYTES):
                        expanded_bytes += len(chunk)
                        if expanded_bytes > V2_RESOURCE_POLICY.arxiv_expanded_max_bytes:
                            raise ValueError("arXiv source expanded size exceeds the V2 cap")
                        handle.write(chunk)
    except Exception:
        for path in reversed(written_paths):
            path.unlink(missing_ok=True)
        raise

    return destination


def collect_source_figures(
    source_root: Path,
    output_dir: Path,
    *,
    max_candidates: int | None = None,
) -> list[dict[str, Any]]:
    root = Path(source_root).expanduser().resolve()
    destination = Path(output_dir).expanduser()

    candidates: dict[str, Path] = {}
    for child in root.iterdir():
        if child.is_file() and _is_supported_figure(child):
            candidates[child.name] = child

    for child in sorted(root.rglob("*")):
        if not child.is_file() or not _is_supported_figure(child):
            continue
        if not _is_figure_path(child.relative_to(root)):
            continue
        rel_path = child.relative_to(root).as_posix()
        candidates[rel_path] = child

    candidate_limit = (
        V2_RESOURCE_POLICY.figure_max_candidates
        if max_candidates is None
        else max_candidates
    )
    if candidate_limit < 0:
        raise ValueError("figure candidate limit must be non-negative")
    if len(candidates) > candidate_limit:
        raise FigureCandidateLimitError(
            actual=len(candidates),
            limit=candidate_limit,
        )
    if len(candidates) > V2_RESOURCE_POLICY.arxiv_max_figure_files:
        raise ValueError("arXiv source figure count exceeds the V2 cap")

    destination.mkdir(parents=True, exist_ok=True)
    figures: list[dict[str, Any]] = []
    for rel_path, source_path in sorted(candidates.items()):
        copied_name = _copied_name(rel_path)
        copied_path = destination / copied_name
        shutil.copy2(source_path, copied_path)
        figures.append(
            {
                "rel_path": rel_path,
                "media_type": "pdf" if source_path.suffix.lower() in PDF_SUFFIXES else "image",
                "image_path": str(copied_path),
                "source_path": str(source_path),
                "source": "arxiv-source",
            }
        )

    return figures


def render_source_figure_pdfs(
    source_figures: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    if len(source_figures) > V2_RESOURCE_POLICY.figure_max_candidates:
        raise FigureCandidateLimitError(
            actual=len(source_figures),
            limit=V2_RESOURCE_POLICY.figure_max_candidates,
        )

    destination = Path(output_dir).expanduser()
    pdf_figures: list[tuple[dict[str, Any], Path]] = []
    for figure in source_figures:
        figure_path = Path(str(figure["image_path"]))
        if figure_path.suffix.lower() != ".pdf":
            continue
        with fitz.open(figure_path) as doc:
            if doc.page_count < 1:
                raise ValueError("source figure PDF has no pages")
            page = doc.load_page(0)
            _enforce_pixel_limit(page.rect.width, page.rect.height)
        pdf_figures.append((figure, figure_path))

    destination.mkdir(parents=True, exist_ok=True)
    rendered: list[dict[str, Any]] = []
    for figure, figure_path in pdf_figures:
        image_name = _rendered_pdf_name(figure, figure_path)
        image_path = destination / image_name
        with fitz.open(figure_path) as doc:
            page = doc.load_page(0)
            pixmap = page.get_pixmap()
            pixmap.save(image_path)

        rendered.append(
            {
                **figure,
                "media_type": "image",
                "image_path": str(image_path),
                "source": "pdf-figure",
            }
        )

    return rendered


def _enforce_pixel_limit(width: float, height: float) -> int:
    pixel_width = max(math.ceil(float(width)), 0)
    pixel_height = max(math.ceil(float(height)), 0)
    pixels = pixel_width * pixel_height
    if pixels > V2_RESOURCE_POLICY.figure_max_pixels_each:
        raise FigurePixelLimitError(
            actual=pixels,
            limit=V2_RESOURCE_POLICY.figure_max_pixels_each,
        )
    return pixels


def _extract_arxiv_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = ARXIV_ID_PATTERN.search(value)
    if match is None:
        return None
    return match.group("id").lower()


def _write_bounded_response(response: Any, destination: Path, *, max_bytes: int) -> None:
    written = 0
    with destination.open("wb") as handle:
        while True:
            try:
                chunk = response.read(min(DOWNLOAD_CHUNK_BYTES, max_bytes - written + 1))
            except TypeError:
                # Compatibility with small response doubles that only expose read().
                chunk = response.read()
                if len(chunk) > max_bytes:
                    raise ValueError("arXiv source compressed size exceeds the V2 cap")
                handle.write(chunk)
                return
            if not chunk:
                return
            written += len(chunk)
            if written > max_bytes:
                raise ValueError("arXiv source compressed size exceeds the V2 cap")
            handle.write(chunk)


def _validate_archive_members(members: list[tarfile.TarInfo]) -> None:
    if len(members) > V2_RESOURCE_POLICY.arxiv_max_members:
        raise ValueError("arXiv source member count exceeds the V2 cap")

    expanded_bytes = 0
    figure_count = 0
    for member in members:
        relative_path = _safe_member_path(member)
        if not member.isdir() and not member.isfile():
            raise ValueError(f"unsafe tar member type: {member.name}")
        if not member.isfile():
            continue
        if member.size < 0:
            raise ValueError(f"unsafe tar member size: {member.name}")
        expanded_bytes += member.size
        if expanded_bytes > V2_RESOURCE_POLICY.arxiv_expanded_max_bytes:
            raise ValueError("arXiv source expanded size exceeds the V2 cap")
        if _is_supported_figure(relative_path):
            figure_count += 1

    if figure_count > V2_RESOURCE_POLICY.figure_max_candidates:
        raise FigureCandidateLimitError(
            actual=figure_count,
            limit=V2_RESOURCE_POLICY.figure_max_candidates,
        )
    if figure_count > V2_RESOURCE_POLICY.arxiv_max_figure_files:
        raise ValueError("arXiv source figure count exceeds the V2 cap")


def _cache_name_for_arxiv_id(arxiv_id: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "__", arxiv_id.strip())
    safe_name = safe_name.strip("_")
    return safe_name or "source"


def _safe_member_path(member: tarfile.TarInfo) -> Path:
    if member.issym() or member.islnk():
        raise ValueError(f"unsafe symlink member: {member.name}")

    member_path = PurePosixPath(member.name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise ValueError(f"unsafe tar member path: {member.name}")

    return Path(*member_path.parts)


def _is_supported_figure(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in IMAGE_SUFFIXES or suffix in PDF_SUFFIXES


def _is_figure_path(rel_path: Path) -> bool:
    return any(part.lower() in SOURCE_DIR_NAMES for part in rel_path.parts[:-1])


def _copied_name(rel_path: str) -> str:
    return rel_path.replace("/", "__")


def _rendered_pdf_name(figure: dict[str, Any], figure_path: Path) -> str:
    rel_path = figure.get("rel_path")
    if isinstance(rel_path, str) and rel_path:
        return f"{Path(_copied_name(rel_path)).stem}.png"
    return f"{figure_path.stem}.png"
