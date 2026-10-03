"""按变更路径识别 Fast/PR Gate 所需的最小验证范围。"""

from __future__ import annotations

import argparse
from pathlib import Path


FRONTEND_PREFIX = "src/wind-hub-admin/"
BACKEND_PREFIX = "src/wind_hub_"

TOOLING_PREFIXES = ("scripts/", ".github/", ".claude/", ".codex/", ".agents/", ".agent/")
GOVERNANCE_PREFIXES = ("ai_shared/", "docs/")
GOVERNANCE_FILES = {"CLAUDE.md", "AGENTS.md", "README.md", ".gitignore", ".env.local.example"}
BACKEND_GLOBAL_FILES = {"pyproject.toml", "poetry.lock"}

PR_TARGET_ORDER = (
    "integration-protocol",
    "integration-rpc",
    "integration-sinks",
    "system-acquisition",
    "system-command",
    "system-diagnostics",
    "system-reload",
    "system-startup",
    "system-task-control",
    "system-e2e",
)


def _starts(path: str, *prefixes: str) -> bool:
    """判断路径是否命中任一前缀。"""
    return path.startswith(prefixes)


def _backend_pr_targets(path: str) -> set[str]:
    """根据稳定架构边界映射后端 PR 目标。"""
    targets: set[str] = set()

    if _starts(
        path,
        "src/wind_hub_core/protocol/",
        "src/wind_hub_collector/adapter/inbound/iec104_slave/",
        "tests/integration/protocols/",
    ):
        targets.add("integration-protocol")

    if (
        _starts(path, "src/wind_hub_core/rpc/", "tests/integration/rpc/")
        or "/grpc/" in path
        or path == "src/wind_hub_server/adapter/outbound/collector_directory.py"
    ):
        targets.add("integration-rpc")

    if _starts(
        path,
        "src/wind_hub_collector/adapter/outbound/sink/",
        "tests/integration/sinks/",
    ):
        targets.add("integration-sinks")

    if _starts(
        path,
        "src/wind_hub_collector/application/runtime/",
        "tests/system/acquisition/",
    ) or path in {
        "src/wind_hub_collector/assembly.py",
        "src/wind_hub_collector/main.py",
        "src/wind_hub_collector/application/usecase/task.py",
    }:
        targets.add("system-acquisition")

    if _starts(path, "tests/system/command/") or path in {
        "src/wind_hub_commander/dispatcher.py",
        "src/wind_hub_commander/runtime.py",
        "src/wind_hub_commander/application/command.py",
        "src/wind_hub_server/application/usecase/device_control.py",
    }:
        targets.add("system-command")

    if (
        _starts(path, "tests/system/diagnostics/")
        or path.endswith("/application/diagnostic.py")
        or path.endswith("/application/usecase/diagnostic.py")
        or path == "src/wind_hub_server/infra/network_probe.py"
    ):
        targets.add("system-diagnostics")

    if _starts(path, "tests/system/reload/") or path in {
        "src/wind_hub_server/config_validation.py",
        "src/wind_hub_server/application/usecase/config.py",
        "src/wind_hub_server/application/usecase/config_admin.py",
        "src/wind_hub_collector/application/usecase/config.py",
    }:
        targets.add("system-reload")

    if _starts(path, "tests/system/startup/") or path in {
        "src/wind_hub_collector/assembly.py",
        "src/wind_hub_collector/main.py",
        "src/wind_hub_commander/assembly.py",
        "src/wind_hub_commander/main.py",
        "src/wind_hub_server/assembly.py",
        "src/wind_hub_server/main.py",
        "src/wind_hub_server/server.py",
        "src/wind_hub_server/settings.py",
    }:
        targets.add("system-startup")

    if _starts(path, "tests/system/task_control/") or path in {
        "src/wind_hub_collector/application/usecase/task.py",
        "src/wind_hub_server/application/usecase/task_assignment.py",
        "src/wind_hub_server/application/usecase/worker_tasks.py",
        "src/wind_hub_ctl/client.py",
        "src/wind_hub_ctl/main.py",
    }:
        targets.add("system-task-control")

    if _starts(
        path,
        "tests/system/e2e/",
        "src/wind_hub_server/adapter/inbound/webapi/v1/",
    ):
        targets.add("system-e2e")

    return targets


def _frontend_e2e_required(path: str) -> bool:
    """仅对当前 Playwright 实际覆盖的应用外壳风险触发 E2E。"""
    return path in {
        "src/wind-hub-admin/src/App.vue",
        "src/wind-hub-admin/src/main.ts",
        "src/wind-hub-admin/playwright.config.ts",
        "src/wind-hub-admin/package.json",
        "src/wind-hub-admin/package-lock.json",
    } or path.startswith("src/wind-hub-admin/tests/e2e/")


def classify(paths: list[str]) -> dict[str, str]:
    """返回 Gate 所需的稳定字符串输出。"""
    normalized = [path.strip().replace("\\", "/") for path in paths if path.strip()]

    backend_fast = any(
        path in BACKEND_GLOBAL_FILES
        or path.startswith("configs/")
        or path.startswith("tests/")
        or path.startswith(BACKEND_PREFIX)
        for path in normalized
    )
    frontend_fast = any(path.startswith(FRONTEND_PREFIX) for path in normalized)
    product = backend_fast or frontend_fast

    targets: set[str] = set()
    for path in normalized:
        targets.update(_backend_pr_targets(path))

    frontend_e2e = any(_frontend_e2e_required(path) for path in normalized)
    tooling = any(path.startswith(TOOLING_PREFIXES) for path in normalized)
    governance = any(
        path in GOVERNANCE_FILES or path.startswith(GOVERNANCE_PREFIXES)
        for path in normalized
    )

    ordered_targets = [target for target in PR_TARGET_ORDER if target in targets]
    return {
        "product": _bool(product),
        "backend_fast": _bool(backend_fast),
        "frontend_fast": _bool(frontend_fast),
        "backend_pr_targets": ",".join(ordered_targets),
        "backend_pr_required": _bool(bool(ordered_targets)),
        "frontend_e2e": _bool(frontend_e2e),
        "tooling": _bool(tooling),
        "governance": _bool(governance),
    }


def _bool(value: bool) -> str:
    """将布尔值转换为 GitHub Actions 友好字符串。"""
    return "true" if value else "false"


def _write_github_output(path: Path, result: dict[str, str]) -> None:
    """写 GitHub Actions step output。"""
    with path.open("a", encoding="utf-8") as handle:
        for key, value in result.items():
            handle.write(f"{key}={value}\n")


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--file-list", type=Path)
    parser.add_argument("--github-output", type=Path)
    return parser


def main() -> int:
    """分类变更范围并输出结果。"""
    args = build_parser().parse_args()
    paths = list(args.paths)
    if args.file_list:
        paths.extend(args.file_list.read_text(encoding="utf-8").splitlines())

    result = classify(paths)
    for key, value in result.items():
        print(f"{key}={value}")

    if args.github_output:
        _write_github_output(args.github_output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
