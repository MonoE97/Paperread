from __future__ import annotations

import json

import pytest


def _mcp_response(payload: dict[str, object], *, request_id: int) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        },
    }


def _search_response() -> dict[str, object]:
    return _mcp_response(
        {
            "query": {"title": "Exact Paper", "titleOperator": "exact"},
            "pagination": {"total": 1},
            "results": [
                {
                    "key": "PARENT1",
                    "title": "Exact Paper",
                    "attachments": [{"key": "ATTACH1"}],
                }
            ],
        },
        request_id=2,
    )


def _details_response() -> dict[str, object]:
    return _mcp_response(
        {
            "key": "PARENT1",
            "itemType": "",
            "title": "Exact Paper",
            "DOI": "10.1000/example",
            "attachments": [
                {
                    "key": "ATTACH1",
                    "contentType": "application/pdf",
                    "path": "/tmp/paper.pdf",
                }
            ],
        },
        request_id=3,
    )


def _parent_snapshot() -> dict[str, object]:
    return {
        "key": "PARENT1",
        "version": 27,
        "data": {
            "key": "PARENT1",
            "version": 27,
            "itemType": "journalArticle",
            "title": "Exact Paper",
            "DOI": "10.1000/example",
        },
    }


def test_build_discovery_bundle_enriches_identity_and_preserves_raw_provenance() -> None:
    from paper_reader.zotero_discovery import build_discovery_bundle

    search_response = _search_response()
    details_response = _details_response()
    parent_snapshot = _parent_snapshot()

    bundle = build_discovery_bundle(
        title="Exact Paper",
        search_response=search_response,
        selected_details_response=details_response,
        fetch_parent=lambda item_key: parent_snapshot,
    )

    assert bundle["search_results"] == [
        {
            "key": "PARENT1",
            "title": "Exact Paper",
            "attachments": [{"key": "ATTACH1"}],
            "version": 27,
            "itemType": "journalArticle",
            "DOI": "10.1000/example",
        }
    ]
    selected = bundle["selected_item"]
    assert selected["version"] == 27
    assert selected["itemType"] == "journalArticle"
    assert selected["_paper_reader"]["discovery"] == {
        "raw_search_response": search_response,
        "raw_selected_details_response": details_response,
        "raw_parent_snapshots": {"PARENT1": parent_snapshot},
    }


def test_build_discovery_bundle_rejects_parent_identity_mismatch() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, build_discovery_bundle

    parent_snapshot = _parent_snapshot()
    parent_snapshot["data"]["title"] = "Different Paper"

    with pytest.raises(DiscoveryError) as exc_info:
        build_discovery_bundle(
            title="Exact Paper",
            search_response=_search_response(),
            selected_details_response=_details_response(),
            fetch_parent=lambda item_key: parent_snapshot,
        )

    assert exc_info.value.code == "parent_identity_mismatch"


def test_build_discovery_bundle_rejects_parent_snapshot_without_version() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, build_discovery_bundle

    parent_snapshot = _parent_snapshot()
    parent_snapshot.pop("version")
    parent_snapshot["data"].pop("version")

    with pytest.raises(DiscoveryError) as exc_info:
        build_discovery_bundle(
            title="Exact Paper",
            search_response=_search_response(),
            selected_details_response=_details_response(),
            fetch_parent=lambda item_key: parent_snapshot,
        )

    assert exc_info.value.code == "invalid_parent_snapshot"


def test_build_discovery_bundle_translates_invalid_parent_shape() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, build_discovery_bundle

    parent_snapshot = _parent_snapshot()
    parent_snapshot.pop("key")
    parent_snapshot["data"].pop("key")

    with pytest.raises(DiscoveryError) as exc_info:
        build_discovery_bundle(
            title="Exact Paper",
            search_response=_search_response(),
            selected_details_response=_details_response(),
            fetch_parent=lambda item_key: parent_snapshot,
        )

    assert exc_info.value.code == "invalid_parent_snapshot"


def test_build_discovery_bundle_translates_invalid_selected_details() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, build_discovery_bundle

    invalid_details = _mcp_response({"title": "Exact Paper"}, request_id=3)

    with pytest.raises(DiscoveryError) as exc_info:
        build_discovery_bundle(
            title="Exact Paper",
            search_response=_search_response(),
            selected_details_response=invalid_details,
            fetch_parent=lambda item_key: _parent_snapshot(),
        )

    assert exc_info.value.code == "invalid_mcp_response"


def test_build_discovery_bundle_rejects_duplicate_normalized_title() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, build_discovery_bundle

    search_response = _search_response()
    search_payload = json.loads(search_response["result"]["content"][0]["text"])
    search_payload["results"].append({"key": "PARENT2", "title": " exact   paper "})
    search_response["result"]["content"][0]["text"] = json.dumps(search_payload)

    with pytest.raises(DiscoveryError) as exc_info:
        build_discovery_bundle(
            title="Exact Paper",
            search_response=search_response,
            selected_details_response=_details_response(),
            fetch_parent=lambda item_key: _parent_snapshot(),
        )

    assert exc_info.value.code == "duplicate_normalized_title"


def test_discover_exact_title_calls_only_required_read_tools() -> None:
    from paper_reader.zotero_discovery import discover_exact_title

    calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        if name == "search_library":
            return _search_response()
        if name == "get_item_details":
            return _details_response()
        raise AssertionError(f"unexpected tool: {name}")

    bundle = discover_exact_title(
        "Exact Paper",
        call_tool=call_tool,
        fetch_parent=lambda item_key: _parent_snapshot(),
    )

    assert bundle["selected_item"]["version"] == 27
    assert calls == [
        (
            "search_library",
            {
                "title": "Exact Paper",
                "titleOperator": "exact",
                "mode": "complete",
                "limit": 100,
            },
        ),
        ("get_item_details", {"itemKey": "PARENT1", "mode": "complete"}),
    ]


def test_http_client_rejects_write_tool_before_network() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, McpHttpClient

    client = McpHttpClient("http://127.0.0.1:23120/mcp")

    with pytest.raises(DiscoveryError) as exc_info:
        client.call_tool("write_note", {"action": "create", "content": "forbidden"})

    assert exc_info.value.code == "forbidden_mcp_tool"
