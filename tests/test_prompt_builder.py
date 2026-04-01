from pathlib import Path

from ralph_ng.plan import Task, load_plan
from ralph_ng.prompt_builder import PromptBuilder


def test_prompt_includes_task_contract_spec_file_contents_and_overlays(fixtures_dir: Path) -> None:
    plan = load_plan(fixtures_dir / "valid_plan.yaml")
    task = plan.tasks[0]
    builder = PromptBuilder(
        repo_root=fixtures_dir,
        spec_paths=[fixtures_dir / "sample_spec.md"],
    )

    prompt = builder.build(
        task,
        repair_notes="Tighten the implementation and keep tests green.",
        ratchet_overlays=["pytest tests/test_regression.py -x"],
    )

    assert "TASK-001" in prompt
    assert "Update existing module" in prompt
    assert "The existing module is updated." in prompt
    assert "pytest tests/test_existing_module.py -x" in prompt
    assert "pytest tests/test_regression.py -x" in prompt
    assert "Tighten the implementation and keep tests green." in prompt
    assert "Sample Spec" in prompt
    assert "Improve the example implementation safely." in prompt
    assert "def existing() -> str:" in prompt


def test_prompt_mentions_missing_expected_files(fixtures_dir: Path) -> None:
    plan = load_plan(fixtures_dir / "valid_plan.yaml")
    task = plan.tasks[1]
    builder = PromptBuilder(repo_root=fixtures_dir, spec_paths=[])

    prompt = builder.build(task)

    assert "project/src/another_module.py" in prompt


def test_prompt_describes_binary_expected_files_without_crashing(tmp_path: Path) -> None:
    task = Task(
        id="TASK-001",
        title="Binary prompt",
        description="Ensure binary files do not crash executor prompt assembly.",
        acceptance_criteria=["prompt builds"],
        verification_commands=[],
        expected_files=["public/icon-192.png"],
    )
    icon_path = tmp_path / "public" / "icon-192.png"
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    icon_path.write_bytes(b"\x89PNG\r\n\x1a\nbinary-payload")
    builder = PromptBuilder(repo_root=tmp_path, spec_paths=[])

    prompt = builder.build(task)

    assert "public/icon-192.png" in prompt
    assert "(binary file: image/png, 22 bytes)" in prompt
