from __future__ import annotations

import importlib
import importlib.util


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
