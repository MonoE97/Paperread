from __future__ import annotations

import hashlib


def canonicalize_note_html_for_hash(content: str) -> str:
    """Match Zotero readback's terminal-newline normalization for hash checks."""
    return content.rstrip("\r\n")


def note_html_sha256(content: str) -> str:
    return hashlib.sha256(canonicalize_note_html_for_hash(content).encode("utf-8")).hexdigest()
