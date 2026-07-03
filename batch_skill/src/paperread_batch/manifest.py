from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from paperread_batch.io import read_json


MANIFEST_SCHEMA_VERSION = "paperread-batch.manifest.v1"
DEFAULT_CONCURRENCY = 3
ZOTERO_WRITE_POLICY = "zotero_write"
PREPARE_ONLY_WRITE_POLICY = "prepare_only"
DEFAULT_WRITE_POLICY = ZOTERO_WRITE_POLICY
VALID_WRITE_POLICIES = {ZOTERO_WRITE_POLICY, PREPARE_ONLY_WRITE_POLICY}
VALID_INPUT_TYPES = {"zotero_item", "zotero_title", "pdf_path"}
VALID_EXPECTED_OUTPUTS = {"zotero_note_candidate", "local_note"}
ITEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


class ManifestError(ValueError):
    pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{label} must be a non-empty string")
    return value.strip()


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be an object")
    return value


def _validate_item_id(value: Any, label: str) -> str:
    item_id = _nonempty_string(value, label)
    if not ITEM_ID_PATTERN.fullmatch(item_id):
        raise ManifestError(
            f"{label} must use only letters, numbers, underscore, dot, or hyphen, and must not start with punctuation"
        )
    return item_id


def _validate_pdf_path(raw_path: Any, *, label: str) -> str:
    path = Path(_nonempty_string(raw_path, label)).expanduser().resolve()
    if path.suffix.lower() != ".pdf":
        raise ManifestError(f"{label} must point to a .pdf file")
    if not path.exists():
        raise ManifestError(f"{label} does not exist: {path}")
    if not path.is_file():
        raise ManifestError(f"{label} is not a file: {path}")
    return str(path)


def _expected_output_for_input_type(input_type: str) -> str:
    return "local_note" if input_type == "pdf_path" else "zotero_note_candidate"


def _normalize_item(raw_item: Any, index: int) -> dict[str, Any]:
    item = _require_object(raw_item, f"items[{index}]")
    item_id = _validate_item_id(item.get("item_id"), f"items[{index}].item_id")
    input_type = _nonempty_string(item.get("input_type"), f"items[{index}].input_type")
    if input_type not in VALID_INPUT_TYPES:
        raise ManifestError(f"items[{index}] unknown input_type: {input_type}")

    expected_output = _nonempty_string(item.get("expected_output"), f"items[{index}].expected_output")
    if expected_output not in VALID_EXPECTED_OUTPUTS:
        raise ManifestError(f"items[{index}] unknown expected_output: {expected_output}")
    required_expected_output = _expected_output_for_input_type(input_type)
    if expected_output != required_expected_output:
        raise ManifestError(
            f"items[{index}] expected_output for {input_type} must be {required_expected_output}"
        )

    input_payload = _require_object(item.get("input"), f"items[{index}].input")
    normalized_input: dict[str, Any]
    if input_type == "pdf_path":
        normalized_input = {"path": _validate_pdf_path(input_payload.get("path"), label=f"items[{index}].input.path")}
    elif input_type == "zotero_title":
        normalized_input = {"title": _nonempty_string(input_payload.get("title"), f"items[{index}].input.title")}
    else:
        normalized_input = {
            "item_key": _nonempty_string(input_payload.get("item_key"), f"items[{index}].input.item_key"),
            "title": _nonempty_string(input_payload.get("title"), f"items[{index}].input.title"),
        }

    return {
        "item_id": item_id,
        "input_type": input_type,
        "input": normalized_input,
        "expected_output": expected_output,
    }


def validate_manifest(manifest: Any) -> dict[str, Any]:
    payload = _require_object(manifest, "manifest")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(f"schema_version must be {MANIFEST_SCHEMA_VERSION}")
    batch_title = _nonempty_string(payload.get("batch_title"), "batch_title")
    created_at = _nonempty_string(payload.get("created_at"), "created_at")
    source_summary = _require_object(payload.get("source_summary"), "source_summary")
    source_type = _nonempty_string(source_summary.get("source_type"), "source_summary.source_type")
    description = _nonempty_string(source_summary.get("description"), "source_summary.description")
    default_concurrency = payload.get("default_concurrency", DEFAULT_CONCURRENCY)
    if not isinstance(default_concurrency, int) or default_concurrency < 1:
        raise ManifestError("default_concurrency must be a positive integer")
    write_policy = _nonempty_string(payload.get("write_policy"), "write_policy")
    if write_policy not in VALID_WRITE_POLICIES:
        allowed = ", ".join(sorted(VALID_WRITE_POLICIES))
        raise ManifestError(f"write_policy must be one of: {allowed}")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ManifestError("items must be a non-empty list")

    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items):
        item = _normalize_item(raw_item, index)
        if item["item_id"] in seen:
            raise ManifestError(f"duplicate item_id: {item['item_id']}")
        seen.add(item["item_id"])
        items.append(item)

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_at": created_at,
        "batch_title": batch_title,
        "default_concurrency": default_concurrency,
        "write_policy": write_policy,
        "source_summary": {
            "source_type": source_type,
            "description": description,
        },
        "items": items,
    }


def build_manifest(
    *,
    batch_title: str,
    source_summary: dict[str, str],
    items: list[dict[str, Any]],
    default_concurrency: int = DEFAULT_CONCURRENCY,
    write_policy: str = DEFAULT_WRITE_POLICY,
    created_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_at": created_at or _now_iso(),
        "batch_title": batch_title,
        "default_concurrency": default_concurrency,
        "write_policy": write_policy,
        "source_summary": source_summary,
        "items": items,
    }


def _item_id(index: int) -> str:
    return f"{index + 1:03d}"


def _non_comment_lines(path: Path) -> list[str]:
    lines: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def manifest_from_pdf_folder(
    folder: Path,
    *,
    batch_title: str,
    recursive: bool = False,
    write_policy: str = DEFAULT_WRITE_POLICY,
) -> dict[str, Any]:
    root = Path(folder).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ManifestError(f"pdf folder is not a directory: {root}")
    iterator = root.rglob("*") if recursive else root.iterdir()
    pdfs = sorted(path.resolve() for path in iterator if path.is_file() and path.suffix.lower() == ".pdf")
    items = [
        {
            "item_id": _item_id(index),
            "input_type": "pdf_path",
            "input": {"path": str(path)},
            "expected_output": "local_note",
        }
        for index, path in enumerate(pdfs)
    ]
    manifest = build_manifest(
        batch_title=batch_title,
        source_summary={"source_type": "pdf_folder", "description": str(root)},
        items=items,
        write_policy=write_policy,
    )
    return validate_manifest(manifest)


def manifest_from_pdf_paths_file(
    paths_file: Path,
    *,
    batch_title: str,
    write_policy: str = DEFAULT_WRITE_POLICY,
) -> dict[str, Any]:
    source = Path(paths_file).expanduser().resolve()
    paths: list[Path] = []
    for line in _non_comment_lines(source):
        path = Path(line).expanduser()
        if not path.is_absolute():
            path = source.parent / path
        paths.append(path.resolve())
    items = [
        {
            "item_id": _item_id(index),
            "input_type": "pdf_path",
            "input": {"path": str(path)},
            "expected_output": "local_note",
        }
        for index, path in enumerate(paths)
    ]
    manifest = build_manifest(
        batch_title=batch_title,
        source_summary={"source_type": "pdf_paths", "description": str(source)},
        items=items,
        write_policy=write_policy,
    )
    return validate_manifest(manifest)


def manifest_from_zotero_titles_file(
    titles_file: Path,
    *,
    batch_title: str,
    write_policy: str = DEFAULT_WRITE_POLICY,
) -> dict[str, Any]:
    titles = _non_comment_lines(titles_file)
    items = [
        {
            "item_id": _item_id(index),
            "input_type": "zotero_title",
            "input": {"title": title},
            "expected_output": "zotero_note_candidate",
        }
        for index, title in enumerate(titles)
    ]
    manifest = build_manifest(
        batch_title=batch_title,
        source_summary={"source_type": "zotero_titles", "description": str(Path(titles_file).resolve())},
        items=items,
        write_policy=write_policy,
    )
    return validate_manifest(manifest)


def manifest_from_zotero_collection_inventory(
    inventory_json: Path,
    *,
    batch_title: str,
    collection_query: str = "",
    write_policy: str = DEFAULT_WRITE_POLICY,
) -> dict[str, Any]:
    inventory = _require_object(read_json(inventory_json), "inventory")
    collection = _require_object(inventory.get("collection"), "inventory.collection")
    collection_key = str(collection.get("key") or "").strip()
    collection_name = str(collection.get("name") or "").strip()
    description = collection_name or collection_key or str(Path(inventory_json).resolve()).strip()
    if not description:
        raise ManifestError("inventory.collection must include name or key")
    expected_collection = collection_query.strip()
    if expected_collection and expected_collection not in {collection_key, collection_name}:
        raise ManifestError(
            f"collection_mismatch: {expected_collection} does not match inventory collection {description}"
        )
    raw_items = inventory.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ManifestError("inventory.items must be a non-empty list")

    items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items):
        item = _require_object(raw_item, f"inventory.items[{index}]")
        item_key = str(item.get("item_key") or item.get("key") or "").strip()
        title = str(item.get("title") or "").strip()
        if not item_key or not title:
            raise ManifestError(f"inventory.items[{index}] must include item_key/key and title")
        items.append(
            {
                "item_id": _item_id(index),
                "input_type": "zotero_item",
                "input": {"item_key": item_key, "title": title},
                "expected_output": "zotero_note_candidate",
            }
        )

    manifest = build_manifest(
        batch_title=batch_title,
        source_summary={"source_type": "zotero_collection", "description": description},
        items=items,
        write_policy=write_policy,
    )
    return validate_manifest(manifest)
