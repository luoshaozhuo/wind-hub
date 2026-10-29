"""统一交互式 CLI：配置、对象查询和协议诊断。"""

from __future__ import annotations

import argparse
import asyncio
import cmd
import json
import math
import shlex
import sys
from typing import Any

from core.application.errors import ConfigError

from .config_session import ConfigSession, REQUIRED_FILES
from .help import completion_words, help_text
from .inventory import devices, points, sinks
from .protocol_diagnostic import execute
from .presentation import Timer, emit, response




def _value(raw: str) -> bool | int | float | str:
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if value is None or isinstance(value, dict | list):
        raise ValueError("write value must be a scalar")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("write value must be finite")
    if isinstance(value, bool | int | float | str):
        return value
    raise ValueError("invalid write value")


def _options(
    args: list[str], default_timeout: float = 5.0
) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(prog="diagnose protocol", add_help=True)
    parser.add_argument("--interval", type=float)
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=default_timeout)
    parser.add_argument("--ca", type=int)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--yes", action="store_true")
    options, positional = parser.parse_known_args(args)
    unknown_flags = [token for token in positional if token.startswith("--")]
    if unknown_flags:
        raise ValueError(f"unrecognized options: {unknown_flags}")
    return options, positional


class WindHubShell(cmd.Cmd):
    """One CLI session; commands share a single validated configuration snapshot."""

    intro = "Wind Hub CLI. Type 'help' for commands."
    prompt = "wind-hub> "

    def __init__(self) -> None:
        super().__init__()
        self.config = ConfigSession()
        self._interactive = False
        self._format = "table"
        self._timeout = 5.0

    def default(self, line: str) -> None:
        try:
            tokens = shlex.split(line)
            if not tokens:
                return
            timer = Timer()
            result = self.dispatch(tokens)
            if result is not None:
                loaded = self.config._current
                fingerprint = loaded.fingerprint if loaded is not None else None
                emit(
                    response(" ".join(tokens[:3]), result,
                             fingerprint=fingerprint, duration_ms=timer.elapsed_ms()),
                    format_name=self._format,
                )
        except (ConfigError, ValueError, OSError, RuntimeError, KeyError) as exc:
            error = {"status": "FAIL", "code": "CLI_ERROR",
                     "message": f"{type(exc).__name__}: {exc}"}
            emit(response("command", error, fingerprint=None, duration_ms=0),
                 format_name=self._format)
        except KeyboardInterrupt:
            print("\nInterrupted.")

    def _candidates(self) -> list[str]:
        words = completion_words()
        loaded = self.config._current
        if loaded is not None:
            snapshot = loaded.snapshot
            words.extend(str(key) for key in snapshot.site.devices)
            words.extend(str(key) for key in snapshot.sinks)
            words.extend(
                point_id
                for table in snapshot.point_tables.values()
                for point_id in table.points
            )
        return sorted(set(words))

    def completenames(self, text: str, *_: object) -> list[str]:
        return [item for item in self._candidates() if item.startswith(text)]

    def completedefault(self, text: str, *_: object) -> list[str]:
        return self.completenames(text)

    def preloop(self) -> None:
        super().preloop()
        try:
            import readline
            from pathlib import Path

            self._history_path = Path.home() / ".wind_hub_history"
            if self._history_path.is_file():
                readline.read_history_file(str(self._history_path))
        except (ImportError, OSError):
            pass

    def precmd(self, line: str) -> str:
        if " write " in f" {line.lower()} ":
            try:
                import readline

                count = readline.get_current_history_length()
                if count:
                    readline.remove_history_item(count - 1)
            except (ImportError, OSError, ValueError):
                pass
        return line

    def postloop(self) -> None:
        try:
            import readline

            path = getattr(self, "_history_path", None)
            if path is not None:
                readline.write_history_file(str(path))
        except (ImportError, OSError):
            pass
        super().postloop()

    def emptyline(self) -> None:
        return

    def do_help(self, arg: str) -> None:
        """Show command syntax."""
        print(help_text(shlex.split(arg)))

    def do_exit(self, arg: str) -> bool:
        """Exit interactive CLI."""
        return True

    def do_clear(self, arg: str) -> None:
        print(chr(27) + "[2J" + chr(27) + "[H", end="")

    def do_quit(self, arg: str) -> bool:
        """Exit interactive CLI."""
        return True

    def do_EOF(self, arg: str) -> bool:
        print()
        return True

    def dispatch(self, tokens: list[str]) -> object:
        action, *rest = tokens
        if action == "help":
            print(help_text(rest))
            return None
        if "--help" in tokens:
            print(help_text([token for token in tokens if token != "--help"]))
            return None
        if action == "config":
            if not rest:
                raise ValueError(
                    "usage: config validate PATH | read PATH | reload | diff PATH | show"
                )
            operation, *args = rest
            if operation == "validate":
                if len(args) == 3 and args[1:] == ["--scope", "all"]:
                    args = args[:1]
                if len(args) != 1:
                    raise ValueError("usage: config validate PATH [--scope all]")
            if operation in {"read", "diff"} and len(args) != 1:
                raise ValueError(f"usage: config {operation} PATH")
            if operation in {"reload", "show"} and args:
                raise ValueError(f"usage: config {operation}")
            if operation == "read":
                loaded = self.config.read(args[0])
                return {
                    "status": "PASS",
                    "path": str(loaded.path),
                    "fingerprint": loaded.fingerprint,
                }
            if operation == "validate":
                from pathlib import Path

                base = Path(args[0]).expanduser().resolve()
                if not base.is_dir():
                    return {
                        "status": "FAIL",
                        "code": "CONFIG_DIRECTORY_NOT_FOUND",
                        "path": str(base),
                        "message": "Configuration directory does not exist",
                    }
                missing = [name for name in REQUIRED_FILES if not (base / name).is_file()]
                if missing:
                    return {
                        "status": "FAIL",
                        "code": "CONFIG_FILE_MISSING",
                        "path": str(base),
                        "missing_files": missing,
                        "message": "Required configuration files are missing",
                    }
                try:
                    loaded = self.config.validate(base)
                except Exception as exc:
                    return {
                        "status": "FAIL",
                        "code": "CONFIG_INVALID",
                        "path": str(base),
                        "message": f"Configuration validation failed ({type(exc).__name__})",
                    }
                return {
                    "status": "PASS",
                    "path": str(loaded.path),
                    "fingerprint": loaded.fingerprint,
                }
            if operation == "reload":
                loaded = self.config.reload()
                return {
                    "status": "PASS",
                    "path": str(loaded.path),
                    "fingerprint": loaded.fingerprint,
                }
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
                        "devices": len(loaded.snapshot.site.devices),
                        "sinks": len(loaded.snapshot.sinks),
                        "loaded_at": loaded.loaded_at.isoformat(),
                        "last_reload": "FAIL" if self.config.reload_failed else "PASS"}
            raise ValueError(f"unsupported config command: {operation}")
        if action in {"devices", "points", "sinks"}:
            loaded = self.config.current
            rest = [token for token in rest if token != "--verbose"]
            if action == "devices":
                return devices(loaded.snapshot, rest[0] if rest else None)
            if action == "sinks":
                return sinks(loaded.snapshot, rest[0] if rest else None)
            if not 1 <= len(rest) <= 2:
                raise ValueError("usage: points DEVICE [POINT]")
            return points(loaded.snapshot, rest[0], rest[1] if len(rest) == 2 else None)
        if action == "gateways":
            self.config.current
            return {
                "status": "UNKNOWN",
                "code": "NOT_DEFINED",
                "message": "Gateway inventory is not defined by the current ConfigSnapshot",
                "items": [],
            }
        if action == "clear":
            print(chr(27) + "[2J" + chr(27) + "[H", end="")
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
            default_timeout = 1.0 if operation == "ping" else 3.0
            parser.add_argument("--timeout", type=float, default=default_timeout)
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
            try:
                if operation == "tcp":
                    if port is None:
                        raise ValueError("tcp requires PORT when host is not a configured device")
                    return asyncio.run(tcp(host, port, timeout=parsed.timeout))
                if operation == "ping":
                    return asyncio.run(ping(host, count=parsed.count, timeout=parsed.timeout))
                if operation == "route":
                    return asyncio.run(route(host, timeout=parsed.timeout))
                return asyncio.run(resolve(host, timeout=parsed.timeout))
            except (OSError, TimeoutError) as exc:
                return {
                    "status": "FAIL",
                    "code": "NETWORK_PROBE_FAILED",
                    "host": host,
                    "port": port,
                    "message": f"{type(exc).__name__}: {exc}",
                }
        loaded = self.config.require_diagnosable()
        if not args or args == ["--restart"]:
            return {
                "status": "UNKNOWN",
                "code": "AUTO_DIAGNOSIS_PENDING",
                "message": (
                    "Full communication-chain diagnosis is phase 3; "
                    "manual network and protocol tools are available."
                ),
            }
        if len(args) < 3 or args[0] != "protocol":
            raise ValueError("usage: diagnose [--restart] | diagnose protocol OP DEVICE ...")
        operation = args[1]
        device_id = args[2]
        opts, positional = _options(args[3:], self._timeout)
        if operation in {"check", "gi"} and positional:
            raise ValueError(f"{operation} does not accept point IDs")
        if opts.ca is not None and operation != "gi":
            raise ValueError("--ca is only valid with gi")
        if opts.duration is not None and operation != "listen":
            raise ValueError("--duration is only valid with listen")
        if opts.count is not None and operation != "read":
            raise ValueError("--count is only valid with read")
        if operation in {"read", "read-many", "write"} and not positional:
            raise ValueError(f"{operation} requires configured point IDs")
        if operation == "write" and len(positional) != 2:
            raise ValueError("usage: diagnose protocol write DEVICE POINT VALUE")
        if operation == "read-many" and opts.interval is not None:
            raise ValueError("read-many is a single native batch operation")
        if operation in {"listen", "benchmark"}:
            from .protocol_stream import benchmark, listen

            device_info = devices(loaded.snapshot, device_id)[0]
            point_info = [points(loaded.snapshot, device_id, point_id)[0]
                          for point_id in positional]
            context = {
                "device_id": device_id,
                "protocol": device_info["protocol"],
                "endpoint": {"host": device_info["host"], "port": device_info["port"]},
                "protocol_options": device_info["protocol_options"],
                "points": point_info,
            }
            if operation == "listen":
                if opts.duration is None and self._format == "json":
                    raise ValueError("unbounded JSON listen requires --duration")
                on_sample = None
                if opts.duration is None:
                    print(json.dumps(context, ensure_ascii=False, default=str))
                    on_sample = lambda sample: print(
                        json.dumps(sample, ensure_ascii=False, allow_nan=False)
                    )
                try:
                    payload = asyncio.run(
                        listen(loaded.snapshot, device_id, positional,
                               duration=opts.duration, timeout=opts.timeout,
                               on_sample=on_sample)
                    )
                except Exception as exc:
                    return {
                        "status": "FAIL", "code": "PROTOCOL_LISTEN_FAILED",
                        **context, "message": f"{type(exc).__name__}: {exc}",
                    }
                return {"status": "PASS", **context, "data": payload}
            if not positional:
                raise ValueError("benchmark requires at least one point ID")
            try:
                payload = asyncio.run(
                    benchmark(loaded.snapshot, device_id, positional,
                              samples=opts.samples,
                              interval=0.2 if opts.interval is None else opts.interval,
                              timeout=opts.timeout)
                )
            except Exception as exc:
                return {
                    "status": "FAIL", "code": "PROTOCOL_BENCHMARK_FAILED",
                    **context, "message": f"{type(exc).__name__}: {exc}",
                }
            return {"status": "PASS", **context, "data": payload}
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
        from .inventory import devices as inventory_devices
        device_info = inventory_devices(loaded.snapshot, device_id)[0]
        selected_points = [
            points(loaded.snapshot, device_id, point_id)[0]
            for point_id in point_ids
        ]
        if opts.count is not None and not 1 <= opts.count <= 100000:
            raise ValueError("--count must be in 1..100000")
        if opts.count is not None and opts.interval is None:
            raise ValueError("--count requires --interval")
        if opts.interval is not None and not 0.1 <= opts.interval <= 3600:
            raise ValueError("--interval must be in 0.1..3600 seconds")
        streaming = operation == "read" and opts.interval is not None and opts.count is None
        if streaming and self._format == "json":
            raise ValueError("continuous JSON output requires --count")
        if streaming:
            print(json.dumps({"device_id": device_id, "endpoint": device_info,
                              "points": selected_points}, ensure_ascii=False, default=str))
        on_sample = (
            (lambda sample: print(json.dumps(sample, ensure_ascii=False, allow_nan=False)))
            if streaming
            else None
        )
        try:
            payload = asyncio.run(
                execute(
                    loaded.snapshot,
                    device_id,
                    operation,
                    point_ids=point_ids,
                    value=value,
                    interval=opts.interval,
                    count=0 if streaming else (opts.count or 1),
                    timeout=opts.timeout,
                    common_address=opts.ca,
                    on_sample=on_sample,
                )
            )
        except Exception as exc:
            return {
                "status": "FAIL",
                "code": "PROTOCOL_DIAGNOSTIC_FAILED",
                "device_id": device_id,
                "endpoint": {"host": device_info["host"], "port": device_info["port"]},
                "protocol": device_info["protocol"],
                "protocol_options": device_info["protocol_options"],
                "points": selected_points,
                "message": f"{type(exc).__name__}: {exc}",
                "data": [],
            }
        outcome = payload[0].get("outcome") if operation == "write" and payload else None
        quality_warning = any(
            str(item.get("quality", "good")).lower() != "good"
            for item in payload if "quality" in item
        )
        status = (
            "UNKNOWN" if outcome == "unknown" or (
                operation == "gi" and bool(payload)
                and payload[0].get("status") == "UNKNOWN"
            )
            else "FAIL" if outcome == "confirmed_failure"
            else "WARNING" if quality_warning
            else "PASS"
        )
        return {
            "status": status,
            "device_id": device_id,
            "protocol": device_info["protocol"],
            "endpoint": {
                "host": device_info["host"],
                "port": device_info["port"],
            },
            "protocol_options": device_info["protocol_options"],
            "points": selected_points,
            "data": payload,
        }


def main() -> int:
    shell = WindHubShell()
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--help":
        print(help_text([]))
        return 0
    if len(arguments) >= 2 and arguments[0] == "--config":
        try:
            shell.config.read(arguments[1])
        except (ConfigError, ValueError, OSError) as exc:
            print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        arguments = arguments[2:]
    if len(arguments) >= 2 and arguments[0] == "--format":
        shell._format = arguments[1]
        if shell._format not in {"table", "json"}:
            print("FAIL --format must be table or json", file=sys.stderr)
            return 2
        arguments = arguments[2:]
    if len(arguments) >= 2 and arguments[0] == "--timeout":
        try:
            shell._timeout = float(arguments[1])
            if not math.isfinite(shell._timeout) or shell._timeout <= 0:
                raise ValueError("timeout must be a positive finite number")
        except ValueError as exc:
            print(f"FAIL invalid --timeout: {exc}", file=sys.stderr)
            return 2
        arguments = arguments[2:]
    if arguments:
        timer = Timer()
        try:
            payload = shell.dispatch(arguments)
            if payload is None:
                return 0
            loaded = shell.config._current
            result = response(
                " ".join(arguments[:3]), payload,
                fingerprint=loaded.fingerprint if loaded is not None else None,
                duration_ms=timer.elapsed_ms(),
            )
            emit(result, format_name=shell._format)
            if result["status"] == "UNKNOWN":
                return 3
            if result["status"] == "FAIL":
                return 2 if arguments[:2] == ["config", "validate"] else 1
            return 0
        except (ConfigError, ValueError, OSError, RuntimeError, KeyError) as exc:
            if shell._format == "json":
                emit(
                    response(
                        " ".join(arguments[:3]),
                        {"status": "FAIL", "code": "CLI_ERROR",
                         "message": f"{type(exc).__name__}: {exc}"},
                        fingerprint=None,
                        duration_ms=timer.elapsed_ms(),
                    ),
                    format_name="json",
                )
            else:
                print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130

    shell._interactive = True
    shell.cmdloop()
    return 0
