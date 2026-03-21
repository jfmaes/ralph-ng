"""Environment preflight checks for ralph-ng."""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class PreflightCheck:
    name: str
    passed: bool
    message: str


@dataclass(slots=True)
class PreflightResult:
    checks: list[PreflightCheck] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def summary(self) -> str:
        lines = []
        for check in self.checks:
            icon = "PASS" if check.passed else "FAIL"
            lines.append(f"  [{icon}] {check.name}: {check.message}")
        return "\n".join(lines)


def run_preflight(
    *,
    repo_root: Path,
    config,
    plan=None,
) -> PreflightResult:
    """Run environment preflight checks before starting a run."""
    result = PreflightResult()

    # Check provider CLIs
    if config.roles is not None:
        executor_cli = config.roles.executor.cli_path
        if shutil.which(executor_cli):
            result.checks.append(PreflightCheck(
                name=f"Executor CLI ({executor_cli})",
                passed=True,
                message=f"Found at {shutil.which(executor_cli)}",
            ))
        else:
            result.checks.append(PreflightCheck(
                name=f"Executor CLI ({executor_cli})",
                passed=False,
                message=f"'{executor_cli}' not found in PATH",
            ))

        reviewer_cli = config.roles.reviewer.cli_path
        if shutil.which(reviewer_cli):
            result.checks.append(PreflightCheck(
                name=f"Reviewer CLI ({reviewer_cli})",
                passed=True,
                message=f"Found at {shutil.which(reviewer_cli)}",
            ))
        else:
            result.checks.append(PreflightCheck(
                name=f"Reviewer CLI ({reviewer_cli})",
                passed=False,
                message=f"'{reviewer_cli}' not found in PATH",
            ))
    else:
        cli = config.models.cli
        if shutil.which(cli):
            result.checks.append(PreflightCheck(
                name=f"CLI ({cli})",
                passed=True,
                message=f"Found at {shutil.which(cli)}",
            ))
        else:
            result.checks.append(PreflightCheck(
                name=f"CLI ({cli})",
                passed=False,
                message=f"'{cli}' not found in PATH",
            ))

    # Check package managers based on repo files
    pkg_managers = {
        "package.json": ["npm", "pnpm", "yarn", "bun"],
        "requirements.txt": ["pip"],
        "pyproject.toml": ["pip"],
        "Cargo.toml": ["cargo"],
        "go.mod": ["go"],
    }
    for manifest, tools in pkg_managers.items():
        if (repo_root / manifest).exists():
            found = [t for t in tools if shutil.which(t)]
            if found:
                result.checks.append(PreflightCheck(
                    name=f"Package manager for {manifest}",
                    passed=True,
                    message=f"Found: {', '.join(found)}",
                ))
            else:
                result.checks.append(PreflightCheck(
                    name=f"Package manager for {manifest}",
                    passed=False,
                    message=f"None of {tools} found in PATH",
                ))

    # Check verification command binaries from plan
    if plan is not None:
        checked_binaries: set[str] = set()
        for task in plan.tasks:
            for cmd in task.verification_commands:
                try:
                    binary = shlex.split(cmd)[0]
                except (ValueError, IndexError):
                    continue
                if binary in checked_binaries:
                    continue
                checked_binaries.add(binary)
                # Skip shell builtins and common portable tools
                if binary in ("test", "[", "echo", "true", "false", "exit", "cd", "bash", "sh"):
                    continue
                if shutil.which(binary):
                    result.checks.append(PreflightCheck(
                        name=f"Verification binary ({binary})",
                        passed=True,
                        message=f"Found at {shutil.which(binary)}",
                    ))
                else:
                    result.checks.append(PreflightCheck(
                        name=f"Verification binary ({binary})",
                        passed=False,
                        message=f"'{binary}' not found in PATH — verification commands using it will fail",
                    ))

    # Check git state
    import subprocess
    git_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if git_status.returncode == 0 and not git_status.stdout.strip():
        result.checks.append(PreflightCheck(
            name="Git workspace",
            passed=True,
            message="Clean",
        ))
    elif git_status.returncode == 0:
        result.checks.append(PreflightCheck(
            name="Git workspace",
            passed=False,
            message="Dirty — uncommitted changes may interfere with the run",
        ))
    else:
        result.checks.append(PreflightCheck(
            name="Git workspace",
            passed=False,
            message=f"git status failed: {git_status.stderr.strip()}",
        ))

    return result
