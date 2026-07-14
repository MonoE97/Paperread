from __future__ import annotations

from dataclasses import replace
import json
from html import escape
from pathlib import Path
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from paper_reader_batch.v2_contracts import (
    LOCAL_PREPARE_RESULT_SCHEMA_VERSION,
    RECONCILIATION_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    WORKER_RESULT_SCHEMA_VERSION,
    WRITE_RESULT_SCHEMA_VERSION,
    ArtifactRef,
    BatchReport,
    LocalPrepareResult,
    PdfManifestItem,
    ReconciliationResult,
    ReportItem,
    StateItem,
    WorkerResult,
    WriteResult,
)
from paper_reader_batch.v2_errors import BatchRuntimeError
from paper_reader_batch.v2_json import (
    MAX_JSON_ARTIFACT_BYTES,
    MAX_OPAQUE_ARTIFACT_BYTES,
    active_transition_targets,
    canonical_json_bytes,
    canonical_sha256,
    completed_transition_matches,
    entry_exists,
    held_exact_sibling_files,
    locked_file,
    normalized_absolute_path,
    publish_bytes_no_replace,
    read_bytes,
    read_committed_transitions,
    read_active_transition_owner,
    read_locked_bytes,
    read_pending_swap,
    replace_bytes_atomic,
    sha256_bytes,
    validate_locked_path,
)
from paper_reader_batch.v2_reducer import apply_event, initial_state


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_THIRTY_SECOND_HEADING = re.compile(r"^#{1,6}\s+30\s*秒结论\s*#*\s*$")
_CONCLUSION_SECTION_HEADING = re.compile(r"^##\s+0\.\s*阅读结论\s*#*\s*$")
_SECOND_LEVEL_HEADING = re.compile(r"^##(?:\s|$)")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_ZERO_SHA256 = "0" * 64
_REPORT_REPLACE_TARGETS = frozenset({"state.json", "batch-report.json", "batch-report.md"})


def _invalid(message: str, cause: Exception | None = None) -> BatchRuntimeError:
    error = BatchRuntimeError("report_source_invalid", message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _ref_read_limit(size_bytes: object, *, json_artifact: bool) -> int:
    if type(size_bytes) is not int or size_bytes < 0:
        raise _invalid("artifact reference must declare non-negative integer size_bytes")
    limit = MAX_JSON_ARTIFACT_BYTES if json_artifact else MAX_OPAQUE_ARTIFACT_BYTES
    return min(size_bytes, limit)


def _read_outer_model(
    ref: ArtifactRef,
    model_type: type[_ModelT] | None = None,
    *,
    schema_version: str,
    basename: str,
    id_field: str,
) -> tuple[Path, bytes, _ModelT | dict[str, object]]:
    if ref.schema_version != schema_version:
        raise _invalid(f"artifact ref schema must be exactly {schema_version}")
    path = normalized_absolute_path(Path(ref.path))
    if path.name != basename:
        raise _invalid(f"artifact ref must target {basename}")
    raw = read_bytes(
        path,
        code="report_source_invalid",
        max_bytes=_ref_read_limit(ref.size_bytes, json_artifact=True),
    )
    if len(raw) != ref.size_bytes or sha256_bytes(raw) != ref.sha256:
        raise _invalid(f"artifact ref bytes differ from bound size/hash: {path}")
    try:
        payload = json.loads(
            raw,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise _invalid(f"artifact ref is not strict JSON: {path}", exc)
    if not isinstance(payload, dict) or payload.get("schema_version") != schema_version:
        raise _invalid(f"artifact envelope schema must be exactly {schema_version}: {path}")
    if model_type is None:
        model: _ModelT | dict[str, object] = payload
        payload_for_hash = payload
        artifact_id = payload.get(id_field)
    else:
        try:
            model = model_type.model_validate_json(raw)
        except ValidationError as exc:
            raise _invalid(f"artifact envelope fails strict validation: {path}", exc)
        payload_for_hash = model.model_dump(mode="json")
        artifact_id = getattr(model, id_field)
    if raw != canonical_json_bytes(payload_for_hash) or artifact_id != ref.artifact_id:
        raise _invalid(f"artifact envelope is noncanonical or has a mismatched id: {path}")
    return path, raw, model


def _read_candidate_inner(
    run_dir: Path,
    candidate_dir: Path,
    candidate: dict[str, object],
    *,
    role: str,
    basename: str,
    media_type: str,
) -> tuple[Path, bytes, dict[str, object]]:
    artifacts = candidate.get("artifacts")
    if not isinstance(artifacts, list):
        raise _invalid("candidate artifacts must be a JSON array")
    matches = [ref for ref in artifacts if isinstance(ref, dict) and ref.get("role") == role]
    if len(matches) != 1:
        raise _invalid(f"candidate must bind exactly one {role} artifact")
    ref = matches[0]
    expected_path = candidate_dir / basename
    expected_relative = expected_path.relative_to(run_dir).as_posix()
    if ref.get("path") != expected_relative or ref.get("media_type") != media_type:
        raise _invalid(f"candidate {role} ref does not use its fixed path/media type")
    raw = read_bytes(
        expected_path,
        code="report_source_invalid",
        max_bytes=_ref_read_limit(
            ref.get("size_bytes"),
            json_artifact=(media_type == "application/json"),
        ),
    )
    if len(raw) != ref.get("size_bytes") or sha256_bytes(raw) != ref.get("sha256"):
        raise _invalid(f"candidate {role} bytes differ from the immutable ref")
    return expected_path, raw, ref


def _outside_fence_lines(markdown: str) -> list[tuple[int, str]]:
    visible: list[tuple[int, str]] = []
    fence_marker: str | None = None
    fence_length = 0
    for index, line in enumerate(markdown.splitlines()):
        matched = _FENCE.match(line)
        if matched is not None:
            marker = matched.group(1)
            if fence_marker is None:
                fence_marker = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_marker and len(marker) >= fence_length:
                fence_marker = None
                fence_length = 0
            continue
        if fence_marker is None:
            visible.append((index, line))
    return visible


def _split_markdown_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    cells: list[str] = []
    buffer: list[str] = []
    inner = stripped[1:-1]
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == "\\" and index + 1 < len(inner) and inner[index + 1] in {"|", "\\"}:
            buffer.append(inner[index + 1])
            index += 2
            continue
        if char == "|":
            cells.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
        index += 1
    cells.append("".join(buffer).strip())
    return cells


def _extract_markdown_takeaway(markdown: str) -> tuple[str, str] | None:
    lines = markdown.splitlines()
    visible = _outside_fence_lines(markdown)
    canonical_headings = [
        index
        for index, line in visible
        if _CONCLUSION_SECTION_HEADING.fullmatch(line.strip())
    ]
    if len(canonical_headings) > 1:
        raise _invalid("rendered note contains more than one canonical 0. 阅读结论 section")
    if canonical_headings:
        start = canonical_headings[0]
        end = len(lines)
        for index, line in visible:
            if index > start and _SECOND_LEVEL_HEADING.match(line.strip()):
                end = index
                break
        rows: list[str] = []
        visible_by_index = {index: line for index, line in visible}
        for index in range(start + 1, end):
            line = visible_by_index.get(index)
            if line is None:
                continue
            cells = _split_markdown_table_row(line)
            if (
                cells is not None
                and len(cells) >= 2
                and cells[0] == "30 秒结论"
                and cells[1]
            ):
                rows.append(cells[1])
        if len(rows) > 1:
            raise _invalid("canonical 0. 阅读结论 table contains duplicate 30 秒结论 rows")
        if rows:
            return rows[0], "rendered_note_30_second_row"
        return None

    section_headings = [
        index for index, line in visible if _THIRTY_SECOND_HEADING.fullmatch(line.strip())
    ]
    if len(section_headings) > 1:
        raise _invalid("rendered note contains more than one 30 秒结论 section")
    if section_headings:
        start = section_headings[0]
        visible_by_index = {index: line for index, line in visible}
        block: list[str] = []
        for index in range(start + 1, len(lines)):
            line = visible_by_index.get(index)
            if line is None:
                continue
            stripped = line.strip()
            if not stripped:
                if block:
                    break
                continue
            if stripped.startswith("#"):
                break
            block.append(stripped)
        if block:
            return " ".join(block), "rendered_note_30_second_section"
    return None


def _takeaway_from_candidate(candidate_ref: ArtifactRef) -> dict[str, str]:
    candidate_path, _candidate_raw, candidate = _read_outer_model(
        candidate_ref,
        schema_version="paper_reader.candidate.v2",
        basename="candidate.json",
        id_field="candidate_id",
    )
    candidate_dir = candidate_path.parent
    candidate_id = candidate.get("candidate_id")
    if (
        not isinstance(candidate_id, str)
        or candidate_dir.name != candidate_id
        or candidate_dir.parent.name != "candidates"
    ):
        raise _invalid("candidate ref is outside its fixed run/candidates/<candidate_id> directory")
    run_dir = candidate_dir.parent.parent
    note_path, note_raw, _note_ref = _read_candidate_inner(
        run_dir,
        candidate_dir,
        candidate,
        role="note_markdown",
        basename="note.md",
        media_type="text/markdown",
    )
    summary_path, summary_raw, _summary_ref = _read_candidate_inner(
        run_dir,
        candidate_dir,
        candidate,
        role="summary_snapshot",
        basename="summary.json",
        media_type="application/json",
    )
    try:
        markdown = note_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid("candidate note Markdown is not UTF-8", exc)
    extracted = _extract_markdown_takeaway(markdown)
    if extracted is not None:
        takeaway, source_type = extracted
        return {
            "thirty_second_takeaway": takeaway,
            "takeaway_source_type": source_type,
            "takeaway_source_path": str(note_path),
            "takeaway_source_sha256": sha256_bytes(note_raw),
        }

    try:
        summary = json.loads(
            summary_raw,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise _invalid("candidate summary fallback is invalid JSON", exc)
    if (
        not isinstance(summary, dict)
        or summary.get("schema_version") != "paper_reader.summary.v2"
        or summary_raw != canonical_json_bytes(summary)
    ):
        raise _invalid("candidate summary fallback is not canonical JSON")
    tldr_value = summary.get("tldr")
    one_sentence_value = summary.get("one_sentence_summary")
    if tldr_value is not None and not isinstance(tldr_value, str):
        raise _invalid("candidate summary tldr fallback must be a string or null")
    if not isinstance(one_sentence_value, str):
        raise _invalid("candidate summary one_sentence_summary fallback must be a string")
    tldr = tldr_value.strip() if isinstance(tldr_value, str) else ""
    if tldr:
        takeaway = tldr
        source_type = "structured_tldr_fallback"
    else:
        takeaway = one_sentence_value.strip()
        source_type = "structured_one_sentence_summary_fallback"
    if not takeaway:
        raise _invalid("candidate has no 30 秒结论, tldr, or one_sentence_summary")
    return {
        "thirty_second_takeaway": takeaway,
        "takeaway_source_type": source_type,
        "takeaway_source_path": str(summary_path),
        "takeaway_source_sha256": sha256_bytes(summary_raw),
    }


def _load_result(
    run_dir: Path,
    *,
    lane: str,
    digest: str,
    model_type: type[_ModelT],
    schema_version: str,
) -> _ModelT:
    path = run_dir / "results" / lane / f"{digest}.json"
    raw = read_bytes(
        path,
        code="journal_corrupt",
        max_bytes=MAX_JSON_ARTIFACT_BYTES,
    )
    if sha256_bytes(raw) != digest:
        raise BatchRuntimeError("journal_corrupt", f"{lane} result digest differs from state ref")
    try:
        payload = json.loads(
            raw,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise BatchRuntimeError("journal_corrupt", f"{lane} result is invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != schema_version:
        raise BatchRuntimeError(
            "unsupported_run_schema",
            f"{lane} result schema must be exactly {schema_version}",
        )
    try:
        result = model_type.model_validate(payload)
    except ValidationError as exc:
        raise BatchRuntimeError("journal_corrupt", f"{lane} result fails strict validation") from exc
    if raw != canonical_json_bytes(result):
        raise BatchRuntimeError("journal_corrupt", f"{lane} result is not canonical JSON")
    return result


def _validate_worker_binding(view: Any, item: StateItem) -> WorkerResult | None:
    if item.worker_result_sha256 is None:
        return None
    result = _load_result(
        view.run_dir,
        lane="worker",
        digest=item.worker_result_sha256,
        model_type=WorkerResult,
        schema_version=WORKER_RESULT_SCHEMA_VERSION,
    )
    if (
        result.manifest_sha256 != view.manifest_sha256
        or result.item_id != item.item_id
        or result.worker_id != item.worker_last_actor_id
        or result.claim_id != item.worker_last_claim_id
        or result.attempt_id != item.worker_last_attempt_id
        or result.attempt_number != item.worker_attempt_count
        or result.lease_token_sha256 != item.worker_last_lease_token_sha256
        or result.status != item.worker_status
        or (result.candidate.sha256 if result.candidate else None) != item.candidate_sha256
        or (result.error.code if result.error else None) != item.worker_failure_code
        or (result.error.message if result.error else None) != item.worker_failure_message
    ):
        raise BatchRuntimeError("journal_corrupt", f"worker result ref/state binding differs: {item.item_id}")
    return result


def _validate_local_prepare_binding(view: Any, item: StateItem) -> LocalPrepareResult | None:
    if item.local_prepare_result_sha256 is None:
        return None
    result = _load_result(
        view.run_dir,
        lane="local-prepare",
        digest=item.local_prepare_result_sha256,
        model_type=LocalPrepareResult,
        schema_version=LOCAL_PREPARE_RESULT_SCHEMA_VERSION,
    )
    if (
        result.manifest_sha256 != view.manifest_sha256
        or result.item_id != item.item_id
        or result.worker_id != item.local_prepare_last_actor_id
        or result.claim_id != item.local_prepare_last_claim_id
        or result.attempt_id != item.local_prepare_last_attempt_id
        or result.attempt_number != item.local_prepare_attempt_count
        or result.lease_token_sha256 != item.local_prepare_last_lease_token_sha256
        or result.status != item.local_prepare_status
        or (result.error.code if result.error else None) != item.local_prepare_failure_code
        or (result.error.message if result.error else None) != item.local_prepare_failure_message
    ):
        raise BatchRuntimeError(
            "journal_corrupt",
            f"local-prepare result ref/state binding differs: {item.item_id}",
        )
    return result


def _validate_write_binding(view: Any, item: StateItem) -> None:
    if item.write_result_sha256 is not None:
        result = _load_result(
            view.run_dir,
            lane="write",
            digest=item.write_result_sha256,
            model_type=WriteResult,
            schema_version=WRITE_RESULT_SCHEMA_VERSION,
        )
        if (
            result.manifest_sha256 != view.manifest_sha256
            or result.item_id != item.item_id
            or result.status != "written"
            or item.write_status != "written"
            or result.candidate_sha256 != item.candidate_sha256
            or result.authorization_sha256 != item.authorization_sha256
            or result.authorization_nonce_sha256 != item.authorization_nonce_sha256
            or result.external_claim_id != item.external_claim_id
            or result.started_event_sha256 != item.write_started_event_sha256
        ):
            raise BatchRuntimeError("journal_corrupt", f"write result ref/state binding differs: {item.item_id}")
    if item.reconciliation_sha256 is not None:
        result = _load_result(
            view.run_dir,
            lane="reconcile",
            digest=item.reconciliation_sha256,
            model_type=ReconciliationResult,
            schema_version=RECONCILIATION_SCHEMA_VERSION,
        )
        if (
            result.manifest_sha256 != view.manifest_sha256
            or result.item_id != item.item_id
            or result.candidate_sha256 != item.candidate_sha256
            or result.authorization_sha256 != item.authorization_sha256
        ):
            raise BatchRuntimeError(
                "journal_corrupt",
                f"reconciliation result ref/state binding differs: {item.item_id}",
            )


def _report_item_status(item: StateItem) -> str:
    if item.worker_status != "queued":
        return item.worker_status
    if item.local_prepare_status == "not_applicable":
        return "queued"
    return item.local_prepare_status


def _failure(item: StateItem) -> tuple[str, str]:
    for code, message in (
        (item.worker_failure_code, item.worker_failure_message),
        (item.local_prepare_failure_code, item.local_prepare_failure_message),
        (item.write_failure_code, item.write_failure_message),
    ):
        if code is not None or message is not None:
            return code or "", message or ""
    return "", ""


def _build_report(
    view: Any,
    *,
    generated_at: str,
    report_generation_id: str = _ZERO_SHA256,
    report_markdown_sha256: str = _ZERO_SHA256,
) -> BatchReport:
    report_items: list[ReportItem] = []
    for item in view.state.items:
        worker_result = _validate_worker_binding(view, item)
        _validate_local_prepare_binding(view, item)
        _validate_write_binding(view, item)
        takeaway: dict[str, str] = {
            "thirty_second_takeaway": "",
            "takeaway_source_type": "",
        }
        if worker_result is not None and worker_result.status == "succeeded":
            if worker_result.candidate is None:  # strict contract makes this defensive only
                raise BatchRuntimeError("journal_corrupt", "successful worker result lacks candidate ref")
            takeaway = _takeaway_from_candidate(worker_result.candidate)
        failure_code, failure_message = _failure(item)
        report_items.append(
            ReportItem(
                item_id=item.item_id,
                input_type=item.input_type,
                status=_report_item_status(item),
                write_status=item.write_status,
                thirty_second_takeaway=takeaway["thirty_second_takeaway"],
                takeaway_source_type=takeaway["takeaway_source_type"],
                takeaway_source_path=takeaway.get("takeaway_source_path"),
                takeaway_source_sha256=takeaway.get("takeaway_source_sha256"),
                failure_code=failure_code,
                failure_message=failure_message,
            )
        )
    effective_write_policy = (
        "local_only"
        if all(isinstance(item, PdfManifestItem) for item in view.manifest.items)
        else view.manifest.write_policy
    )
    try:
        return BatchReport(
            schema_version=REPORT_SCHEMA_VERSION,
            manifest_id=view.manifest.manifest_id,
            manifest_sha256=view.manifest_sha256,
            generated_at=generated_at,
            report_generation_id=report_generation_id,
            report_markdown_sha256=report_markdown_sha256,
            batch_status=view.state.batch_status,
            write_policy=view.manifest.write_policy,
            effective_write_policy=effective_write_policy,
            items=report_items,
        )
    except ValidationError as exc:
        raise BatchRuntimeError("report_invalid", "report fails the strict V2 contract") from exc


def _cell(value: object) -> str:
    collapsed = re.sub(r"\s+", " ", str(value or "")).strip()
    return escape(collapsed, quote=False).replace("|", "\\|")


def _heading_text(value: str) -> str:
    collapsed = escape(re.sub(r"\s+", " ", value).strip(), quote=False)
    for marker in ("\\", "`", "*", "_", "[", "]", "<", ">"):
        collapsed = collapsed.replace(marker, f"\\{marker}")
    return collapsed


def _render_markdown(report: BatchReport, *, batch_title: str) -> bytes:
    lines = [
        f"# paper_reader_batch Report: {_heading_text(batch_title)}",
        "",
        f"- Manifest id: {report.manifest_id}",
        f"- Manifest SHA-256: {report.manifest_sha256}",
        f"- Generated: {report.generated_at}",
        f"- Report generation: {report.report_generation_id}",
        f"- Batch status: {report.batch_status}",
        f"- Write policy: {report.write_policy}",
        f"- Effective write policy: {report.effective_write_policy}",
        "",
        "| Item | Input | Status | Write | 30 秒结论 | Failure | Source |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.items:
        failure = ": ".join(value for value in (item.failure_code, item.failure_message) if value)
        source = ""
        if item.takeaway_source_path is not None:
            source = (
                f"{item.takeaway_source_type}: {item.takeaway_source_path}"
                f" sha256={item.takeaway_source_sha256}"
            )
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    item.item_id,
                    item.input_type,
                    item.status,
                    item.write_status,
                    item.thirty_second_takeaway,
                    failure,
                    source,
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            (
                "Note: Local PDF items are local-output only and never enter Zotero write-through."
                if report.effective_write_policy == "local_only"
                else "Note: This is an operational report built from bound single-paper results."
            ),
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _replace_or_publish(path: Path, content: bytes, *, transition_id: str) -> None:
    max_bytes = (
        MAX_JSON_ARTIFACT_BYTES
        if path.suffix.casefold() == ".json"
        else MAX_OPAQUE_ARTIFACT_BYTES
    )
    if len(content) > max_bytes:
        raise BatchRuntimeError(
            "report_output_invalid",
            f"generated report exceeds its {max_bytes}-byte output limit: {path}",
        )
    if entry_exists(path):
        current = read_bytes(
            path,
            code="report_output_invalid",
            max_bytes=max_bytes,
        )
        if current == content:
            return
        replace_bytes_atomic(
            path,
            content,
            expected_current=current,
            transition_id=transition_id,
            allowed_transition_targets=_REPORT_REPLACE_TARGETS,
        )
    else:
        publish_bytes_no_replace(path, content)


def _build_report_artifacts(view, *, generated_at: str) -> tuple[BatchReport, bytes, bytes]:
    report = _build_report(view, generated_at=generated_at)
    report_core = report.model_dump(
        mode="json",
        exclude={"report_generation_id", "report_markdown_sha256"},
    )
    generation_id = canonical_sha256(
        {
            "report": report_core,
            "latest_event_sha256": view.state.latest_event_sha256,
        }
    )
    report = report.model_copy(update={"report_generation_id": generation_id})
    markdown_bytes = _render_markdown(report, batch_title=view.manifest.batch_title)
    report = BatchReport.model_validate(
        report.model_copy(
            update={"report_markdown_sha256": sha256_bytes(markdown_bytes)}
        ).model_dump(mode="json")
    )
    return report, canonical_json_bytes(report), markdown_bytes


def _recover_report_swaps(view) -> None:
    json_path = view.run_dir / "batch-report.json"
    markdown_path = view.run_dir / "batch-report.md"
    prefix_views = []
    prefix_state = initial_state(view.manifest, view.events[0])
    prefix_events = [view.events[0]]
    prefix_views.append(replace(view, state=prefix_state, events=list(prefix_events)))
    for event in view.events[1:]:
        prefix_state = apply_event(prefix_state, view.manifest, event)
        prefix_events.append(event)
        prefix_views.append(replace(view, state=prefix_state, events=list(prefix_events)))

    active = active_transition_targets(
        view.run_dir,
        replace_targets=_REPORT_REPLACE_TARGETS,
    )
    for path, limit in (
        (markdown_path, MAX_OPAQUE_ARTIFACT_BYTES),
        (json_path, MAX_JSON_ARTIFACT_BYTES),
    ):
        if path.name not in active:
            continue
        owner_raw = read_active_transition_owner(
            path,
            replace_targets=_REPORT_REPLACE_TARGETS,
        )
        if owner_raw is None:
            raise BatchRuntimeError("unsafe_storage", "report transition payload has no active owner")
        owner = json.loads(owner_raw)
        pending = read_pending_swap(
            path,
            max_bytes=limit,
            replace_targets=_REPORT_REPLACE_TARGETS,
        )
        committed = read_committed_transitions(
            path,
            max_bytes=limit,
            replace_targets=_REPORT_REPLACE_TARGETS,
        )
        if pending is not None:
            previous_raw, desired_raw = pending
        elif committed:
            desired_raw, previous_raw, _transition_name = committed[0]
        else:
            previous_raw = read_bytes(path, code="report_output_invalid", max_bytes=limit)
            candidates: list[bytes] = []
            for prefix_view in prefix_views:
                _model, expected_json, expected_markdown = _build_report_artifacts(
                    prefix_view,
                    generated_at=prefix_view.events[-1].occurred_at,
                )
                candidates.append(expected_json if path.suffix == ".json" else expected_markdown)
            desired_matches = [
                raw
                for raw in candidates
                if sha256_bytes(raw) == owner["new_sha256"] and len(raw) == owner["new_size"]
            ]
            if len(desired_matches) != 1:
                raise BatchRuntimeError(
                    "storage_recovery_required",
                    "owner-only report transition cannot be reconstructed from durable journal time",
                )
            desired_raw = desired_matches[0]
        if (
            sha256_bytes(previous_raw) != owner["old_sha256"]
            or len(previous_raw) != owner["old_size"]
            or sha256_bytes(desired_raw) != owner["new_sha256"]
            or len(desired_raw) != owner["new_size"]
        ):
            raise BatchRuntimeError("unsafe_storage", "active report transition differs from its owner mapping")
        replace_bytes_atomic(
            path,
            desired_raw,
            expected_current=previous_raw,
            transition_id=owner["transition_id"],
            allowed_transition_targets=_REPORT_REPLACE_TARGETS,
        )

    json_exists = entry_exists(json_path)
    markdown_exists = entry_exists(markdown_path)
    if not json_exists and not markdown_exists:
        return
    if json_exists and not markdown_exists:
        raise BatchRuntimeError(
            "unsafe_storage",
            "report JSON commit marker exists without its Markdown payload",
        )

    json_public = (
        read_bytes(json_path, code="report_output_invalid", max_bytes=MAX_JSON_ARTIFACT_BYTES)
        if json_exists
        else None
    )
    markdown_public = read_bytes(
        markdown_path,
        code="report_output_invalid",
        max_bytes=MAX_OPAQUE_ARTIFACT_BYTES,
    )
    json_pending = (
        read_pending_swap(
            json_path,
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
            replace_targets=_REPORT_REPLACE_TARGETS,
        )
        if json_exists
        else None
    )
    markdown_pending = read_pending_swap(
        markdown_path,
        max_bytes=MAX_OPAQUE_ARTIFACT_BYTES,
        replace_targets=_REPORT_REPLACE_TARGETS,
    )
    json_variants = [raw for raw in (json_public, json_pending[1] if json_pending else None) if raw is not None]
    markdown_variants = [markdown_public]
    if markdown_pending is not None and markdown_pending[1] not in markdown_variants:
        markdown_variants.append(markdown_pending[1])

    valid_json: dict[bytes, tuple[bytes, BatchReport]] = {}
    for json_raw in json_variants:
        try:
            report = BatchReport.model_validate_json(json_raw)
        except ValidationError:
            continue
        if canonical_json_bytes(report) != json_raw:
            continue
        for prefix_view in reversed(prefix_views):
            try:
                _model, expected_json, expected_markdown = _build_report_artifacts(
                    prefix_view,
                    generated_at=report.generated_at,
                )
            except BatchRuntimeError:
                continue
            if expected_json == json_raw:
                valid_json[json_raw] = (expected_markdown, report)
                break

    valid_markdown: dict[bytes, tuple[bytes, BatchReport]] = {}
    generated_line = re.compile(r"^- Generated: (?P<generated>[^\r\n]+)$", re.MULTILINE)
    for markdown_raw in markdown_variants:
        try:
            markdown_text = markdown_raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        match = generated_line.search(markdown_text)
        if match is None:
            continue
        for prefix_view in reversed(prefix_views):
            try:
                expected_model, expected_json, expected_markdown = _build_report_artifacts(
                    prefix_view,
                    generated_at=match.group("generated"),
                )
            except BatchRuntimeError:
                continue
            if markdown_raw == expected_markdown:
                valid_markdown[markdown_raw] = (expected_json, expected_model)
                break
    if set(json_variants) - set(valid_json) or set(markdown_variants) - set(valid_markdown):
        raise BatchRuntimeError(
            "unsafe_storage",
            "report transition contains bytes not bound to a current-journal report",
        )

    desired_json: bytes
    desired_markdown: bytes
    desired_report: BatchReport
    if json_pending is not None:
        desired_json = json_pending[1]
        desired_markdown, desired_report = valid_json[desired_json]
        if markdown_pending is not None and markdown_pending[1] != desired_markdown:
            raise BatchRuntimeError("unsafe_storage", "report transitions target different generations")
        if markdown_public != desired_markdown and markdown_pending is None:
            raise BatchRuntimeError("unsafe_storage", "JSON transition started before Markdown became durable")
    elif markdown_pending is not None:
        desired_markdown = markdown_pending[1]
        desired_json, desired_report = valid_markdown[desired_markdown]
    else:
        assert markdown_public is not None
        desired_json, desired_report = valid_markdown[markdown_public]
        desired_markdown = markdown_public
        if json_public == desired_json:
            return
        if json_public is not None:
            previous_markdown, _previous_report = valid_json[json_public]
            if not completed_transition_matches(
                markdown_path,
                transition_id=f"report:{desired_report.report_generation_id}:markdown",
                previous_data=previous_markdown,
                data=markdown_public,
                replace_targets=_REPORT_REPLACE_TARGETS,
            ):
                raise BatchRuntimeError(
                    "unsafe_storage",
                    "mismatched report pair has no completed Markdown transition provenance",
                )

    generation_id = desired_report.report_generation_id
    _replace_or_publish(
        markdown_path,
        desired_markdown,
        transition_id=f"report:{generation_id}:markdown",
    )
    _replace_or_publish(
        json_path,
        desired_json,
        transition_id=f"report:{generation_id}:json",
    )
    if (
        read_bytes(json_path, code="report_output_invalid", max_bytes=MAX_JSON_ARTIFACT_BYTES)
        != desired_json
        or read_bytes(markdown_path, code="report_output_invalid", max_bytes=MAX_OPAQUE_ARTIFACT_BYTES)
        != desired_markdown
    ):
        raise BatchRuntimeError("storage_path_changed", "recovered report pair changed after publication")


def run_report(run_dir: Path) -> dict[str, Any]:
    """Replay V2 truth and publish Markdown before its JSON commit marker."""

    from paper_reader_batch.v2_journal import load_run_view

    preflight = load_run_view(run_dir, ignore_report_swaps=True)
    lock_path = preflight.run_dir / ".run.lock"
    inherited_descriptors: list[int] = []
    with locked_file(
        lock_path,
        create=False,
        inherited_lock_descriptors=inherited_descriptors,
    ) as descriptor:
        lease_secret = read_locked_bytes(descriptor)
        validate_locked_path(lock_path, descriptor)
        view = load_run_view(
            preflight.run_dir,
            held_lease_secret=lease_secret,
            lock_descriptor=descriptor,
            lock_ancestor_descriptors=tuple(inherited_descriptors),
            ignore_report_swaps=True,
        )
        _recover_report_swaps(view)
        if (
            view.pending_event is not None
            or view.incomplete_event_writes
            or view.state_pending_write is not None
            or view.incomplete_state_writes
        ):
            raise BatchRuntimeError(
                "recovery_required",
                "report generation requires a fully committed journal; run recover first",
            )
        report, report_bytes, markdown_bytes = _build_report_artifacts(
            view,
            # Report identity is derived only from replayed journal truth. The
            # durable event time lets owner-only recovery reconstruct exact
            # bytes without a second reservation protocol.
            generated_at=view.events[-1].occurred_at,
        )
        json_path = view.run_dir / "batch-report.json"
        markdown_path = view.run_dir / "batch-report.md"
        previous_markdown = (
            read_bytes(
                markdown_path,
                code="report_output_invalid",
                max_bytes=MAX_OPAQUE_ARTIFACT_BYTES,
            )
            if entry_exists(markdown_path)
            else None
        )
        previous_json = (
            read_bytes(
                json_path,
                code="report_output_invalid",
                max_bytes=MAX_JSON_ARTIFACT_BYTES,
            )
            if entry_exists(json_path)
            else None
        )
        validate_locked_path(lock_path, descriptor)
        _replace_or_publish(
            markdown_path,
            markdown_bytes,
            transition_id=f"report:{report.report_generation_id}:markdown",
        )
        with held_exact_sibling_files(
            view.run_dir,
            {markdown_path.name: markdown_bytes},
        ) as markdown_guard:
            validate_locked_path(lock_path, descriptor)
            markdown_guard()
            _replace_or_publish(
                json_path,
                report_bytes,
                transition_id=f"report:{report.report_generation_id}:json",
            )
            validate_locked_path(lock_path, descriptor)
            markdown_guard()
            with held_exact_sibling_files(
                view.run_dir,
                {
                    markdown_path.name: markdown_bytes,
                    json_path.name: report_bytes,
                },
            ) as pair_guard:
                pair_guard()
                markdown_guard()
                if (
                    previous_markdown is not None
                    and previous_markdown != markdown_bytes
                    and not completed_transition_matches(
                        markdown_path,
                        transition_id=(
                            f"report:{report.report_generation_id}:markdown"
                        ),
                        previous_data=previous_markdown,
                        data=markdown_bytes,
                        replace_targets=_REPORT_REPLACE_TARGETS,
                    )
                ):
                    raise BatchRuntimeError(
                        "storage_path_changed",
                        "report Markdown lacks current transition provenance",
                    )
                if (
                    previous_json is not None
                    and previous_json != report_bytes
                    and not completed_transition_matches(
                        json_path,
                        transition_id=f"report:{report.report_generation_id}:json",
                        previous_data=previous_json,
                        data=report_bytes,
                        replace_targets=_REPORT_REPLACE_TARGETS,
                    )
                ):
                    raise BatchRuntimeError(
                        "storage_path_changed",
                        "report JSON lacks current transition provenance",
                    )
                validate_locked_path(lock_path, descriptor)
                pair_guard()
                markdown_guard()
                validate_locked_path(lock_path, descriptor)

    return {
        "run_dir": str(preflight.run_dir),
        "report_json": str(json_path),
        "report_markdown": str(markdown_path),
        "report_sha256": sha256_bytes(report_bytes),
        "report_markdown_sha256": sha256_bytes(markdown_bytes),
        "report_generation_id": report.report_generation_id,
        "batch_status": report.batch_status,
        "effective_write_policy": report.effective_write_policy,
    }


__all__ = ["run_report"]
