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


def _paged_search_response(
    *,
    title: str,
    operator: str,
    results: list[dict[str, object]],
    total: int,
    offset: int,
    request_id: int,
) -> dict[str, object]:
    return _mcp_response(
        {
            "query": {
                "title": title,
                "titleOperator": operator,
                "limit": "100",
                "offset": str(offset),
            },
            "pagination": {
                "limit": 100,
                "offset": offset,
                "total": total,
                "hasMore": offset + len(results) < total,
            },
            "results": results,
        },
        request_id=request_id,
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


def test_build_discovery_bundle_rejects_non_string_parent_item_type() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, build_discovery_bundle

    parent_snapshot = _parent_snapshot()
    parent_snapshot["data"]["itemType"] = 123

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
    search_payload["pagination"]["total"] = 2
    search_response["result"]["content"][0]["text"] = json.dumps(search_payload)

    with pytest.raises(DiscoveryError) as exc_info:
        build_discovery_bundle(
            title="Exact Paper",
            search_response=search_response,
            selected_details_response=_details_response(),
            fetch_parent=lambda item_key: _parent_snapshot(),
        )

    assert exc_info.value.code == "duplicate_normalized_title"


def test_build_discovery_bundle_rejects_incomplete_search_inventory() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, build_discovery_bundle

    search_response = _search_response()
    search_payload = json.loads(search_response["result"]["content"][0]["text"])
    search_payload["pagination"]["total"] = 2
    search_response["result"]["content"][0]["text"] = json.dumps(search_payload)

    with pytest.raises(DiscoveryError) as exc_info:
        build_discovery_bundle(
            title="Exact Paper",
            search_response=search_response,
            selected_details_response=_details_response(),
            fetch_parent=lambda item_key: _parent_snapshot(),
        )

    assert exc_info.value.code == "incomplete_search_inventory"


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
                "offset": 0,
            },
        ),
        ("get_item_details", {"itemKey": "PARENT1", "mode": "complete"}),
    ]


def test_discover_exact_title_resolves_unique_title_fragment() -> None:
    from paper_reader.zotero_discovery import discover_exact_title

    fragment = "Stack pressure effects and viscoplastic deformation"
    full_title = f"{fragment} in argyrodite solid-state electrolyte"
    calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        if name == "get_item_details":
            return _mcp_response(
                {
                    "key": "PARENT1",
                    "itemType": "",
                    "title": full_title,
                    "DOI": "10.1000/example",
                    "attachments": [],
                },
                request_id=5,
            )
        operator = str(arguments["titleOperator"])
        title = str(arguments["title"])
        if operator == "exact" and title == fragment:
            return _paged_search_response(
                title=title,
                operator=operator,
                results=[],
                total=0,
                offset=0,
                request_id=2,
            )
        if operator == "contains":
            return _paged_search_response(
                title=title,
                operator=operator,
                results=[{"key": "PARENT1", "title": full_title}],
                total=1,
                offset=0,
                request_id=3,
            )
        if operator == "exact" and title == full_title:
            return _paged_search_response(
                title=title,
                operator=operator,
                results=[{"key": "PARENT1", "title": full_title}],
                total=1,
                offset=0,
                request_id=4,
            )
        raise AssertionError((name, arguments))

    parent = _parent_snapshot()
    parent["data"]["title"] = full_title
    bundle = discover_exact_title(
        fragment,
        call_tool=call_tool,
        fetch_parent=lambda item_key: parent,
    )

    assert bundle["selected_item"]["title"] == full_title
    discovery = bundle["selected_item"]["_paper_reader"]["discovery"]
    assert discovery["requested_title"] == fragment
    assert len(discovery["raw_title_resolution_search_responses"]) == 2
    assert [arguments["titleOperator"] for name, arguments in calls if name == "search_library"] == [
        "exact",
        "contains",
        "exact",
    ]


def test_discover_exact_title_rejects_item_key_change_during_fragment_resolution() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, discover_exact_title

    fragment = "Stack pressure effects"
    full_title = f"{fragment} in argyrodite solid-state electrolyte"

    def call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name == "get_item_details":
            return _mcp_response(
                {
                    "key": "PARENT2",
                    "itemType": "",
                    "title": full_title,
                    "DOI": "10.1000/example",
                    "attachments": [],
                },
                request_id=5,
            )
        operator = str(arguments["titleOperator"])
        query_title = str(arguments["title"])
        if operator == "exact" and query_title == fragment:
            results: list[dict[str, object]] = []
        elif operator == "contains":
            results = [{"key": "PARENT1", "title": full_title}]
        elif operator == "exact" and query_title == full_title:
            results = [{"key": "PARENT2", "title": full_title}]
        else:
            raise AssertionError((name, arguments))
        return _paged_search_response(
            title=query_title,
            operator=operator,
            results=results,
            total=len(results),
            offset=int(arguments["offset"]),
            request_id=2,
        )

    parent = _parent_snapshot()
    parent["key"] = "PARENT2"
    parent["data"]["key"] = "PARENT2"
    parent["data"]["title"] = full_title
    with pytest.raises(DiscoveryError) as exc_info:
        discover_exact_title(
            fragment,
            call_tool=call_tool,
            fetch_parent=lambda item_key: parent,
        )

    assert exc_info.value.code == "title_resolution_changed"


def test_discover_exact_title_reads_all_fragment_pages_before_resolving() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, discover_exact_title

    fragment = "shared fragment"
    calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        if name != "search_library":
            raise AssertionError((name, arguments))
        operator = str(arguments["titleOperator"])
        offset = int(arguments["offset"])
        if operator == "exact":
            return _paged_search_response(
                title=fragment,
                operator=operator,
                results=[],
                total=0,
                offset=0,
                request_id=2,
            )
        result = (
            {"key": "PARENT1", "title": "A shared fragment paper"}
            if offset == 0
            else {"key": "PARENT2", "title": "Another shared fragment paper"}
        )
        return _paged_search_response(
            title=fragment,
            operator=operator,
            results=[result],
            total=2,
            offset=offset,
            request_id=3 + offset,
        )

    with pytest.raises(DiscoveryError) as exc_info:
        discover_exact_title(
            fragment,
            call_tool=call_tool,
            fetch_parent=lambda item_key: _parent_snapshot(),
        )

    assert exc_info.value.code == "ambiguous_title_fragment"
    assert [arguments["offset"] for name, arguments in calls if name == "search_library"] == [
        0,
        0,
        1,
    ]


def test_discover_exact_title_detects_duplicate_on_later_exact_page() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, discover_exact_title

    calls: list[dict[str, object]] = []

    def call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name != "search_library":
            raise AssertionError((name, arguments))
        calls.append(arguments)
        offset = int(arguments["offset"])
        key = "PARENT1" if offset == 0 else "PARENT2"
        return _paged_search_response(
            title="Exact Paper",
            operator="exact",
            results=[{"key": key, "title": "Exact Paper"}],
            total=2,
            offset=offset,
            request_id=2 + offset,
        )

    with pytest.raises(DiscoveryError) as exc_info:
        discover_exact_title(
            "Exact Paper",
            call_tool=call_tool,
            fetch_parent=lambda item_key: _parent_snapshot(),
        )

    assert exc_info.value.code == "duplicate_normalized_title"
    assert [arguments["offset"] for arguments in calls] == [0, 1]


def test_http_client_rejects_write_tool_before_network() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, McpHttpClient

    client = McpHttpClient("http://127.0.0.1:23120/mcp")

    with pytest.raises(DiscoveryError) as exc_info:
        client.call_tool("write_note", {"action": "create", "content": "forbidden"})

    assert exc_info.value.code == "forbidden_mcp_tool"


def test_decode_mcp_response_parses_multiline_sse_and_selects_matching_id() -> None:
    from paper_reader.zotero_discovery import _decode_mcp_response

    body = (
        b"event: message\n"
        b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        b"event: message\n"
        b'data: {"jsonrpc":"2.0",\n'
        b'data: "id":7,"result":{"ok":true}}\n\n'
    )

    assert _decode_mcp_response(body, expected_id=7) == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"ok": True},
    }


def test_decode_mcp_response_rejects_mismatched_json_rpc_id() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, _decode_mcp_response

    body = b'{"jsonrpc":"2.0","id":8,"result":{}}'

    with pytest.raises(DiscoveryError) as exc_info:
        _decode_mcp_response(body, expected_id=7)

    assert exc_info.value.code == "invalid_mcp_response"


def test_decode_mcp_response_rejects_boolean_id_for_integer_request() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, _decode_mcp_response

    body = b'{"jsonrpc":"2.0","id":true,"result":{}}'

    with pytest.raises(DiscoveryError) as exc_info:
        _decode_mcp_response(body, expected_id=1)

    assert exc_info.value.code == "invalid_mcp_response"


def test_decode_mcp_response_rejects_nonfinite_json_number() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, _decode_mcp_response

    body = b'{"jsonrpc":"2.0","id":1,"result":{"value":NaN}}'

    with pytest.raises(DiscoveryError) as exc_info:
        _decode_mcp_response(body, expected_id=1)

    assert exc_info.value.code == "invalid_mcp_response"


class _FakeResponse:
    def __init__(self, body: bytes, *, content_length: int | None = None) -> None:
        self._body = body
        self._offset = 0
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise AssertionError("response body must be read with an explicit bound")
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _FakeOpener:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def open(self, request, timeout):
        return self.response


class _SequenceOpener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses

    def open(self, request, timeout):
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_http_client_rejects_content_length_over_response_limit() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, McpHttpClient

    response = _FakeResponse(b"", content_length=33)
    client = McpHttpClient(
        "http://127.0.0.1:23120/mcp",
        opener=_FakeOpener(response),
        max_response_bytes=32,
    )

    with pytest.raises(DiscoveryError) as exc_info:
        client._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert exc_info.value.code == "resource_limit"


def test_http_client_rejects_chunked_body_over_response_limit() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, McpHttpClient

    response = _FakeResponse(b"x" * 33)
    client = McpHttpClient(
        "http://127.0.0.1:23120/mcp",
        opener=_FakeOpener(response),
        max_response_bytes=32,
    )

    with pytest.raises(DiscoveryError) as exc_info:
        client.get_json("http://127.0.0.1:23119/api/users/0/items/PARENT1")

    assert exc_info.value.code == "resource_limit"


def test_discover_exact_title_rejects_blank_title_before_tool_call() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, discover_exact_title

    calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        return _paged_search_response(
            title="",
            operator=str(arguments["titleOperator"]),
            results=[],
            total=0,
            offset=int(arguments["offset"]),
            request_id=2,
        )

    with pytest.raises(DiscoveryError) as exc_info:
        discover_exact_title(
            "   ",
            call_tool=call_tool,
            fetch_parent=lambda item_key: _parent_snapshot(),
        )

    assert exc_info.value.code == "invalid_title"
    assert calls == []


def test_build_discovery_bundle_rejects_raw_response_aggregate_over_limit(
    monkeypatch,
) -> None:
    import paper_reader.zotero_discovery as module

    search_response = _search_response()
    search_response["padding"] = "x" * 512
    monkeypatch.setattr(module, "DISCOVERY_RAW_RESPONSE_MAX_BYTES", 256, raising=False)

    with pytest.raises(module.DiscoveryError) as exc_info:
        module.build_discovery_bundle(
            title="Exact Paper",
            search_response=search_response,
            selected_details_response=_details_response(),
            fetch_parent=lambda item_key: _parent_snapshot(),
        )

    assert exc_info.value.code == "resource_limit"


def test_build_discovery_bundle_rejects_auxiliary_response_aggregate_over_limit(
    monkeypatch,
) -> None:
    import paper_reader.zotero_discovery as module

    details_response = _details_response()
    parent_snapshot = _parent_snapshot()
    baseline_bytes = module._serialized_json_size(details_response)
    baseline_bytes += module._serialized_json_size(parent_snapshot)
    parent_snapshot["padding"] = "x" * 128
    monkeypatch.setattr(
        module,
        "DISCOVERY_AUXILIARY_RESPONSE_MAX_BYTES",
        baseline_bytes,
        raising=False,
    )

    with pytest.raises(module.DiscoveryError) as exc_info:
        module.build_discovery_bundle(
            title="Exact Paper",
            search_response=_search_response(),
            selected_details_response=details_response,
            fetch_parent=lambda item_key: parent_snapshot,
        )

    assert exc_info.value.code == "resource_limit"


def test_discover_exact_title_resolves_visible_fragment_across_html_markup() -> None:
    from paper_reader.zotero_discovery import discover_exact_title

    visible_title = (
        "Interfacial degradation of the NMC/Li6 PS5 Cl composite cathode "
        "in all-solid-state batteries"
    )
    stored_title = (
        "Interfacial degradation of the NMC/Li<sub>6</sub> PS<sub>5</sub> Cl "
        "composite cathode in all-solid-state batteries"
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        if name == "get_item_details":
            return _mcp_response(
                {
                    "key": "PARENT1",
                    "itemType": "",
                    "title": stored_title,
                    "DOI": "10.1000/example",
                    "attachments": [],
                },
                request_id=20,
            )
        query_title = str(arguments["title"])
        operator = str(arguments["titleOperator"])
        if operator == "exact" and query_title == visible_title:
            results: list[dict[str, object]] = []
        elif operator == "contains" and query_title == visible_title:
            results = []
        elif operator == "exact" and query_title == stored_title:
            results = [{"key": "PARENT1", "title": stored_title}]
        else:
            results = [{"key": "PARENT1", "title": stored_title}]
        return _paged_search_response(
            title=query_title,
            operator=operator,
            results=results,
            total=len(results),
            offset=int(arguments["offset"]),
            request_id=len(calls) + 1,
        )

    parent = _parent_snapshot()
    parent["data"]["title"] = stored_title
    bundle = discover_exact_title(
        visible_title,
        call_tool=call_tool,
        fetch_parent=lambda item_key: parent,
    )

    assert bundle["selected_item"]["title"] == stored_title
    anchor_queries = [
        str(arguments["title"])
        for name, arguments in calls
        if name == "search_library"
        and arguments["titleOperator"] == "contains"
        and arguments["title"] != visible_title
    ]
    assert anchor_queries


def test_http_client_clears_partial_session_when_initialized_notification_fails() -> None:
    from paper_reader.zotero_discovery import DiscoveryError, McpHttpClient

    initialize_response = _FakeResponse(
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05"}}'
    )
    initialize_response.headers["Mcp-Session-Id"] = "session-1"
    client = McpHttpClient(
        "http://127.0.0.1:23120/mcp",
        opener=_SequenceOpener([initialize_response, OSError("notification failed")]),
        max_response_bytes=1024,
    )

    with pytest.raises(DiscoveryError) as exc_info:
        client.initialize()

    assert exc_info.value.code == "mcp_unavailable"
    assert client.session_id is None
