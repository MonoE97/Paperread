from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.parse import quote

from paper_reader.zotero_live import fetch_json_url


FetchJson = Callable[[str], object]


@runtime_checkable
class ZoteroReadProvider(Protocol):
    """Production boundary for the three read-only Zotero lifecycle reads."""

    def get_parent(self, item_key: str) -> dict[str, Any]: ...

    def get_children(self, parent_key: str) -> list[dict[str, Any]]: ...

    def get_note(self, note_key: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class LocalApiZoteroReadProvider:
    base_url: str = "http://127.0.0.1:23119"
    fetch_json: FetchJson = fetch_json_url
    page_size: int = 100

    def _item_url(self, item_key: str) -> str:
        return (
            f"{self.base_url.rstrip('/')}/api/users/0/items/"
            f"{quote(item_key)}?format=json"
        )

    def get_parent(self, item_key: str) -> dict[str, Any]:
        payload = self.fetch_json(self._item_url(item_key))
        if not isinstance(payload, dict):
            raise ValueError("Zotero parent response is not an object")
        return payload

    def get_children(self, parent_key: str) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        start = 0
        while True:
            url = (
                f"{self.base_url.rstrip('/')}/api/users/0/items/{quote(parent_key)}/children"
                f"?format=json&limit={self.page_size}&start={start}"
            )
            payload = self.fetch_json(url)
            if not isinstance(payload, list):
                raise ValueError("Zotero children response is not an array")
            if not all(isinstance(item, dict) for item in payload):
                raise ValueError("Zotero children response contains a non-object item")
            children.extend(payload)
            if len(payload) < self.page_size:
                return children
            start += self.page_size

    def get_note(self, note_key: str) -> dict[str, Any]:
        payload = self.fetch_json(self._item_url(note_key))
        if not isinstance(payload, dict):
            raise ValueError("Zotero note response is not an object")
        return payload


__all__ = ["LocalApiZoteroReadProvider", "ZoteroReadProvider"]
