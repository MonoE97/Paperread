from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote


DEFAULT_ZOTERO_SQLITE_PATH = Path.home() / "Zotero" / "zotero.sqlite"


def _sqlite_uri(path: Path, *, immutable: bool = False) -> str:
    suffix = "&immutable=1" if immutable else ""
    return f"file:{quote(str(path.expanduser().resolve()))}?mode=ro{suffix}"


def _is_locked_error(error: sqlite3.Error) -> bool:
    return "locked" in str(error).lower()


def _item_exists(conn: sqlite3.Connection, item_key: str) -> bool:
    row = conn.execute("SELECT 1 FROM items WHERE key = ? LIMIT 1", (item_key,)).fetchone()
    return row is not None


def _query_extra(conn: sqlite3.Connection, item_key: str) -> str:
    row = conn.execute(
        """
        SELECT itemDataValues.value
        FROM items
        JOIN itemData ON itemData.itemID = items.itemID
        JOIN fieldsCombined ON fieldsCombined.fieldID = itemData.fieldID
        JOIN itemDataValues ON itemDataValues.valueID = itemData.valueID
        WHERE items.key = ? AND fieldsCombined.fieldName = 'extra'
        LIMIT 1
        """,
        (item_key,),
    ).fetchone()
    return str(row[0]).strip() if row and row[0] is not None else ""


def _lookup_with_mode(item_key: str, sqlite_path: Path, *, immutable: bool) -> tuple[str, bool]:
    conn = sqlite3.connect(_sqlite_uri(sqlite_path, immutable=immutable), uri=True)
    try:
        exists = _item_exists(conn, item_key)
        if not exists:
            return "", False
        return _query_extra(conn, item_key), True
    finally:
        conn.close()


def lookup_extra_by_item_key(
    item_key: str,
    *,
    sqlite_path: Path = DEFAULT_ZOTERO_SQLITE_PATH,
    allow_immutable: bool = True,
    ro_retries: int = 2,
    retry_sleep_seconds: float = 0.05,
) -> dict[str, Any]:
    """Read a Zotero item's Extra field from zotero.sqlite without mutating the DB."""
    key = str(item_key).strip()
    db_path = Path(sqlite_path).expanduser()
    if not key or not db_path.exists():
        return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}

    diagnostics: list[str] = []
    mode = "ro"
    attempt = 0
    while True:
        try:
            extra, exists = _lookup_with_mode(key, db_path, immutable=False)
            if attempt > 0:
                diagnostics.append("sqlite_ro_retry_after_locked")
            break
        except sqlite3.OperationalError as error:
            if not _is_locked_error(error):
                return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}
            if attempt < max(0, ro_retries):
                attempt += 1
                if retry_sleep_seconds > 0:
                    time.sleep(retry_sleep_seconds)
                continue
            if not allow_immutable:
                return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}
            mode = "immutable"
            diagnostics.append("sqlite_immutable_snapshot_used")
            try:
                extra, exists = _lookup_with_mode(key, db_path, immutable=True)
                break
            except sqlite3.Error:
                return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}
        except sqlite3.Error:
            return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}

    if not exists:
        return {"extra": "", "warnings": ["sqlite_extra_item_not_found"], "provenance": {}}

    return {
        "extra": extra,
        "warnings": [],
        "provenance": {
            "source": "zotero_sqlite",
            "item_key": key,
            "sqlite_path": str(db_path),
            "sqlite_mode": mode,
            "diagnostics": diagnostics,
        },
    }
