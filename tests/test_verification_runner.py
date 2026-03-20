from pathlib import Path

from ralph_ng.verification import VerificationRunner


def test_passing_command_returns_success(tmp_path: Path) -> None:
    runner = VerificationRunner(timeout_seconds=1, cwd=tmp_path)

    results = runner.run_commands(["python3 -c \"print('ok')\""])

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].exit_code == 0
    assert results[0].stdout.strip() == "ok"


def test_failing_command_returns_failure_with_output(tmp_path: Path) -> None:
    runner = VerificationRunner(timeout_seconds=1, cwd=tmp_path)

    results = runner.run_commands(
        [
            "python3 -c \"import sys; print('bad'); print('err', file=sys.stderr); sys.exit(3)\"",
        ]
    )

    assert results[0].success is False
    assert results[0].exit_code == 3
    assert "bad" in results[0].stdout
    assert "err" in results[0].stderr


def test_timeout_kills_process_and_returns_timeout_result(tmp_path: Path) -> None:
    runner = VerificationRunner(timeout_seconds=0.2, cwd=tmp_path)

    results = runner.run_commands(["python3 -c \"import time; time.sleep(1)\""])

    assert results[0].success is False
    assert results[0].timed_out is True
    assert results[0].exit_code is None


def test_multiple_commands_run_sequentially_and_all_results_are_collected(tmp_path: Path) -> None:
    runner = VerificationRunner(timeout_seconds=1, cwd=tmp_path)

    results = runner.run_commands(
        [
            "python3 -c \"print('one')\"",
            "python3 -c \"import sys; sys.exit(2)\"",
        ]
    )

    assert [result.command for result in results] == [
        "python3 -c \"print('one')\"",
        "python3 -c \"import sys; sys.exit(2)\"",
    ]
    assert results[0].success is True
    assert results[1].success is False
