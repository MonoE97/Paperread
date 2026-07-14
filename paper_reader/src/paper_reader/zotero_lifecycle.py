from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import date
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from paper_reader.contracts import (
    ArtifactRef,
    GateState,
    Identifier,
    LocalSourceIdentity,
    PaperReaderRun,
    ZoteroSourceIdentity,
)
from paper_reader.local_lifecycle import LocalLifecycleError, _local_source_identity
from paper_reader.resource_policy import V2_RESOURCE_POLICY
from paper_reader.runs import slugify_title
from paper_reader.storage import (
    DirectoryAnchorLike,
    PublishConflictError,
    atomic_publish_tree,
    atomic_write_bytes,
    canonical_json_bytes,
    canonical_json_sha256,
    create_anchored_directory,
    new_random_id,
    new_uuid,
    open_anchored_directory,
    open_resolved_source_guard,
    remove_anchored_tree,
    rfc3339_utc,
    tree_snapshot_from_bytes,
)
from paper_reader.v2_loader import DirectoryAnchor, RunLoadError
from paper_reader.workflow import select_pdf_attachment
from paper_reader.zotero_item_io import normalize_item_details_payload


DEFAULT_SKILL_ROOT = Path(__file__).resolve().parents[2]
_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)


class ZoteroLifecycleError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        data: dict[str, str | int] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


@dataclass(frozen=True, slots=True)
class InitializedZoteroRun:
    run_dir: Path
    run: PaperReaderRun


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _validated_identifier(value: object, *, context: str, code: str) -> str:
    try:
        return _IDENTIFIER_ADAPTER.validate_python(value, strict=True)
    except ValidationError as exc:
        raise ZoteroLifecycleError(
            code,
            f"{context} must be a valid Identifier",
        ) from exc


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def display_title(value: object) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(unescape(str(value)))
        parser.close()
    except Exception as exc:
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            f"Zotero title contains invalid HTML: {exc}",
        ) from exc
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def normalized_title(value: object) -> str:
    return display_title(value).casefold()


def normalized_doi(value: object) -> str:
    return str(value or "").strip().casefold()


def _non_negative_version(value: object, *, context: str) -> int:
    if type(value) is not int or value < 0:
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            f"{context} version must be a non-negative integer",
        )
    return value


def _required_version(payload: dict[str, Any], *, context: str) -> int:
    if "version" not in payload:
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            f"{context} version is required",
        )
    return _non_negative_version(payload["version"], context=context)


def _validated_item_type(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            f"{context} itemType must be a non-empty string",
        )
    item_type = value.strip()
    if item_type in {"attachment", "note"}:
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            f"{context} itemType must identify a regular Zotero item",
        )
    return item_type


def normalize_parent_snapshot(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ZoteroLifecycleError("invalid_parent_snapshot", "Zotero parent snapshot must be an object")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    key = str(payload.get("key") or data.get("key") or "").strip()
    title = display_title(data.get("title", ""))
    if not key or not title:
        raise ZoteroLifecycleError(
            "invalid_parent_snapshot",
            "Zotero parent snapshot requires key and title",
        )
    version_value = payload.get("version", data.get("version", 0))
    if type(version_value) is not int or version_value < 0:
        raise ZoteroLifecycleError(
            "invalid_parent_snapshot",
            "Zotero parent snapshot version must be a non-negative integer",
        )
    return {
        "key": key,
        "title": title,
        "normalized_title": normalized_title(title),
        "DOI": normalized_doi(data.get("DOI", "")),
        "version": version_value,
    }


def parent_fingerprint(parent: dict[str, Any]) -> str:
    normalized = normalize_parent_snapshot(parent)
    return canonical_json_sha256(
        {
            "key": normalized["key"],
            "title": normalized["normalized_title"],
            "DOI": normalized["DOI"],
            "version": normalized["version"],
        }
    )


def _bundle_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _bundle_size_limit_error(size_bytes: int, max_bytes: int) -> ZoteroLifecycleError:
    return ZoteroLifecycleError(
        "run_size_limit_exceeded",
        f"saved Zotero discovery bundle size {size_bytes} exceeds the run limit {max_bytes}",
        data={"run_size_bytes": size_bytes, "max_bytes": max_bytes},
    )


def _read_stable_bundle_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Read one stable, single-link regular discovery file through no-follow descriptors."""
    requested = Path(path).expanduser()
    parent_fd = -1
    descriptor = -1
    try:
        parent_path = requested.parent.resolve(strict=True)
        parent_fd = os.open(
            parent_path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        named_before = os.stat(requested.name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(
            requested.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or opened_before.st_nlink != 1
            or _bundle_file_identity(named_before) != _bundle_file_identity(opened_before)
        ):
            raise ZoteroLifecycleError(
                "discovery_bundle_unreadable",
                "saved Zotero discovery bundle must be a stable single-link regular file",
            )
        if opened_before.st_size > max_bytes:
            raise _bundle_size_limit_error(opened_before.st_size, max_bytes)

        chunks: list[bytes] = []
        size_bytes = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - size_bytes + 1))
            if not chunk:
                break
            size_bytes += len(chunk)
            if size_bytes > max_bytes:
                raise _bundle_size_limit_error(size_bytes, max_bytes)
            chunks.append(chunk)

        opened_after = os.fstat(descriptor)
        named_after = os.stat(requested.name, dir_fd=parent_fd, follow_symlinks=False)
        expected_identity = _bundle_file_identity(opened_before)
        if (
            _bundle_file_identity(opened_after) != expected_identity
            or _bundle_file_identity(named_after) != expected_identity
            or size_bytes != opened_after.st_size
        ):
            raise ZoteroLifecycleError(
                "discovery_bundle_changed",
                "saved Zotero discovery bundle changed while it was being read",
            )
        return b"".join(chunks)
    except ZoteroLifecycleError:
        raise
    except OSError as exc:
        raise ZoteroLifecycleError(
            "discovery_bundle_unreadable",
            f"saved Zotero discovery bundle is unreadable: {path}: {exc}",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)


def _read_bundle(path: Path) -> tuple[bytes, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    raw_bytes = _read_stable_bundle_bytes(
        Path(path),
        max_bytes=V2_RESOURCE_POLICY.run_max_bytes,
    )
    try:
        payload = json.loads(raw_bytes, parse_constant=_reject_nonfinite_json)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            "saved Zotero discovery bundle must be valid UTF-8 JSON",
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"search_results", "selected_item"}:
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            "discovery bundle must contain only search_results and selected_item",
        )
    inventory = payload["search_results"]
    if not isinstance(inventory, list) or not inventory:
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            "search_results must be a non-empty array",
        )
    if not all(isinstance(item, dict) for item in inventory):
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            "each search result must be an object",
        )
    try:
        selected = normalize_item_details_payload(payload["selected_item"])
        selected["itemType"] = _validated_item_type(
            selected.get("itemType"),
            context="selected_item",
        )
        canonical_json_bytes(selected)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            f"selected_item is invalid: {exc}",
        ) from exc
    return raw_bytes, payload, selected, inventory


def _validated_inventory(
    inventory: list[dict[str, Any]],
    *,
    selected: dict[str, Any],
    expected_item_key: str,
) -> list[dict[str, Any]]:
    selected_key = _validated_identifier(
        selected.get("key"),
        context="selected_item key",
        code="invalid_discovery_bundle",
    )
    if selected_key != expected_item_key:
        raise ZoteroLifecycleError(
            "selected_item_key_mismatch",
            f"selected item key {selected_key!r} does not match expected key {expected_item_key!r}",
        )
    normalized_inventory: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, item in enumerate(inventory):
        key = _validated_identifier(
            item.get("key"),
            context=f"search_results[{index}] key",
            code="invalid_discovery_bundle",
        )
        title = display_title(item.get("title", ""))
        if not key or not title:
            raise ZoteroLifecycleError(
                "invalid_discovery_bundle",
                f"search_results[{index}] requires key and title",
            )
        if key in keys:
            raise ZoteroLifecycleError(
                "duplicate_search_result_key",
                f"search inventory repeats key {key}",
            )
        keys.add(key)
        item_type = _validated_item_type(
            item.get("itemType"),
            context=f"search_results[{index}]",
        )
        version = _required_version(item, context=f"search_results[{index}]")
        normalized_inventory.append(
            {
                "key": key,
                "title": title,
                "normalized_title": normalized_title(title),
                "DOI": normalized_doi(item.get("DOI", "")),
                "itemType": item_type,
                "version": version,
            }
        )
    if selected_key not in keys:
        raise ZoteroLifecycleError(
            "selected_item_not_in_inventory",
            f"selected item key {selected_key} is absent from search_results",
        )
    selected_normalized_title = normalized_title(selected.get("title", ""))
    selected_doi = normalized_doi(selected.get("DOI", ""))
    selected_item_type = _validated_item_type(
        selected.get("itemType"),
        context="selected_item",
    )
    selected_version = _required_version(selected, context="selected_item")
    matches = [
        item for item in normalized_inventory if item["normalized_title"] == selected_normalized_title
    ]
    if len(matches) > 1:
        raise ZoteroLifecycleError(
            "duplicate_normalized_title",
            "multiple search results have the selected item's normalized title",
            data={"match_count": len(matches)},
        )
    selected_inventory = next(item for item in normalized_inventory if item["key"] == selected_key)
    selected_membership = {
        "key": selected_key,
        "normalized_title": selected_normalized_title,
        "DOI": selected_doi,
        "itemType": selected_item_type,
        "version": selected_version,
    }
    inventory_membership = {
        key: selected_inventory[key]
        for key in ("key", "normalized_title", "DOI", "itemType", "version")
    }
    if inventory_membership != selected_membership:
        raise ZoteroLifecycleError(
            "selected_item_inventory_mismatch",
            "selected item key/title/DOI/itemType/version does not match its search inventory entry",
        )
    return normalized_inventory


def _attachment_identity(
    selected: dict[str, Any],
) -> tuple[str, LocalSourceIdentity, dict[str, Any], dict[str, Any]]:
    attachments = selected.get("attachments", [])
    if not isinstance(attachments, list) or not all(
        isinstance(item, dict) for item in attachments
    ):
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            "selected_item attachments must be an array of objects",
        )
    attachment = select_pdf_attachment(attachments)
    if attachment is None:
        raise ZoteroLifecycleError(
            "zotero_pdf_unavailable",
            "selected Zotero item has no readable local primary PDF attachment",
        )
    attachment_key = _validated_identifier(
        attachment.get("key"),
        context="selected primary PDF attachment key",
        code="invalid_discovery_bundle",
    )
    attachment_path = Path(str(attachment.get("path", "")))
    try:
        identity = _local_source_identity(attachment_path)
    except LocalLifecycleError as exc:
        raise ZoteroLifecycleError(
            "zotero_pdf_unavailable",
            f"selected Zotero PDF attachment is unavailable: {exc}",
            data={"attachment_key": attachment_key, "attachment_path": str(attachment_path)},
        ) from exc
    normalized_attachment = dict(attachment)
    normalized_attachment["key"] = attachment_key
    normalized_attachment["path"] = identity.resolved_path
    return attachment_key, identity, normalized_attachment, attachment


def _artifact_ref(path: str, role: str, content: bytes) -> ArtifactRef:
    return ArtifactRef(
        role=role,
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type="application/json",
    )


def _build_validated_run(
    *,
    raw_bytes: bytes,
    normalized_snapshot: dict[str, Any],
    source_core: dict[str, Any],
) -> tuple[PaperReaderRun, bytes, bytes]:
    raw_path = "source/discovery.raw.json"
    normalized_path = "source/source.json"
    normalized_bytes = canonical_json_bytes(normalized_snapshot)
    raw_ref = _artifact_ref(raw_path, "raw_discovery_bundle", raw_bytes)
    normalized_ref = _artifact_ref(normalized_path, "normalized_source", normalized_bytes)
    source = ZoteroSourceIdentity(
        **source_core,
        raw_discovery_bundle=raw_ref,
        normalized_source=normalized_ref,
    )
    run = PaperReaderRun(
        schema_version="paper_reader.run.v2",
        run_id=new_random_id("run"),
        created_at=rfc3339_utc(),
        source=source,
        target=None,
        status="initialized",
        artifacts=(raw_ref, normalized_ref),
        gate=GateState(status="not_evaluated"),
        live_preflight=None,
    )
    run_bytes = canonical_json_bytes(run)
    PaperReaderRun.model_validate_json(run_bytes)
    return run, normalized_bytes, run_bytes


def _stage_run(
    staging: Path,
    *,
    staging_anchor: DirectoryAnchorLike,
    raw_bytes: bytes,
    normalized_bytes: bytes,
    run_bytes: bytes,
) -> None:
    source_dir = staging / "source"
    atomic_write_bytes(
        source_dir / "discovery.raw.json",
        raw_bytes,
        anchor=staging_anchor,
    )
    atomic_write_bytes(
        source_dir / "source.json",
        normalized_bytes,
        anchor=staging_anchor,
    )
    atomic_write_bytes(staging / "run.json", run_bytes, anchor=staging_anchor)


def initialize_zotero_run(
    raw_mcp_response: Path,
    *,
    expected_item_key: str,
    skill_root: Path | None = None,
    today: date | None = None,
) -> InitializedZoteroRun:
    expected_item_key = _validated_identifier(
        expected_item_key,
        context="expected_item_key",
        code="invalid_expected_item_key",
    )
    raw_bytes, _bundle, selected, inventory = _read_bundle(Path(raw_mcp_response))
    normalized_inventory = _validated_inventory(
        inventory,
        selected=selected,
        expected_item_key=expected_item_key,
    )
    (
        attachment_key,
        attachment_identity,
        normalized_attachment,
        selected_attachment,
    ) = _attachment_identity(selected)
    title = display_title(selected["title"])
    doi = normalized_doi(selected.get("DOI", ""))
    parent_version = _required_version(selected, context="selected_item")
    selected_normalized = dict(selected)
    selected_normalized.update({"title": title, "DOI": doi, "version": parent_version})
    selected_normalized["attachments"] = [
        normalized_attachment if item is selected_attachment else item
        for item in selected.get("attachments", [])
    ]
    normalized_snapshot = {
        "format": "paper_reader.zotero-source.v2-internal",
        "search_inventory": normalized_inventory,
        "selected_item": selected_normalized,
        "selected_attachment": normalized_attachment,
    }
    parent_core = {
        "key": expected_item_key,
        "title": title,
        "DOI": doi,
        "version": parent_version,
    }
    source_core = {
        "item_key": expected_item_key,
        "title": title,
        "doi": doi,
        "parent_version": parent_version,
        "parent_fingerprint": parent_fingerprint(parent_core),
        "attachment_key": attachment_key,
        "attachment": attachment_identity,
    }
    try:
        run, normalized_bytes, run_bytes = _build_validated_run(
            raw_bytes=raw_bytes,
            normalized_snapshot=normalized_snapshot,
            source_core=source_core,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ZoteroLifecycleError(
            "invalid_discovery_bundle",
            f"discovery bundle cannot form a strict canonical V2 run: {exc}",
        ) from exc
    projected_size = len(raw_bytes) + len(normalized_bytes) + len(run_bytes)
    if projected_size > V2_RESOURCE_POLICY.run_max_bytes:
        raise ZoteroLifecycleError(
            "run_size_limit_exceeded",
            (
                f"projected Zotero run size {projected_size} exceeds "
                f"{V2_RESOURCE_POLICY.run_max_bytes} bytes"
            ),
            data={
                "run_size_bytes": projected_size,
                "max_bytes": V2_RESOURCE_POLICY.run_max_bytes,
            },
        )

    resolved_root = Path(skill_root or DEFAULT_SKILL_ROOT).expanduser().resolve(strict=True)
    runs_day = resolved_root / "runs" / (today or date.today()).isoformat()
    slug = slugify_title(title)
    resolved_pdf = Path(attachment_identity.resolved_path)
    try:
        source_guard_context = open_resolved_source_guard(
            resolved_pdf,
            max_bytes=V2_RESOURCE_POLICY.local_pdf_max_bytes,
            expected_sha256=attachment_identity.sha256,
            expected_size=attachment_identity.size_bytes,
            expected_device=attachment_identity.device,
            expected_inode=attachment_identity.inode,
        )
    except (OSError, ValueError) as exc:
        raise ZoteroLifecycleError(
            "zotero_pdf_unavailable",
            f"selected Zotero PDF became unavailable before allocation: {exc}",
        ) from exc
    with source_guard_context as source_guard:
        fcntl.flock(source_guard.descriptor, fcntl.LOCK_EX)
        try:
            try:
                source_guard.verify()
            except (OSError, ValueError) as exc:
                raise ZoteroLifecycleError(
                    "zotero_pdf_unavailable",
                    f"selected Zotero PDF changed before run allocation: {exc}",
                ) from exc
            try:
                root_anchor_context = DirectoryAnchor.open(
                    resolved_root,
                    manifest_path=resolved_root / "run.json",
                )
            except RunLoadError as exc:
                raise ZoteroLifecycleError("initialization_failed", str(exc)) from exc
            with root_anchor_context as root_anchor:
                try:
                    runs_anchor_context = open_anchored_directory(
                        root_anchor,
                        runs_day,
                        create=True,
                    )
                except Exception as exc:
                    raise ZoteroLifecycleError(
                        "initialization_failed",
                        f"Zotero runs directory cannot be created safely: {exc}",
                    ) from exc
                with runs_anchor_context as runs_anchor:
                    version = 1
                    while True:
                        suffix = "" if version == 1 else f"_v{version}"
                        destination = runs_day / f"{slug}{suffix}"
                        staging = runs_day / f".{destination.name}.{new_uuid()}.staging"
                        staging_anchor = create_anchored_directory(runs_anchor, staging)
                        try:
                            _stage_run(
                                staging,
                                staging_anchor=staging_anchor,
                                raw_bytes=raw_bytes,
                                normalized_bytes=normalized_bytes,
                                run_bytes=run_bytes,
                            )
                            staging_snapshot = tree_snapshot_from_bytes(
                                {
                                    "source/discovery.raw.json": raw_bytes,
                                    "source/source.json": normalized_bytes,
                                    "run.json": run_bytes,
                                }
                            )
                            try:
                                source_guard.verify()
                            except (OSError, ValueError) as exc:
                                raise ZoteroLifecycleError(
                                    "zotero_pdf_unavailable",
                                    "selected Zotero PDF changed before run publication: "
                                    f"{exc}",
                                ) from exc
                            try:
                                atomic_publish_tree(
                                    staging,
                                    destination,
                                    anchor=runs_anchor,
                                    expected_staging_anchor=staging_anchor,
                                    expected_tree_snapshot=staging_snapshot,
                                )
                            except PublishConflictError:
                                version += 1
                                continue
                            except Exception as exc:
                                raise ZoteroLifecycleError(
                                    "initialization_failed",
                                    f"Zotero run reservation failed: {destination}: {exc}",
                                ) from exc
                            try:
                                source_guard.verify()
                            except (OSError, ValueError) as exc:
                                raise ZoteroLifecycleError(
                                    "zotero_pdf_unavailable",
                                    "selected Zotero PDF changed while the run was allocated: "
                                    f"{exc}",
                                ) from exc
                            return InitializedZoteroRun(run_dir=destination, run=run)
                        finally:
                            try:
                                remove_anchored_tree(
                                    runs_anchor,
                                    staging,
                                    expected=staging_anchor,
                                )
                            finally:
                                staging_anchor.close()
        finally:
            fcntl.flock(source_guard.descriptor, fcntl.LOCK_UN)


__all__ = [
    "DEFAULT_SKILL_ROOT",
    "InitializedZoteroRun",
    "ZoteroLifecycleError",
    "display_title",
    "initialize_zotero_run",
    "normalize_parent_snapshot",
    "normalized_title",
    "parent_fingerprint",
]
