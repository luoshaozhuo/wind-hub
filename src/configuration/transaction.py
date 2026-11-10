"""Shared filesystem commit marker understood by config readers and writers."""

from __future__ import annotations

COMMIT_MARKER = ".commander-config-transaction.json"
COMMIT_STORE = ".commander-config-transactions"

__all__ = ["COMMIT_MARKER", "COMMIT_STORE"]
