from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class V2ResourcePolicy:
    local_pdf_max_bytes: int = 256 * 1024 * 1024
    pdf_max_pages: int = 500
    extracted_text_max_chars: int = 20_000_000
    run_max_bytes: int = 512 * 1024 * 1024
    figure_default_limit: int = 4
    figure_hard_limit: int = 8
    figure_max_candidates: int = 200
    figure_max_pixels_each: int = 20_000_000
    figure_max_pixels_total: int = 80_000_000
    figure_max_bytes_total: int = 64 * 1024 * 1024
    arxiv_compressed_max_bytes: int = 64 * 1024 * 1024
    arxiv_expanded_max_bytes: int = 256 * 1024 * 1024
    arxiv_max_members: int = 5_000
    arxiv_max_figure_files: int = 1_000
    arxiv_timeout_seconds: float = 20.0


V2_RESOURCE_POLICY = V2ResourcePolicy()


__all__ = ["V2ResourcePolicy", "V2_RESOURCE_POLICY"]
