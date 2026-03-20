from typer.testing import CliRunner

from ralph_ng.cli import app, generate_run_id


def test_cli_help_lists_expected_commands() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "validate-plan" in result.stdout
    assert "start" in result.stdout
    assert "resume" in result.stdout
    assert "status" in result.stdout


def test_generate_run_id_is_unique() -> None:
    run_ids = {generate_run_id() for _ in range(20)}

    assert len(run_ids) == 20
