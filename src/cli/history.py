"""Suppress sensitive or side-effecting CLI commands from interactive history."""

from __future__ import annotations

import shlex

from prompt_toolkit.history import InMemoryHistory


def is_sensitive(string: str) -> bool:
    """Whether a command line must never enter shell history."""
    try:
        parts = shlex.split(string)
    except ValueError:
        return False
    if not parts:
        return False
    if parts[0] == "commander" and len(parts) > 1 and parts[1] == "write":
        return True
    if parts[0] == "config" and len(parts) > 1 and parts[1] in {"push", "apply"}:
        return True
    return any("token" in part.lower() or "secret" in part.lower() for part in parts)


class SafeHistory(InMemoryHistory):
    """Never retain write payloads, credentials, or mutation requests.

    prompt_toolkit 3.0.53 inserts accepted lines into the recall list via
    ``append_string`` (which also fills ``_loaded_strings`` used by up-arrow
    navigation); overriding only ``store_string`` no longer hides a line, so the
    filter has to run at ``append_string`` as well.
    """

    def append_string(self, string: str) -> None:
        if is_sensitive(string):
            return
        super().append_string(string)

    def store_string(self, string: str) -> None:
        if is_sensitive(string):
            return
        super().store_string(string)
