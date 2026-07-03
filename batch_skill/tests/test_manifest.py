import json
from pathlib import Path

import pytest

from paperread_batch.manifest import (
    DEFAULT_WRITE_POLICY,
    MANIFEST_SCHEMA_VERSION,
    PREPARE_ONLY_WRITE_POLICY,
    ManifestError,
    ZOTERO_WRITE_POLICY,
    build_manifest,
    manifest_from_pdf_folder,
    manifest_from_pdf_paths_file,
    manifest_from_zotero_collection_inventory,
    manifest_from_zotero_titles_file,
    validate_manifest,
)


def test_validate_manifest_accepts_mixed_v1_items(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_at": "2026-07-02T10:00:00+08:00",
        "batch_title": "mixed batch",
        "default_concurrency": 3,
        "write_policy": ZOTERO_WRITE_POLICY,
        "source_summary": {"source_type": "mixed", "description": "test"},
        "items": [
            {
                "item_id": "001",
                "input_type": "zotero_item",
                "input": {"item_key": "ABC123", "title": "Resolved Zotero Item"},
                "expected_output": "zotero_note_candidate",
            },
            {
                "item_id": "002",
                "input_type": "zotero_title",
                "input": {"title": "A title fragment"},
                "expected_output": "zotero_note_candidate",
            },
            {
                "item_id": "003",
                "input_type": "pdf_path",
                "input": {"path": str(pdf)},
                "expected_output": "local_note",
            },
        ],
    }

    normalized = validate_manifest(manifest)

    assert normalized["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert normalized["write_policy"] == ZOTERO_WRITE_POLICY
    assert normalized["items"][2]["input"]["path"] == str(pdf.resolve())


def test_build_manifest_defaults_to_zotero_write_policy() -> None:
    manifest = build_manifest(
        batch_title="default write",
        source_summary={"source_type": "manual", "description": "test"},
        items=[
            {
                "item_id": "001",
                "input_type": "zotero_title",
                "input": {"title": "one"},
                "expected_output": "zotero_note_candidate",
            }
        ],
    )

    normalized = validate_manifest(manifest)

    assert DEFAULT_WRITE_POLICY == ZOTERO_WRITE_POLICY
    assert normalized["write_policy"] == ZOTERO_WRITE_POLICY


def test_build_manifest_accepts_prepare_only_override() -> None:
    manifest = build_manifest(
        batch_title="dry run",
        source_summary={"source_type": "manual", "description": "test"},
        items=[
            {
                "item_id": "001",
                "input_type": "zotero_title",
                "input": {"title": "one"},
                "expected_output": "zotero_note_candidate",
            }
        ],
        write_policy=PREPARE_ONLY_WRITE_POLICY,
    )

    normalized = validate_manifest(manifest)

    assert normalized["write_policy"] == PREPARE_ONLY_WRITE_POLICY


def test_validate_manifest_rejects_unknown_write_policy() -> None:
    manifest = build_manifest(
        batch_title="bad policy",
        source_summary={"source_type": "manual", "description": "test"},
        items=[
            {
                "item_id": "001",
                "input_type": "zotero_title",
                "input": {"title": "one"},
                "expected_output": "zotero_note_candidate",
            }
        ],
    )
    manifest["write_policy"] = "unsafe_auto_update"

    with pytest.raises(ManifestError, match="write_policy"):
        validate_manifest(manifest)


def test_validate_manifest_rejects_duplicate_item_ids() -> None:
    manifest = build_manifest(
        batch_title="duplicate ids",
        source_summary={"source_type": "manual", "description": "test"},
        items=[
            {
                "item_id": "001",
                "input_type": "zotero_title",
                "input": {"title": "one"},
                "expected_output": "zotero_note_candidate",
            },
            {
                "item_id": "001",
                "input_type": "zotero_title",
                "input": {"title": "two"},
                "expected_output": "zotero_note_candidate",
            },
        ],
    )

    with pytest.raises(ManifestError, match="duplicate item_id"):
        validate_manifest(manifest)


def test_validate_manifest_rejects_path_like_item_ids() -> None:
    manifest = build_manifest(
        batch_title="unsafe ids",
        source_summary={"source_type": "manual", "description": "test"},
        items=[
            {
                "item_id": "../../outside",
                "input_type": "zotero_title",
                "input": {"title": "one"},
                "expected_output": "zotero_note_candidate",
            }
        ],
    )

    with pytest.raises(ManifestError, match="item_id"):
        validate_manifest(manifest)


def test_validate_manifest_rejects_unknown_item_type() -> None:
    manifest = build_manifest(
        batch_title="bad type",
        source_summary={"source_type": "manual", "description": "test"},
        items=[
            {
                "item_id": "001",
                "input_type": "zotero_collection_item",
                "input": {"title": "legacy type"},
                "expected_output": "zotero_note_candidate",
            }
        ],
    )

    with pytest.raises(ManifestError, match="unknown input_type"):
        validate_manifest(manifest)


def test_manifest_from_pdf_folder_is_non_recursive_and_absolute(tmp_path: Path) -> None:
    (tmp_path / "Upper.PDF").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "notes.txt").write_text("ignore", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "c.pdf").write_bytes(b"%PDF-1.4\n")

    manifest = manifest_from_pdf_folder(tmp_path, batch_title="folder batch")

    assert [item["item_id"] for item in manifest["items"]] == ["001", "002", "003"]
    assert {Path(item["input"]["path"]).name for item in manifest["items"]} == {"Upper.PDF", "a.pdf", "b.pdf"}
    assert all(Path(item["input"]["path"]).is_absolute() for item in manifest["items"])
    for item in manifest["items"]:
        assert item["input_type"] == "pdf_path"
        assert item["expected_output"] == "local_note"
        assert set(item["input"]) == {"path"}
        for forbidden_key in ["item_key", "parent_key", "note_key", "zotero_parent_key", "zotero_note_key"]:
            assert forbidden_key not in item
            assert forbidden_key not in item["input"]


def test_manifest_from_pdf_paths_file_makes_absolute_paths(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    paths_file = tmp_path / "paths.txt"
    paths_file.write_text(f"# comment\n{pdf}\n\n", encoding="utf-8")

    manifest = manifest_from_pdf_paths_file(paths_file, batch_title="paths batch")

    assert len(manifest["items"]) == 1
    assert manifest["items"][0]["input_type"] == "pdf_path"
    assert manifest["items"][0]["input"]["path"] == str(pdf.resolve())


def test_manifest_from_pdf_paths_file_resolves_relative_paths_from_file_directory(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    paths_file = tmp_path / "paths.txt"
    paths_file.write_text("paper.pdf\n", encoding="utf-8")

    manifest = manifest_from_pdf_paths_file(paths_file, batch_title="relative paths batch")

    assert manifest["items"][0]["input"]["path"] == str(pdf.resolve())


def test_manifest_from_zotero_titles_file_uses_title_items(tmp_path: Path) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("# comment\nFirst paper\n\nSecond paper\n", encoding="utf-8")

    manifest = manifest_from_zotero_titles_file(titles, batch_title="titles batch")

    assert [item["item_id"] for item in manifest["items"]] == ["001", "002"]
    assert manifest["write_policy"] == ZOTERO_WRITE_POLICY
    assert [item["input"]["title"] for item in manifest["items"]] == ["First paper", "Second paper"]
    assert all(item["input_type"] == "zotero_title" for item in manifest["items"])


def test_manifest_from_zotero_titles_file_accepts_prepare_only_override(tmp_path: Path) -> None:
    titles = tmp_path / "titles.txt"
    titles.write_text("First paper\n", encoding="utf-8")

    manifest = manifest_from_zotero_titles_file(
        titles,
        batch_title="titles batch",
        write_policy=PREPARE_ONLY_WRITE_POLICY,
    )

    assert manifest["write_policy"] == PREPARE_ONLY_WRITE_POLICY


def test_manifest_from_zotero_collection_inventory_uses_zotero_items(tmp_path: Path) -> None:
    inventory = tmp_path / "collection-items.json"
    inventory.write_text(
        json.dumps(
            {
                "collection": {"key": "COLL1", "name": "My Collection"},
                "items": [
                    {"item_key": "KEY1", "title": "First item"},
                    {"key": "KEY2", "title": "Second item"},
                ],
            }
        ),
        encoding="utf-8",
    )

    manifest = manifest_from_zotero_collection_inventory(inventory, batch_title="collection batch")

    assert manifest["source_summary"]["source_type"] == "zotero_collection"
    assert manifest["source_summary"]["description"] == "My Collection"
    assert [item["input_type"] for item in manifest["items"]] == ["zotero_item", "zotero_item"]
    assert manifest["items"][0]["input"] == {"item_key": "KEY1", "title": "First item"}
    assert manifest["items"][1]["input"] == {"item_key": "KEY2", "title": "Second item"}
