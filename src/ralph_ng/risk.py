"""Diff risk classification — orchestrator-computed, never self-reported.

Risk is determined from the diff before review invocation based on:
- Line count (size-based tiers)
- File count (multiplier)
- Path matching (security/high-risk overrides)
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from fnmatch import fnmatch
from typing import Sequence


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class _RiskOrd(IntEnum):
    """Ordinal values for risk comparison and bumping."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3


_LEVEL_TO_ORD = {
    RiskLevel.LOW: _RiskOrd.LOW,
    RiskLevel.MEDIUM: _RiskOrd.MEDIUM,
    RiskLevel.HIGH: _RiskOrd.HIGH,
    RiskLevel.CRITICAL: _RiskOrd.CRITICAL,
}

_ORD_TO_LEVEL = {v: k for k, v in _LEVEL_TO_ORD.items()}


# Built-in defaults — config extends these, `!pattern` removes.
DEFAULT_SECURITY_PATHS: frozenset[str] = frozenset(
    {
        "*/auth/*",
        "*/crypto/*",
        "*/security/*",
        "*/secrets/*",
        "*/.env*",
        "*/credentials*",
        "*/token*",
        "*/permission*",
        "*/acl/*",
    }
)

DEFAULT_HIGH_RISK_PATHS: frozenset[str] = frozenset(
    {
        "*/migration*",
        "*/schema*",
        "*/database/*",
        "*/db/*",
        "*/deploy/*",
        "*/ci/*",
        "*/.github/*",
        "*/Dockerfile*",
        "*/docker-compose*",
        "*/infra/*",
        "*/terraform/*",
    }
)


def _merge_patterns(
    builtin: frozenset[str],
    config_patterns: Sequence[str] | None,
) -> set[str]:
    """Merge config patterns with built-in defaults.

    Config entries extend the defaults. Prefix a pattern with ``!`` to
    remove a built-in.
    """
    result = set(builtin)
    if config_patterns is None:
        return result
    for pattern in config_patterns:
        if pattern.startswith("!"):
            result.discard(pattern[1:])
        else:
            result.add(pattern)
    return result


def _matches_any(path: str, patterns: set[str]) -> bool:
    for pattern in patterns:
        if fnmatch(path, pattern):
            return True
    return False


def _bump(level: RiskLevel, steps: int = 1) -> RiskLevel:
    """Bump risk level up by N steps, clamped at CRITICAL."""
    current = _LEVEL_TO_ORD[level]
    new = min(current + steps, _RiskOrd.CRITICAL)
    return _ORD_TO_LEVEL[_RiskOrd(new)]


def classify_diff_risk(
    diff_lines: int,
    changed_files: Sequence[str],
    *,
    security_paths: Sequence[str] | None = None,
    high_risk_paths: Sequence[str] | None = None,
) -> RiskLevel:
    """Classify the risk of a diff.

    Parameters
    ----------
    diff_lines:
        Number of added+removed lines in the diff.
    changed_files:
        List of changed file paths (relative to repo root).
    security_paths:
        Config overrides for security path patterns.
    high_risk_paths:
        Config overrides for high-risk path patterns.
    """
    # Size-based tier
    if diff_lines > 500:
        level = RiskLevel.CRITICAL
    elif diff_lines > 200:
        level = RiskLevel.HIGH
    elif diff_lines >= 50:
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW

    # File count multiplier
    file_count = len(changed_files)
    if file_count > 15:
        level = RiskLevel.CRITICAL
    elif file_count > 8:
        if _LEVEL_TO_ORD[level] < _RiskOrd.HIGH:
            level = RiskLevel.HIGH
    elif file_count > 3:
        if _LEVEL_TO_ORD[level] < _RiskOrd.MEDIUM:
            level = RiskLevel.MEDIUM

    # Path-based overrides
    sec_patterns = _merge_patterns(DEFAULT_SECURITY_PATHS, security_paths)
    hr_patterns = _merge_patterns(DEFAULT_HIGH_RISK_PATHS, high_risk_paths)

    for path in changed_files:
        if _matches_any(path, sec_patterns):
            return RiskLevel.CRITICAL  # security paths force critical
        if _matches_any(path, hr_patterns):
            level = _bump(level, 1)

    return level


def count_diff_lines(diff_text: str) -> int:
    """Count added+removed lines from a unified diff."""
    count = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            count += 1
        elif line.startswith("-") and not line.startswith("---"):
            count += 1
    return count


def extract_changed_files(diff_text: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    files: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            if path and path not in files:
                files.append(path)
        elif line.startswith("--- a/"):
            path = line[6:]
            if path and path not in files:
                files.append(path)
    return files
