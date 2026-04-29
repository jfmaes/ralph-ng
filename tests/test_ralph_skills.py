from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1] / "skills"
RALPH_SKILLS = [
    "init-ralph",
    "ralph-plan-converter",
    "ralph-plan-repair",
    "ralph-run-triage",
]


def test_ralph_skills_bootstrap_and_activate_local_venv() -> None:
    for skill_name in RALPH_SKILLS:
        skill_text = (SKILL_DIR / skill_name / "SKILL.md").read_text(encoding="utf-8")

        assert "/home/jenkins/projects/ralph-ng" not in skill_text, skill_name
        assert "command -v ralph" in skill_text, skill_name
        assert "Set RALPH_NG_ROOT=/path/to/ralph-ng" in skill_text, skill_name
        assert "python3 -m venv" in skill_text, skill_name
        assert ".venv/bin/pip" in skill_text, skill_name
        assert "source \"$RALPH_NG_ROOT/.venv/bin/activate\"" in skill_text, skill_name
        assert "ralph --help" in skill_text, skill_name
