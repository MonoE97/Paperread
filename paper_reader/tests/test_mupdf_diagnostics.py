from __future__ import annotations

from types import SimpleNamespace

import pytest

from paper_reader import mupdf_diagnostics
from paper_reader.mupdf_diagnostics import record_mupdf_diagnostics


class _FakeTools:
    def __init__(self, buffered: str) -> None:
        self.errors = 1
        self.warnings = 0
        self.buffered = buffered

    def mupdf_display_errors(self, on=None):
        if on is None:
            return self.errors
        self.errors = on
        return bool(on)

    def mupdf_display_warnings(self, on=None):
        if on is None:
            return self.warnings
        self.warnings = on
        return bool(on)

    def reset_mupdf_warnings(self) -> None:
        return None

    def mupdf_warnings(self, reset=1) -> str:
        value = self.buffered
        if reset:
            self.buffered = ""
        return value


def test_record_mupdf_diagnostics_deduplicates_and_restores_display(monkeypatch) -> None:
    tools = _FakeTools(
        "invalid marked content and clip nesting\n"
        "... repeated 17 times...\n"
        "format error: No common ancestor in structure tree\n"
        "structure tree broken, assume tree is missing\n"
        "format error: No common ancestor in structure tree\n"
    )
    monkeypatch.setattr(mupdf_diagnostics, "fitz", SimpleNamespace(TOOLS=tools))

    @record_mupdf_diagnostics
    def extract() -> dict[str, object]:
        assert tools.errors is False
        assert tools.warnings is False
        return {"warnings": ["existing_warning"]}

    assert extract() == {
        "warnings": [
            "existing_warning",
            "mupdf:invalid marked content and clip nesting",
            "mupdf:format error: No common ancestor in structure tree",
            "mupdf:structure tree broken, assume tree is missing",
        ]
    }
    assert tools.errors == 1
    assert tools.warnings == 0


def test_record_mupdf_diagnostics_restores_display_when_extraction_raises(monkeypatch) -> None:
    tools = _FakeTools("format error: broken structure tree")
    monkeypatch.setattr(mupdf_diagnostics, "fitz", SimpleNamespace(TOOLS=tools))

    @record_mupdf_diagnostics
    def extract() -> dict[str, object]:
        raise RuntimeError("extraction failed")

    with pytest.raises(RuntimeError, match="extraction failed"):
        extract()

    assert tools.errors == 1
    assert tools.warnings == 0


def test_record_mupdf_diagnostics_restores_first_toggle_when_second_toggle_fails(
    monkeypatch,
) -> None:
    class FailingTools(_FakeTools):
        def mupdf_display_warnings(self, on=None):
            if on is False:
                raise RuntimeError("cannot disable warnings")
            return super().mupdf_display_warnings(on)

    tools = FailingTools("")
    monkeypatch.setattr(mupdf_diagnostics, "fitz", SimpleNamespace(TOOLS=tools))

    @record_mupdf_diagnostics
    def extract() -> dict[str, object]:
        raise AssertionError("extractor must not run")

    with pytest.raises(RuntimeError, match="cannot disable warnings"):
        extract()

    assert tools.errors == 1
    assert tools.warnings == 0
