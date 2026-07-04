from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from paperread_batch.io import JsonFileError, exclusive_file_lock, read_json, write_json_atomic, write_text_atomic
from paperread_batch.manifest import (
    DEFAULT_WRITE_POLICY,
    ManifestError,
    VALID_WRITE_POLICIES,
    manifest_from_pdf_folder,
    manifest_from_pdf_paths_file,
    manifest_from_zotero_collection_inventory,
    manifest_from_zotero_titles_file,
    validate_manifest,
)
from paperread_batch.local_prepare import prepare_pdf_bundle_subprocess
from paperread_batch.report import build_report, render_markdown_report
from paperread_batch.runs import allocate_batch_run_dir
from paperread_batch.state import (
    INTERRUPTED,
    RUNNING,
    SUCCEEDED,
    StateError,
    allocate_next,
    initial_state,
    mark_interrupted_running_items,
    pending_write_items,
    record_item_result,
    record_local_prepare_result,
    record_write_result,
    retry_failed,
    set_resume_decision,
)
from paperread_batch.worker_contract import render_worker_prompt

console = Console()
app = typer.Typer(help="Batch orchestration utilities for Paperread.")
manifest_app = typer.Typer(help="Build batch manifest files.")
app.add_typer(manifest_app, name="manifest")


@app.callback()
def main() -> None:
    """Top-level CLI entry point."""
    return None


@app.command()
def version() -> None:
    """Print the package version."""
    from paperread_batch import __version__

    typer.echo(__version__)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _exit_error(message: str) -> None:
    console.print(message, soft_wrap=True)
    raise typer.Exit(1)


def _read_object(path: Path, label: str) -> dict:
    try:
        payload = read_json(path)
    except JsonFileError as exc:
        _exit_error(f"{label}_unreadable: {exc}")
    if not isinstance(payload, dict):
        _exit_error(f"{label}_invalid: expected JSON object: {path}")
    return payload


def _batch_skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _find_paperread_root(explicit_root: Path | None) -> Path | None:
    if explicit_root is not None:
        candidate = explicit_root.expanduser()
        if (candidate / "SKILL.md").exists():
            return candidate.resolve()
        return None

    candidates: list[Path] = []
    env_root = os.environ.get("PAPERREAD_SKILL_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    batch_root = _batch_skill_root()
    candidates.extend([batch_root.parent / "paperread", batch_root.parent / "skill"])
    for candidate in candidates:
        skill_md = candidate.expanduser() / "SKILL.md"
        if skill_md.exists():
            return candidate.expanduser().resolve()
    return None


def _validate_paperread_root(explicit_root: Path | None) -> Path:
    root = _find_paperread_root(explicit_root)
    if root is None:
        if explicit_root is not None:
            _exit_error(f"paperread_unavailable: --paperread-root does not contain SKILL.md: {explicit_root}")
        _exit_error("paperread_unavailable: set --paperread-root or PAPERREAD_SKILL_ROOT")
    skill_md = root / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    if "\nname: paperread\n" not in text:
        _exit_error(f"paperread_invalid: {skill_md} does not declare name: paperread")
    if not (root / "pyproject.toml").exists():
        _exit_error(f"paperread_unavailable: missing pyproject.toml at {root}")
    try:
        result = subprocess.run(
            ["uv", "run", "paperread", "--help"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _exit_error(f"paperread_unavailable: cannot invoke paperread at {root}: {exc}")
    if result.returncode != 0:
        _exit_error(f"paperread_unavailable: uv run paperread --help failed at {root}: {result.stderr}")
    return root


def _manifest_item_by_id(manifest: dict, item_id: str) -> dict:
    for item in manifest["items"]:
        if item["item_id"] == item_id:
            return item
    return {}


def _selected_for_dispatch(manifest: dict, selected_state_items: list[dict]) -> list[dict]:
    dispatch: list[dict] = []
    for state_item in selected_state_items:
        manifest_item = _manifest_item_by_id(manifest, state_item["item_id"])
        payload = dict(state_item)
        if manifest_item:
            payload["input"] = manifest_item["input"]
            payload["expected_output"] = manifest_item["expected_output"]
        dispatch.append(payload)
    return dispatch


def _read_batch_files(batch_run: Path) -> tuple[dict, dict]:
    run_dir = Path(batch_run)
    return _read_manifest(run_dir), _read_state(run_dir)


def _read_manifest(run_dir: Path) -> dict:
    return validate_manifest(_read_object(run_dir / "manifest.json", "manifest"))


def _read_state(run_dir: Path) -> dict:
    return _read_object(run_dir / "state.json", "state")


def _state_lock_path(run_dir: Path) -> Path:
    return Path(run_dir) / ".state.lock"


def _has_existing_batch_state(run_dir: Path) -> bool:
    return (run_dir / "manifest.json").exists() or (run_dir / "state.json").exists()


def _item_result_path(items_dir: Path, item_id: str) -> Path:
    root = Path(items_dir).resolve()
    target = (root / f"{item_id}.json").resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise StateError(f"unsafe item_id for result archive: {item_id}") from exc
    return target


def _attempt_item_result_path(items_dir: Path, item_id: str, attempt_count: int) -> Path:
    if attempt_count < 1:
        raise StateError("attempt_count must be positive for result archive")
    return _item_result_path(items_dir, f"{item_id}.attempt-{attempt_count}")


def _prepare_result_path(items_dir: Path, item_id: str) -> Path:
    return _item_result_path(items_dir, f"{item_id}.prepare")


def _write_result_path(items_dir: Path, item_id: str) -> Path:
    return _item_result_path(items_dir, f"{item_id}.write")


def _local_prepare_candidates(manifest: dict, state: dict) -> list[dict]:
    state_by_id = {item["item_id"]: item for item in state["items"]}
    candidates: list[dict] = []
    for manifest_item in manifest["items"]:
        if manifest_item["input_type"] != "pdf_path":
            continue
        state_item = state_by_id[manifest_item["item_id"]]
        if state_item.get("status") in {RUNNING, SUCCEEDED}:
            continue
        local_prepare_status = str(state_item.get("local_prepare_status", "")).strip() or "pending"
        if local_prepare_status == "prepared":
            continue
        if local_prepare_status not in {"pending", "failed"}:
            continue
        candidates.append(manifest_item)
    return candidates


def _date_from_iso(timestamp: str):
    return datetime.fromisoformat(timestamp).date()


def _default_batch_run_dir(manifest: dict, *, now: str) -> Path:
    return allocate_batch_run_dir(
        _batch_skill_root() / "runs",
        manifest["batch_title"],
        run_date=_date_from_iso(now),
    )


def _read_archived_item_result(path: Path, item_id: str) -> dict:
    try:
        payload = read_json(path)
    except JsonFileError as exc:
        raise StateError(f"item result unreadable for {item_id}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StateError(f"item result invalid for {item_id}: expected JSON object: {path}")
    return payload


def _record_archived_item_results(state: dict, manifest: dict, run_dir: Path, *, now: str) -> dict:
    updated = state
    items_dir = run_dir / "items"
    for item in state["items"]:
        item_id = item["item_id"]
        if item.get("status") not in {RUNNING, INTERRUPTED}:
            continue
        result_path = _item_result_path(items_dir, item_id)
        if not result_path.exists():
            continue
        result_payload = _read_archived_item_result(result_path, item_id)
        try:
            updated = record_item_result(updated, manifest, item_id, result_payload, now=now)
        except StateError as exc:
            message = str(exc)
            stale_markers = (
                "worker_id does not match current item assignment",
                "attempt_count does not match current item assignment",
                "item is not currently assigned",
            )
            if any(marker in message for marker in stale_markers):
                updated = set_resume_decision(updated, item_id, f"archived_result_ignored: {message}")
                continue
            raise
    return updated


@manifest_app.command("from-pdf-folder")
def manifest_from_pdf_folder_command(
    folder: Path,
    batch_title: str = typer.Option(..., "--batch-title", help="Batch title."),
    output: Path = typer.Option(..., "--output", "-o", help="Write manifest JSON here."),
    recursive: bool = typer.Option(False, "--recursive", help="Scan subdirectories explicitly."),
    write_policy: str = typer.Option(
        DEFAULT_WRITE_POLICY,
        "--write-policy",
        help=f"Write policy: {', '.join(sorted(VALID_WRITE_POLICIES))}.",
    ),
) -> None:
    """Build a manifest from direct child PDF files."""
    try:
        manifest = manifest_from_pdf_folder(
            folder,
            batch_title=batch_title,
            recursive=recursive,
            write_policy=write_policy,
        )
    except ManifestError as exc:
        _exit_error(f"manifest_failed: {exc}")
    write_json_atomic(output, manifest)
    console.print(f"Wrote manifest: {output}")


@manifest_app.command("from-pdf-paths")
def manifest_from_pdf_paths_command(
    paths_file: Path,
    batch_title: str = typer.Option(..., "--batch-title", help="Batch title."),
    output: Path = typer.Option(..., "--output", "-o", help="Write manifest JSON here."),
    write_policy: str = typer.Option(
        DEFAULT_WRITE_POLICY,
        "--write-policy",
        help=f"Write policy: {', '.join(sorted(VALID_WRITE_POLICIES))}.",
    ),
) -> None:
    """Build a manifest from a text file of PDF paths."""
    try:
        manifest = manifest_from_pdf_paths_file(paths_file, batch_title=batch_title, write_policy=write_policy)
    except ManifestError as exc:
        _exit_error(f"manifest_failed: {exc}")
    write_json_atomic(output, manifest)
    console.print(f"Wrote manifest: {output}")


@manifest_app.command("from-zotero-titles")
def manifest_from_zotero_titles_command(
    titles_file: Path,
    batch_title: str = typer.Option(..., "--batch-title", help="Batch title."),
    output: Path = typer.Option(..., "--output", "-o", help="Write manifest JSON here."),
    write_policy: str = typer.Option(
        DEFAULT_WRITE_POLICY,
        "--write-policy",
        help=f"Write policy: {', '.join(sorted(VALID_WRITE_POLICIES))}.",
    ),
) -> None:
    """Build a manifest from a text file of Zotero titles or title fragments."""
    try:
        manifest = manifest_from_zotero_titles_file(
            titles_file,
            batch_title=batch_title,
            write_policy=write_policy,
        )
    except ManifestError as exc:
        _exit_error(f"manifest_failed: {exc}")
    write_json_atomic(output, manifest)
    console.print(f"Wrote manifest: {output}")


@manifest_app.command("from-zotero-collection")
def manifest_from_zotero_collection_command(
    collection: str,
    items_json: Path = typer.Option(..., "--items-json", help="Read-only collection inventory JSON."),
    batch_title: str = typer.Option(..., "--batch-title", help="Batch title."),
    output: Path = typer.Option(..., "--output", "-o", help="Write manifest JSON here."),
    write_policy: str = typer.Option(
        DEFAULT_WRITE_POLICY,
        "--write-policy",
        help=f"Write policy: {', '.join(sorted(VALID_WRITE_POLICIES))}.",
    ),
) -> None:
    """Build a manifest from a read-only Zotero collection inventory JSON file."""
    try:
        manifest = manifest_from_zotero_collection_inventory(
            items_json,
            batch_title=batch_title,
            collection_query=collection,
            write_policy=write_policy,
        )
    except (ManifestError, JsonFileError) as exc:
        _exit_error(f"manifest_failed: {exc}")
    write_json_atomic(output, manifest)
    console.print(f"Wrote manifest: {output}")


@app.command("init")
def init_command(
    manifest: Path = typer.Option(..., "--manifest", help="Manifest JSON to initialize from."),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Batch run directory. Defaults to runs/YYYY-MM-DD/<batch-slug>/ under this skill root.",
    ),
    now: str | None = typer.Option(None, "--now", help="Timestamp override for tests."),
) -> None:
    """Initialize a batch run directory."""
    try:
        normalized_manifest = validate_manifest(_read_object(manifest, "manifest"))
    except ManifestError as exc:
        _exit_error(f"manifest_invalid: {exc}")
    run_dir = Path(output) if output is not None else _default_batch_run_dir(
        normalized_manifest,
        now=now or _now_iso(),
    )
    if _has_existing_batch_state(run_dir):
        _exit_error(f"batch_run_exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "items").mkdir(exist_ok=True)
    write_json_atomic(run_dir / "manifest.json", normalized_manifest)
    with exclusive_file_lock(_state_lock_path(run_dir)):
        write_json_atomic(run_dir / "state.json", initial_state(normalized_manifest))
    typer.echo(f"batch_run_initialized: {run_dir}")


@app.command("validate")
def validate_command(
    batch_run: Path,
    paperread_root: Path | None = typer.Option(None, "--paperread-root", help="Installed paperread skill root."),
) -> None:
    """Validate a batch run before dispatch."""
    run_dir = Path(batch_run)
    if not run_dir.exists() or not run_dir.is_dir():
        _exit_error(f"batch_run_invalid: not a directory: {run_dir}")
    try:
        _manifest, _state = _read_batch_files(run_dir)
    except ManifestError as exc:
        _exit_error(f"manifest_invalid: {exc}")
    if not os.access(run_dir, os.W_OK):
        _exit_error(f"batch_run_invalid: not writable: {run_dir}")
    root = _validate_paperread_root(paperread_root)
    console.print(f"batch_run_valid: {run_dir}")
    console.print(f"paperread_root: {root}")


@app.command("prepare-local-pdfs")
def prepare_local_pdfs_command(
    batch_run: Path,
    paperread_root: Path | None = typer.Option(None, "--paperread-root", help="Installed paperread skill root."),
    concurrency: int = typer.Option(3, "--concurrency", min=1, help="Maximum concurrent prepare-pdf subprocesses."),
    timeout_seconds: int = typer.Option(900, "--timeout-seconds", min=30, help="Timeout per PDF prepare subprocess."),
) -> None:
    """Prepare local PDF analysis bundles concurrently as a non-LLM fallback."""
    run_dir = Path(batch_run)
    root = _validate_paperread_root(paperread_root)
    manifest = _read_manifest(run_dir)
    with exclusive_file_lock(_state_lock_path(run_dir)):
        state = _read_state(run_dir)
        pdf_item_count = sum(1 for item in manifest["items"] if item["input_type"] == "pdf_path")
        pdf_items = _local_prepare_candidates(manifest, state)
    if pdf_item_count == 0:
        console.print("local_prepare_skipped: no pdf_path items")
        return
    if not pdf_items:
        console.print("local_prepare_skipped: no pending pdf_path items")
        return
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_item = {
            executor.submit(
                prepare_pdf_bundle_subprocess,
                paperread_root=root,
                pdf_path=item["input"]["path"],
                timeout_seconds=timeout_seconds,
            ): item
            for item in pdf_items
        }
        for future in as_completed(future_to_item):
            item = future_to_item[future]
            try:
                results[item["item_id"]] = future.result()
            except Exception as exc:
                results[item["item_id"]] = {
                    "schema_version": "paperread-batch.local-prepare-result.v1",
                    "status": "failed",
                    "analysis_dir": "",
                    "final_note_path": "",
                    "manifest_path": "",
                    "failure_reason": str(exc),
                }
    with exclusive_file_lock(_state_lock_path(run_dir)):
        state = _read_state(run_dir)
        items_dir = run_dir / "items"
        items_dir.mkdir(exist_ok=True)
        updated = state
        current_items = {item["item_id"]: item for item in state["items"]}
        recorded_count = 0
        for item_id, result in sorted(results.items()):
            current_item = current_items.get(item_id)
            if current_item is None:
                continue
            if current_item.get("status") in {RUNNING, SUCCEEDED}:
                continue
            if current_item.get("local_prepare_status") == "prepared":
                continue
            archived_result = {**result, "item_id": item_id}
            write_json_atomic(_prepare_result_path(items_dir, item_id), archived_result)
            updated = record_local_prepare_result(updated, item_id, archived_result)
            recorded_count += 1
        write_json_atomic(run_dir / "state.json", updated)
    console.print(f"local_prepare_recorded: {recorded_count}")


@app.command("next")
def next_command(
    batch_run: Path,
    limit: int = typer.Option(3, "--limit", min=1, help="Maximum number of pending items to allocate."),
    now: str | None = typer.Option(None, "--now", help="Timestamp override for tests."),
) -> None:
    """Allocate the next pending items and mark them running."""
    run_dir = Path(batch_run)
    manifest = _read_manifest(run_dir)
    with exclusive_file_lock(_state_lock_path(run_dir)):
        state = _read_state(run_dir)
        try:
            updated, selected = allocate_next(state, limit=limit, now=now or _now_iso())
        except StateError as exc:
            _exit_error(f"next_failed: {exc}")
        write_json_atomic(run_dir / "state.json", updated)
    typer.echo(json.dumps(_selected_for_dispatch(manifest, selected), ensure_ascii=False, indent=2))


@app.command("worker-prompt")
def worker_prompt_command(batch_run: Path, item_id: str) -> None:
    """Render a deterministic prompt for one currently assigned worker item."""
    run_dir = Path(batch_run)
    manifest, state = _read_batch_files(run_dir)
    state_item = next((item for item in state["items"] if item.get("item_id") == item_id), None)
    if state_item is None:
        _exit_error(f"worker_prompt_failed: unknown item_id: {item_id}")
    if state_item.get("status") != RUNNING:
        _exit_error(f"worker_prompt_failed: item is not currently running: {item_id}")
    assignment = _selected_for_dispatch(manifest, [state_item])[0]
    typer.echo(render_worker_prompt(batch_run=str(run_dir), assignment=assignment))


@app.command("record-result")
def record_result_command(
    batch_run: Path,
    item_id: str,
    result: Path = typer.Option(..., "--result", help="Item result JSON."),
    now: str | None = typer.Option(None, "--now", help="Timestamp override for tests."),
) -> None:
    """Record one worker result into state."""
    run_dir = Path(batch_run)
    manifest = _read_manifest(run_dir)
    result_payload = _read_object(result, "result")
    with exclusive_file_lock(_state_lock_path(run_dir)):
        state = _read_state(run_dir)
        try:
            updated = record_item_result(state, manifest, item_id, result_payload, now=now or _now_iso())
        except (StateError, JsonFileError) as exc:
            _exit_error(f"record_result_failed: {exc}")
        items_dir = run_dir / "items"
        items_dir.mkdir(exist_ok=True)
        attempt_count = int(result_payload.get("attempt_count", 0))
        write_json_atomic(_attempt_item_result_path(items_dir, item_id, attempt_count), result_payload)
        write_json_atomic(_item_result_path(items_dir, item_id), result_payload)
        write_json_atomic(run_dir / "state.json", updated)
    console.print(f"recorded_result: {item_id}")


@app.command("validate-result")
def validate_result_command(
    batch_run: Path,
    item_id: str,
    result: Path = typer.Option(..., "--result", help="Item result JSON."),
    now: str | None = typer.Option(None, "--now", help="Timestamp override for tests."),
) -> None:
    """Validate one worker result without mutating state."""
    run_dir = Path(batch_run)
    manifest = _read_manifest(run_dir)
    result_payload = _read_object(result, "result")
    state = _read_state(run_dir)
    try:
        record_item_result(state, manifest, item_id, result_payload, now=now or _now_iso())
    except (StateError, JsonFileError) as exc:
        _exit_error(f"validate_result_failed: {exc}")
    console.print(f"result_valid: {item_id}")


@app.command("next-write")
def next_write_command(
    batch_run: Path,
    limit: int = typer.Option(1, "--limit", min=1, help="Maximum number of prepared Zotero notes to emit."),
) -> None:
    """List prepared Zotero note candidates that still need MCP write_note and verification."""
    if limit != 1:
        _exit_error("next_write_failed: Zotero writes are serial; use --limit 1")
    run_dir = Path(batch_run)
    manifest = _read_manifest(run_dir)
    with exclusive_file_lock(_state_lock_path(run_dir)):
        state = _read_state(run_dir)
        try:
            selected = pending_write_items(manifest, state, limit=limit)
        except (StateError, JsonFileError) as exc:
            _exit_error(f"next_write_failed: {exc}")
    typer.echo(json.dumps(selected, ensure_ascii=False, indent=2))


@app.command("record-write")
def record_write_command(
    batch_run: Path,
    item_id: str,
    result: Path = typer.Option(..., "--result", help="Write result JSON with verify report path."),
    now: str | None = typer.Option(None, "--now", help="Timestamp override for tests."),
) -> None:
    """Record a verified Zotero note write into state."""
    run_dir = Path(batch_run)
    manifest = _read_manifest(run_dir)
    result_payload = _read_object(result, "write_result")
    with exclusive_file_lock(_state_lock_path(run_dir)):
        state = _read_state(run_dir)
        try:
            updated = record_write_result(state, manifest, item_id, result_payload, now=now or _now_iso())
        except (StateError, JsonFileError) as exc:
            _exit_error(f"record_write_failed: {exc}")
        items_dir = run_dir / "items"
        items_dir.mkdir(exist_ok=True)
        write_json_atomic(_write_result_path(items_dir, item_id), result_payload)
        write_json_atomic(run_dir / "state.json", updated)
    console.print(f"recorded_write: {item_id}")


@app.command("report")
def report_command(
    batch_run: Path,
    reported_at: str | None = typer.Option(None, "--reported-at", help="Timestamp override for tests."),
) -> None:
    """Generate batch report JSON and Markdown."""
    run_dir = Path(batch_run)
    manifest = _read_manifest(run_dir)
    with exclusive_file_lock(_state_lock_path(run_dir)):
        state = _read_state(run_dir)
    report = build_report(manifest, state, reported_at=reported_at or _now_iso())
    write_json_atomic(run_dir / "batch-report.json", report)
    write_text_atomic(run_dir / "batch-report.md", render_markdown_report(report))
    console.print(f"Wrote report: {run_dir / 'batch-report.md'}")


@app.command("retry-failed")
def retry_failed_command(batch_run: Path) -> None:
    """Reset failed and interrupted items to pending."""
    run_dir = Path(batch_run)
    _manifest = _read_manifest(run_dir)
    with exclusive_file_lock(_state_lock_path(run_dir)):
        state = _read_state(run_dir)
        write_json_atomic(run_dir / "state.json", retry_failed(state))
    console.print("retry_failed_ready")


@app.command("resume")
def resume_command(batch_run: Path) -> None:
    """Recover archived results and interrupt remaining running items."""
    run_dir = Path(batch_run)
    manifest = _read_manifest(run_dir)
    now = _now_iso()
    with exclusive_file_lock(_state_lock_path(run_dir)):
        state = _read_state(run_dir)
        try:
            recovered = _record_archived_item_results(state, manifest, run_dir, now=now)
            updated = mark_interrupted_running_items(recovered)
        except (StateError, JsonFileError) as exc:
            _exit_error(f"resume_failed: {exc}")
        write_json_atomic(run_dir / "state.json", updated)
    console.print("running_items_marked_interrupted")
