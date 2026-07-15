from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import secrets
import tarfile
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

import fitz

from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.storage import (
    HeldExactFileGuard,
    ImmutableTreeEntry,
    ImmutableTreeSnapshot,
    OwnedDirectoryAnchor,
    OwnedPublishedFile,
    anchored_entry_exists,
    atomic_publish_tree,
    canonical_json_bytes,
    create_anchored_directory,
    open_anchored_directory,
    open_anchored_regular_file,
    open_directory_anchor,
    open_resolved_source_guard,
    publish_bytes_no_replace,
    read_anchored_bytes,
    remove_anchored_file,
    snapshot_anchored_tree,
    stat_anchored_entry,
    validate_directory_anchor,
)

ARXIV_ID_PATTERN = re.compile(
    r"(?<!\d)(?:arxiv:)?(?P<id>(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}))(?:v\d+)?(?!\d)",
    re.IGNORECASE,
)
SOURCE_DIR_NAMES = ("pics", "figures", "fig", "images", "img")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
PDF_SUFFIXES = {".pdf"}
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = V2_RESOURCE_POLICY.arxiv_timeout_seconds
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
CACHE_COMPLETION_MARKER_NAME = ".paper-reader-arxiv-cache-completion.v2.json"
CACHE_COMPLETION_SCHEMA = "paper_reader.arxiv-cache-completion.v2"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class FigureCandidateLimitError(ValueError):
    def __init__(self, *, actual: int, limit: int) -> None:
        super().__init__(f"figure candidate count {actual} exceeds {limit}")
        self.actual = actual
        self.limit = limit


class FigurePixelLimitError(ValueError):
    def __init__(
        self,
        *,
        actual: int,
        limit: int,
        resource_name: str = "figure_pixels_each",
    ) -> None:
        label = "figure pixels total" if resource_name == "figure_pixels_total" else "figure pixels"
        super().__init__(f"{label} {actual} exceeds {limit}")
        self.actual = actual
        self.limit = limit
        self.resource_name = resource_name


def _close_all(
    owners: Any,
    *,
    primary_error: BaseException | None = None,
) -> None:
    first_cleanup_error: BaseException | None = None
    for owner in owners:
        try:
            owner.close()
        except BaseException as exc:
            if first_cleanup_error is None:
                first_cleanup_error = exc
    if primary_error is None and first_cleanup_error is not None:
        raise first_cleanup_error


def _remove_closed_published_file(
    anchor: OwnedDirectoryAnchor,
    published: OwnedPublishedFile,
) -> None:
    reopened = open_anchored_regular_file(
        anchor,
        published.path,
        expected_size=published.identity[2],
    )
    primary_error: BaseException | None = None
    try:
        if (
            reopened.identity != published.identity
            or reopened.content_sha256 != published.content_sha256
        ):
            raise ValueError(f"published file identity changed: {published.path}")
        remove_anchored_file(
            anchor,
            published.path,
            expected=reopened,
            missing_ok=True,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        _close_all([reopened], primary_error=primary_error)


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
    destination_root = Path(os.path.abspath(Path(workdir).expanduser()))
    cache_name = _cache_name_for_arxiv_id(arxiv_id)
    source_root = destination_root / cache_name
    legacy_archive_path = destination_root / f"{cache_name}.tar.gz"
    legacy_temp_root = destination_root / f".{cache_name}.tmp"
    attempt_token = secrets.token_hex(16)
    temp_root = destination_root / f".{cache_name}.{attempt_token}.tmp"
    url = f"https://arxiv.org/e-print/{arxiv_id}"
    effective_timeout = min(float(timeout_seconds), V2_RESOURCE_POLICY.arxiv_timeout_seconds)
    root_anchor: OwnedDirectoryAnchor | None = None
    temp_anchor: OwnedDirectoryAnchor | None = None
    primary_error: BaseException | None = None
    try:
        root_anchor = open_directory_anchor(destination_root, create=True)
        if anchored_entry_exists(root_anchor, source_root):
            try:
                with open_anchored_directory(root_anchor, source_root) as cached_anchor:
                    _validate_cache_anchor(cached_anchor, arxiv_id=arxiv_id)
                return source_root
            except (OSError, ValueError):
                return None
        # A fixed-name residue is not owned by this request.  Refuse it rather
        # than following, truncating, or deleting a hostile host entry.
        if anchored_entry_exists(root_anchor, legacy_archive_path) or anchored_entry_exists(
            root_anchor,
            legacy_temp_root,
        ):
            return None

        with urllib.request.urlopen(url, timeout=effective_timeout) as response:
            archive_bytes = _read_bounded_response(
                response,
                max_bytes=V2_RESOURCE_POLICY.arxiv_compressed_max_bytes,
            )
        temp_anchor = create_anchored_directory(root_anchor, temp_root)
        with io.BytesIO(archive_bytes) as archive_handle:
            payload_snapshot = _extract_source_package_file(archive_handle, temp_anchor)
        marker_bytes = _cache_completion_bytes(
            arxiv_id=arxiv_id,
            payload_snapshot=payload_snapshot,
        )
        marker = publish_bytes_no_replace(
            marker_bytes,
            temp_root / CACHE_COMPLETION_MARKER_NAME,
            anchor=temp_anchor,
            hold_open=True,
        )
        if not isinstance(marker, OwnedPublishedFile):  # pragma: no cover - API invariant
            raise ValueError("arXiv cache marker publication lost its held identity")
        marker.close()
        sealed_snapshot = _validate_cache_anchor(temp_anchor, arxiv_id=arxiv_id)
        try:
            atomic_publish_tree(
                temp_root,
                source_root,
                anchor=root_anchor,
                expected_staging_anchor=temp_anchor,
                expected_tree_snapshot=sealed_snapshot,
            )
        except (OSError, ValueError):
            if not _recover_exact_published_cache(
                root_anchor,
                source_root=source_root,
                staging_anchor=temp_anchor,
                arxiv_id=arxiv_id,
                expected_snapshot=sealed_snapshot,
            ):
                raise
        return source_root
    except FigureCandidateLimitError as exc:
        primary_error = exc
        raise
    except (OSError, tarfile.TarError, urllib.error.URLError, ValueError) as exc:
        primary_error = exc
        return None
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        owners = []
        if temp_anchor is not None:
            # Failed attempts use high-entropy hidden names.  Leave an
            # uncommitted quarantine tree behind rather than recursively
            # deleting through pathnames that may have raced.  A completed
            # rename removes this name atomically.
            owners.append(temp_anchor)
        if root_anchor is not None:
            owners.append(root_anchor)
        _close_all(owners, primary_error=primary_error)


def extract_source_package(archive_path: Path, output_dir: Path) -> Path:
    archive = Path(os.path.abspath(Path(archive_path).expanduser()))
    destination = Path(os.path.abspath(Path(output_dir).expanduser()))
    with open_resolved_source_guard(
        archive,
        max_bytes=V2_RESOURCE_POLICY.arxiv_compressed_max_bytes,
    ) as archive_guard, open_directory_anchor(destination, create=True) as destination_anchor:
        archive_guard.verify()
        archive_descriptor = os.dup(archive_guard.descriptor)
        os.lseek(archive_descriptor, 0, os.SEEK_SET)
        with os.fdopen(archive_descriptor, "rb") as archive_handle:
            _extract_source_package_file(archive_handle, destination_anchor)
        archive_guard.verify()
    return destination


def _extract_source_package_file(
    archive_handle: Any,
    destination_anchor: OwnedDirectoryAnchor,
) -> ImmutableTreeSnapshot:
    written_files: list[OwnedPublishedFile] = []
    primary_error: BaseException | None = None
    try:
        expected_entries: list[ImmutableTreeEntry] = []
        expected_directories: set[str] = set()
        with tarfile.open(fileobj=archive_handle, mode="r:*") as package:
            members = package.getmembers()
            _validate_archive_members(
                members,
                name_max=int(
                    os.fpathconf(destination_anchor.descriptor, "PC_NAME_MAX")
                ),
            )
            expanded_bytes = 0
            for member in members:
                relative_path = _safe_member_path(member)
                relative_posix = PurePosixPath(relative_path.as_posix())
                expected_directories.update(
                    parent.as_posix()
                    for parent in relative_posix.parents
                    if parent != PurePosixPath(".")
                )
                target_path = destination_anchor.path / relative_path
                if member.isdir():
                    expected_directories.add(relative_posix.as_posix())
                    with open_anchored_directory(
                        destination_anchor,
                        target_path,
                        create=True,
                    ):
                        pass
                    continue

                extracted = package.extractfile(member)
                if extracted is None:
                    raise ValueError(f"unsafe tar member: {member.name}")
                chunks: list[bytes] = []
                member_bytes = 0
                with extracted:
                    while chunk := extracted.read(DOWNLOAD_CHUNK_BYTES):
                        member_bytes += len(chunk)
                        expanded_bytes += len(chunk)
                        if member_bytes > member.size:
                            raise ValueError(f"unsafe tar member size: {member.name}")
                        if expanded_bytes > V2_RESOURCE_POLICY.arxiv_expanded_max_bytes:
                            raise ValueError("arXiv source expanded size exceeds the V2 cap")
                        chunks.append(chunk)
                if member_bytes != member.size:
                    raise ValueError(f"unsafe tar member size: {member.name}")
                content = b"".join(chunks)
                content_sha256 = hashlib.sha256(content).hexdigest()
                published = publish_bytes_no_replace(
                    content,
                    target_path,
                    anchor=destination_anchor,
                    hold_open=True,
                )
                if not isinstance(published, OwnedPublishedFile):  # pragma: no cover
                    raise ValueError("arXiv member publication lost its held identity")
                written_files.append(published)
                if (
                    published.identity[2] != len(content)
                    or published.content_sha256 != content_sha256
                ):
                    raise ValueError(
                        f"arXiv member publication changed: {relative_posix.as_posix()}"
                    )
                expected_entries.append(
                    ImmutableTreeEntry(
                        path=relative_posix.as_posix(),
                        kind="file",
                        size_bytes=len(content),
                        sha256=content_sha256,
                    )
                )
                published.close()
        expected_entries.extend(
            ImmutableTreeEntry(
                path=directory,
                kind="directory",
                size_bytes=0,
                sha256=None,
            )
            for directory in expected_directories
        )
        expected_snapshot = ImmutableTreeSnapshot(
            entries=tuple(
                sorted(expected_entries, key=lambda item: (item.path, item.kind))
            )
        )
        observed_snapshot = snapshot_anchored_tree(
            destination_anchor,
            max_file_bytes=V2_RESOURCE_POLICY.arxiv_expanded_max_bytes,
            max_total_bytes=V2_RESOURCE_POLICY.arxiv_expanded_max_bytes,
            max_members=_arxiv_payload_member_limit(),
            max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
        )
        if observed_snapshot != expected_snapshot:
            raise ValueError("extracted arXiv source tree differs from archive bytes")
        return expected_snapshot
    except BaseException as exc:
        primary_error = exc
        for published in reversed(written_files):
            try:
                _remove_closed_published_file(destination_anchor, published)
            except BaseException:
                pass
        raise
    finally:
        _close_all(written_files, primary_error=primary_error)


def _arxiv_payload_member_limit() -> int:
    return max(
        min(
            V2_RESOURCE_POLICY.arxiv_max_members,
            V2_RESOURCE_POLICY.artifact_tree_max_members - 1,
        ),
        0,
    )


def _cache_completion_bytes(
    *,
    arxiv_id: str,
    payload_snapshot: ImmutableTreeSnapshot,
) -> bytes:
    entries = [
        {
            "kind": entry.kind,
            "path": entry.path,
            "sha256": entry.sha256,
            "size_bytes": entry.size_bytes,
        }
        for entry in payload_snapshot.entries
    ]
    return canonical_json_bytes(
        {
            "arxiv_id": arxiv_id,
            "payload_entries": entries,
            "schema": CACHE_COMPLETION_SCHEMA,
        }
    )


def _parse_cache_completion(
    marker_bytes: bytes,
    *,
    arxiv_id: str,
) -> ImmutableTreeSnapshot:
    try:
        document = json.loads(marker_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid arXiv cache completion marker") from exc
    if type(document) is not dict or set(document) != {
        "arxiv_id",
        "payload_entries",
        "schema",
    }:
        raise ValueError("invalid arXiv cache completion marker")
    if document["schema"] != CACHE_COMPLETION_SCHEMA:
        raise ValueError("invalid arXiv cache completion marker schema")
    if document["arxiv_id"] != arxiv_id:
        raise ValueError("arXiv cache completion marker source mismatch")
    raw_entries = document["payload_entries"]
    if type(raw_entries) is not list or len(raw_entries) > _arxiv_payload_member_limit():
        raise ValueError("invalid arXiv cache completion marker member count")

    entries: list[ImmutableTreeEntry] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for raw_entry in raw_entries:
        if type(raw_entry) is not dict or set(raw_entry) != {
            "kind",
            "path",
            "sha256",
            "size_bytes",
        }:
            raise ValueError("invalid arXiv cache completion marker entry")
        path = raw_entry["path"]
        kind = raw_entry["kind"]
        size_bytes = raw_entry["size_bytes"]
        sha256 = raw_entry["sha256"]
        if type(path) is not str or not path or path in seen_paths:
            raise ValueError("invalid arXiv cache completion marker path")
        parsed_path = PurePosixPath(path)
        if (
            parsed_path.is_absolute()
            or parsed_path.as_posix() != path
            or ".." in parsed_path.parts
            or path == CACHE_COMPLETION_MARKER_NAME
            or len(parsed_path.parts) > V2_RESOURCE_POLICY.artifact_tree_max_depth
        ):
            raise ValueError("invalid arXiv cache completion marker path")
        if type(size_bytes) is not int or size_bytes < 0:
            raise ValueError("invalid arXiv cache completion marker size")
        if kind == "directory":
            if size_bytes != 0 or sha256 is not None:
                raise ValueError("invalid arXiv cache completion directory entry")
        elif kind == "file":
            if (
                size_bytes > V2_RESOURCE_POLICY.arxiv_expanded_max_bytes
                or type(sha256) is not str
                or SHA256_PATTERN.fullmatch(sha256) is None
            ):
                raise ValueError("invalid arXiv cache completion file entry")
            total_bytes += size_bytes
            if total_bytes > V2_RESOURCE_POLICY.arxiv_expanded_max_bytes:
                raise ValueError("arXiv cache payload exceeds the expanded-size cap")
        else:
            raise ValueError("invalid arXiv cache completion entry kind")
        seen_paths.add(path)
        entries.append(
            ImmutableTreeEntry(
                path=path,
                kind=kind,
                size_bytes=size_bytes,
                sha256=sha256,
            )
        )

    snapshot = ImmutableTreeSnapshot(entries=tuple(entries))
    if tuple(sorted(entries, key=lambda item: (item.path, item.kind))) != snapshot.entries:
        raise ValueError("arXiv cache completion entries are not canonical")
    if canonical_json_bytes(document) != marker_bytes:
        raise ValueError("arXiv cache completion marker is not canonical")
    return snapshot


def _validate_cache_anchor(
    cache_anchor: OwnedDirectoryAnchor,
    *,
    arxiv_id: str,
) -> ImmutableTreeSnapshot:
    validate_directory_anchor(cache_anchor)
    marker_path = cache_anchor.path / CACHE_COMPLETION_MARKER_NAME
    marker_metadata = stat_anchored_entry(cache_anchor, marker_path)
    if marker_metadata.st_size > V2_RESOURCE_POLICY.structured_artifact_max_bytes:
        raise ValueError("arXiv cache completion marker exceeds the artifact cap")
    marker_bytes = read_anchored_bytes(
        cache_anchor,
        marker_path,
        expected_size=marker_metadata.st_size,
        max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
    )
    payload_snapshot = _parse_cache_completion(marker_bytes, arxiv_id=arxiv_id)
    marker_entry = ImmutableTreeEntry(
        path=CACHE_COMPLETION_MARKER_NAME,
        kind="file",
        size_bytes=len(marker_bytes),
        sha256=hashlib.sha256(marker_bytes).hexdigest(),
    )
    expected_snapshot = ImmutableTreeSnapshot(
        entries=tuple(
            sorted(
                (*payload_snapshot.entries, marker_entry),
                key=lambda item: (item.path, item.kind),
            )
        )
    )
    observed_snapshot = snapshot_anchored_tree(
        cache_anchor,
        max_file_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
        max_total_bytes=V2_RESOURCE_POLICY.run_max_bytes,
        max_members=V2_RESOURCE_POLICY.artifact_tree_max_members,
        max_depth=V2_RESOURCE_POLICY.artifact_tree_max_depth,
    )
    validate_directory_anchor(cache_anchor)
    if observed_snapshot != expected_snapshot:
        raise ValueError("arXiv cache closed set does not match its completion marker")
    return expected_snapshot


def _recover_exact_published_cache(
    root_anchor: OwnedDirectoryAnchor,
    *,
    source_root: Path,
    staging_anchor: OwnedDirectoryAnchor,
    arxiv_id: str,
    expected_snapshot: ImmutableTreeSnapshot,
) -> bool:
    try:
        metadata = stat_anchored_entry(root_anchor, source_root)
        if (metadata.st_dev, metadata.st_ino) != (
            staging_anchor.device,
            staging_anchor.inode,
        ):
            return False
        with open_anchored_directory(root_anchor, source_root) as published_anchor:
            if (published_anchor.device, published_anchor.inode) != (
                staging_anchor.device,
                staging_anchor.inode,
            ):
                return False
            observed_snapshot = _validate_cache_anchor(
                published_anchor,
                arxiv_id=arxiv_id,
            )
            if observed_snapshot != expected_snapshot:
                return False
        return True
    except (OSError, ValueError):
        return False


def collect_source_figures(
    source_root: Path,
    output_dir: Path,
    *,
    max_candidates: int | None = None,
    expected_arxiv_id: str | None = None,
) -> list[dict[str, Any]]:
    if expected_arxiv_id is not None and (
        type(expected_arxiv_id) is not str or not expected_arxiv_id
    ):
        raise ValueError("expected arXiv id must be a non-empty string")
    root = Path(os.path.abspath(Path(source_root).expanduser()))
    destination = Path(os.path.abspath(Path(output_dir).expanduser()))

    source_anchor = open_directory_anchor(root)
    source_guards: list[HeldExactFileGuard] = []
    primary_error: BaseException | None = None
    try:
        sealed_arxiv_id = expected_arxiv_id
        marker_path = root / CACHE_COMPLETION_MARKER_NAME
        if sealed_arxiv_id is None and anchored_entry_exists(source_anchor, marker_path):
            marker_metadata = stat_anchored_entry(source_anchor, marker_path)
            marker_bytes = read_anchored_bytes(
                source_anchor,
                marker_path,
                expected_size=marker_metadata.st_size,
                max_bytes=V2_RESOURCE_POLICY.structured_artifact_max_bytes,
            )
            try:
                marker_document = json.loads(marker_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid arXiv cache completion marker") from exc
            if (
                type(marker_document) is not dict
                or set(marker_document) != {"arxiv_id", "payload_entries", "schema"}
                or marker_document.get("schema") != CACHE_COMPLETION_SCHEMA
                or type(marker_document.get("arxiv_id")) is not str
                or not marker_document["arxiv_id"]
            ):
                raise ValueError("invalid arXiv cache completion marker")
            sealed_arxiv_id = marker_document["arxiv_id"]

        if sealed_arxiv_id is not None:
            initial_snapshot = _validate_cache_anchor(
                source_anchor,
                arxiv_id=sealed_arxiv_id,
            )
        else:
            initial_snapshot = snapshot_anchored_tree(
                source_anchor,
                max_file_bytes=V2_RESOURCE_POLICY.arxiv_expanded_max_bytes,
                max_total_bytes=V2_RESOURCE_POLICY.arxiv_expanded_max_bytes,
                max_members=V2_RESOURCE_POLICY.arxiv_max_members,
            )
        candidates = {
            entry.path: entry
            for entry in initial_snapshot.entries
            if entry.kind == "file"
            and _is_supported_figure(Path(entry.path))
            and (
                len(PurePosixPath(entry.path).parts) == 1
                or _is_figure_path(Path(entry.path))
            )
        }

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

        copied_names: dict[str, tuple[str, str]] = {}
        for rel_path in sorted(candidates):
            copied_name = _copied_name(rel_path)
            collision_key = _portable_name_key(copied_name)
            if previous := copied_names.get(collision_key):
                raise ValueError(
                    "arXiv source figure output name collision: "
                    f"{previous[1]} and {rel_path}"
                )
            copied_names[collision_key] = (copied_name, rel_path)
        _validate_output_file_names(
            destination,
            [item[0] for item in copied_names.values()],
        )

        candidate_bytes: dict[str, bytes] = {}
        for rel_path, entry in sorted(candidates.items()):
            source_path = root / Path(*PurePosixPath(rel_path).parts)
            owned_source = open_anchored_regular_file(
                source_anchor,
                source_path,
                expected_size=entry.size_bytes,
            )
            try:
                content = _read_owned_file_bytes(owned_source)
            except BaseException as exc:
                _close_all([owned_source], primary_error=exc)
                raise
            source_guard = HeldExactFileGuard(
                anchor=source_anchor,
                owned_file=owned_source,
                expected_bytes=content,
                label=f"arXiv source figure {rel_path}",
            )
            source_guards.append(source_guard)
            source_guard.verify()
            if (
                owned_source.content_sha256 != entry.sha256
                or hashlib.sha256(content).hexdigest() != entry.sha256
            ):
                raise ValueError(f"arXiv source figure changed after discovery: {rel_path}")
            candidate_bytes[rel_path] = content
        if sealed_arxiv_id is not None:
            final_snapshot = _validate_cache_anchor(
                source_anchor,
                arxiv_id=sealed_arxiv_id,
            )
        else:
            final_snapshot = snapshot_anchored_tree(
                source_anchor,
                max_file_bytes=V2_RESOURCE_POLICY.arxiv_expanded_max_bytes,
                max_total_bytes=V2_RESOURCE_POLICY.arxiv_expanded_max_bytes,
                max_members=V2_RESOURCE_POLICY.arxiv_max_members,
            )
        if final_snapshot != initial_snapshot:
            raise ValueError("arXiv source tree changed while figures were collected")

        destination_anchor = open_directory_anchor(destination, create=True)
        published_files: list[OwnedPublishedFile] = []
        expected_output_entries: list[ImmutableTreeEntry] = []
        figures: list[dict[str, Any]] = []
        destination_error: BaseException | None = None
        try:
            for rel_path, entry in sorted(candidates.items()):
                source_path = root / Path(*PurePosixPath(rel_path).parts)
                copied_name = _copied_name(rel_path)
                copied_path = destination / copied_name
                published = publish_bytes_no_replace(
                    candidate_bytes[rel_path],
                    copied_path,
                    anchor=destination_anchor,
                    hold_open=True,
                )
                if not isinstance(published, OwnedPublishedFile):  # pragma: no cover
                    raise ValueError("arXiv figure publication lost its held identity")
                published_files.append(published)
                published.close()
                content = candidate_bytes[rel_path]
                content_sha256 = hashlib.sha256(content).hexdigest()
                expected_output_entries.append(
                    ImmutableTreeEntry(
                        path=copied_name,
                        kind="file",
                        size_bytes=len(content),
                        sha256=content_sha256,
                    )
                )
                figures.append(
                    {
                        "rel_path": rel_path,
                        "media_type": "pdf"
                        if source_path.suffix.lower() in PDF_SUFFIXES
                        else "image",
                        "image_path": str(copied_path),
                        "source_path": str(source_path),
                        "source": "arxiv-source",
                        "artifact_size_bytes": len(content),
                        "artifact_sha256": content_sha256,
                    }
                )
            for source_guard in source_guards:
                source_guard.verify()
            expected_output = ImmutableTreeSnapshot(
                entries=tuple(
                    sorted(
                        expected_output_entries,
                        key=lambda item: (item.path, item.kind),
                    )
                )
            )
            observed_output = snapshot_anchored_tree(
                destination_anchor,
                max_file_bytes=V2_RESOURCE_POLICY.arxiv_expanded_max_bytes,
                max_total_bytes=V2_RESOURCE_POLICY.arxiv_expanded_max_bytes,
                max_members=V2_RESOURCE_POLICY.figure_max_candidates,
                max_depth=1,
            )
            if observed_output != expected_output:
                raise ValueError("collected arXiv figure output tree changed")
            if sealed_arxiv_id is not None and _validate_cache_anchor(
                source_anchor,
                arxiv_id=sealed_arxiv_id,
            ) != initial_snapshot:
                raise ValueError("sealed arXiv source tree changed before figure return")
            return figures
        except BaseException as exc:
            destination_error = exc
            for published in reversed(published_files):
                try:
                    _remove_closed_published_file(destination_anchor, published)
                except BaseException:
                    pass
            raise
        finally:
            _close_all(
                [*published_files, destination_anchor],
                primary_error=destination_error,
            )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        _close_all([*source_guards, source_anchor], primary_error=primary_error)


def render_source_figure_pdfs(
    source_figures: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    if len(source_figures) > V2_RESOURCE_POLICY.figure_max_candidates:
        raise FigureCandidateLimitError(
            actual=len(source_figures),
            limit=V2_RESOURCE_POLICY.figure_max_candidates,
        )

    pdf_sources: list[tuple[dict[str, Any], Path]] = []
    rendered_names: dict[str, tuple[str, str]] = {}
    for figure in source_figures:
        figure_path = Path(
            os.path.abspath(Path(str(figure["image_path"])).expanduser())
        )
        if figure_path.suffix.lower() != ".pdf":
            continue
        image_name = _rendered_pdf_name(figure, figure_path)
        source_label = str(figure.get("rel_path") or figure_path)
        collision_key = _portable_name_key(image_name)
        if previous := rendered_names.get(collision_key):
            raise ValueError(
                "rendered arXiv figure output name collision: "
                f"{previous[1]} and {source_label}"
            )
        rendered_names[collision_key] = (image_name, source_label)
        pdf_sources.append((figure, figure_path))

    destination = Path(os.path.abspath(Path(output_dir).expanduser()))
    _validate_output_file_names(
        destination,
        [item[0] for item in rendered_names.values()],
    )
    source_anchor: OwnedDirectoryAnchor | None = None
    source_guards: list[HeldExactFileGuard] = []
    pdf_figures: list[tuple[dict[str, Any], Path, bytes]] = []
    aggregate_pdf_bytes = 0
    aggregate_pdf_pixels = 0
    primary_error: BaseException | None = None
    try:
        if pdf_sources:
            source_roots = {figure_path.anchor for _figure, figure_path in pdf_sources}
            if len(source_roots) != 1 or not next(iter(source_roots)):
                raise ValueError("source figure PDFs must share one absolute filesystem root")
            source_anchor = open_directory_anchor(next(iter(source_roots)))
        for figure, figure_path in pdf_sources:
            if source_anchor is None:  # pragma: no cover - guarded by pdf_sources
                raise ValueError("source figure root anchor is unavailable")
            metadata = stat_anchored_entry(source_anchor, figure_path)
            if metadata.st_size > V2_RESOURCE_POLICY.arxiv_expanded_max_bytes:
                raise ValueError("source figure PDF exceeds the arXiv expanded-size cap")
            aggregate_pdf_bytes += metadata.st_size
            if aggregate_pdf_bytes > V2_RESOURCE_POLICY.arxiv_expanded_max_bytes:
                raise ValueError(
                    "source figure PDF aggregate input bytes exceed the arXiv expanded-size cap"
                )
            owned_source = open_anchored_regular_file(
                source_anchor,
                figure_path,
                expected_size=metadata.st_size,
            )
            try:
                pdf_bytes = _read_owned_file_bytes(owned_source)
            except BaseException as exc:
                _close_all([owned_source], primary_error=exc)
                raise
            _validate_optional_artifact_binding(figure, owned_source, pdf_bytes)
            source_guard = HeldExactFileGuard(
                anchor=source_anchor,
                owned_file=owned_source,
                expected_bytes=pdf_bytes,
                label=f"arXiv source figure PDF {figure_path}",
            )
            source_guards.append(source_guard)
            source_guard.verify()
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                if doc.page_count < 1:
                    raise ValueError("source figure PDF has no pages")
                page = doc.load_page(0)
                aggregate_pdf_pixels += _enforce_pixel_limit(
                    page.rect.width,
                    page.rect.height,
                )
                if aggregate_pdf_pixels > V2_RESOURCE_POLICY.figure_max_pixels_total:
                    raise FigurePixelLimitError(
                        actual=aggregate_pdf_pixels,
                        limit=V2_RESOURCE_POLICY.figure_max_pixels_total,
                        resource_name="figure_pixels_total",
                    )
            pdf_figures.append((figure, figure_path, pdf_bytes))

        rendered_payloads: list[tuple[dict[str, Any], Path, str, bytes]] = []
        aggregate_rendered_bytes = 0
        for figure, figure_path, pdf_bytes in pdf_figures:
            image_name = _rendered_pdf_name(figure, figure_path)
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                page = doc.load_page(0)
                pixmap = page.get_pixmap()
                image_bytes = pixmap.tobytes("png")
            aggregate_rendered_bytes += len(image_bytes)
            if aggregate_rendered_bytes > V2_RESOURCE_POLICY.figure_max_bytes_total:
                raise ValueError(
                    "rendered source figure aggregate bytes exceed the V2 cap"
                )
            rendered_payloads.append(
                (figure, figure_path, image_name, image_bytes)
            )

        rendered: list[dict[str, Any]] = []
        published_files: list[OwnedPublishedFile] = []
        expected_output_entries: list[ImmutableTreeEntry] = []
        destination_anchor = open_directory_anchor(destination, create=True)
        destination_error: BaseException | None = None
        try:
            for figure, figure_path, image_name, image_bytes in rendered_payloads:
                image_path = destination / image_name
                published = publish_bytes_no_replace(
                    image_bytes,
                    image_path,
                    anchor=destination_anchor,
                    hold_open=True,
                )
                if not isinstance(published, OwnedPublishedFile):  # pragma: no cover
                    raise ValueError("rendered arXiv figure lost its held identity")
                published_files.append(published)
                published.close()
                image_sha256 = hashlib.sha256(image_bytes).hexdigest()
                expected_output_entries.append(
                    ImmutableTreeEntry(
                        path=image_name,
                        kind="file",
                        size_bytes=len(image_bytes),
                        sha256=image_sha256,
                    )
                )
                rendered.append(
                    {
                        **figure,
                        "media_type": "image",
                        "image_path": str(image_path),
                        "source": "pdf-figure",
                        "artifact_size_bytes": len(image_bytes),
                        "artifact_sha256": image_sha256,
                    }
                )
            for source_guard in source_guards:
                source_guard.verify()
            expected_output = ImmutableTreeSnapshot(
                entries=tuple(
                    sorted(
                        expected_output_entries,
                        key=lambda item: (item.path, item.kind),
                    )
                )
            )
            observed_output = snapshot_anchored_tree(
                destination_anchor,
                max_file_bytes=V2_RESOURCE_POLICY.figure_max_bytes_total,
                max_total_bytes=V2_RESOURCE_POLICY.figure_max_bytes_total,
                max_members=V2_RESOURCE_POLICY.figure_max_candidates,
                max_depth=1,
            )
            if observed_output != expected_output:
                raise ValueError("rendered arXiv figure output tree changed")
            return rendered
        except BaseException as exc:
            destination_error = exc
            for published in reversed(published_files):
                try:
                    _remove_closed_published_file(destination_anchor, published)
                except BaseException:
                    pass
            raise
        finally:
            _close_all(
                [*published_files, destination_anchor],
                primary_error=destination_error,
            )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        _close_all(
            [*source_guards, *([source_anchor] if source_anchor is not None else [])],
            primary_error=primary_error,
        )


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


def _read_bounded_response(response: Any, *, max_bytes: int) -> bytes:
    written = 0
    chunks: list[bytes] = []
    while True:
        try:
            chunk = response.read(min(DOWNLOAD_CHUNK_BYTES, max_bytes - written + 1))
        except TypeError:
            # Compatibility with small response doubles that only expose read().
            chunk = response.read()
            if len(chunk) > max_bytes - written:
                raise ValueError("arXiv source compressed size exceeds the V2 cap")
            chunks.append(chunk)
            return b"".join(chunks)
        if not chunk:
            return b"".join(chunks)
        written += len(chunk)
        if written > max_bytes:
            raise ValueError("arXiv source compressed size exceeds the V2 cap")
        chunks.append(chunk)


def _read_owned_file_bytes(owned: OwnedPublishedFile) -> bytes:
    expected_size = owned.identity[2]
    chunks: list[bytes] = []
    offset = 0
    while offset < expected_size:
        chunk = os.pread(
            owned.descriptor,
            min(DOWNLOAD_CHUNK_BYTES, expected_size - offset),
            offset,
        )
        if not chunk:
            raise ValueError(f"held arXiv source file became short: {owned.path}")
        chunks.append(chunk)
        offset += len(chunk)
    if os.pread(owned.descriptor, 1, expected_size):
        raise ValueError(f"held arXiv source file grew while read: {owned.path}")
    return b"".join(chunks)


def _portable_name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _filesystem_name_max(path: Path) -> int:
    probe = Path(os.path.abspath(path))
    while True:
        try:
            os.lstat(probe)
            return int(os.pathconf(probe, "PC_NAME_MAX"))
        except FileNotFoundError:
            if probe == probe.parent:
                raise
            probe = probe.parent


def _validate_output_file_names(destination: Path, names: list[str]) -> None:
    name_max = _filesystem_name_max(destination.parent)
    for name in names:
        if len(os.fsencode(name)) > name_max:
            raise ValueError(
                f"generated output file name exceeds filesystem limit {name_max}: {name}"
            )


def _validate_optional_artifact_binding(
    figure: dict[str, Any],
    owned_source: OwnedPublishedFile,
    content: bytes,
) -> None:
    expected_size = figure.get("artifact_size_bytes")
    expected_sha256 = figure.get("artifact_sha256")
    if expected_size is None and expected_sha256 is None:
        return
    if (
        type(expected_size) is not int
        or expected_size < 0
        or type(expected_sha256) is not str
        or SHA256_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise ValueError("source figure artifact binding is invalid")
    if (
        owned_source.identity[2] != expected_size
        or len(content) != expected_size
        or owned_source.content_sha256 != expected_sha256
        or hashlib.sha256(content).hexdigest() != expected_sha256
    ):
        raise ValueError("source figure PDF does not match its bound size/hash")


def _validate_archive_members(
    members: list[tarfile.TarInfo],
    *,
    name_max: int,
) -> None:
    if type(name_max) is not int or name_max < 1:
        raise ValueError("arXiv source filesystem name limit is invalid")
    if len(members) > V2_RESOURCE_POLICY.arxiv_max_members:
        raise ValueError("arXiv source member count exceeds the V2 cap")

    # The downloaded cache adds one completion marker before the extracted tree
    # is atomically published.  Validate the materialized closed set (including
    # implicit parent directories) against that final publication boundary
    # before extracting the first byte.
    payload_member_limit = _arxiv_payload_member_limit()
    materialized_paths: set[str] = set()
    declared_kinds: dict[str, str] = {}
    required_directories: set[str] = set()
    portable_paths: dict[str, str] = {}
    expanded_bytes = 0
    figure_count = 0
    for member in members:
        relative_path = _safe_member_path(member)
        relative_parts = PurePosixPath(relative_path.as_posix()).parts
        if not relative_parts:
            raise ValueError(f"unsafe tar member path: {member.name}")
        if any(len(os.fsencode(component)) > name_max for component in relative_parts):
            raise ValueError(
                f"arXiv source member file name exceeds filesystem limit {name_max}: "
                f"{member.name}"
            )
        if len(relative_parts) > V2_RESOURCE_POLICY.artifact_tree_max_depth:
            raise ValueError("arXiv source tree depth exceeds the V2 cap")
        if not member.isdir() and not member.isfile():
            raise ValueError(f"unsafe tar member type: {member.name}")
        normalized_path = PurePosixPath(*relative_parts).as_posix()
        if relative_parts[0] == CACHE_COMPLETION_MARKER_NAME:
            raise ValueError(
                "arXiv source member uses the reserved cache completion marker"
            )
        if normalized_path in declared_kinds:
            raise ValueError(f"duplicate arXiv source member: {normalized_path}")
        member_kind = "directory" if member.isdir() else "file"
        for index in range(1, len(relative_parts)):
            parent_path = PurePosixPath(*relative_parts[:index]).as_posix()
            if declared_kinds.get(parent_path) == "file":
                raise ValueError(
                    "arXiv source member file/directory conflict: "
                    f"{parent_path}"
                )
            required_directories.add(parent_path)
        if member_kind == "file" and normalized_path in required_directories:
            raise ValueError(
                "arXiv source member file/directory conflict: "
                f"{normalized_path}"
            )
        declared_kinds[normalized_path] = member_kind
        for index in range(1, len(relative_parts) + 1):
            materialized_path = PurePosixPath(*relative_parts[:index]).as_posix()
            portable_key = _portable_name_key(materialized_path)
            if (
                (previous_path := portable_paths.get(portable_key)) is not None
                and previous_path != materialized_path
            ):
                raise ValueError(
                    "portable arXiv source path collision: "
                    f"{previous_path} and {materialized_path}"
                )
            portable_paths[portable_key] = materialized_path
            materialized_paths.add(materialized_path)
        if len(materialized_paths) > payload_member_limit:
            raise ValueError("arXiv source member count exceeds the V2 cap")
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
