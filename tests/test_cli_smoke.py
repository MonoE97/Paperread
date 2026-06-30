from typer.testing import CliRunner

from paperread.cli import app


def test_version_command_prints_version() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"
