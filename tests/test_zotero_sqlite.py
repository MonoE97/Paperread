from __future__ import annotations

import sqlite3
from pathlib import Path

from zotero_paperread.zotero_sqlite import lookup_extra_by_item_key


def make_zotero_db(
    path: Path,
    *,
    key: str = "ABC123",
    extra: str = "https://mp.weixin.qq.com/s/example",
) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
        CREATE TABLE fieldsCombined (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        """
    )
    conn.execute("INSERT INTO items (itemID, key, itemTypeID) VALUES (10, ?, 22)", (key,))
    conn.execute("INSERT INTO fieldsCombined (fieldID, fieldName) VALUES (1, 'extra')")
    conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (1, ?)", (extra,))
    conn.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (10, 1, 1)")
    conn.commit()
    conn.close()


def test_lookup_extra_by_item_key_reads_extra_without_writing(tmp_path: Path) -> None:
    db_path = tmp_path / "zotero.sqlite"
    make_zotero_db(db_path)

    result = lookup_extra_by_item_key("ABC123", sqlite_path=db_path)

    assert result["extra"] == "https://mp.weixin.qq.com/s/example"
    assert result["provenance"]["source"] == "zotero_sqlite"
    assert result["provenance"]["sqlite_mode"] == "ro"
    assert result["warnings"] == []


def test_lookup_extra_by_item_key_soft_fails_when_db_missing(tmp_path: Path) -> None:
    result = lookup_extra_by_item_key("ABC123", sqlite_path=tmp_path / "missing.sqlite")

    assert result["extra"] == ""
    assert result["warnings"] == ["sqlite_extra_unavailable"]


def test_lookup_extra_by_item_key_soft_fails_when_item_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "zotero.sqlite"
    make_zotero_db(db_path, key="OTHER1")

    result = lookup_extra_by_item_key("ABC123", sqlite_path=db_path)

    assert result["extra"] == ""
    assert result["warnings"] == ["sqlite_extra_item_not_found"]
