from __future__ import annotations

import importlib
import importlib.util

import pytest


def _module():
    module_name = "paper_reader.zotero_read"
    assert importlib.util.find_spec(module_name) is not None, "Zotero read provider module is missing"
    return importlib.import_module(module_name)


def _parent() -> dict[str, object]:
    return {
        "key": "PARENT1",
        "version": 17,
        "library": {"type": "user", "id": 0, "name": "My Library"},
        "links": {"self": {"href": "http://127.0.0.1:23119/api/users/0/items/PARENT1"}},
        "meta": {"creatorSummary": "Lovelace", "parsedDate": "2026"},
        "data": {
            "key": "PARENT1",
            "version": 17,
            "itemType": "journalArticle",
            "title": "A Useful Paper",
            "DOI": "10.1000/example.doi",
            "creators": [],
            "tags": [],
            "collections": [],
            "relations": {},
            "dateAdded": "2026-07-10T00:00:00Z",
            "dateModified": "2026-07-10T00:00:00Z",
        },
    }


def _note(key: str = "NOTE1") -> dict[str, object]:
    return {
        "key": key,
        "version": 4,
        "library": {"type": "user", "id": 0, "name": "My Library"},
        "links": {"self": {"href": f"http://127.0.0.1:23119/api/users/0/items/{key}"}},
        "meta": {},
        "data": {
            "key": key,
            "version": 4,
            "itemType": "note",
            "parentItem": "PARENT1",
            "note": "<h1>[Codex Summary] A Useful Paper - 2026-07-10</h1><p>body</p>",
            "tags": [{"tag": "codex-summary", "type": 1}],
            "collections": [],
            "relations": {},
            "dateAdded": "2026-07-10T00:00:00Z",
            "dateModified": "2026-07-10T00:00:00Z",
        },
    }


def test_read_provider_protocol_accepts_complete_in_memory_provider() -> None:
    module = _module()

    class InMemoryProvider:
        def get_parent(self, item_key: str) -> dict[str, object]:
            assert item_key == "PARENT1"
            return _parent()

        def get_children(self, parent_key: str) -> list[dict[str, object]]:
            assert parent_key == "PARENT1"
            return [_note()]

        def get_note(self, note_key: str) -> dict[str, object]:
            assert note_key == "NOTE1"
            return _note(note_key)

    provider = InMemoryProvider()

    assert isinstance(provider, module.ZoteroReadProvider)
    assert provider.get_parent("PARENT1")["data"]["title"] == "A Useful Paper"
    assert provider.get_children("PARENT1")[0]["data"]["itemType"] == "note"
    assert provider.get_note("NOTE1")["key"] == "NOTE1"


def test_default_local_api_provider_returns_full_read_only_payload_shapes() -> None:
    module = _module()
    payloads = [_parent(), [_note()], _note()]

    def fetch_json(_url: str):
        return payloads.pop(0)

    provider = module.LocalApiZoteroReadProvider(
        base_url="http://zotero.test",
        fetch_json=fetch_json,
    )

    assert provider.get_parent("PARENT1") == _parent()
    assert provider.get_children("PARENT1") == [_note()]
    assert provider.get_note("NOTE1") == _note()
    assert payloads == []


def test_children_read_fails_closed_when_server_repeats_a_full_page() -> None:
    module = _module()
    note = _note()
    requested_urls: list[str] = []

    def fetch_json(url: str):
        requested_urls.append(url)
        return [note]

    provider = module.LocalApiZoteroReadProvider(
        base_url="http://zotero.test",
        fetch_json=fetch_json,
        page_size=1,
        max_pages=3,
        max_children=3,
    )

    with pytest.raises(module.ZoteroReadError) as exc_info:
        provider.get_children("PARENT1")

    assert exc_info.value.code == "zotero_children_pagination_stalled"
    assert len(requested_urls) == 2


def test_children_read_enforces_page_limit_before_an_unbounded_fetch_loop() -> None:
    module = _module()
    counter = 0

    def fetch_json(_url: str):
        nonlocal counter
        counter += 1
        return [_note(f"NOTE{counter}")]

    provider = module.LocalApiZoteroReadProvider(
        fetch_json=fetch_json,
        page_size=1,
        max_pages=2,
        max_children=10,
    )

    with pytest.raises(module.ZoteroReadError) as exc_info:
        provider.get_children("PARENT1")

    assert exc_info.value.code == "zotero_children_page_limit_exceeded"
    assert exc_info.value.data == {"page_count": 2, "max_pages": 2}
    assert counter == 2


def test_children_read_enforces_member_limit_before_extending_result() -> None:
    module = _module()
    provider = module.LocalApiZoteroReadProvider(
        fetch_json=lambda _url: [_note("NOTE1"), _note("NOTE2")],
        page_size=2,
        max_pages=2,
        max_children=1,
    )

    with pytest.raises(module.ZoteroReadError) as exc_info:
        provider.get_children("PARENT1")

    assert exc_info.value.code == "zotero_children_member_limit_exceeded"
    assert exc_info.value.data == {"member_count": 2, "max_children": 1}
