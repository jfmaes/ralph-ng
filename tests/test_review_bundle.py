from pathlib import Path

from ralph_ng.plan import load_plan
from ralph_ng.review_bundle import ReviewBundleBuilder
from ralph_ng.verification import VerificationResult


def test_review_bundle_includes_task_diff_results_repairs_and_overlay_history(fixtures_dir: Path) -> None:
    task = load_plan(fixtures_dir / "valid_plan.yaml").tasks[0]
    builder = ReviewBundleBuilder(repo_root=fixtures_dir)
    verification_results = [
        VerificationResult(
            command="pytest tests/test_existing_module.py -x",
            exit_code=0,
            stdout="passed",
            stderr="",
            duration_seconds=0.12,
            timed_out=False,
            success=True,
        )
    ]

    bundle = builder.build(
        task,
        git_diff="diff --git a/example b/example",
        verification_results=verification_results,
        repair_notes="Address the evaluator feedback.",
        ratchet_overlay_history=["pytest tests/test_regression.py -x"],
    )

    assert bundle.task.id == "TASK-001"
    assert bundle.git_diff == "diff --git a/example b/example"
    assert bundle.verification_results[0].stdout == "passed"
    assert bundle.repair_notes == "Address the evaluator feedback."
    assert bundle.ratchet_overlay_history == ["pytest tests/test_regression.py -x"]
    assert "project/src/existing_module.py" in bundle.changed_file_snippets
    assert "def existing() -> str:" in bundle.changed_file_snippets["project/src/existing_module.py"]
