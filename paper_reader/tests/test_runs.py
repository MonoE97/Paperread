from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from paper_reader.runs import allocate_run_dir, slugify_title, write_run_manifest


def test_slugify_title_normalizes_unicode_punctuation() -> None:
    assert (
        slugify_title("Deep‐Learning Assisted Polarization Holograms")
        == "deep-learning-assisted-polarization-holograms"
    )


def test_slugify_title_transliterates_common_greek_letters() -> None:
    assert slugify_title("β-Ga2O3 photodetectors") == "beta-ga2o3-photodetectors"


def test_slugify_title_uses_stable_ascii_fallback_for_non_ascii_titles() -> None:
    slug = slugify_title("中文标题")

    assert slug == "u4e2d-u6587-u6807-u9898"
    assert slug != "untitled"


def test_allocate_run_dir_uses_date_slug_and_collision_suffix(tmp_path: Path) -> None:
    base_dir = tmp_path / "runs"

    first = allocate_run_dir(base_dir, "CrystalGRW: Geodesic Random Walks", today=date(2026, 4, 24))
    first.mkdir(parents=True)
    second = allocate_run_dir(base_dir, "CrystalGRW: Geodesic Random Walks", today=date(2026, 4, 24))
    second.mkdir(parents=True)
    third = allocate_run_dir(base_dir, "CrystalGRW: Geodesic Random Walks", today=date(2026, 4, 24))

    expected_base = base_dir / "2026-04-24"
    assert first == expected_base / "crystalgrw-geodesic-random-walks"
    assert second == expected_base / "crystalgrw-geodesic-random-walks-2"
    assert third == expected_base / "crystalgrw-geodesic-random-walks-3"


def test_write_run_manifest_records_core_metadata(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "2026-04-24" / "example-paper"
    run_dir.mkdir(parents=True)

    manifest_path = write_run_manifest(
        run_dir,
        {
            "title": "Example Paper",
            "item_key": "ABC123",
            "created_at": "2026-04-24T09:30:00",
            "status": "initialized",
            "warnings": ["none"],
        },
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_path == run_dir / "run.json"
    assert manifest["title"] == "Example Paper"
    assert manifest["slug"] == "example-paper"
    assert manifest["item_key"] == "ABC123"
    assert manifest["created_at"] == "2026-04-24T09:30:00"
    assert manifest["status"] == "initialized"
    assert manifest["warnings"] == ["none"]
