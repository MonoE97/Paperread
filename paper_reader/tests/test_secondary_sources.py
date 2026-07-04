from __future__ import annotations

from paper_reader.secondary_sources import build_secondary_sources, extract_http_urls


def test_extract_http_urls_accepts_only_http_and_https_and_dedupes() -> None:
    text = """
    https://mp.weixin.qq.com/s/example?scene=334,
    http://example.org/a.
    zotero://select/library/items/ABC123
    file:///tmp/local.pdf
    https://mp.weixin.qq.com/s/example?scene=334
    """

    assert extract_http_urls(text) == [
        "https://mp.weixin.qq.com/s/example?scene=334",
        "http://example.org/a",
    ]


def test_build_secondary_sources_records_cross_check_boundary() -> None:
    details = {
        "key": "ABC123",
        "title": "Example Paper",
        "extra": "Related: https://mp.weixin.qq.com/s/example?scene=334",
        "_paper_reader": {
            "enrichment": {
                "extra": {
                    "source": "zotero_sqlite",
                    "sqlite_mode": "immutable",
                    "diagnostics": ["sqlite_immutable_snapshot_used"],
                }
            },
            "warnings": [],
        },
    }

    payload = build_secondary_sources(details)

    assert payload["item_key"] == "ABC123"
    assert payload["usage_boundary"] == "cross-check only; must not be cited in evidence_summary"
    assert payload["warnings"] == []
    assert payload["sources"] == [
        {
            "source_id": "secondary-001",
            "url": "https://mp.weixin.qq.com/s/example?scene=334",
            "source_field": "extra",
            "source_provenance": "zotero_sqlite",
            "capture_status": "pending_capture",
        }
    ]


def test_build_secondary_sources_does_not_promote_successful_sqlite_diagnostics_to_warnings() -> None:
    payload = build_secondary_sources(
        {
            "key": "ABC123",
            "title": "Example",
            "extra": "https://mp.weixin.qq.com/s/example",
            "_paper_reader": {
                "warnings": ["sqlite_immutable_snapshot_used"],
                "enrichment": {
                    "extra": {
                        "source": "zotero_sqlite",
                        "sqlite_mode": "immutable",
                        "diagnostics": ["sqlite_immutable_snapshot_used"],
                    }
                },
            },
        }
    )

    assert payload["sources"][0]["source_provenance"] == "zotero_sqlite"
    assert payload["warnings"] == []


def test_build_secondary_sources_soft_handles_missing_extra() -> None:
    payload = build_secondary_sources({"key": "ABC123", "title": "No Extra"})

    assert payload["sources"] == []
    assert payload["warnings"] == ["missing_extra_field"]
