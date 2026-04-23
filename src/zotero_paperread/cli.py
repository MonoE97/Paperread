from __future__ import annotations

import typer

app = typer.Typer(help="Zotero-first paper reading utilities.")


@app.callback()
def main() -> None:
    """Top-level CLI entry point."""
    return None


@app.command()
def version() -> None:
    """Print the package version."""
    from zotero_paperread import __version__

    typer.echo(__version__)
