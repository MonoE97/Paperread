from __future__ import annotations

import sqlite3
from pathlib import Path

from paper_reader.zotero_sqlite import lookup_extra_by_item_key


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


def test_lookup_extra_retries_read_only_before_immutable(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "zotero.sqlite"
    make_zotero_db(db_path)
    calls: list[bool] = []

    def fake_lookup(item_key: str, sqlite_path: Path, *, immutable: bool) -> tuple[str, bool]:
        calls.append(immutable)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return "https://mp.weixin.qq.com/s/retry-success", True

    monkeypatch.setattr("paper_reader.zotero_sqlite._lookup_with_mode", fake_lookup)

    result = lookup_extra_by_item_key(
        "ABC123",
        sqlite_path=db_path,
        ro_retries=1,
        retry_sleep_seconds=0,
    )

    assert result["extra"] == "https://mp.weixin.qq.com/s/retry-success"
    assert result["warnings"] == []
    assert result["provenance"]["sqlite_mode"] == "ro"
    assert result["provenance"]["diagnostics"] == ["sqlite_ro_retry_after_locked"]
    assert calls == [False, False]


def test_lookup_extra_records_successful_immutable_as_diagnostic(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "zotero.sqlite"
    make_zotero_db(db_path)
    calls: list[bool] = []

    def fake_lookup(item_key: str, sqlite_path: Path, *, immutable: bool) -> tuple[str, bool]:
        calls.append(immutable)
        if not immutable:
            raise sqlite3.OperationalError("database is locked")
        return "https://mp.weixin.qq.com/s/immutable-success", True

    monkeypatch.setattr("paper_reader.zotero_sqlite._lookup_with_mode", fake_lookup)

    result = lookup_extra_by_item_key(
        "ABC123",
        sqlite_path=db_path,
        ro_retries=0,
        retry_sleep_seconds=0,
    )

    assert result["extra"] == "https://mp.weixin.qq.com/s/immutable-success"
    assert result["warnings"] == []
    assert result["provenance"]["sqlite_mode"] == "immutable"
    assert result["provenance"]["diagnostics"] == ["sqlite_immutable_snapshot_used"]
    assert calls == [False, True]


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
