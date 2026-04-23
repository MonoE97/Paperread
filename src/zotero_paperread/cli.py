from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from zotero_paperread.note import render_note, validate_note
from zotero_paperread.pdf_extract import extract_pdf

app = typer.Typer(help="Zotero-first paper reading utilities.")
console = Console()


@app.callback()
def main() -> None:
    """Top-level CLI entry point."""
    return None


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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


@app.command("render-note")
def render_note_command(
    metadata_json: Path,
    summary_json: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write Markdown note to this file."),
    generated_date: str | None = typer.Option(None, "--generated-date", help="Override generated date."),
) -> None:
    """Render a Zotero note from metadata and summary JSON."""
    note = render_note(read_json(metadata_json), read_json(summary_json), generated_date=generated_date)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(note, encoding="utf-8")
    console.print(f"Wrote note Markdown: {output}")


@app.command("validate-note")
def validate_note_command(note_path: Path) -> None:
    """Validate a rendered note."""
    errors = validate_note(note_path.read_text(encoding="utf-8"))
    if errors:
        for error in errors:
            console.print(error)
        raise typer.Exit(1)
    console.print("note_valid")


@app.command("preview-note")
def preview_note_command(note_path: Path) -> None:
    """Print a rendered note without writing to Zotero."""
    console.print(note_path.read_text(encoding="utf-8"))
