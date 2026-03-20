import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from ralph_ng.adapters.base import InvocationStatus
from ralph_ng.adapters.claude_evaluator import ClaudeEvaluatorAdapter


def make_stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def verdict_text(payload: dict) -> str:
    return json.dumps(payload)


def test_evaluator_parses_accept_verdict_and_tokens(tmp_path: Path) -> None:
    stdout = make_stream(
        {
            "type": "content_block_delta",
            "delta": {
                "type": "text_delta",
                "text": verdict_text({"decision": "accept", "summary": "Looks good"}),
            },
        },
        {"type": "message_delta", "usage": {"input_tokens": 20, "output_tokens": 15}},
    )
    completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")

    with patch(
        "ralph_ng.adapters.claude_evaluator.subprocess.run",
        return_value=completed,
    ):
        adapter = ClaudeEvaluatorAdapter(model="opus")
        result = adapter.evaluate("bundle text", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.COMPLETED
    assert result.verdict is not None
    assert result.verdict.decision == "accept"
    assert result.verdict.summary == "Looks good"
    assert result.input_tokens == 20
    assert result.output_tokens == 15


def test_evaluator_parses_reject_verdict_with_new_checks(tmp_path: Path) -> None:
    stdout = make_stream(
        {
            "type": "content_block_delta",
            "delta": {
                "text": verdict_text(
                    {
                        "decision": "reject",
                        "summary": "Missing regression coverage.",
                        "repair_notes": "Add a regression test for the edge case.",
                        "new_verification_commands": ["pytest tests/test_regression.py -x"],
                    }
                )
            },
        }
    )
    completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")

    with patch(
        "ralph_ng.adapters.claude_evaluator.subprocess.run",
        return_value=completed,
    ):
        adapter = ClaudeEvaluatorAdapter(model="opus")
        result = adapter.evaluate("bundle text", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.COMPLETED
    assert result.verdict is not None
    assert result.verdict.decision == "reject"
    assert result.verdict.repair_notes == "Add a regression test for the edge case."
    assert result.verdict.new_verification_commands == ["pytest tests/test_regression.py -x"]


def test_evaluator_retries_on_malformed_output(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    outputs = [
        subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="plain text\n", stderr=""),
        subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=make_stream(
                {
                    "type": "content_block_delta",
                    "delta": {"text": verdict_text({"decision": "accept", "summary": "ok"})},
                }
            ),
            stderr="",
        ),
    ]

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return outputs[len(calls) - 1]

    with patch("ralph_ng.adapters.claude_evaluator.subprocess.run", side_effect=fake_run):
        adapter = ClaudeEvaluatorAdapter(model="opus", max_retries=1)
        result = adapter.evaluate("bundle text", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.COMPLETED
    assert result.verdict is not None
    assert result.verdict.decision == "accept"
    assert len(calls) == 2
    assert any("invalid JSON verdict" in part for part in calls[1])


def test_evaluator_rejects_schema_invalid_reject_verdict(tmp_path: Path) -> None:
    stdout = make_stream(
        {
            "type": "content_block_delta",
            "delta": {
                "text": verdict_text(
                    {
                        "decision": "reject",
                        "summary": "Needs more work",
                        "repair_notes": "",
                        "new_verification_commands": [],
                    }
                )
            },
        }
    )
    completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")

    with patch(
        "ralph_ng.adapters.claude_evaluator.subprocess.run",
        return_value=completed,
    ):
        adapter = ClaudeEvaluatorAdapter(model="opus", max_retries=0)
        result = adapter.evaluate("bundle text", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.MALFORMED_OUTPUT
    assert result.verdict is None
