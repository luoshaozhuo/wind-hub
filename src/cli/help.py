"""集中定义 CLI 帮助文本。命令入口与测试以此作为公开语法索引。"""

from __future__ import annotations

GENERAL = """Wind Hub CLI
Usage:
  wind-hub [--config PATH] [--format table|json] [COMMAND ...]
  wind-hub                         Start interactive shell

Commands:
  config        Validate, load, reload, compare and inspect configuration
  devices       List configured devices and effective protocol endpoints
  points        List configured device points and native addresses
  sinks         List configured Sink outputs (credentials hidden)
  gateways      Report Gateway configuration availability
  diagnose      Run automatic diagnostics, or invoke a diagnostic tool
  help          Show general or command-specific help
  clear         Clear terminal
  exit          Exit interactive shell

Examples:
  config read ./configs/example
  devices
  points turbine01
  diagnose protocol read turbine01 active_power
  diagnose protocol read turbine01 active_power --interval 1
  help diagnose protocol write
"""

COMMANDS: dict[tuple[str, ...], str] = {
    ("config",): """Usage:
  config validate PATH [--scope all] | read PATH | reload | diff PATH | show
validate: inspect mandatory YAML files, schema and references without replacing the current snapshot
read: validate then atomically activate CLI configuration
reload: validate same directory then atomically replace CLI snapshot; failed reload blocks diagnose
diff: compare current in-memory snapshot to separately validated PATH
show: display active path, fingerprint, load time and reload status
""",
    ("config", "validate"): """Usage: config validate PATH [--scope all]
PATH: local configuration directory; --scope all and --scope=all are equivalent.
Default scope all checks all eight Core YAML files
Output: missing files, failed schema/reference checks, and validation verdict
No config session changes. Exit: 0 PASS; 2 invalid config/path.
""",
    ("config", "read"): """Usage: config read PATH
Loads only when all required files and cross-references validate.
An invalid path never replaces an earlier valid snapshot.
""",
    ("config", "reload"): """Usage: config reload
Reload currently selected directory; validation before any session change.
On failure retain old snapshot for inspection and block automatic diagnose.
""",
    ("config", "diff"): """Usage: config diff PATH
Compare the active memory snapshot against independently validated PATH.
No file writes or active configuration changes.
""",
    ("devices",): """Usage:
  devices [DEVICE_ID] [--verbose]
  Shows effective protocol, host, port and connection options (secrets redacted).""",
    ("points",): """Usage:
  points DEVICE_ID [POINT_ID] [--verbose]
  Shows source point access, protocol address and data type.""",
    ("sinks",): """Usage:
  sinks [SINK_ID] [--verbose]
  Shows Sink kinds, enabled flag and safe public endpoint parameters.""",
    ("gateways",): """Usage:
  gateways [GATEWAY_ID]
  At most one Gateway ID is accepted.
  Current Core ConfigSnapshot has no Gateway collection; result NOT_DEFINED.""",
    ("diagnose",): """Usage:
  diagnose [--restart]
  diagnose result
  diagnose report PATH.json
  diagnose ping/tcp/route/resolve ...
  diagnose protocol OP DEVICE ...
No arguments: execute ordered read-only configuration-driven communication checks.
Manual network commands can work before loading config; protocol tools require valid config.
""",
    ("diagnose", "result"): """Usage: diagnose result
Display the previous automatic diagnosis result stored in this CLI session.
""",
    ("diagnose", "report"): """Usage: diagnose report PATH.json
Atomically export the previous automatic diagnosis result as JSON.
Does not invoke new checks. The parent directory must already exist.
""",
    ("diagnose", "protocol"): """Usage: diagnose protocol OP DEVICE_ID ...
Operations: check, read, read-many, write, listen, gi, benchmark.
All protocol operations use effective configuration and private Driver sessions.
No arbitrary address is accepted by a protocol tool.
""",
    ("diagnose", "protocol", "read"): """Usage:
  diagnose protocol read DEVICE_ID POINT_ID
    [--interval SECONDS] [--count N] [--timeout SECONDS]
--interval: 0.1..3600 s; omitted means one read.
--count: 1..100000, only meaningful with --interval; omitted means repeat until Ctrl+C.
Reads with one connected Driver session; Ctrl+C returns to shell.
Example: diagnose protocol read turbine01 active_power --interval 1 --count 10
""",
    ("diagnose", "protocol", "read-many"): """Usage:
  diagnose protocol read-many DEVICE_ID POINT_ID [POINT_ID ...] [--timeout SECONDS]
Requires native READ_MANY capability. Cannot emulate with repeated read_one calls.
""",
    ("diagnose", "protocol", "check"): """Usage:
  diagnose protocol check DEVICE_ID [--timeout SECONDS]
Connect, check protocol health and release the independent diagnostic session.
""",
    ("diagnose", "protocol", "write"): """Usage:
  diagnose protocol write DEVICE_ID POINT_ID VALUE [--yes] [--timeout SECONDS]
VALUE: JSON scalar or plain string; no lists, objects, null or NaN.
Interactive: displays effective endpoint/point/value and requires YES.
Scripts: --yes is mandatory. UNKNOWN means command MAY have executed; never auto-retry.
Example: diagnose protocol write turbine01 setpoint 1000 --yes
""",
    ("diagnose", "protocol", "listen"): """Usage:
  diagnose protocol listen DEVICE_ID [POINT_ID ...] [--duration SECONDS]
Requires native SUBSCRIBE or LISTEN; no polling fallback.
Without --duration, listen until Ctrl+C; otherwise stop after specified positive seconds.
""",
    ("diagnose", "protocol", "gi"): """Usage:
  diagnose protocol gi DEVICE_ID [--ca CA] [--timeout SECONDS]
Only available when INTERROGATE is supported; CA must be configured for the device.
A partial or timed-out interrogation is not PASS.
""",
    ("diagnose", "protocol", "benchmark"): """Usage:
  diagnose protocol benchmark DEVICE_ID POINT_ID [POINT_ID ...]
    [--samples N] [--interval SECONDS]
Native reads only. Samples 1..200 (default 10), interval default 0.2 seconds.
Reports min, mean, max and p95 duration.
""",
    ("diagnose", "ping"): """Usage:
  diagnose ping HOST_OR_DEVICE [--count N] [--timeout SECONDS]
  Performs ICMP ping using local host routing.""",
    ("diagnose", "tcp"): """Usage:
  diagnose tcp HOST_OR_DEVICE [PORT] [--timeout SECONDS]
  PORT defaults from device config; mandatory with a literal host.""",
    ("diagnose", "route"): """Usage:
  diagnose route HOST
  Inspects local Linux route chosen for HOST.""",
    ("diagnose", "resolve"): """Usage:
  diagnose resolve HOST
  Resolves host to addresses without opening a socket.""",
}


def help_text(parts: list[str]) -> str:
    """Longest known public command prefix; unknown names report general help."""
    if not parts:
        return GENERAL
    for length in range(len(parts), 0, -1):
        key = tuple(parts[:length])
        if key in COMMANDS:
            return COMMANDS[key]
    return GENERAL


def completion_words() -> list[str]:
    words = {"help", "exit", "quit", "clear"}
    for key in COMMANDS:
        words.update(key)
    return sorted(words)
