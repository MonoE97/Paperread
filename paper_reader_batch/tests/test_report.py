from paper_reader_batch.report import build_report, render_markdown_report


def _manifest() -> dict:
    return {
        "schema_version": "paper_reader_batch.manifest.v1",
        "created_at": "2026-07-02T10:00:00+08:00",
        "batch_title": "report batch",
        "default_concurrency": 3,
        "write_policy": "zotero_write",
        "source_summary": {"source_type": "mixed", "description": "test inputs"},
        "items": [
            {
                "item_id": "001",
                "input_type": "zotero_title",
                "input": {"title": "Zotero paper"},
                "expected_output": "zotero_note_candidate",
            },
            {
                "item_id": "002",
                "input_type": "pdf_path",
                "input": {"path": "/local/paper.pdf"},
                "expected_output": "local_note",
            },
            {
                "item_id": "003",
                "input_type": "zotero_title",
                "input": {"title": "Failed paper"},
                "expected_output": "zotero_note_candidate",
            },
        ],
    }


def _state() -> dict:
    return {
        "schema_version": "paper_reader_batch.state.v1",
        "batch_title": "report batch",
        "batch_status": "completed_with_failures",
        "created_at": "2026-07-02T10:00:00+08:00",
        "updated_at": "2026-07-02T10:30:00+08:00",
        "items": [
            {
                "item_id": "001",
                "input_type": "zotero_title",
                "expected_output": "zotero_note_candidate",
                "status": "succeeded",
                "thirty_second_takeaway": "单篇 note 中的结论。",
                "note_md": "/local/paper_reader/runs/paper/note.md",
                "note_html": "/local/paper_reader/runs/paper/note.html",
                "gate_report": "/local/paper_reader/runs/paper/gate-report.json",
                "write_payload": "/local/paper_reader/runs/paper/write-payload.json",
                "write_status": "written",
                "zotero_note_key": "NOTE1",
                "zotero_parent_key": "PARENT1",
                "verify_report": "/local/paper_reader/runs/paper/verify-report.json",
                "content_sha256": "abc",
                "write_completed_at": "2026-07-02T10:20:00+08:00",
                "takeaway_source_type": "rendered_note_30_second_row",
                "takeaway_source_path": "/local/paper_reader/runs/paper/note.md",
                "takeaway_source_sha256": "abc",
                "failure_reason": "",
            },
            {
                "item_id": "002",
                "input_type": "pdf_path",
                "expected_output": "local_note",
                "status": "succeeded",
                "thirty_second_takeaway": "PDF note 中的结论。",
                "local_note_path": "/local/paper_note.md",
                "takeaway_source_type": "rendered_note_30_second_row",
                "takeaway_source_path": "/local/paper_analysis/note.md",
                "takeaway_source_sha256": "def",
                "failure_reason": "",
            },
            {
                "item_id": "003",
                "input_type": "zotero_title",
                "expected_output": "zotero_note_candidate",
                "status": "failed",
                "thirty_second_takeaway": "",
                "failure_reason": "duplicate Zotero title",
            },
        ],
    }


def test_build_report_counts_statuses_and_outputs() -> None:
    report = build_report(_manifest(), _state(), reported_at="2026-07-02T10:31:00+08:00")

    assert report["batch_title"] == "report batch"
    assert report["counts_by_status"] == {"failed": 1, "succeeded": 2}
    assert report["counts_by_expected_output"] == {"local_note": 1, "zotero_note_candidate": 2}
    assert report["items"][0]["write_status"] == "written"
    assert report["items"][0]["zotero_note_key"] == "NOTE1"
    assert report["items"][1]["write_status"] == "not_applicable"
    assert report["items"][2]["write_status"] == "failed"


def test_build_report_preserves_pending_prepare_write_status() -> None:
    state = _state()
    state["batch_status"] = "running"
    state["items"][0] = {
        "item_id": "001",
        "input_type": "zotero_title",
        "expected_output": "zotero_note_candidate",
        "status": "pending",
        "write_status": "pending_prepare",
        "thirty_second_takeaway": "",
        "failure_reason": "",
    }

    report = build_report(_manifest(), state, reported_at="2026-07-02T10:31:00+08:00")

    assert report["items"][0]["write_status"] == "pending_prepare"


def test_markdown_report_is_deterministic_and_uses_existing_takeaways() -> None:
    report = build_report(_manifest(), _state(), reported_at="2026-07-02T10:31:00+08:00")

    markdown = render_markdown_report(report)

    assert "# paper_reader_batch Report: report batch" in markdown
    assert "单篇 note 中的结论。" in markdown
    assert "PDF note 中的结论。" in markdown
    assert "duplicate Zotero title" in markdown
    assert "written" in markdown
    assert "NOTE1" in markdown
    assert "local-only path" in markdown
    assert "重新总结" not in markdown
