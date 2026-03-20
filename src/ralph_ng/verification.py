"""Local verification command execution."""

from __future__ import annotations

from pathlib import Path
import subprocess
import time

from pydantic import BaseModel, ConfigDict

from .utils import coerce_text


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    success: bool


class VerificationRunner:
    """Run verification commands sequentially with timeouts."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        cwd: str | Path | None = None,
        shell: str = "bash",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.cwd = None if cwd is None else Path(cwd)
        self.shell = shell

    def run_commands(self, commands: list[str]) -> list[VerificationResult]:
        return [self._run_command(command) for command in commands]

    def _run_command(self, command: str) -> VerificationResult:
        started_at = time.monotonic()
        try:
            completed = subprocess.run(
                [self.shell, "-lc", command],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started_at
            return VerificationResult(
                command=command,
                exit_code=None,
                stdout=coerce_text(exc.stdout if hasattr(exc, "stdout") else exc.output),
                stderr=coerce_text(exc.stderr),
                duration_seconds=duration,
                timed_out=True,
                success=False,
            )

        duration = time.monotonic() - started_at
        return VerificationResult(
            command=command,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=duration,
            timed_out=False,
            success=completed.returncode == 0,
        )
