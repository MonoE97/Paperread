from __future__ import annotations

import pytest

from paper_reader import secondary_sources
from paper_reader.secondary_sources import build_secondary_sources, extract_http_urls


def test_extract_http_urls_accepts_only_http_and_https_and_dedupes() -> None:
    text = """
    <https://mp.weixin.qq.com/s/example?scene=334>
    <http://example.org/a>
    zotero://select/library/items/ABC123
    file:///tmp/local.pdf
    https://mp.weixin.qq.com/s/example?scene=334
    """

    assert extract_http_urls(text) == [
        "https://mp.weixin.qq.com/s/example?scene=334",
        "http://example.org/a",
    ]


def test_extract_http_urls_preserves_balanced_parentheses_in_exact_query() -> None:
    text = "Related <https://example.org/search?q=(stack+pressure)&lang=zh>"

    assert extract_http_urls(text) == [
        "https://example.org/search?q=(stack+pressure)&lang=zh"
    ]


def test_extract_http_urls_never_strips_url_valid_query_suffix_bytes() -> None:
    exact_url = "https://example.org/article?signature=abc)!;,.."

    assert extract_http_urls(f"Signed source: <{exact_url}>") == [exact_url]


def test_extract_http_urls_removes_plain_text_wrappers_and_sentence_punctuation() -> None:
    text = (
        "Related (https://example.org/a). "
        "另见：https://example.org/b，"
        "最后是 https://example.org/c。"
    )

    assert extract_http_urls(text) == [
        "https://example.org/a",
        "https://example.org/b",
        "https://example.org/c",
    ]


def test_extract_http_urls_accepts_uppercase_scheme_without_rewriting_exact_url() -> None:
    assert extract_http_urls("HTTPS://example.org/A?Token=Exact") == [
        "HTTPS://example.org/A?Token=Exact"
    ]


def test_extract_http_urls_preserves_balanced_parentheses_in_plain_query() -> None:
    assert extract_http_urls("Related https://example.org/search?q=(stack+pressure).") == [
        "https://example.org/search?q=(stack+pressure)"
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


@pytest.mark.parametrize("extra", [123, ["https://example.test/context"]])
def test_build_secondary_source_plan_rejects_ambiguous_extra_types(extra: object) -> None:
    with pytest.raises(ValueError, match="extra must be a string or missing"):
        secondary_sources.build_secondary_source_plan(
            {
                "key": "ABC123",
                "title": "Example",
                "extra": extra,
            },
            source_snapshot_sha256="a" * 64,
        )


def test_build_secondary_source_plan_preserves_query_and_applies_capture_boundaries() -> None:
    plan = secondary_sources.build_secondary_source_plan(
        {
            "key": "ABC123",
            "title": "Example",
            "DOI": "10.1000/example",
            "url": "https://publisher.test/article",
            "extra": (
                "https://mp.weixin.qq.com/s/example?scene=24&clicktime=123 "
                "https://doi.org/10.1000/example "
                "https://publisher.test/article "
                "http://localhost/private"
            ),
        },
        source_snapshot_sha256="a" * 64,
    )

    assert plan["eligible_source_count"] == 1
    assert [item["source_id"] for item in plan["sources"]] == [
        "secondary-001",
        "secondary-002",
        "secondary-003",
        "secondary-004",
    ]
    assert plan["sources"][0]["url"] == (
        "https://mp.weixin.qq.com/s/example?scene=24&clicktime=123"
    )
    assert [item["rejection_reason"] for item in plan["sources"]] == [
        None,
        "primary_source",
        "primary_source",
        "unsafe_url",
    ]


def test_build_secondary_source_plan_rejects_more_members_than_strict_capture_accepts() -> None:
    extra = " ".join(
        f"https://public.example/article/{index}" for index in range(257)
    )

    with pytest.raises(ValueError, match="at most 256 URLs"):
        secondary_sources.build_secondary_source_plan(
            {
                "key": "ABC123",
                "title": "Example",
                "extra": extra,
            },
            source_snapshot_sha256="a" * 64,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://224.0.0.1/context",
        "http://2130706433/context",
        "http://0x7f000001/context",
        "http://127.1/context",
    ],
)
def test_build_secondary_source_plan_rejects_non_unicast_and_ambiguous_numeric_hosts(
    url: str,
) -> None:
    plan = secondary_sources.build_secondary_source_plan(
        {
            "key": "ABC123",
            "title": "Example",
            "extra": url,
        },
        source_snapshot_sha256="a" * 64,
    )

    assert plan["eligible_source_count"] == 0
    assert plan["sources"][0]["rejection_reason"] == "unsafe_url"


@pytest.mark.parametrize(
    "url",
    [
        "http://[64:ff9b::1]/context",
        "http://[4000::1]/context",
        "http://192.0.0.9/context",
        "http://192.0.0.10/context",
    ],
)
def test_build_secondary_source_plan_matches_strict_browser_ip_policy(
    url: str,
) -> None:
    plan = secondary_sources.build_secondary_source_plan(
        {
            "key": "ABC123",
            "title": "Example",
            "extra": url,
        },
        source_snapshot_sha256="a" * 64,
    )

    assert plan["eligible_source_count"] == 0
    assert plan["sources"][0]["rejection_reason"] == "unsafe_url"
