"""Self-healing recovery engine for ralph-ng.

Launches bounded diagnosis sessions when tasks fail with recoverable
failure classes. Uses the opposite provider from the one that failed
for cross-model diagnosis.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import RalphConfig, SelfHealingConfig
from .events import EventLogger
from .failure import FailureClass, RECOVERY_POLICY
from .prompts.diagnostic_debugger import build_diagnosis_prompt, DIAGNOSIS_JSON_SCHEMA
from .state import StateStore


# Auto-fix commands that are NEVER allowed
BLOCKED_COMMAND_PREFIXES = (
    "sudo ",
    "apt ",
    "apt-get ",
    "brew ",
    "yum ",
    "dnf ",
    "pacman ",
    "snap ",
    "git reset --hard",
    "git clean",
    "rm -rf /",
)

SHELL_METACHARACTER_PATTERN = re.compile(r"[;&|<>`$()]")

SAFE_AUTO_FIX_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("npm", "install"),
    ("npm", "ci"),
    ("pnpm", "install"),
    ("yarn", "install"),
    ("pip", "install"),
    ("pip3", "install"),
    ("uv", "sync"),
    ("uv", "pip", "install"),
    ("poetry", "install"),
    ("python", "-m", "pip", "install"),
    ("python3", "-m", "pip", "install"),
)

DANGEROUS_AUTO_FIX_PATTERNS = (
    re.compile(r"\brm\s+-[^\s]*r[^\s]*f?[^\s]*\s+(?:\.git|/|\.)\b", re.IGNORECASE),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\b", re.IGNORECASE),
)


@dataclass(slots=True)
class DiagnosisResult:
    """Result of a self-healing diagnosis session."""
    failure_class: str
    root_cause_summary: str
    confidence: float
    can_auto_fix: bool
    auto_fix_commands: list[str]
    human_actions: list[str]
    notes_for_resume: str
    raw_output: str = ""


@dataclass(slots=True)
class RecoveryOutcome:
    """Outcome of a self-healing recovery attempt."""
    attempted: bool
    succeeded: bool
    diagnosis: DiagnosisResult | None
    actions_taken: list[str]
    summary: str


def is_failure_eligible_for_diagnosis(failure_class: FailureClass) -> bool:
    """Check if a failure class is eligible for self-healing diagnosis."""
    eligible = {
        FailureClass.PROVIDER_CRASH,
        FailureClass.PROVIDER_TIMEOUT,
        FailureClass.PROVIDER_SCHEMA_ERROR,
        FailureClass.PROVIDER_MALFORMED_OUTPUT,
        FailureClass.VERIFICATION_MISSING_BINARY,
        FailureClass.VERIFICATION_TEST_FAILURE,
        FailureClass.DEPENDENCY_MISSING_PROJECT,
    }
    return failure_class in eligible


def _validate_auto_fix_command(command: str) -> bool:
    """Check if an auto-fix command is safe to execute."""
    stripped = command.strip()
    lowered = stripped.lower()
    if not stripped or "\n" in stripped:
        return False
    if SHELL_METACHARACTER_PATTERN.search(stripped):
        return False
    for pattern in DANGEROUS_AUTO_FIX_PATTERNS:
        if pattern.search(stripped):
            return False
    for prefix in BLOCKED_COMMAND_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return False
    try:
        tokens = tuple(shlex.split(stripped))
    except ValueError:
        return False
    return any(tokens[: len(allowed)] == allowed for allowed in SAFE_AUTO_FIX_COMMANDS)


def _run_auto_fix_command(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: float = 60.0,
) -> tuple[bool, str]:
    """Run a single auto-fix command. Returns (success, output)."""
    try:
        result = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output[:1000]
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout_seconds}s"
    except OSError as e:
        return False, str(e)


def run_diagnosis_session(
    *,
    config: SelfHealingConfig,
    task_id: str,
    task_title: str,
    task_description: str,
    failure_class: str,
    error_message: str,
    stderr_summary: str = "",
    verification_logs: list[dict] | None = None,
    workspace_diff_summary: str = "",
    dependency_manifests: dict[str, str] | None = None,
    cwd: Path,
    events: EventLogger | None = None,
) -> DiagnosisResult | None:
    """Launch a diagnosis session using the configured provider.

    Returns a DiagnosisResult or None if diagnosis fails.
    """
    prompt = build_diagnosis_prompt(
        task_id=task_id,
        task_title=task_title,
        task_description=task_description,
        failure_class=failure_class,
        error_message=error_message,
        stderr_summary=stderr_summary,
        verification_logs=verification_logs,
        workspace_diff_summary=workspace_diff_summary,
        dependency_manifests=dependency_manifests,
    )

    # Build the CLI command based on provider
    if config.diagnosis_provider == "claude":
        command = [
            config.diagnosis_cli_path,
            "-p", prompt,
            "--model", config.diagnosis_model,
            "--output-format", "text",
        ]
        if config.disable_plugins_and_slash_commands:
            command.append("--disable-slash-commands")
    elif config.diagnosis_provider == "codex":
        command = [
            config.diagnosis_cli_path,
            "exec",
            prompt,
            "--model", config.diagnosis_model,
        ]
        if config.disable_plugins_and_slash_commands:
            command[1:1] = ["--disable", "plugins"]
    else:
        return None

    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=config.diagnosis_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None

    if result.returncode != 0:
        return None

    output = result.stdout.strip()
    if not output:
        return None

    # Parse the diagnosis output
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        # Try to find JSON in the output
        start = output.find("{")
        end = output.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(output[start:end])
            except json.JSONDecodeError:
                return None
        else:
            return None

    return DiagnosisResult(
        failure_class=data.get("failure_class", failure_class),
        root_cause_summary=data.get("root_cause_summary", "Unknown"),
        confidence=float(data.get("confidence", 0.0)),
        can_auto_fix=bool(data.get("can_auto_fix", False)),
        auto_fix_commands=data.get("auto_fix_commands", []),
        human_actions=data.get("human_actions", []),
        notes_for_resume=data.get("notes_for_resume", ""),
        raw_output=output,
    )


def attempt_self_healing(
    *,
    config: SelfHealingConfig,
    task_id: str,
    task_title: str,
    task_description: str,
    failure_class: FailureClass,
    error_message: str,
    stderr_summary: str = "",
    verification_logs: list[dict] | None = None,
    workspace_diff_summary: str = "",
    dependency_manifests: dict[str, str] | None = None,
    cwd: Path,
    store: StateStore | None = None,
    run_id: str = "",
    events: EventLogger | None = None,
) -> RecoveryOutcome:
    """Attempt to self-heal a failed task.

    1. Run a diagnosis session
    2. If diagnosis says auto-fixable, run the fix commands
    3. Return the outcome
    """
    if not config.enabled:
        return RecoveryOutcome(
            attempted=False, succeeded=False, diagnosis=None,
            actions_taken=[], summary="Self-healing disabled in config",
        )

    if not is_failure_eligible_for_diagnosis(failure_class):
        return RecoveryOutcome(
            attempted=False, succeeded=False, diagnosis=None,
            actions_taken=[],
            summary=f"Failure class {failure_class} is not eligible for self-healing",
        )

    if store is not None and run_id:
        try:
            row = store.connection.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM recovery_attempts
                WHERE run_id = ? AND task_id = ? AND recovery_kind = 'diagnosis'
                """,
                (run_id, task_id),
            ).fetchone()
            previous_attempts = 0 if row is None else int(row["cnt"])
        except Exception:
            previous_attempts = 0
        if previous_attempts >= config.max_diagnosis_per_task:
            return RecoveryOutcome(
                attempted=False,
                succeeded=False,
                diagnosis=None,
                actions_taken=[],
                summary=(
                    f"Diagnosis budget exhausted for {task_id}: "
                    f"{previous_attempts}/{config.max_diagnosis_per_task} attempts used"
                ),
            )

    if events:
        events.log("self_healing.diagnosis_started", task_id=task_id, data={
            "run_id": run_id, "failure_class": failure_class.value,
        })

    diagnosis = run_diagnosis_session(
        config=config,
        task_id=task_id,
        task_title=task_title,
        task_description=task_description,
        failure_class=failure_class.value,
        error_message=error_message,
        stderr_summary=stderr_summary,
        verification_logs=verification_logs,
        workspace_diff_summary=workspace_diff_summary,
        dependency_manifests=dependency_manifests,
        cwd=cwd,
        events=events,
    )

    if diagnosis is None:
        if events:
            events.log("self_healing.diagnosis_failed", task_id=task_id, data={
                "run_id": run_id, "reason": "Diagnosis session produced no result",
            })
        return RecoveryOutcome(
            attempted=True, succeeded=False, diagnosis=None,
            actions_taken=["diagnosis_attempted"],
            summary="Diagnosis session failed to produce a result",
        )

    if events:
        events.log("self_healing.diagnosis_completed", task_id=task_id, data={
            "run_id": run_id,
            "can_auto_fix": diagnosis.can_auto_fix,
            "confidence": diagnosis.confidence,
            "root_cause": diagnosis.root_cause_summary,
        })

    if not diagnosis.can_auto_fix:
        return RecoveryOutcome(
            attempted=True, succeeded=False, diagnosis=diagnosis,
            actions_taken=["diagnosis_completed"],
            summary=f"Diagnosis: {diagnosis.root_cause_summary}. Cannot auto-fix. "
                     f"Human actions: {'; '.join(diagnosis.human_actions)}",
        )

    # Attempt auto-fix
    actions_taken = ["diagnosis_completed"]
    all_succeeded = True
    max_actions = config.max_auto_fix_actions_per_task

    for i, command in enumerate(diagnosis.auto_fix_commands[:max_actions]):
        if not _validate_auto_fix_command(command):
            actions_taken.append(f"BLOCKED: {command}")
            all_succeeded = False
            continue

        if events:
            events.log("self_healing.auto_fix_running", task_id=task_id, data={
                "run_id": run_id, "command": command, "index": i,
            })

        success, output = _run_auto_fix_command(command, cwd=cwd)
        action_desc = f"{'OK' if success else 'FAIL'}: {command}"
        actions_taken.append(action_desc)

        if events:
            events.log("self_healing.auto_fix_result", task_id=task_id, data={
                "run_id": run_id, "command": command, "success": success,
            })

        if not success:
            all_succeeded = False
            break

    # Persist recovery attempt if store available
    if store and run_id:
        try:
            store.connection.execute(
                """INSERT INTO recovery_attempts
                   (run_id, task_id, attempt_number, failure_class, recovery_kind, status, summary, created_at)
                   VALUES (?, ?, 1, ?, 'diagnosis', ?, ?, ?)""",
                (run_id, task_id, failure_class.value,
                 "succeeded" if all_succeeded else "failed",
                 "; ".join(actions_taken),
                 __import__("ralph_ng.state", fromlist=["utc_now"]).utc_now()),
            )
            store.connection.commit()
        except Exception:
            pass  # recovery_attempts table may not exist yet

    return RecoveryOutcome(
        attempted=True,
        succeeded=all_succeeded,
        diagnosis=diagnosis,
        actions_taken=actions_taken,
        summary=f"Auto-fix {'succeeded' if all_succeeded else 'failed'}: {diagnosis.root_cause_summary}",
    )
