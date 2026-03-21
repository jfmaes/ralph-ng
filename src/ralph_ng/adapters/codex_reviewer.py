"""Backward-compatible reviewer adapter alias."""

from __future__ import annotations

from .structured_reviewer import StructuredReviewerAdapter


class CodexReviewerAdapter(StructuredReviewerAdapter):
    """Backward-compatible name for the generic structured reviewer adapter."""
