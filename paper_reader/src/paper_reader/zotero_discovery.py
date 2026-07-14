from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener

from paper_reader.zotero_item_io import normalize_item_details_payload
from paper_reader.zotero_lifecycle import (
    ZoteroLifecycleError,
    normalized_doi,
    normalized_title,
    normalize_parent_snapshot,
)


FetchParent = Callable[[str], dict[str, Any]]
CallTool = Callable[[str, dict[str, object]], dict[str, Any]]
READ_ONLY_DISCOVERY_TOOLS = frozenset({"search_library", "get_item_details"})


class DiscoveryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _decode_mcp_response(body: bytes) -> dict[str, Any]:
    stripped = body.strip()
    if not stripped:
        return {}
    if stripped.startswith(b"data:"):
        data_lines = [
            line.removeprefix(b"data:").strip()
            for line in stripped.splitlines()
            if line.startswith(b"data:")
        ]
        if not data_lines:
            raise DiscoveryError("invalid_mcp_response", "MCP event stream has no data event")
        stripped = data_lines[-1]
    try:
        payload = json.loads(stripped)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscoveryError("invalid_mcp_response", "MCP response is not JSON") from exc
    if not isinstance(payload, dict):
        raise DiscoveryError("invalid_mcp_response", "MCP response must be an object")
    if "error" in payload:
        raise DiscoveryError("mcp_error", f"MCP returned an error: {payload['error']}")
    return payload


class McpHttpClient:
    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float = 30.0,
        opener: OpenerDirector | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.opener = opener or build_opener(ProxyHandler({}))
        self.session_id: str | None = None
        self._request_id = 0

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, str]]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                return _decode_mcp_response(response.read()), response.headers
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise DiscoveryError("mcp_unavailable", f"Zotero MCP request failed: {exc}") from exc

    def initialize(self) -> None:
        if self.session_id:
            return
        self._request_id += 1
        _response, headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "paper-reader-discovery", "version": "2.0"},
                },
            }
        )
        session_id = str(headers.get("Mcp-Session-Id", "")).strip()
        if not session_id:
            raise DiscoveryError("invalid_mcp_response", "MCP initialize response has no session id")
        self.session_id = session_id
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, Any]:
        if name not in READ_ONLY_DISCOVERY_TOOLS:
            raise DiscoveryError(
                "forbidden_mcp_tool",
                f"discovery helper may not call MCP tool {name!r}",
            )
        self.initialize()
        self._request_id += 1
        response, _headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return response

    def get_json(self, url: str) -> dict[str, Any]:
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise DiscoveryError(
                "local_api_unavailable",
                f"Zotero read-only API failed: {exc}",
            ) from exc
        if not isinstance(payload, dict):
            raise DiscoveryError("invalid_parent_snapshot", "Zotero parent response must be an object")
        return payload


def _mcp_text_payload(response: object, *, context: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise DiscoveryError("invalid_mcp_response", f"{context} response must be an object")
    result = response.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        raise DiscoveryError("invalid_mcp_response", f"{context} response has no text content")
    text = content[0].get("text")
    if content[0].get("type") != "text" or not isinstance(text, str):
        raise DiscoveryError("invalid_mcp_response", f"{context} response has invalid text content")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DiscoveryError("invalid_mcp_response", f"{context} text is not JSON") from exc
    if not isinstance(payload, dict):
        raise DiscoveryError("invalid_mcp_response", f"{context} payload must be an object")
    return payload


def _parent_item_type(snapshot: dict[str, Any]) -> str:
    data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else snapshot
    item_type = str(data.get("itemType", "")).strip()
    if not item_type or item_type in {"attachment", "note"}:
        raise DiscoveryError(
            "invalid_parent_snapshot",
            "read-only parent snapshot must identify a regular Zotero item",
        )
    return item_type


def _require_parent_version(snapshot: dict[str, Any]) -> None:
    data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else snapshot
    version = snapshot.get("version", data.get("version"))
    if type(version) is not int or version < 0:
        raise DiscoveryError(
            "invalid_parent_snapshot",
            "read-only parent snapshot requires a non-negative integer version",
        )


def _normalized_parent(snapshot: object) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise DiscoveryError(
            "invalid_parent_snapshot",
            "read-only parent snapshot must be an object",
        )
    _require_parent_version(snapshot)
    try:
        return normalize_parent_snapshot(snapshot)
    except ZoteroLifecycleError as exc:
        raise DiscoveryError("invalid_parent_snapshot", str(exc)) from exc


def _validate_optional_identity(
    item: dict[str, Any],
    *,
    parent: dict[str, Any],
    item_type: str,
    context: str,
) -> None:
    version = item.get("version")
    if version is not None and version != parent["version"]:
        raise DiscoveryError("parent_identity_mismatch", f"{context} version differs from parent")
    doi = normalized_doi(item.get("DOI", ""))
    if doi and doi != parent["DOI"]:
        raise DiscoveryError("parent_identity_mismatch", f"{context} DOI differs from parent")
    observed_item_type = str(item.get("itemType", "")).strip()
    if observed_item_type and observed_item_type != item_type:
        raise DiscoveryError("parent_identity_mismatch", f"{context} itemType differs from parent")


def build_discovery_bundle(
    *,
    title: str,
    search_response: dict[str, Any],
    selected_details_response: dict[str, Any],
    fetch_parent: FetchParent,
) -> dict[str, Any]:
    search_payload = _mcp_text_payload(search_response, context="search_library")
    results = search_payload.get("results")
    if (
        not isinstance(results, list)
        or not results
        or not all(isinstance(item, dict) for item in results)
    ):
        raise DiscoveryError("item_not_found", "exact-title search returned no item inventory")

    requested_title = normalized_title(title)
    exact_matches = [
        item
        for item in results
        if normalized_title(item.get("title", "")) == requested_title
    ]
    if not exact_matches:
        raise DiscoveryError("item_not_found", "exact-title search returned no normalized-title match")
    if len(exact_matches) > 1:
        raise DiscoveryError(
            "duplicate_normalized_title",
            "exact-title search returned multiple normalized-title matches",
        )

    try:
        selected = normalize_item_details_payload(selected_details_response)
    except ValueError as exc:
        raise DiscoveryError("invalid_mcp_response", f"invalid selected item details: {exc}") from exc
    selected_key = str(selected.get("key", "")).strip()
    expected_key = str(exact_matches[0].get("key", "")).strip()
    if not selected_key or selected_key != expected_key:
        raise DiscoveryError(
            "selected_item_key_mismatch",
            "selected details do not match the unique exact-title search result",
        )

    raw_parent_snapshots: dict[str, dict[str, Any]] = {}
    enriched_inventory: list[dict[str, Any]] = []
    for index, item in enumerate(results):
        item_key = str(item.get("key", "")).strip()
        if not item_key:
            raise DiscoveryError("invalid_search_inventory", f"search result {index} has no key")
        snapshot = fetch_parent(item_key)
        raw_parent_snapshots[item_key] = snapshot
        parent = _normalized_parent(snapshot)
        item_type = _parent_item_type(snapshot)
        if (
            item_key != parent["key"]
            or normalized_title(item.get("title", "")) != parent["normalized_title"]
        ):
            raise DiscoveryError(
                "parent_identity_mismatch",
                f"search result {index} differs from its read-only parent snapshot",
            )
        _validate_optional_identity(
            item,
            parent=parent,
            item_type=item_type,
            context=f"search result {index}",
        )
        enriched_inventory.append(
            {
                **item,
                "version": parent["version"],
                "itemType": item_type,
                "DOI": parent["DOI"],
            }
        )

    selected_parent_snapshot = raw_parent_snapshots[selected_key]
    selected_parent = _normalized_parent(selected_parent_snapshot)
    selected_item_type = _parent_item_type(selected_parent_snapshot)
    if normalized_title(selected.get("title", "")) != selected_parent["normalized_title"]:
        raise DiscoveryError("parent_identity_mismatch", "selected title differs from parent")
    _validate_optional_identity(
        selected,
        parent=selected_parent,
        item_type=selected_item_type,
        context="selected details",
    )

    paper_reader_meta = (
        dict(selected.get("_paper_reader", {}))
        if isinstance(selected.get("_paper_reader"), dict)
        else {}
    )
    paper_reader_meta["discovery"] = {
        "raw_search_response": search_response,
        "raw_selected_details_response": selected_details_response,
        "raw_parent_snapshots": raw_parent_snapshots,
    }
    enriched_selected = {
        **selected,
        "version": selected_parent["version"],
        "itemType": selected_item_type,
        "DOI": selected_parent["DOI"],
        "_paper_reader": paper_reader_meta,
    }
    return {
        "search_results": enriched_inventory,
        "selected_item": enriched_selected,
    }


def discover_exact_title(
    title: str,
    *,
    call_tool: CallTool,
    fetch_parent: FetchParent,
) -> dict[str, Any]:
    search_response = call_tool(
        "search_library",
        {
            "title": title,
            "titleOperator": "exact",
            "mode": "complete",
            "limit": 100,
        },
    )
    search_payload = _mcp_text_payload(search_response, context="search_library")
    results = search_payload.get("results")
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise DiscoveryError("item_not_found", "exact-title search returned no item inventory")
    requested_title = normalized_title(title)
    exact_matches = [
        item
        for item in results
        if normalized_title(item.get("title", "")) == requested_title
    ]
    if not exact_matches:
        raise DiscoveryError("item_not_found", "exact-title search returned no normalized-title match")
    if len(exact_matches) > 1:
        raise DiscoveryError(
            "duplicate_normalized_title",
            "exact-title search returned multiple normalized-title matches",
        )
    item_key = str(exact_matches[0].get("key", "")).strip()
    if not item_key:
        raise DiscoveryError("invalid_search_inventory", "exact-title search result has no key")
    selected_details_response = call_tool(
        "get_item_details",
        {"itemKey": item_key, "mode": "complete"},
    )
    return build_discovery_bundle(
        title=title,
        search_response=search_response,
        selected_details_response=selected_details_response,
        fetch_parent=fetch_parent,
    )


def discover_exact_title_http(
    title: str,
    *,
    mcp_endpoint: str = "http://127.0.0.1:23120/mcp",
    local_api_base: str = "http://127.0.0.1:23119",
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    client = McpHttpClient(mcp_endpoint, timeout_seconds=timeout_seconds)

    def fetch_parent(item_key: str) -> dict[str, Any]:
        base = local_api_base.rstrip("/")
        return client.get_json(f"{base}/api/users/0/items/{quote(item_key)}?format=json")

    return discover_exact_title(
        title,
        call_tool=client.call_tool,
        fetch_parent=fetch_parent,
    )


__all__ = [
    "DiscoveryError",
    "McpHttpClient",
    "build_discovery_bundle",
    "discover_exact_title",
    "discover_exact_title_http",
]
