"""Tests for diff risk classification."""

from __future__ import annotations

import pytest

from ralph_ng.risk import (
    DEFAULT_HIGH_RISK_PATHS,
    DEFAULT_SECURITY_PATHS,
    RiskLevel,
    classify_diff_risk,
    count_diff_lines,
    extract_changed_files,
)


class TestSizeBasedTiers:
    def test_low_risk(self) -> None:
        assert classify_diff_risk(10, ["src/foo.py"]) == RiskLevel.LOW

    def test_medium_risk(self) -> None:
        assert classify_diff_risk(50, ["src/foo.py"]) == RiskLevel.MEDIUM

    def test_high_risk(self) -> None:
        assert classify_diff_risk(201, ["src/foo.py"]) == RiskLevel.HIGH

    def test_critical_risk(self) -> None:
        assert classify_diff_risk(501, ["src/foo.py"]) == RiskLevel.CRITICAL

    def test_boundary_49_is_low(self) -> None:
        assert classify_diff_risk(49, ["src/foo.py"]) == RiskLevel.LOW

    def test_boundary_200_is_medium(self) -> None:
        assert classify_diff_risk(200, ["src/foo.py"]) == RiskLevel.MEDIUM

    def test_boundary_500_is_high(self) -> None:
        assert classify_diff_risk(500, ["src/foo.py"]) == RiskLevel.HIGH


class TestFileCountMultiplier:
    def test_4_files_bumps_low_to_medium(self) -> None:
        files = [f"src/f{i}.py" for i in range(4)]
        assert classify_diff_risk(10, files) == RiskLevel.MEDIUM

    def test_9_files_bumps_to_high(self) -> None:
        files = [f"src/f{i}.py" for i in range(9)]
        assert classify_diff_risk(10, files) == RiskLevel.HIGH

    def test_16_files_forces_critical(self) -> None:
        files = [f"src/f{i}.py" for i in range(16)]
        assert classify_diff_risk(10, files) == RiskLevel.CRITICAL

    def test_3_files_stays_low(self) -> None:
        files = [f"src/f{i}.py" for i in range(3)]
        assert classify_diff_risk(10, files) == RiskLevel.LOW


class TestPathOverrides:
    def test_security_path_forces_critical(self) -> None:
        assert classify_diff_risk(5, ["src/auth/login.py"]) == RiskLevel.CRITICAL

    @pytest.mark.parametrize(
        "path",
        [
            "auth/login.py",
            ".env",
            ".env.local",
        ],
    )
    def test_root_level_security_paths_force_critical(self, path: str) -> None:
        assert classify_diff_risk(5, [path]) == RiskLevel.CRITICAL

    def test_high_risk_path_bumps_one(self) -> None:
        assert classify_diff_risk(10, ["src/migration_001.py"]) == RiskLevel.MEDIUM

    @pytest.mark.parametrize(
        "path",
        [
            "Dockerfile",
            "Dockerfile.dev",
            ".github/workflows/ci.yml",
        ],
    )
    def test_root_level_high_risk_paths_bump_one(self, path: str) -> None:
        assert classify_diff_risk(10, [path]) == RiskLevel.MEDIUM

    def test_high_risk_path_bumps_medium_to_high(self) -> None:
        assert classify_diff_risk(50, ["src/migration_001.py"]) == RiskLevel.HIGH

    def test_multiple_high_risk_bumps(self) -> None:
        files = ["src/migration_001.py", "infra/deploy/script.sh"]
        # 10 lines = LOW, but 2 high-risk files bump +1 each = HIGH
        assert classify_diff_risk(10, files) == RiskLevel.HIGH

    def test_security_beats_all(self) -> None:
        # Even with 1 line, security path → CRITICAL
        assert classify_diff_risk(1, ["config/secrets/api_key.txt"]) == RiskLevel.CRITICAL


class TestConfigOverrides:
    def test_custom_security_pattern(self) -> None:
        assert classify_diff_risk(
            5,
            ["src/custom_vault/key.py"],
            security_paths=["*/custom_vault/*"],
        ) == RiskLevel.CRITICAL

    def test_remove_builtin_security_pattern(self) -> None:
        # Remove auth pattern, so auth path is no longer security-critical
        result = classify_diff_risk(
            5,
            ["src/auth/login.py"],
            security_paths=["!*/auth/*"],
        )
        assert result == RiskLevel.LOW

    def test_custom_high_risk_pattern(self) -> None:
        result = classify_diff_risk(
            10,
            ["src/custom_risky/thing.py"],
            high_risk_paths=["*/custom_risky/*"],
        )
        assert result == RiskLevel.MEDIUM  # bumped from LOW

    def test_remove_builtin_high_risk_pattern(self) -> None:
        result = classify_diff_risk(
            10,
            ["src/migration_001.py"],
            high_risk_paths=["!*/migration*"],
        )
        assert result == RiskLevel.LOW


class TestDefaultPatterns:
    def test_security_defaults_exist(self) -> None:
        assert "*/auth/*" in DEFAULT_SECURITY_PATHS
        assert "*/crypto/*" in DEFAULT_SECURITY_PATHS
        assert "*/secrets/*" in DEFAULT_SECURITY_PATHS

    def test_high_risk_defaults_exist(self) -> None:
        assert "*/migration*" in DEFAULT_HIGH_RISK_PATHS
        assert "*/deploy/*" in DEFAULT_HIGH_RISK_PATHS
        assert "*/.github/*" in DEFAULT_HIGH_RISK_PATHS


class TestDiffHelpers:
    def test_count_diff_lines(self) -> None:
        diff = """\
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 unchanged
-removed
+added1
+added2
"""
        assert count_diff_lines(diff) == 3  # 1 removed + 2 added

    def test_count_diff_lines_empty(self) -> None:
        assert count_diff_lines("") == 0

    def test_extract_changed_files(self) -> None:
        diff = """\
--- a/foo.py
+++ b/foo.py
@@ -1 +1 @@
-old
+new
--- a/bar.py
+++ b/bar.py
@@ -1 +1 @@
-old
+new
"""
        files = extract_changed_files(diff)
        assert "foo.py" in files
        assert "bar.py" in files

    def test_extract_changed_files_empty(self) -> None:
        assert extract_changed_files("") == []
