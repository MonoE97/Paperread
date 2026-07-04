from __future__ import annotations

import json
from typing import Any


def render_worker_prompt(*, batch_run: str, assignment: dict[str, Any]) -> str:
    item_id = assignment["item_id"]
    input_type = assignment["input_type"]
    expected_output = assignment["expected_output"]
    lines = [
        f"# Paperread Batch Worker: {item_id}",
        "",
        f"batch_run: {batch_run}",
        f"item_id: {item_id}",
        f"worker_id: {assignment['worker_id']}",
        f"attempt_count: {assignment['attempt_count']}",
        f"input_type: {input_type}",
        f"expected_output: {expected_output}",
        "",
        "## Assignment JSON",
        "",
        "```json",
        json.dumps(assignment, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Required Result",
        "",
        "Write one JSON result using schema_version `paperread-batch.item-result.v1`.",
    ]
    if input_type == "pdf_path":
        prepared_analysis_dir = str(assignment.get("prepared_analysis_dir", "")).strip()
        prepared_final_note_path = str(assignment.get("prepared_final_note_path", "")).strip()
        lines.extend(
            [
                "",
                "## Local PDF Rules",
                "",
                "This item is local-output only. Do not search Zotero, do not check Zotero duplicates, do not call refresh-live-notes, do not create write-payload.json, and do not write Zotero.",
            ]
        )
        if assignment.get("local_prepare_status") == "prepared" and prepared_analysis_dir:
            lines.extend(
                [
                    "",
                    "## Prepared Bundle",
                    "",
                    f"prepared_analysis_dir: {prepared_analysis_dir}",
                    f"prepared_final_note_path: {prepared_final_note_path}",
                    "Continue from the prepared local PDF bundle: read context.md and section_context.md from prepared_analysis_dir. Use figure_context.md only if it is present. Write summary.json and review.json there; run validate-summary-json, apply-review, lint-summary, validate-trusted-summary, and prepare-local-note-candidate on that directory. Do not run prepare-pdf again unless the prepared bundle is missing or unreadable.",
                ]
            )
        else:
            lines.append("Run the `$paperread` local PDF workflow and return `local_note_path` plus `local_gate_report`.")
    else:
        lines.extend(
            [
                "",
                "## Zotero Rules",
                "",
                "Prepare a Zotero note candidate through `$paperread`. Stop on exact duplicate normalized titles. Do not call Zotero MCP write_note. Return `write_payload` only if the gate is write_ready.",
            ]
        )
    return "\n".join(lines) + "\n"
