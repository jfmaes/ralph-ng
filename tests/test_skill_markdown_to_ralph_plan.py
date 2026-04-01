from pathlib import Path
import subprocess
import sys
from textwrap import dedent

from ralph_ng.validation import validate_plan_file


def test_markdown_to_ralph_plan_generates_valid_plan_and_copies_spec(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "tests").mkdir()

    (repo / "src" / "widget.py").write_text("def widget() -> str:\n    return 'ok'\n", encoding="utf-8")
    (repo / "tests" / "test_widget.py").write_text(
        "from src.widget import widget\n\n\ndef test_widget() -> None:\n    assert widget() == 'ok'\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# Example\n", encoding="utf-8")
    (repo / "spec.md").write_text("# Spec\n\nExample spec.\n", encoding="utf-8")
    (repo / "implementation_plan.md").write_text(
        dedent(
            """
            ## Slice 1: Foundation

            ### Task 1: Add widget behavior
            - Depends on: nothing
            - Test first: `tests/test_widget.py`
            - Create:
              - `src/widget.py`
              - `tests/test_widget.py`
            - Done when:
              - Widget behavior is covered by tests.

            ## Slice 2: Follow-up

            ### Task 2: Polish widget docs
            - Modify:
              - `src/widget.py`
              - `README.md`
            - Done when:
              - Tests pass and the docs mention widget usage.
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    script_path = Path(__file__).resolve().parents[1] / "skills" / "ralph-plan-converter" / "scripts" / "markdown_to_ralph_plan.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "implementation_plan.md",
            "--output",
            "ralph-plan.yaml",
            "--repo-root",
            str(repo),
            "--spec",
            str(repo / "spec.md"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout

    plan = validate_plan_file(repo / "ralph-plan.yaml", base_dir=repo)

    assert [task.id for task in plan.tasks] == ["TASK-001", "TASK-002"]
    assert plan.tasks[1].depends_on == ["TASK-001"]
    assert plan.tasks[0].verification_commands == ["pytest tests/test_widget.py -x"]
    assert plan.tasks[1].verification_commands == ["pytest -q"]
    assert (repo / "specs" / "spec.md").read_text(encoding="utf-8") == "# Spec\n\nExample spec.\n"
