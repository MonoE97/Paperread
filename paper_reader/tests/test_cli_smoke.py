from typer.testing import CliRunner

import paper_reader
from paper_reader.cli import app


def test_version_command_prints_version() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"


def test_cli_help_and_package_docstring_describe_zotero_and_local_pdf_paths() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Zotero titles and local PDF paths" in result.stdout
    assert "Zotero-first" not in result.stdout
    assert paper_reader.__doc__ is not None
    assert "Zotero titles and local PDF paths" in paper_reader.__doc__
    assert "Zotero-first" not in paper_reader.__doc__
