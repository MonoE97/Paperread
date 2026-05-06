from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import typer
from rich.console import Console

from zotero_paperread.figures import extract_figures
from zotero_paperread.note import build_note_labels, render_note, render_note_html, validate_note, validate_trusted_summary
from zotero_paperread.note_table_migration import (
    classify_note_content,
    convert_note_tables_to_html,
    has_markdown_table_separator,
)
from zotero_paperread.pdf_extract import extract_pdf
from zotero_paperread.review import apply_review_to_summary
from zotero_paperread.runs import allocate_run_dir, write_run_manifest
from zotero_paperread.summary_lint import lint_summary
from zotero_paperread.workflow import prepare_item_bundle
from zotero_paperread.zotero_details import next_version_suffix_from_details
from zotero_paperread.zotero_item_io import write_item_details_files

app = typer.Typer(help="Zotero-first paper reading utilities.")
console = Console()


@app.callback()
def main() -> None:
    """Top-level CLI entry point."""
    return None


def exit_with_json_error(message: str) -> None:
    typer.echo(message)
    raise typer.Exit(1)


def format_unreadable_json_error(path: Path, *, label: str, reason: str) -> str:
    return f"json_unreadable: {label} {path}: {reason}"


def read_json_or_exit(path: Path, *, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        exit_with_json_error(f"json_missing: {label} {path}")
    except IsADirectoryError:
        exit_with_json_error(format_unreadable_json_error(path, label=label, reason="is a directory"))
    except PermissionError:
        exit_with_json_error(format_unreadable_json_error(path, label=label, reason="permission denied"))
    except UnicodeDecodeError:
        exit_with_json_error(format_unreadable_json_error(path, label=label, reason="not valid UTF-8 text"))
    except json.JSONDecodeError as exc:
        exit_with_json_error(
            f"json_invalid: {label} {path} line {exc.lineno} column {exc.colno}: {exc.msg}"
        )
    except OSError as exc:
        exit_with_json_error(format_unreadable_json_error(path, label=label, reason=str(exc)))

    if not isinstance(payload, dict):
        exit_with_json_error(f"json_invalid: {label} {path}: expected top-level JSON object")
    return payload


def resolve_base_dir(base_dir: Path) -> Path:
    if base_dir.is_absolute():
        return base_dir
    return Path(__file__).resolve().parents[2] / base_dir


def render_note_to_path(
    metadata_json: Path,
    summary_json: Path,
    output: Path,
    generated_date: str | None = None,
    version_suffix: str = "",
) -> str:
    note = render_note(
        read_json_or_exit(metadata_json, label="metadata JSON"),
        read_json_or_exit(summary_json, label="summary JSON"),
        generated_date=generated_date,
        version_suffix=version_suffix,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(note, encoding="utf-8")
    console.print(f"Wrote note Markdown: {output}")
    return note


def write_note_html_to_path(note: str, output: Path) -> None:
    html = render_note_html(note)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    console.print(f"Wrote note HTML: {output}")


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
    version_suffix: str = typer.Option("", "--version-suffix", help="Append a suffix such as ' (v2)' to the note title."),
) -> None:
    """Render a Zotero note from metadata and summary JSON."""
    render_note_to_path(
        metadata_json,
        summary_json,
        output,
        generated_date=generated_date,
        version_suffix=version_suffix,
    )


@app.command("finalize-note")
def finalize_note_command(
    metadata_json: Path,
    summary_json: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write Markdown note to this file."),
    html_output: Path | None = typer.Option(
        None,
        "--html-output",
        help="Write Zotero-ready HTML converted from the finalized Markdown note.",
    ),
    generated_date: str | None = typer.Option(None, "--generated-date", help="Override generated date."),
    version_suffix: str = typer.Option("", "--version-suffix", help="Append a suffix such as ' (v2)' to the note title."),
) -> None:
    """Render and validate a Zotero note sequentially."""
    note = render_note_to_path(
        metadata_json,
        summary_json,
        output,
        generated_date=generated_date,
        version_suffix=version_suffix,
    )
    validate_note_path_or_exit(output)
    if html_output is not None:
        write_note_html_to_path(note, html_output)


@app.command("next-version-suffix")
def next_version_suffix_command(
    details_json: Path,
    paper_title: str = typer.Option(..., "--paper-title", help="Paper title used in generated note titles."),
    generated_date: str = typer.Option(..., "--generated-date", help="Generated note date in YYYY-MM-DD form."),
) -> None:
    """Print the next same-day generated-note title suffix."""
    suffix = next_version_suffix_from_details(
        read_json_or_exit(details_json, label="details JSON"),
        paper_title=paper_title,
        generated_date=generated_date,
    )
    typer.echo(suffix)


@app.command("note-tags")
def note_tags_command(summary_json: Path) -> None:
    """Print Zotero note tags derived from summary JSON."""
    tags = build_note_labels(read_json_or_exit(summary_json, label="summary JSON"))
    typer.echo(json.dumps(tags, ensure_ascii=False))


@app.command("validate-note")
def validate_note_command(note_path: Path) -> None:
    """Validate a rendered note."""
    validate_note_path_or_exit(note_path)


@app.command("render-note-html")
def render_note_html_command(
    note_path: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write Zotero-ready HTML to this file."),
) -> None:
    """Convert an existing rendered Markdown note into Zotero-ready HTML."""
    write_note_html_to_path(read_note_text_or_exit(note_path), output)


@app.command("classify-note-tables")
def classify_note_tables_command(note_path: Path) -> None:
    """Classify a raw Zotero note file before table migration."""
    content = read_note_text_or_exit(note_path)
    content_with_line_breaks = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    typer.echo(
        json.dumps(
            {
                "content_type": classify_note_content(content),
                "has_markdown_table": has_markdown_table_separator(content_with_line_breaks),
                "has_html_table": "<table" in content.lower(),
            },
            ensure_ascii=False,
        )
    )


@app.command("convert-note-tables")
def convert_note_tables_command(
    note_path: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write converted HTML to this file."),
    report: Path = typer.Option(..., "--report", help="Write conversion report JSON to this file."),
) -> None:
    """Convert Markdown tables in a raw Zotero note file without writing to Zotero."""
    result = convert_note_tables_to_html(read_note_text_or_exit(note_path))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.content, encoding="utf-8")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {
                "status": result.status,
                "content_type": result.content_type,
                "reason": result.reason,
                "before_hash": result.before_hash,
                "after_hash": result.after_hash,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    console.print(f"Wrote converted note HTML: {output}")
    console.print(f"Wrote conversion report: {report}")


@app.command("validate-summary-json")
def validate_summary_json_command(summary_json: Path) -> None:
    """Check that summary JSON is readable and has an object at the top level."""
    read_json_or_exit(summary_json, label="summary JSON")
    console.print("summary_json_readable_object")


@app.command("lint-summary")
def lint_summary_command(summary_json: Path) -> None:
    """Run non-fatal summary lint checks used before write-through."""
    issues = lint_summary(read_json_or_exit(summary_json, label="summary JSON"))
    if issues:
        typer.echo(json.dumps({"status": "failed", "issues": issues}, ensure_ascii=False, indent=2))
        raise typer.Exit(1)
    typer.echo(json.dumps({"status": "passed", "issues": []}, ensure_ascii=False))


@app.command("apply-review")
def apply_review_command(
    summary_json: Path,
    review_json: Path,
    output: Path | None = typer.Option(None, "--output", "-o", help="Write updated summary JSON."),
) -> None:
    """Apply review gate fields to summary JSON deterministically."""
    summary = read_json_or_exit(summary_json, label="summary JSON")
    review = read_json_or_exit(review_json, label="review JSON")
    try:
        updated = apply_review_to_summary(summary, review)
    except ValueError as exc:
        console.print(str(exc), soft_wrap=True)
        raise typer.Exit(1)
    target = output or summary_json
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    console.print(f"Wrote reviewed summary JSON: {target}")


@app.command("validate-trusted-summary")
def validate_trusted_summary_command(summary_json: Path) -> None:
    """Validate semantic write-readiness fields in summary JSON."""
    errors = validate_trusted_summary(read_json_or_exit(summary_json, label="summary JSON"))
    if errors:
        for error in errors:
            console.print(f"trusted_summary_invalid: {error}", soft_wrap=True)
        raise typer.Exit(1)
    console.print("trusted_summary_valid")


@app.command("preview-note")
def preview_note_command(note_path: Path) -> None:
    """Print a rendered note without writing to Zotero."""
    console.print(note_path.read_text(encoding="utf-8"))


@app.command("save-item-details")
def save_item_details_command(
    input_json: Path,
    output: Path = typer.Option(..., "--output", "-o", help="Write normalized item details JSON."),
    raw_output: Path | None = typer.Option(None, "--raw-output", help="Optionally write raw MCP payload JSON."),
) -> None:
    """Save raw MCP item details as normalized run item-details.json."""
    payload = json.loads(input_json.read_text(encoding="utf-8"))
    result = write_item_details_files(payload, normalized_path=output, raw_path=raw_output)
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("prepare-item")
def prepare_item_command(
    details_json: Path,
    workdir: Path = typer.Option(..., "--workdir", help="Directory for metadata, extraction, and context files."),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, help="Extract at most this many PDF pages."),
) -> None:
    """Prepare a summarization bundle from raw Zotero item details JSON."""
    payload = prepare_item_bundle(
        read_json_or_exit(details_json, label="details JSON"),
        workdir=workdir,
        max_pages=max_pages,
    )
    typer.echo(json.dumps(payload, ensure_ascii=False))
