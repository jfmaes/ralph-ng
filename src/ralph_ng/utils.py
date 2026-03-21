"""Shared utility helpers."""

from __future__ import annotations

import json
from typing import Any


def coerce_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def make_human_intervention_payload(
    *,
    summary: str,
    what_failed: str,
    expected_human_actions: list[str],
    findings: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Serialize structured human-intervention context for persistence."""
    payload: dict[str, Any] = {
        "kind": "human_intervention",
        "summary": summary,
        "what_failed": what_failed,
        "expected_human_actions": expected_human_actions,
    }
    if findings:
        payload["findings"] = findings
    if metadata:
        payload["metadata"] = metadata
    return json.dumps(payload, ensure_ascii=False)


def parse_human_intervention_payload(raw: str) -> dict[str, Any] | None:
    """Parse a structured human-intervention payload, returning None on mismatch."""
    text = raw.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != "human_intervention":
        return None
    return payload
