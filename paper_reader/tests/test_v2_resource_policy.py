from __future__ import annotations

import importlib
import importlib.util


def test_v2_resource_policy_fixes_local_pdf_figure_and_arxiv_caps() -> None:
    assert importlib.util.find_spec("paper_reader.resource_policy") is not None
    policy = importlib.import_module("paper_reader.resource_policy").V2_RESOURCE_POLICY

    assert policy.local_pdf_max_bytes == 256 * 1024 * 1024
    assert policy.pdf_max_pages == 500
    assert policy.extracted_text_max_chars == 20_000_000
    assert policy.run_max_bytes == 512 * 1024 * 1024
    assert policy.structured_artifact_max_bytes == policy.run_max_bytes
    assert policy.figure_default_limit == 4
    assert policy.figure_hard_limit == 8
    assert policy.figure_max_candidates == 200
    assert policy.figure_max_pixels_each == 20_000_000
    assert policy.figure_max_pixels_total == 80_000_000
    assert policy.figure_max_bytes_total == 64 * 1024 * 1024
    assert policy.arxiv_compressed_max_bytes == 64 * 1024 * 1024
    assert policy.arxiv_expanded_max_bytes == 256 * 1024 * 1024
    assert policy.arxiv_max_members == 5_000
    assert policy.arxiv_max_figure_files == 1_000
    assert policy.arxiv_timeout_seconds == 20.0
