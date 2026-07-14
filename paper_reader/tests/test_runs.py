from __future__ import annotations

from paper_reader import runs
from paper_reader.runs import slugify_title


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


def test_runs_module_does_not_expose_v1_allocation_or_manifest_mutators() -> None:
    assert not hasattr(runs, "allocate_run_dir")
    assert not hasattr(runs, "write_run_manifest")
