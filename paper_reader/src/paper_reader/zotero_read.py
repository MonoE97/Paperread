from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.parse import quote

from paper_reader.zotero_live import (
    DEFAULT_CHILD_MAX_MEMBERS,
    DEFAULT_CHILD_MAX_PAGES,
    DEFAULT_CHILD_PAGE_SIZE,
    ZoteroReadError,
    _fetch_item_children,
    fetch_json_url,
)


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
    page_size: int = DEFAULT_CHILD_PAGE_SIZE
    max_pages: int = DEFAULT_CHILD_MAX_PAGES
    max_children: int = DEFAULT_CHILD_MAX_MEMBERS

    def __post_init__(self) -> None:
        for name in ("page_size", "max_pages", "max_children"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

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
        return _fetch_item_children(
            parent_key,
            base_url=self.base_url,
            fetch_json=self.fetch_json,
            page_size=self.page_size,
            max_pages=self.max_pages,
            max_children=self.max_children,
        )

    def get_note(self, note_key: str) -> dict[str, Any]:
        payload = self.fetch_json(self._item_url(note_key))
        if not isinstance(payload, dict):
            raise ValueError("Zotero note response is not an object")
        return payload


__all__ = ["LocalApiZoteroReadProvider", "ZoteroReadError", "ZoteroReadProvider"]
