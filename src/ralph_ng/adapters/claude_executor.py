"""Backward-compatible executor adapter alias."""

from __future__ import annotations

from .transport_executor import TransportExecutorAdapter


class ClaudeExecutorAdapter(TransportExecutorAdapter):
    """Backward-compatible name for the generic transport executor adapter."""
