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
  diagnose ping HOST_OR_DEVICE [--count N] [--timeout SECONDS]
  diagnose tcp HOST_OR_DEVICE [PORT] [--timeout SECONDS]
  diagnose route HOST
  diagnose resolve HOST
  diagnose protocol check DEVICE
  diagnose protocol read DEVICE POINT [--interval SECONDS] [--count N] [--timeout SECONDS]
  diagnose protocol read-many DEVICE POINT... [--timeout SECONDS]
  diagnose protocol write DEVICE POINT VALUE [--timeout SECONDS]
  diagnose protocol gi DEVICE [--ca NUMBER] [--timeout SECONDS]
  diagnose protocol listen DEVICE [POINT...] [--duration SECONDS]
  diagnose protocol benchmark DEVICE POINT... [--samples N] [--interval SECONDS]
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
    if isinstance(value, float) and not __import__("math").isfinite(value):
        raise ValueError("write value must be finite")
    if isinstance(value, bool | int | float | str):
        return value
    raise ValueError("invalid write value")


def _options(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="diagnose protocol", add_help=True)
    parser.add_argument("--interval", type=float)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--ca", type=int)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--yes", action="store_true")
    options, positional = parser.parse_known_args(args)
    return options, positional


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

    def do_clear(self, arg: str) -> None:
        print("\\033[2J\\033[H", end="")

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
                        "devices": len(loaded.snapshot.site.devices), "sinks": len(loaded.snapshot.sinks),
                        "loaded_at": loaded.loaded_at.isoformat(),
                        "last_reload": "FAIL" if self.config.reload_failed else "PASS"}
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
            self.config.current
            return {"status": "UNKNOWN", "code": "NOT_DEFINED", "message": "Gateway inventory is not defined by the current ConfigSnapshot", "items": []}
        if action == "clear":
            print("\\033[2J\\033[H", end="")
            return None
        if action == "diagnose":
            return self._diagnose(rest)
        raise ValueError(f"unknown command: {action}. Type help.")

    def _diagnose(self, args: list[str]) -> object:
        if args and args[0] in {"tcp", "resolve", "ping", "route"}:
            from .network_diagnostic import ping, resolve, route, tcp

            operation = args[0]
            parser = argparse.ArgumentParser(prog=f"diagnose {operation}")
            parser.add_argument("host")
            if operation == "tcp":
                parser.add_argument("port", nargs="?", type=int)
            if operation == "ping":
                parser.add_argument("--count", type=int, default=4)
            parser.add_argument("--timeout", type=float, default=1.0 if operation == "ping" else 3.0)
            parsed = parser.parse_args(args[1:])
            host = parsed.host
            port = getattr(parsed, "port", None)
            if self.config._current is not None:
                from .inventory import devices

                configured = self.config.current.snapshot.site.devices
                if any(str(key) == host for key in configured):
                    info = devices(self.config.current.snapshot, host)[0]
                    host = str(info["host"])
                    if operation == "tcp" and port is None:
                        port = int(info["port"])
            if operation == "tcp":
                if port is None:
                    raise ValueError("tcp requires PORT when host is not a configured device")
                return asyncio.run(tcp(host, port, timeout=parsed.timeout))
            if operation == "ping":
                return asyncio.run(ping(host, count=parsed.count, timeout=parsed.timeout))
            if operation == "route":
                return asyncio.run(route(host, timeout=parsed.timeout))
            return asyncio.run(resolve(host, timeout=parsed.timeout))
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
        opts, positional = _options(args[3:])
        if operation in {"read", "read-many", "write"} and not positional:
            raise ValueError(f"{operation} requires configured point IDs")
        if operation == "write" and len(positional) != 2:
            raise ValueError("usage: diagnose protocol write DEVICE POINT VALUE")
        if operation in {"listen", "benchmark"}:
            from .protocol_stream import benchmark, listen

            if operation == "listen":
                return asyncio.run(
                    listen(loaded.snapshot, device_id, positional,
                           duration=opts.duration, timeout=opts.timeout)
                )
            if not positional:
                raise ValueError("benchmark requires at least one point ID")
            return asyncio.run(
                benchmark(loaded.snapshot, device_id, positional,
                          samples=opts.samples, interval=opts.interval or 0.2,
                          timeout=opts.timeout)
            )
        value = _value(positional[1]) if operation == "write" else None
        if operation == "write" and not opts.yes:
            if not getattr(self, "_interactive", False):
                raise ValueError("non-interactive write requires --yes")
            target = devices(loaded.snapshot, device_id)[0]
            point = points(loaded.snapshot, device_id, positional[0])[0]
            print("WRITE WARNING: physical device may execute this command.")
            print(json.dumps({"device": device_id, "endpoint": target,
                              "point": point, "value": value},
                             ensure_ascii=False, default=str))
            if input("Execute once? Type YES: ").strip() != "YES":
                return {"status": "SKIPPED", "message": "write not confirmed"}
        point_ids = positional[:1] if operation == "write" else positional
        return asyncio.run(
            execute(
                loaded.snapshot,
                device_id,
                operation,
                point_ids=point_ids,
                value=value,
                interval=opts.interval,
                count=0 if opts.interval is not None and opts.count == 1 else opts.count,
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
    try:
        from pathlib import Path

        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.history import FileHistory

        completer = WordCompleter(
            ["config", "validate", "read", "reload", "diff", "show",
             "devices", "points", "sinks", "gateways", "diagnose",
             "protocol", "check", "read-many", "write", "gi", "benchmark",
             "listen", "tcp", "resolve", "help", "exit"],
            ignore_case=True,
            sentence=True,
        )
        session: PromptSession[str] = PromptSession(
            history=FileHistory(str(Path.home() / ".wind_hub_history")),
            completer=completer,
            complete_while_typing=False,
        )
        shell._interactive = True
        print(shell.intro)
        while True:
            try:
                if shell.onecmd(session.prompt(shell.prompt)):
                    break
            except KeyboardInterrupt:
                print("Interrupted.")
            except EOFError:
                break
    except ImportError:
        shell.cmdloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
