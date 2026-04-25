from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import typer
from rich.console import Console

from zotero_paperread.figures import extract_figures
from zotero_paperread.note import render_note, validate_note
from zotero_paperread.pdf_extract import extract_pdf
from zotero_paperread.runs import allocate_run_dir, write_run_manifest
from zotero_paperread.workflow import prepare_item_bundle

app = typer.Typer(help="Zotero-first paper reading utilities.")
console = Console()


@app.callback()
def main() -> None:
    """Top-level CLI entry point."""
    return None


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_base_dir(base_dir: Path) -> Path:
    if base_dir.is_absolute():
        return base_dir
    return Path(__file__).resolve().parents[2] / base_dir


def render_note_to_path(
    metadata_json: Path,
    summary_json: Path,
    output: Path,
    generated_date: str | None = None,
) -> None:
    note = render_note(read_json(metadata_json), read_json(summary_json), generated_date=generated_date)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(note, encoding="utf-8")
    console.print(f"Wrote note Markdown: {output}")


def read_note_text_or_exit(note_path: Path) -> str:
    try:
        return note_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        console.print(f"note_missing: {note_path} (render-note has not completed or the path is wrong)")
        raise typer.Exit(1)


def validate_note_path_or_exit(note_path: Path) -> None:
    errors = validate_note(read_note_text_or_exit(note_path))
    if errors:
        for error in errors:
            console.print(error)
        raise typer.Exit(1)
    console.print("note_valid")


@app.command()
def version() -> None:
    """Print the package version."""
    from zotero_paperread import __version__

    typer.echo(__version__)


@app.command("extract-pdf")
def extract_pdf_command(
    pdf_path: Path,
    output: Path | None = typer.Option(None, "--output", "-o", help="Write JSON to this file."),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, help="Extract at most this many pages."),
) -> None:
    """Extract text from a PDF and emit JSON."""
    result = extract_pdf(pdf_path, max_pages=max_pages)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if output is None:
        console.print(payload)
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload + "\n", encoding="utf-8")
    console.print(f"Wrote extraction JSON: {output}")


@app.command("extract-figures")
def extract_figures_command(
    pdf_path: Path,
    output_dir: Path = typer.Option(..., "--output-dir", help="Directory for extracted figure assets."),
    top_k: int = typer.Option(4, "--top-k", min=0, help="Return at most this many ranked figures."),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, help="Inspect at most this many PDF pages."),
    arxiv_id: str | None = typer.Option(None, "--arxiv-id", help="Optional arXiv identifier override."),
) -> None:
    """Extract representative figures from a PDF and emit JSON."""
    payload = extract_figures(
        pdf_path,
        output_dir=output_dir,
        top_k=top_k,
        max_pages=max_pages,
        arxiv_id=arxiv_id,
    )
    typer.echo(json.dumps(payload, ensure_ascii=False))


@app.command("create-run")
def create_run_command(
    title: str = typer.Option(..., "--title", help="Paper title used for slugging."),
    item_key: str = typer.Option("", "--item-key", help="Optional Zotero item key for the manifest."),
    base_dir: Path = typer.Option(Path("runs"), "--base-dir", help="Project-local runs directory."),
    today: str | None = typer.Option(None, "--today", help="Override date for deterministic tests."),
) -> None:
    """Allocate a project-local run directory and write run.json."""
    run_date = date.fromisoformat(today) if today else date.today()
    resolved_base_dir = resolve_base_dir(base_dir)
    run_dir = allocate_run_dir(resolved_base_dir, title=title, today=run_date)
    allocated_slug = run_dir.name
    manifest_path = write_run_manifest(
        run_dir,
        {
            "title": title,
            "slug": allocated_slug,
            "item_key": item_key,
            "created_at": run_date.isoformat(),
            "status": "initialized",
        },
    )
    typer.echo(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "manifest_path": str(manifest_path),
                "slug": allocated_slug,
                "date": run_date.isoformat(),
            },
            ensure_ascii=False,
        )
    )


@app.command("render-note")
def render_note_command(
    metadata_json: Path,
    summary_json: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write Markdown note to this file."),
    generated_date: str | None = typer.Option(None, "--generated-date", help="Override generated date."),
) -> None:
    """Render a Zotero note from metadata and summary JSON."""
    render_note_to_path(metadata_json, summary_json, output, generated_date=generated_date)


@app.command("finalize-note")
def finalize_note_command(
    metadata_json: Path,
    summary_json: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write Markdown note to this file."),
    generated_date: str | None = typer.Option(None, "--generated-date", help="Override generated date."),
) -> None:
    """Render and validate a Zotero note sequentially."""
    render_note_to_path(metadata_json, summary_json, output, generated_date=generated_date)
    validate_note_path_or_exit(output)


@app.command("validate-note")
def validate_note_command(note_path: Path) -> None:
    """Validate a rendered note."""
    validate_note_path_or_exit(note_path)


@app.command("preview-note")
def preview_note_command(note_path: Path) -> None:
    """Print a rendered note without writing to Zotero."""
    console.print(note_path.read_text(encoding="utf-8"))


@app.command("prepare-item")
def prepare_item_command(
    details_json: Path,
    workdir: Path = typer.Option(..., "--workdir", help="Directory for metadata, extraction, and context files."),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, help="Extract at most this many PDF pages."),
) -> None:
    """Prepare a summarization bundle from raw Zotero item details JSON."""
    payload = prepare_item_bundle(read_json(details_json), workdir=workdir, max_pages=max_pages)
    typer.echo(json.dumps(payload, ensure_ascii=False))
