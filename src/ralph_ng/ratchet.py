"""Runtime ratchet overlay management."""

from __future__ import annotations

from pathlib import Path
import re
import shlex
import shutil
import subprocess

from pydantic import BaseModel, ConfigDict, Field
import yaml


DANGEROUS_COMMAND_PATTERNS = [
    re.compile(r"\b(?:curl|wget)\b.*\|\s*(?:bash|sh|zsh)\b", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/", re.IGNORECASE),
]

ENVIRONMENT_PROBE_PATTERNS = [
    re.compile(r"^\s*command\s+-v\s+\S+\s*$", re.IGNORECASE),
    re.compile(r"^\s*which\s+\S+\s*$", re.IGNORECASE),
    re.compile(r"^\s*type\s+\S+\s*$", re.IGNORECASE),
]

PORTABLE_ALTERNATIVES: dict[str, str] = {
    "rg": "grep -rnE",
    "fd": "find . -name",
    "bat": "cat",
    "exa": "ls",
    "delta": "diff",
}


class RejectedCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    reason: str


class RatchetApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: list[str] = Field(default_factory=list)
    rejected: list[RejectedCheck] = Field(default_factory=list)


class RatchetOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    verification_commands: list[str] = Field(default_factory=list)


class RatchetManager:
    """Validate and persist reviewer-added verification commands."""

    def __init__(
        self,
        *,
        overlay_dir: str | Path,
        cwd: str | Path | None,
        max_checks_per_task: int,
        validation_timeout_seconds: float,
        shell: str = "bash",
    ) -> None:
        self.overlay_dir = Path(overlay_dir)
        self.overlay_dir.mkdir(parents=True, exist_ok=True)
        self.cwd = None if cwd is None else Path(cwd)
        self.max_checks_per_task = max_checks_per_task
        self.validation_timeout_seconds = validation_timeout_seconds
        self.shell = shell

    def add_checks(self, task_id: str, commands: list[str]) -> RatchetApplyResult:
        overlay = self.load_overlay(task_id)
        accepted: list[str] = []
        rejected: list[RejectedCheck] = []
        seen = set(overlay.verification_commands)

        for command in commands:
            if command in seen:
                rejected.append(
                    RejectedCheck(
                        command=command,
                        reason="duplicate verification command",
                    )
                )
                continue
            if len(overlay.verification_commands) + len(accepted) >= self.max_checks_per_task:
                rejected.append(
                    RejectedCheck(
                        command=command,
                        reason=f"per-task ratchet cap of {self.max_checks_per_task} reached",
                    )
                )
                continue

            reason = self._validate_command(command)
            if reason is None:
                accepted.append(command)
                seen.add(command)
            else:
                rejected.append(RejectedCheck(command=command, reason=reason))

        if accepted:
            overlay.verification_commands.extend(accepted)
            self._save_overlay(overlay)

        return RatchetApplyResult(accepted=accepted, rejected=rejected)

    def load_overlay(self, task_id: str) -> RatchetOverlay:
        path = self.overlay_path(task_id)
        if not path.exists():
            return RatchetOverlay(task_id=task_id)

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return RatchetOverlay.model_validate(data)

    def effective_commands(self, task_id: str, original_commands: list[str]) -> list[str]:
        overlay = self.load_overlay(task_id)
        return [*original_commands, *overlay.verification_commands]

    def overlay_path(self, task_id: str) -> Path:
        return self.overlay_dir / f"{task_id}.yaml"

    def _save_overlay(self, overlay: RatchetOverlay) -> None:
        self.overlay_path(overlay.task_id).write_text(
            yaml.safe_dump(overlay.model_dump(), sort_keys=False),
            encoding="utf-8",
        )

    def _validate_command(self, command: str) -> str | None:
        preflight_reason = self._preflight_reject_reason(command)
        if preflight_reason is not None:
            return preflight_reason

        try:
            completed = subprocess.run(
                [self.shell, "-lc", command],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.validation_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "validation timed out before command executability could be confirmed"

        stderr = completed.stderr.lower()
        stdout = completed.stdout.lower()
        if self._looks_like_missing_binary(command, completed.returncode, completed.stderr):
            # Suggest a portable alternative if available
            try:
                executable = shlex.split(command)[0]
            except (ValueError, IndexError):
                executable = ""
            alt = PORTABLE_ALTERNATIVES.get(executable)
            if alt and not shutil.which(executable):
                return f"missing binary '{executable}' — portable alternative: {alt}"
            return "missing binary or command not found"
        if "syntax error" in stderr or "syntaxerror" in stderr or "syntax error" in stdout:
            return "syntax error detected during ratchet validation"
        return None

    def _preflight_reject_reason(self, command: str) -> str | None:
        if "\n" in command:
            return "multi-line ratchet commands are not allowed"
        for pattern in DANGEROUS_COMMAND_PATTERNS:
            if pattern.search(command):
                return "command uses a blocked dangerous shell pattern"
        for pattern in ENVIRONMENT_PROBE_PATTERNS:
            if pattern.match(command):
                return "environment probe commands are not allowed as persistent verification checks"
        return None

    def _looks_like_missing_binary(self, command: str, returncode: int, stderr: str) -> bool:
        if returncode == 127:
            return True
        stderr_lower = stderr.lower()
        if "command not found" not in stderr_lower and "no such file or directory" not in stderr_lower:
            return False
        try:
            executable = shlex.split(command)[0]
        except (ValueError, IndexError):
            return False
        executable_lower = executable.lower()
        return executable_lower in stderr_lower
