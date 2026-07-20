from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener

from paper_reader.zotero_item_io import normalize_item_details_payload
from paper_reader.zotero_lifecycle import (
    ZoteroLifecycleError,
    display_title,
    normalized_doi,
    normalized_title,
    normalize_parent_snapshot,
)


FetchParent = Callable[[str], dict[str, Any]]
CallTool = Callable[[str, dict[str, object]], dict[str, Any]]
READ_ONLY_DISCOVERY_TOOLS = frozenset({"search_library", "get_item_details"})
DISCOVERY_PAGE_SIZE = 100
DISCOVERY_MAX_PAGES = 100
DISCOVERY_MAX_RESULTS = DISCOVERY_PAGE_SIZE * DISCOVERY_MAX_PAGES
DISCOVERY_RAW_RESPONSE_MAX_BYTES = 64 * 1024 * 1024
DISCOVERY_AUXILIARY_RESPONSE_MAX_BYTES = 64 * 1024 * 1024
DISCOVERY_TITLE_ANCHOR_ATTEMPTS = 3
DISCOVERY_TITLE_ANCHOR_MIN_CHARS = 6
DEFAULT_HTTP_RESPONSE_MAX_BYTES = 16 * 1024 * 1024
HTTP_READ_CHUNK_BYTES = 64 * 1024


class DiscoveryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _strict_json_loads(value: str | bytes) -> Any:
    return json.loads(value, parse_constant=_reject_nonfinite_json)


def _validated_mcp_payload(
    payload: object,
    *,
    expected_id: str | int | None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DiscoveryError("invalid_mcp_response", "MCP response must be an object")
    if payload.get("jsonrpc") != "2.0":
        raise DiscoveryError("invalid_mcp_response", "MCP response must use JSON-RPC 2.0")
    if expected_id is not None:
        observed_id = payload.get("id")
        if type(observed_id) is not type(expected_id) or observed_id != expected_id:
            raise DiscoveryError(
                "invalid_mcp_response",
                "MCP response id does not match the request id",
            )
        has_result = "result" in payload
        has_error = "error" in payload
        if has_result == has_error:
            raise DiscoveryError(
                "invalid_mcp_response",
                "MCP response must contain exactly one of result or error",
            )
    if "error" in payload:
        raise DiscoveryError("mcp_error", f"MCP returned an error: {payload['error']}")
    return payload


def _sse_payloads(text: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    data_lines: list[str] = []

    def finish_event() -> None:
        if not data_lines:
            return
        data = "\n".join(data_lines)
        data_lines.clear()
        if not data:
            return
        try:
            payload = _strict_json_loads(data)
        except (json.JSONDecodeError, ValueError) as exc:
            raise DiscoveryError(
                "invalid_mcp_response",
                "MCP event-stream data is not JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise DiscoveryError(
                "invalid_mcp_response",
                "MCP event-stream data must be an object",
            )
        payloads.append(payload)

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        if not line:
            finish_event()
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
    finish_event()
    if not payloads:
        raise DiscoveryError("invalid_mcp_response", "MCP event stream has no data event")
    return payloads


def _decode_mcp_response(
    body: bytes,
    *,
    expected_id: str | int | None = None,
) -> dict[str, Any]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiscoveryError("invalid_mcp_response", "MCP response is not UTF-8") from exc
    stripped = text.strip()
    if not stripped:
        if expected_id is not None:
            raise DiscoveryError("invalid_mcp_response", "MCP request returned an empty response")
        return {}
    try:
        payload = _strict_json_loads(stripped)
    except json.JSONDecodeError:
        payloads = _sse_payloads(text)
        if expected_id is None:
            return _validated_mcp_payload(payloads[-1], expected_id=None)
        matching = [payload for payload in payloads if payload.get("id") == expected_id]
        if len(matching) != 1:
            raise DiscoveryError(
                "invalid_mcp_response",
                "MCP event stream does not contain exactly one matching response id",
            )
        return _validated_mcp_payload(matching[0], expected_id=expected_id)
    except ValueError as exc:
        raise DiscoveryError(
            "invalid_mcp_response",
            "MCP response contains a non-finite JSON number",
        ) from exc
    return _validated_mcp_payload(payload, expected_id=expected_id)


def _read_bounded_response(response: Any, *, max_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except (TypeError, ValueError) as exc:
            raise DiscoveryError(
                "invalid_http_response",
                "HTTP Content-Length must be a non-negative integer",
            ) from exc
        if declared_bytes < 0:
            raise DiscoveryError(
                "invalid_http_response",
                "HTTP Content-Length must be a non-negative integer",
            )
        if declared_bytes > max_bytes:
            raise DiscoveryError(
                "resource_limit",
                f"HTTP response exceeds the {max_bytes}-byte discovery limit",
            )

    body = bytearray()
    while True:
        read_size = min(HTTP_READ_CHUNK_BYTES, max_bytes - len(body) + 1)
        chunk = response.read(read_size)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise DiscoveryError("invalid_http_response", "HTTP response returned non-byte data")
        body.extend(chunk)
        if len(body) > max_bytes:
            raise DiscoveryError(
                "resource_limit",
                f"HTTP response exceeds the {max_bytes}-byte discovery limit",
            )
    return bytes(body)


class McpHttpClient:
    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float = 30.0,
        opener: OpenerDirector | None = None,
        max_response_bytes: int = DEFAULT_HTTP_RESPONSE_MAX_BYTES,
    ) -> None:
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError("max_response_bytes must be a positive integer")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.opener = opener or build_opener(ProxyHandler({}))
        self.max_response_bytes = max_response_bytes
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
                body = _read_bounded_response(
                    response,
                    max_bytes=self.max_response_bytes,
                )
                expected_id = payload.get("id")
                if type(expected_id) not in {str, int}:
                    expected_id = None
                return (
                    _decode_mcp_response(body, expected_id=expected_id),
                    response.headers,
                )
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
                    "clientInfo": {"name": "paper-reader-discovery", "version": "2.1"},
                },
            }
        )
        session_id = str(headers.get("Mcp-Session-Id", "")).strip()
        if not session_id:
            raise DiscoveryError("invalid_mcp_response", "MCP initialize response has no session id")
        self.session_id = session_id
        notification_complete = False
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            notification_complete = True
        finally:
            if not notification_complete:
                self.session_id = None

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
                body = _read_bounded_response(
                    response,
                    max_bytes=self.max_response_bytes,
                )
                payload = _strict_json_loads(body.decode("utf-8"))
        except DiscoveryError:
            raise
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
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
        payload = _strict_json_loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise DiscoveryError("invalid_mcp_response", f"{context} text is not JSON") from exc
    if not isinstance(payload, dict):
        raise DiscoveryError("invalid_mcp_response", f"{context} payload must be an object")
    return payload


def _search_page(
    response: dict[str, Any],
    *,
    expected_offset: int,
) -> tuple[list[dict[str, Any]], int]:
    payload = _mcp_text_payload(response, context="search_library")
    results = payload.get("results")
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise DiscoveryError(
            "invalid_search_inventory",
            "search_library results must be an array of objects",
        )
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        raise DiscoveryError(
            "incomplete_search_inventory",
            "search_library response has no pagination metadata",
        )
    total = pagination.get("total")
    if type(total) is not int or total < 0:
        raise DiscoveryError(
            "incomplete_search_inventory",
            "search_library pagination total must be a non-negative integer",
        )
    observed_offset = pagination.get("offset", expected_offset)
    if type(observed_offset) is not int or observed_offset != expected_offset:
        raise DiscoveryError(
            "incomplete_search_inventory",
            "search_library pagination offset does not match the requested page",
        )
    if expected_offset + len(results) > total:
        raise DiscoveryError(
            "incomplete_search_inventory",
            "search_library returned more results than its pagination total",
        )
    has_more = pagination.get("hasMore")
    if has_more is not None:
        expected_has_more = expected_offset + len(results) < total
        if type(has_more) is not bool or has_more is not expected_has_more:
            raise DiscoveryError(
                "incomplete_search_inventory",
                "search_library hasMore disagrees with its result inventory",
            )
    return results, total


def _serialized_json_size(response: object) -> int:
    try:
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DiscoveryError(
            "invalid_mcp_response",
            "discovery response is not canonical JSON data",
        ) from exc
    return len(encoded)


def _require_raw_response_budget(search_responses: Sequence[dict[str, Any]]) -> None:
    aggregate_bytes = 0
    for response in search_responses:
        aggregate_bytes += _serialized_json_size(response)
        if aggregate_bytes > DISCOVERY_RAW_RESPONSE_MAX_BYTES:
            raise DiscoveryError(
                "resource_limit",
                "search_library raw responses exceed the discovery aggregate byte limit",
            )


def _complete_search_inventory(
    search_responses: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not search_responses:
        raise DiscoveryError(
            "incomplete_search_inventory",
            "search_library returned no response pages",
        )
    if len(search_responses) > DISCOVERY_MAX_PAGES:
        raise DiscoveryError(
            "resource_limit",
            f"search_library exceeded the {DISCOVERY_MAX_PAGES}-page discovery limit",
        )
    _require_raw_response_budget(search_responses)

    inventory: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    expected_total: int | None = None
    expected_offset = 0
    for page_index, response in enumerate(search_responses):
        page, total = _search_page(response, expected_offset=expected_offset)
        if total > DISCOVERY_MAX_RESULTS:
            raise DiscoveryError(
                "resource_limit",
                f"search_library total exceeds the {DISCOVERY_MAX_RESULTS}-item discovery limit",
            )
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise DiscoveryError(
                "incomplete_search_inventory",
                "search_library pagination total changed between pages",
            )
        for result_index, item in enumerate(page):
            key = str(item.get("key", "")).strip()
            if not key:
                raise DiscoveryError(
                    "invalid_search_inventory",
                    f"search result {expected_offset + result_index} has no key",
                )
            if key in seen_keys:
                raise DiscoveryError(
                    "incomplete_search_inventory",
                    f"search_library repeated item key {key!r} across pages",
                )
            seen_keys.add(key)
            inventory.append(item)
        expected_offset += len(page)
        if page_index < len(search_responses) - 1 and expected_offset >= total:
            raise DiscoveryError(
                "incomplete_search_inventory",
                "search_library returned an unexpected page after inventory completion",
            )

    if expected_total is None or len(inventory) != expected_total:
        raise DiscoveryError(
            "incomplete_search_inventory",
            "search_library response pages do not cover the complete inventory",
        )
    return inventory


def _fetch_complete_search(
    *,
    title: str,
    title_operator: str,
    call_tool: CallTool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    responses: list[dict[str, Any]] = []
    aggregate_response_bytes = 0
    expected_total: int | None = None
    offset = 0
    for _page_index in range(DISCOVERY_MAX_PAGES):
        response = call_tool(
            "search_library",
            {
                "title": title,
                "titleOperator": title_operator,
                "mode": "complete",
                "limit": DISCOVERY_PAGE_SIZE,
                "offset": offset,
            },
        )
        aggregate_response_bytes += _serialized_json_size(response)
        if aggregate_response_bytes > DISCOVERY_RAW_RESPONSE_MAX_BYTES:
            raise DiscoveryError(
                "resource_limit",
                "search_library raw responses exceed the discovery aggregate byte limit",
            )
        responses.append(response)
        page, total = _search_page(response, expected_offset=offset)
        if total > DISCOVERY_MAX_RESULTS:
            raise DiscoveryError(
                "resource_limit",
                f"search_library total exceeds the {DISCOVERY_MAX_RESULTS}-item discovery limit",
            )
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise DiscoveryError(
                "incomplete_search_inventory",
                "search_library pagination total changed between pages",
            )
        next_offset = offset + len(page)
        if next_offset == total:
            return responses, _complete_search_inventory(responses)
        if not page or next_offset <= offset:
            raise DiscoveryError(
                "incomplete_search_inventory",
                "search_library pagination stalled before the complete inventory was returned",
            )
        offset = next_offset
    raise DiscoveryError(
        "resource_limit",
        f"search_library exceeded the {DISCOVERY_MAX_PAGES}-page discovery limit",
    )


def _title_search_anchors(title: str) -> list[str]:
    visible_title = display_title(title)
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for index, token in enumerate(re.findall(r"[^\W_]+", visible_title, flags=re.UNICODE)):
        normalized_token = token.casefold()
        if (
            len(token) < DISCOVERY_TITLE_ANCHOR_MIN_CHARS
            or normalized_token in seen
            or normalized_token == visible_title.casefold()
        ):
            continue
        seen.add(normalized_token)
        ranked.append((-len(token), index, token))
    ranked.sort()
    return [token for _length, _index, token in ranked[:DISCOVERY_TITLE_ANCHOR_ATTEMPTS]]


def _visible_fragment_matches(
    inventory: Sequence[dict[str, Any]],
    *,
    requested_title: str,
) -> list[dict[str, Any]]:
    return [
        item
        for item in inventory
        if requested_title in normalized_title(item.get("title", ""))
    ]


def _parent_item_type(snapshot: dict[str, Any]) -> str:
    data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else snapshot
    raw_item_type = data.get("itemType")
    if not isinstance(raw_item_type, str) or not raw_item_type.strip():
        raise DiscoveryError(
            "invalid_parent_snapshot",
            "read-only parent snapshot itemType must be a non-empty string",
        )
    item_type = raw_item_type.strip()
    if item_type in {"attachment", "note"}:
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


def _optional_extra(
    payload: dict[str, Any],
    *,
    context: str,
    error_code: str,
) -> tuple[bool, str]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if "extra" not in data:
        return False, ""
    extra = data["extra"]
    if not isinstance(extra, str):
        raise DiscoveryError(error_code, f"{context} Extra must be a string when present")
    return True, extra


def build_discovery_bundle(
    *,
    title: str,
    search_response: dict[str, Any],
    selected_details_response: dict[str, Any],
    fetch_parent: FetchParent,
) -> dict[str, Any]:
    results = _complete_search_inventory([search_response])
    return _build_discovery_bundle(
        title=title,
        results=results,
        raw_search_responses=[search_response],
        selected_details_response=selected_details_response,
        fetch_parent=fetch_parent,
    )


def _build_discovery_bundle(
    *,
    title: str,
    results: list[dict[str, Any]],
    raw_search_responses: Sequence[dict[str, Any]],
    selected_details_response: dict[str, Any],
    fetch_parent: FetchParent,
    requested_title: str | None = None,
    raw_title_resolution_search_responses: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    if not results:
        raise DiscoveryError("item_not_found", "exact-title search returned no item inventory")

    auxiliary_response_bytes = _serialized_json_size(selected_details_response)
    if auxiliary_response_bytes > DISCOVERY_AUXILIARY_RESPONSE_MAX_BYTES:
        raise DiscoveryError(
            "resource_limit",
            "selected details and parent snapshots exceed the discovery aggregate byte limit",
        )

    exact_title_normalized = normalized_title(title)
    exact_matches = [
        item
        for item in results
        if normalized_title(item.get("title", "")) == exact_title_normalized
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
        auxiliary_response_bytes += _serialized_json_size(snapshot)
        if auxiliary_response_bytes > DISCOVERY_AUXILIARY_RESPONSE_MAX_BYTES:
            raise DiscoveryError(
                "resource_limit",
                "selected details and parent snapshots exceed the discovery aggregate byte limit",
            )
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
    parent_has_extra, parent_extra = _optional_extra(
        selected_parent_snapshot,
        context="read-only parent snapshot",
        error_code="invalid_parent_snapshot",
    )
    selected_has_extra, selected_extra = _optional_extra(
        selected,
        context="selected details",
        error_code="invalid_mcp_response",
    )
    if parent_has_extra and selected_has_extra and selected_extra != parent_extra:
        raise DiscoveryError(
            "parent_extra_mismatch",
            "selected details Extra differs from the read-only parent snapshot",
        )
    # Extra is authoritative only in the parent snapshot. Keep an unbound
    # selected-details value solely inside the raw discovery provenance.
    selected.pop("extra", None)

    paper_reader_meta = (
        dict(selected.get("_paper_reader", {}))
        if isinstance(selected.get("_paper_reader"), dict)
        else {}
    )
    raw_search_provenance: object = (
        raw_search_responses[0]
        if len(raw_search_responses) == 1
        else list(raw_search_responses)
    )
    discovery_provenance: dict[str, Any] = {
        "raw_search_response": raw_search_provenance,
        "raw_selected_details_response": selected_details_response,
        "raw_parent_snapshots": raw_parent_snapshots,
    }
    if requested_title is not None:
        discovery_provenance["requested_title"] = requested_title
        discovery_provenance["resolved_title"] = str(exact_matches[0].get("title", ""))
    if raw_title_resolution_search_responses:
        discovery_provenance["raw_title_resolution_search_responses"] = list(
            raw_title_resolution_search_responses
        )
    paper_reader_meta["discovery"] = discovery_provenance
    if parent_has_extra:
        selected["extra"] = parent_extra
        enrichment = paper_reader_meta.get("enrichment")
        if not isinstance(enrichment, dict):
            enrichment = {}
        enrichment["extra"] = {
            "source": "zotero_parent_snapshot",
            "item_key": selected_key,
            "version": selected_parent["version"],
        }
        paper_reader_meta["enrichment"] = enrichment
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
    try:
        requested_title = normalized_title(title)
    except ZoteroLifecycleError as exc:
        raise DiscoveryError("invalid_title", f"invalid Zotero title input: {exc}") from exc
    if not requested_title:
        raise DiscoveryError("invalid_title", "Zotero title or title fragment must not be blank")

    exact_search_responses, exact_results = _fetch_complete_search(
        title=title,
        title_operator="exact",
        call_tool=call_tool,
    )
    exact_matches = [
        item
        for item in exact_results
        if normalized_title(item.get("title", "")) == requested_title
    ]
    if len(exact_matches) > 1:
        raise DiscoveryError(
            "duplicate_normalized_title",
            "exact-title search returned multiple normalized-title matches",
        )
    title_resolution_search_responses: list[dict[str, Any]] = []
    resolved_item_key: str | None = None
    if exact_matches:
        resolved_title = str(exact_matches[0].get("title", ""))
        final_search_responses = exact_search_responses
        final_results = exact_results
    else:
        title_resolution_search_responses = list(exact_search_responses)
        contains_search_responses, fragment_results = _fetch_complete_search(
            title=title,
            title_operator="contains",
            call_tool=call_tool,
        )
        title_resolution_search_responses.extend(contains_search_responses)
        _require_raw_response_budget(title_resolution_search_responses)
        fragment_matches = _visible_fragment_matches(
            fragment_results,
            requested_title=requested_title,
        )
        if not fragment_matches:
            anchored_matches: dict[str, dict[str, Any]] = {}
            for anchor in _title_search_anchors(title):
                anchor_responses, anchor_results = _fetch_complete_search(
                    title=anchor,
                    title_operator="contains",
                    call_tool=call_tool,
                )
                title_resolution_search_responses.extend(anchor_responses)
                _require_raw_response_budget(title_resolution_search_responses)
                for item in _visible_fragment_matches(
                    anchor_results,
                    requested_title=requested_title,
                ):
                    item_key = str(item.get("key", "")).strip()
                    previous = anchored_matches.get(item_key)
                    if previous is not None and previous != item:
                        raise DiscoveryError(
                            "incomplete_search_inventory",
                            f"search result {item_key!r} changed between title-anchor queries",
                        )
                    anchored_matches[item_key] = item
            fragment_matches = list(anchored_matches.values())
        if not fragment_matches:
            raise DiscoveryError(
                "item_not_found",
                "title-fragment search returned no normalized-title match",
            )
        if len(fragment_matches) > 1:
            normalized_candidates = {
                normalized_title(item.get("title", "")) for item in fragment_matches
            }
            if len(normalized_candidates) == 1:
                raise DiscoveryError(
                    "duplicate_normalized_title",
                    "title-fragment search returned duplicate normalized titles",
                )
            raise DiscoveryError(
                "ambiguous_title_fragment",
                "title fragment matches multiple Zotero items",
            )
        resolved_match = fragment_matches[0]
        resolved_item_key = str(resolved_match.get("key", "")).strip()
        if not resolved_item_key:
            raise DiscoveryError(
                "invalid_search_inventory",
                "title-fragment search result has no key",
            )
        resolved_title = str(resolved_match.get("title", ""))
        final_search_responses, final_results = _fetch_complete_search(
            title=resolved_title,
            title_operator="exact",
            call_tool=call_tool,
        )

    resolved_normalized_title = normalized_title(resolved_title)
    final_exact_matches = [
        item
        for item in final_results
        if normalized_title(item.get("title", "")) == resolved_normalized_title
    ]
    if not final_exact_matches:
        raise DiscoveryError(
            "item_not_found",
            "resolved exact-title search returned no normalized-title match",
        )
    if len(final_exact_matches) > 1:
        raise DiscoveryError(
            "duplicate_normalized_title",
            "resolved exact-title search returned multiple normalized-title matches",
        )
    item_key = str(final_exact_matches[0].get("key", "")).strip()
    if not item_key:
        raise DiscoveryError("invalid_search_inventory", "exact-title search result has no key")
    if resolved_item_key is not None and item_key != resolved_item_key:
        raise DiscoveryError(
            "title_resolution_changed",
            "resolved Zotero item key changed between fragment and exact-title searches",
        )
    selected_details_response = call_tool(
        "get_item_details",
        {"itemKey": item_key, "mode": "complete"},
    )
    return _build_discovery_bundle(
        title=resolved_title,
        results=final_results,
        raw_search_responses=final_search_responses,
        selected_details_response=selected_details_response,
        fetch_parent=fetch_parent,
        requested_title=title,
        raw_title_resolution_search_responses=title_resolution_search_responses,
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
