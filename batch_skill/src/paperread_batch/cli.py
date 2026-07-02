from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from paperread_batch.io import JsonFileError, exclusive_file_lock, read_json, write_json_atomic, write_text_atomic
from paperread_batch.manifest import (
    ManifestError,
    manifest_from_pdf_folder,
    manifest_from_pdf_paths_file,
    manifest_from_zotero_collection_inventory,
    manifest_from_zotero_titles_file,
    validate_manifest,
)
from paperread_batch.report import build_report, render_markdown_report
from paperread_batch.runs import allocate_batch_run_dir
from paperread_batch.state import (
    INTERRUPTED,
    RUNNING,
    StateError,
    allocate_next,
    initial_state,
    mark_interrupted_running_items,
    record_item_result,
    retry_failed,
)

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
        updated = record_item_result(updated, manifest, item_id, result_payload, now=now)
    return updated


@manifest_app.command("from-pdf-folder")
def manifest_from_pdf_folder_command(
    folder: Path,
    batch_title: str = typer.Option(..., "--batch-title", help="Batch title."),
    output: Path = typer.Option(..., "--output", "-o", help="Write manifest JSON here."),
    recursive: bool = typer.Option(False, "--recursive", help="Scan subdirectories explicitly."),
) -> None:
    """Build a manifest from direct child PDF files."""
    try:
        manifest = manifest_from_pdf_folder(folder, batch_title=batch_title, recursive=recursive)
    except ManifestError as exc:
        _exit_error(f"manifest_failed: {exc}")
    write_json_atomic(output, manifest)
    console.print(f"Wrote manifest: {output}")


@manifest_app.command("from-pdf-paths")
def manifest_from_pdf_paths_command(
    paths_file: Path,
    batch_title: str = typer.Option(..., "--batch-title", help="Batch title."),
    output: Path = typer.Option(..., "--output", "-o", help="Write manifest JSON here."),
) -> None:
    """Build a manifest from a text file of PDF paths."""
    try:
        manifest = manifest_from_pdf_paths_file(paths_file, batch_title=batch_title)
    except ManifestError as exc:
        _exit_error(f"manifest_failed: {exc}")
    write_json_atomic(output, manifest)
    console.print(f"Wrote manifest: {output}")


@manifest_app.command("from-zotero-titles")
def manifest_from_zotero_titles_command(
    titles_file: Path,
    batch_title: str = typer.Option(..., "--batch-title", help="Batch title."),
    output: Path = typer.Option(..., "--output", "-o", help="Write manifest JSON here."),
) -> None:
    """Build a manifest from a text file of Zotero titles or title fragments."""
    try:
        manifest = manifest_from_zotero_titles_file(titles_file, batch_title=batch_title)
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
) -> None:
    """Build a manifest from a read-only Zotero collection inventory JSON file."""
    try:
        manifest = manifest_from_zotero_collection_inventory(
            items_json,
            batch_title=batch_title,
            collection_query=collection,
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
        write_json_atomic(_item_result_path(items_dir, item_id), result_payload)
        write_json_atomic(run_dir / "state.json", updated)
    console.print(f"recorded_result: {item_id}")


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
