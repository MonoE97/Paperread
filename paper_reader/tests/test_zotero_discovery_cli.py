from __future__ import annotations

import json


def test_discovery_cli_writes_only_bundle_json_to_stdout(capsys) -> None:
    from paper_reader.zotero_discovery_cli import main

    bundle = {
        "search_results": [{"key": "PARENT1", "title": "Exact Paper", "version": 7}],
        "selected_item": {"key": "PARENT1", "title": "Exact Paper", "version": 7},
    }

    exit_code = main(
        ["--title", "Exact Paper"],
        discover=lambda title, **kwargs: bundle,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == bundle
    assert captured.err == ""


def test_discovery_cli_reports_structured_error_on_stderr(capsys) -> None:
    from paper_reader.zotero_discovery import DiscoveryError
    from paper_reader.zotero_discovery_cli import main

    def fail(title: str, **kwargs):
        raise DiscoveryError("item_not_found", "no exact match")

    exit_code = main(["--title", "Missing Paper"], discover=fail)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "code": "item_not_found",
        "message": "no exact match",
        "ok": False,
    }
