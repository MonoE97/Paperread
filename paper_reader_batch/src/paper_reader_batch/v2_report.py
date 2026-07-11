from __future__ import annotations

import json
from html import escape
from pathlib import Path
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from paper_reader_batch.v2_artifacts import (
    ForeignArtifactRef,
    ForeignCandidate,
    ForeignSummary,
)
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
    canonical_json_bytes,
    canonical_sha256,
    entry_exists,
    locked_file,
    normalized_absolute_path,
    publish_bytes_no_replace,
    read_bytes,
    read_locked_bytes,
    replace_bytes_atomic,
    sha256_bytes,
    utc_now,
    validate_locked_path,
)


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_THIRTY_SECOND_HEADING = re.compile(r"^#{1,6}\s+30\s*秒结论\s*#*\s*$")
_CONCLUSION_SECTION_HEADING = re.compile(r"^##\s+0\.\s*阅读结论\s*#*\s*$")
_SECOND_LEVEL_HEADING = re.compile(r"^##(?:\s|$)")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_ZERO_SHA256 = "0" * 64


def _invalid(message: str, cause: Exception | None = None) -> BatchRuntimeError:
    error = BatchRuntimeError("report_source_invalid", message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _read_outer_model(
    ref: ArtifactRef,
    model_type: type[_ModelT],
    *,
    schema_version: str,
    basename: str,
    id_field: str,
) -> tuple[Path, bytes, _ModelT]:
    if ref.schema_version != schema_version:
        raise _invalid(f"artifact ref schema must be exactly {schema_version}")
    path = normalized_absolute_path(Path(ref.path))
    if path.name != basename:
        raise _invalid(f"artifact ref must target {basename}")
    raw = read_bytes(path, code="report_source_invalid")
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
    try:
        # JSON arrays are the canonical wire representation of strict tuple
        # fields in the single-paper contracts. Validate from the wire bytes,
        # not from an intermediate Python list.
        model = model_type.model_validate_json(raw)
    except ValidationError as exc:
        raise _invalid(f"artifact envelope fails strict validation: {path}", exc)
    if raw != canonical_json_bytes(model) or getattr(model, id_field) != ref.artifact_id:
        raise _invalid(f"artifact envelope is noncanonical or has a mismatched id: {path}")
    return path, raw, model


def _read_candidate_inner(
    run_dir: Path,
    candidate_dir: Path,
    candidate: ForeignCandidate,
    *,
    role: str,
    basename: str,
    media_type: str,
) -> tuple[Path, bytes, ForeignArtifactRef]:
    matches = [ref for ref in candidate.artifacts if ref.role == role]
    if len(matches) != 1:
        raise _invalid(f"candidate must bind exactly one {role} artifact")
    ref = matches[0]
    expected_path = candidate_dir / basename
    expected_relative = expected_path.relative_to(run_dir).as_posix()
    if ref.path != expected_relative or ref.media_type != media_type:
        raise _invalid(f"candidate {role} ref does not use its fixed path/media type")
    raw = read_bytes(expected_path, code="report_source_invalid")
    if len(raw) != ref.size_bytes or sha256_bytes(raw) != ref.sha256:
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
        ForeignCandidate,
        schema_version="paper_reader.candidate.v2",
        basename="candidate.json",
        id_field="candidate_id",
    )
    candidate_dir = candidate_path.parent
    if candidate_dir.name != candidate.candidate_id or candidate_dir.parent.name != "candidates":
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
        summary = ForeignSummary.model_validate_json(summary_raw)
    except ValidationError as exc:
        raise _invalid("candidate summary fallback fails strict validation", exc)
    if summary_raw != canonical_json_bytes(summary):
        raise _invalid("candidate summary fallback is not canonical JSON")
    tldr = summary.tldr.strip() if summary.tldr is not None else ""
    if tldr:
        takeaway = tldr
        source_type = "structured_tldr_fallback"
    else:
        takeaway = summary.one_sentence_summary.strip()
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
    raw = read_bytes(path, code="journal_corrupt")
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


def _replace_or_publish(path: Path, content: bytes) -> None:
    if entry_exists(path):
        current = read_bytes(path, code="report_output_invalid")
        if current == content:
            return
        replace_bytes_atomic(path, content, expected_current=current)
    else:
        publish_bytes_no_replace(path, content)


def run_report(run_dir: Path, *, generated_at: str | None = None) -> dict[str, Any]:
    """Replay V2 truth and publish Markdown before its JSON commit marker."""

    from paper_reader_batch.v2_journal import load_run_view

    preflight = load_run_view(run_dir)
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
        )
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
        report = _build_report(view, generated_at=generated_at or utc_now())
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
        report_bytes = canonical_json_bytes(report)
        json_path = view.run_dir / "batch-report.json"
        markdown_path = view.run_dir / "batch-report.md"
        validate_locked_path(lock_path, descriptor)
        _replace_or_publish(markdown_path, markdown_bytes)
        validate_locked_path(lock_path, descriptor)
        _replace_or_publish(json_path, report_bytes)
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
