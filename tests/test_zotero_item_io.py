from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotero_paperread.zotero_item_io import (
    normalize_item_details_payload,
    write_item_details_files,
)


def test_normalize_item_details_accepts_plain_item_object() -> None:
    payload = {
        "key": "ABC123",
        "title": "Example Paper",
        "attachments": [{"key": "PDF1", "contentType": "application/pdf", "path": "/tmp/a.pdf"}],
        "notes": [],
    }

    assert normalize_item_details_payload(payload)["key"] == "ABC123"


def test_normalize_item_details_accepts_mcp_text_response() -> None:
    item = {"key": "ABC123", "title": "Example Paper", "attachments": [], "notes": []}
    payload = [{"type": "text", "text": json.dumps(item)}]

    assert normalize_item_details_payload(payload)["title"] == "Example Paper"


def test_normalize_item_details_rejects_missing_key() -> None:
    with pytest.raises(ValueError, match="item details missing key"):
        normalize_item_details_payload({"title": "No Key"})


def test_write_item_details_files_writes_raw_and_normalized(tmp_path: Path) -> None:
    item = {"key": "ABC123", "title": "Example Paper", "attachments": [], "notes": []}
    raw_path = tmp_path / "item-details.raw.json"
    normalized_path = tmp_path / "item-details.json"

    result = write_item_details_files(item, normalized_path=normalized_path, raw_path=raw_path)

    assert result["item_key"] == "ABC123"
    assert normalized_path.exists()
    assert raw_path.exists()
    assert json.loads(normalized_path.read_text(encoding="utf-8"))["key"] == "ABC123"
