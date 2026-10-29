"""统一交互式 CLI：配置、对象查询和协议诊断。"""

from __future__ import annotations

import argparse
import asyncio
import cmd
import json
import shlex
import sys
from typing import Any

from core.application.errors import ConfigError

from .config_session import ConfigSession
from .inventory import devices, points, sinks
from .protocol_diagnostic import execute

HELP = """Commands:
  config validate PATH | read PATH | reload | diff PATH | show
  devices [DEVICE] | points DEVICE [POINT] | sinks [SINK] | gateways
  diagnose [--restart]          Run configured diagnostic pipeline
  diagnose protocol check DEVICE
  diagnose protocol read DEVICE POINT [--interval SECONDS] [--count N] [--timeout SECONDS]
  diagnose protocol read-many DEVICE POINT... [--timeout SECONDS]
  diagnose protocol write DEVICE POINT VALUE [--timeout SECONDS]
  diagnose protocol gi DEVICE [--ca NUMBER] [--timeout SECONDS]
  help | exit
"""


def _value(raw: str) -> bool | int | float | str:
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if value is None or isinstance(value, dict | list):
        raise ValueError("write value must be a scalar")
    if isinstance(value, bool | int | float | str):
        return value
    raise ValueError("invalid write value")


def _options(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="diagnose protocol", add_help=True)
    parser.add_argument("--interval", type=float)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--ca", type=int)
    return parser.parse_args(args)


class WindHubShell(cmd.Cmd):
    """One CLI session; commands share a single validated configuration snapshot."""

    intro = "Wind Hub CLI. Type 'help' for commands."
    prompt = "wind-hub> "

    def __init__(self) -> None:
        super().__init__()
        self.config = ConfigSession()
        self.last_results: list[dict[str, Any]] = []
        self._failed_device: str | None = None
        self._failed_fingerprint: str | None = None

    def default(self, line: str) -> None:
        try:
            tokens = shlex.split(line)
            if not tokens:
                return
            result = self.dispatch(tokens)
            if result is not None:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        except (ConfigError, ValueError, OSError, RuntimeError, KeyError) as exc:
            print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nInterrupted.")

    def emptyline(self) -> None:
        return

    def do_help(self, arg: str) -> None:
        """Show command syntax."""
        print(HELP)

    def do_exit(self, arg: str) -> bool:
        """Exit interactive CLI."""
        return True

    def do_quit(self, arg: str) -> bool:
        """Exit interactive CLI."""
        return True

    def do_EOF(self, arg: str) -> bool:
        print()
        return True

    def dispatch(self, tokens: list[str]) -> object:
        action, *rest = tokens
        if action == "config":
            if not rest:
                raise ValueError("usage: config validate PATH | read PATH | reload | diff PATH | show")
            operation, *args = rest
            if operation in {"read", "validate", "diff"} and len(args) != 1:
                raise ValueError(f"usage: config {operation} PATH")
            if operation == "read":
                loaded = self.config.read(args[0])
                return {"status": "PASS", "path": str(loaded.path), "fingerprint": loaded.fingerprint}
            if operation == "validate":
                loaded = self.config.validate(args[0])
                return {"status": "PASS", "path": str(loaded.path), "fingerprint": loaded.fingerprint}
            if operation == "reload":
                loaded = self.config.reload()
                return {"status": "PASS", "path": str(loaded.path), "fingerprint": loaded.fingerprint}
            if operation == "diff":
                change = self.config.diff(args[0])
                return {"has_changes": change.has_changes, "sections": {
                    name: {"added": sorted(section.added), "removed": sorted(section.removed),
                           "changed": sorted(section.changed)}
                    for name, section in change.sections.items()
                }}
            if operation == "show":
                loaded = self.config.current
                return {"path": str(loaded.path), "fingerprint": loaded.fingerprint,
                        "devices": len(loaded.snapshot.site.devices), "sinks": len(loaded.snapshot.sinks)}
            raise ValueError(f"unsupported config command: {operation}")
        if action in {"devices", "points", "sinks"}:
            loaded = self.config.current
            if action == "devices":
                return devices(loaded.snapshot, rest[0] if rest else None)
            if action == "sinks":
                return sinks(loaded.snapshot, rest[0] if rest else None)
            if not 1 <= len(rest) <= 2:
                raise ValueError("usage: points DEVICE [POINT]")
            return points(loaded.snapshot, rest[0], rest[1] if len(rest) == 2 else None)
        if action == "gateways":
            raise ValueError("Gateway inventory is not represented in the shared ConfigSnapshot")
        if action == "diagnose":
            return self._diagnose(rest)
        raise ValueError(f"unknown command: {action}. Type help.")

    def _diagnose(self, args: list[str]) -> object:
        loaded = self.config.require_diagnosable()
        if not args or args == ["--restart"]:
            from .pipeline import run_pipeline

            start = (
                self._failed_device
                if args != ["--restart"] and loaded.fingerprint == self._failed_fingerprint
                else None
            )
            result = asyncio.run(run_pipeline(loaded.snapshot, start_device=start))
            self._failed_device = result.get("failed_device")
            self._failed_fingerprint = loaded.fingerprint
            self.last_results = result["steps"]
            return result
        if len(args) < 3 or args[0] != "protocol":
            raise ValueError("usage: diagnose [--restart] | diagnose protocol OP DEVICE ...")
        operation = args[1]
        device_id = args[2]
        positional: list[str] = []
        option_tokens: list[str] = []
        option_mode = False
        for token in args[3:]:
            if token.startswith("--"):
                option_mode = True
            (option_tokens if option_mode else positional).append(token)
        opts = _options(option_tokens)
        if operation in {"read", "read-many", "write"} and not positional:
            raise ValueError(f"{operation} requires configured point IDs")
        if operation == "write" and len(positional) != 2:
            raise ValueError("usage: diagnose protocol write DEVICE POINT VALUE")
        value = _value(positional[1]) if operation == "write" else None
        point_ids = positional[:1] if operation == "write" else positional
        return asyncio.run(
            execute(
                loaded.snapshot,
                device_id,
                operation,
                point_ids=point_ids,
                value=value,
                interval=opts.interval,
                count=opts.count,
                timeout=opts.timeout,
                common_address=opts.ca,
            )
        )


def main() -> int:
    shell = WindHubShell()
    if len(sys.argv) > 1:
        try:
            result = shell.dispatch(sys.argv[1:])
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        except (ConfigError, ValueError, OSError, RuntimeError) as exc:
            print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    shell.cmdloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
