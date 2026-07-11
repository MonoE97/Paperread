from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable
from uuid import uuid4

from pydantic import ValidationError

from paper_reader_batch.v2_contracts import (
    MANIFEST_SCHEMA_VERSION,
    BatchManifest,
    PdfManifestItem,
    PdfSource,
    SourceSummary,
    ZoteroItemManifestItem,
    ZoteroItemSource,
    ZoteroTitleManifestItem,
    ZoteroTitleSource,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    canonical_json_bytes,
    canonical_sha256,
    entry_exists,
    normalized_absolute_path,
    publish_bytes_no_replace,
    read_bytes,
    read_json_bytes,
    sha256_bytes,
    utc_now,
    validate_parent_directory,
)
from paper_reader_batch.v2_receipts import FaultHook, RequestOutcome, RequestReceiptStore


DEFAULT_CONCURRENCY = 3
DEFAULT_WRITE_POLICY = "zotero_write"


def _normalized_output(path: Path) -> Path:
    target = normalized_absolute_path(path)
    if target.name in {"", ".", ".."}:
        raise BatchRuntimeError("unsafe_path", f"invalid output target: {path}")
    return target


def _read_lines(path: Path, *, label: str) -> tuple[Path, bytes, list[str]]:
    source = Path(path).expanduser().resolve()
    raw = read_bytes(source, code="source_unreadable")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BatchRuntimeError("source_unreadable", f"{label} is not UTF-8: {source}") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not lines:
        raise BatchRuntimeError("invalid_input", f"{label} has no usable entries: {source}")
    return source, raw, lines


def _pdf_source(path: Path) -> PdfSource:
    resolved = Path(path).expanduser().resolve()
    if resolved.suffix.lower() != ".pdf":
        raise BatchRuntimeError("invalid_pdf", f"input is not a .pdf file: {resolved}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        path_before = os.lstat(resolved)
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise BatchRuntimeError("source_unreadable", f"cannot open PDF: {resolved}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (path_before.st_dev, path_before.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise BatchRuntimeError("invalid_pdf", f"PDF input is not a regular file: {resolved}")
        first = os.read(descriptor, 5)
        if first != b"%PDF-":
            raise BatchRuntimeError("invalid_pdf", f"file does not have a PDF signature: {resolved}")
        digest.update(first)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        path_after = os.lstat(resolved)
    finally:
        os.close(descriptor)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or (path_after.st_dev, path_after.st_ino) != (after.st_dev, after.st_ino)
    ):
        raise BatchRuntimeError("source_changed_during_read", f"PDF changed while being fingerprinted: {resolved}")
    if after.st_size < 1:
        raise BatchRuntimeError("invalid_pdf", f"PDF is empty: {resolved}")
    return PdfSource(
        path=str(resolved),
        size_bytes=after.st_size,
        sha256=digest.hexdigest(),
        file_identity={"device": after.st_dev, "inode": after.st_ino},
    )


def _reject_duplicate_sources(items: list[Any]) -> None:
    paths: dict[str, str] = {}
    file_ids: dict[tuple[int, int], str] = {}
    zotero_keys: dict[str, str] = {}
    for item in items:
        if isinstance(item, PdfManifestItem):
            path = item.source.path
            identity = (item.source.file_identity.device, item.source.file_identity.inode)
            if path in paths:
                raise BatchRuntimeError("duplicate_source", f"duplicate normalized PDF path: {path}")
            if identity in file_ids:
                raise BatchRuntimeError(
                    "duplicate_source",
                    f"PDF aliases share one file identity: {file_ids[identity]} and {item.item_id}",
                )
            paths[path] = item.item_id
            file_ids[identity] = item.item_id
        elif isinstance(item, ZoteroItemManifestItem):
            key = item.source.item_key
            if key in zotero_keys:
                raise BatchRuntimeError("duplicate_source", f"duplicate Zotero item key: {key}")
            zotero_keys[key] = item.item_id
        elif item.source.resolved_item_key is not None:
            key = item.source.resolved_item_key
            if key in zotero_keys:
                raise BatchRuntimeError("duplicate_source", f"duplicate resolved Zotero item key: {key}")
            zotero_keys[key] = item.item_id


def _create_manifest(
    *,
    command: str,
    batch_title: str,
    source_summary: SourceSummary,
    items: list[Any],
    input_fingerprint: dict[str, Any],
    output: Path,
    request_id: str,
    skill_root: Path,
    default_concurrency: int,
    write_policy: str,
    created_at: str | None,
    fault: FaultHook | None,
) -> RequestOutcome:
    if not batch_title.strip():
        raise BatchRuntimeError("invalid_input", "batch title must not be empty")
    _reject_duplicate_sources(items)
    if not 1 <= default_concurrency <= 32:
        raise BatchRuntimeError("invalid_input", "default concurrency must be between 1 and 32")
    if write_policy not in {"zotero_write", "prepare_only"}:
        raise BatchRuntimeError("invalid_input", "write policy must be zotero_write or prepare_only")
    target = _normalized_output(output)
    validate_parent_directory(target)
    fingerprint = canonical_sha256(
        {
            "command": command,
            "batch_title": batch_title,
            "default_concurrency": default_concurrency,
            "write_policy": write_policy,
            "created_at_override": created_at,
            "input": input_fingerprint,
            "target": str(target),
        }
    )
    store = RequestReceiptStore(skill_root)

    def target_factory(reserved: set[str]) -> Path:
        if str(target) in reserved or entry_exists(target):
            raise BatchRuntimeError("output_conflict", f"manifest target is already reserved or occupied: {target}")
        return target

    def plan_factory(_target: Path) -> dict[str, Any]:
        try:
            manifest = BatchManifest(
                schema_version=MANIFEST_SCHEMA_VERSION,
                manifest_id=str(uuid4()),
                created_at=created_at or utc_now(),
                batch_title=batch_title.strip(),
                default_concurrency=default_concurrency,
                write_policy=write_policy,
                source_summary=source_summary,
                items=items,
            )
        except ValidationError as exc:
            raise BatchRuntimeError("invalid_manifest", "manifest failed strict validation") from exc
        artifact_bytes = canonical_json_bytes(manifest)
        artifact_sha256 = sha256_bytes(artifact_bytes)
        return {
            "manifest": manifest.model_dump(mode="json"),
            "manifest_sha256": artifact_sha256,
            "semantic_result": {
                "manifest_path": str(target),
                "manifest_id": manifest.manifest_id,
                "manifest_sha256": artifact_sha256,
            },
        }

    def expected_bytes(plan: dict[str, Any]) -> bytes:
        manifest_payload = plan.get("manifest")
        if not isinstance(manifest_payload, dict):
            raise BatchRuntimeError("receipt_corrupt", "manifest receipt plan is invalid")
        return canonical_json_bytes(manifest_payload)

    def inspect(candidate: Path, plan: dict[str, Any]) -> bool:
        try:
            return read_bytes(candidate) == expected_bytes(plan)
        except BatchRuntimeError as exc:
            if exc.code in {"artifact_unreadable", "storage_missing"}:
                return False
            raise

    def publish(candidate: Path, plan: dict[str, Any], resuming: bool) -> None:
        publish_bytes_no_replace(candidate, expected_bytes(plan), allow_existing_exact=resuming)

    return store.execute(
        request_id=request_id,
        command=command,
        request_fingerprint=fingerprint,
        requested_target=target,
        target_factory=target_factory,
        plan_factory=plan_factory,
        publish=publish,
        inspect=inspect,
        fault=fault,
    )


def create_pdf_paths_manifest(
    paths_file: Path,
    *,
    batch_title: str,
    output: Path,
    request_id: str,
    skill_root: Path,
    default_concurrency: int = DEFAULT_CONCURRENCY,
    write_policy: str = DEFAULT_WRITE_POLICY,
    created_at: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    source, raw, lines = _read_lines(paths_file, label="PDF paths file")
    pdf_sources: list[PdfSource] = []
    for line in lines:
        path = Path(line).expanduser()
        if not path.is_absolute():
            path = source.parent / path
        pdf_sources.append(_pdf_source(path))
    items = [
        PdfManifestItem(item_id=f"{index:03d}", source=pdf_source)
        for index, pdf_source in enumerate(pdf_sources, start=1)
    ]
    return _create_manifest(
        command="manifest.from-pdf-paths",
        batch_title=batch_title,
        source_summary=SourceSummary(
            source_type="pdf_paths",
            description=str(source),
            source_sha256=sha256_bytes(raw),
        ),
        items=items,
        input_fingerprint={
            "input_path": str(source),
            "input_sha256": sha256_bytes(raw),
            "sources": [item.source.model_dump(mode="json") for item in items],
        },
        output=output,
        request_id=request_id,
        skill_root=skill_root,
        default_concurrency=default_concurrency,
        write_policy=write_policy,
        created_at=created_at,
        fault=fault,
    )


def create_pdf_folder_manifest(
    folder: Path,
    *,
    batch_title: str,
    output: Path,
    request_id: str,
    skill_root: Path,
    recursive: bool = False,
    default_concurrency: int = DEFAULT_CONCURRENCY,
    write_policy: str = DEFAULT_WRITE_POLICY,
    created_at: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise BatchRuntimeError("invalid_input", f"PDF folder is not a directory: {root}")
    iterator = root.rglob("*") if recursive else root.iterdir()
    paths = sorted((path for path in iterator if path.is_file() and path.suffix.lower() == ".pdf"), key=str)
    if not paths:
        raise BatchRuntimeError("invalid_input", f"PDF folder has no matching files: {root}")
    items = [
        PdfManifestItem(item_id=f"{index:03d}", source=_pdf_source(path))
        for index, path in enumerate(paths, start=1)
    ]
    return _create_manifest(
        command="manifest.from-pdf-folder",
        batch_title=batch_title,
        source_summary=SourceSummary(source_type="pdf_folder", description=str(root)),
        items=items,
        input_fingerprint={
            "folder": str(root),
            "recursive": recursive,
            "sources": [item.source.model_dump(mode="json") for item in items],
        },
        output=output,
        request_id=request_id,
        skill_root=skill_root,
        default_concurrency=default_concurrency,
        write_policy=write_policy,
        created_at=created_at,
        fault=fault,
    )


def create_zotero_titles_manifest(
    titles_file: Path,
    *,
    batch_title: str,
    output: Path,
    request_id: str,
    skill_root: Path,
    default_concurrency: int = DEFAULT_CONCURRENCY,
    write_policy: str = DEFAULT_WRITE_POLICY,
    created_at: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    source, raw, lines = _read_lines(titles_file, label="Zotero titles file")
    normalized_titles: set[str] = set()
    items: list[ZoteroTitleManifestItem] = []
    for index, title in enumerate(lines, start=1):
        normalized = " ".join(title.split()).casefold()
        if normalized in normalized_titles:
            raise BatchRuntimeError("duplicate_source", f"duplicate normalized Zotero title: {title}")
        normalized_titles.add(normalized)
        items.append(ZoteroTitleManifestItem(item_id=f"{index:03d}", source=ZoteroTitleSource(title=title)))
    return _create_manifest(
        command="manifest.from-zotero-titles",
        batch_title=batch_title,
        source_summary=SourceSummary(
            source_type="zotero_titles",
            description=str(source),
            source_sha256=sha256_bytes(raw),
        ),
        items=items,
        input_fingerprint={"input_path": str(source), "input_sha256": sha256_bytes(raw), "titles": lines},
        output=output,
        request_id=request_id,
        skill_root=skill_root,
        default_concurrency=default_concurrency,
        write_policy=write_policy,
        created_at=created_at,
        fault=fault,
    )


def create_zotero_collection_manifest(
    collection_query: str,
    inventory_file: Path,
    *,
    batch_title: str,
    output: Path,
    request_id: str,
    skill_root: Path,
    default_concurrency: int = DEFAULT_CONCURRENCY,
    write_policy: str = DEFAULT_WRITE_POLICY,
    created_at: str | None = None,
    fault: FaultHook | None = None,
) -> RequestOutcome:
    inventory_path = Path(inventory_file).expanduser().resolve()
    raw, inventory = read_json_bytes(inventory_path, code="source_unreadable")
    if not isinstance(inventory, dict) or not isinstance(inventory.get("collection"), dict):
        raise BatchRuntimeError("invalid_inventory", "inventory must contain a collection object")
    collection = inventory["collection"]
    collection_key = str(collection.get("key") or "").strip()
    collection_name = str(collection.get("name") or "").strip()
    if not collection_query.strip() or collection_query.strip() not in {collection_key, collection_name}:
        raise BatchRuntimeError(
            "collection_mismatch",
            f"collection query does not match inventory key/name: {collection_query}",
        )
    raw_items = inventory.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise BatchRuntimeError("invalid_inventory", "inventory must contain non-empty items")
    inventory_sha256 = sha256_bytes(raw)
    items: list[ZoteroItemManifestItem] = []
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise BatchRuntimeError("invalid_inventory", f"inventory item {index} is not an object")
        item_key = str(raw_item.get("item_key") or raw_item.get("key") or "").strip()
        title = str(raw_item.get("title") or "").strip()
        if not item_key or not title:
            raise BatchRuntimeError("invalid_inventory", f"inventory item {index} lacks item key/title")
        items.append(
            ZoteroItemManifestItem(
                item_id=f"{index:03d}",
                source=ZoteroItemSource(
                    item_key=item_key,
                    title=title,
                    inventory_sha256=inventory_sha256,
                    collection_key=collection_key,
                ),
            )
        )
    return _create_manifest(
        command="manifest.from-zotero-collection",
        batch_title=batch_title,
        source_summary=SourceSummary(
            source_type="zotero_collection",
            description=collection_name or collection_key,
            source_sha256=inventory_sha256,
            collection_key=collection_key,
            collection_name=collection_name or None,
        ),
        items=items,
        input_fingerprint={
            "inventory_path": str(inventory_path),
            "inventory_sha256": inventory_sha256,
            "collection_query": collection_query.strip(),
        },
        output=output,
        request_id=request_id,
        skill_root=skill_root,
        default_concurrency=default_concurrency,
        write_policy=write_policy,
        created_at=created_at,
        fault=fault,
    )


def load_manifest(
    path: Path,
    *,
    validate_sources: bool = False,
    drift_context: bool = False,
) -> tuple[BatchManifest, bytes, str]:
    manifest_path = normalized_absolute_path(path)
    raw, payload = read_json_bytes(manifest_path, code="manifest_unreadable")
    if not isinstance(payload, dict) or payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise BatchRuntimeError(
            "unsupported_run_schema",
            f"manifest schema must be exactly {MANIFEST_SCHEMA_VERSION}",
        )
    try:
        manifest = BatchManifest.model_validate_json(raw)
    except ValidationError as exc:
        raise BatchRuntimeError("invalid_manifest", f"manifest failed strict validation: {manifest_path}") from exc
    if raw != canonical_json_bytes(manifest):
        code = "manifest_drift" if drift_context else "invalid_manifest"
        raise BatchRuntimeError(code, f"manifest is not canonical JSON: {manifest_path}")
    if validate_sources:
        validate_manifest_sources(manifest)
    return manifest, raw, sha256_bytes(raw)


def validate_manifest_sources(manifest: BatchManifest) -> None:
    current_pdf_identities: set[tuple[int, int]] = set()
    for item in manifest.items:
        if not isinstance(item, PdfManifestItem):
            continue
        current = validate_pdf_source(item.source)
        identity = (current.file_identity.device, current.file_identity.inode)
        if identity in current_pdf_identities:
            raise BatchRuntimeError("duplicate_source", f"duplicate PDF file identity at validation: {identity}")
        current_pdf_identities.add(identity)


def validate_pdf_source(source: PdfSource) -> PdfSource:
    """Re-fingerprint one bound PDF and reject any path, identity, size, or byte drift."""
    current = _pdf_source(Path(source.path))
    if current != source:
        raise BatchRuntimeError("source_drift", f"PDF source identity changed: {source.path}")
    return current


def validate_manifest_file(path: Path) -> dict[str, Any]:
    manifest, _raw, manifest_sha256 = load_manifest(path, validate_sources=True)
    return {
        "manifest_path": str(normalized_absolute_path(path)),
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest_sha256,
        "item_count": len(manifest.items),
    }
