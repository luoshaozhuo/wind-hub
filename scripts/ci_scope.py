"""按变更路径识别 Fast/PR Gate 所需的最小验证范围。"""

from __future__ import annotations

import argparse
from pathlib import Path

FRONTEND_PREFIX = "src/wind-hub-admin/"
BACKEND_PREFIX = "src/wind_hub_"

TOOLING_PREFIXES = (
    "scripts/",
    ".github/",
    ".claude/",
    ".codex/",
    ".agents/",
    "ai_shared/agent_config/hooks/",
)
GOVERNANCE_PREFIXES = ("ai_shared/",)
GOVERNANCE_FILES = {"CLAUDE.md", "AGENTS.md", "README.md", ".gitignore"}
BACKEND_GLOBAL_FILES = {"pyproject.toml", "poetry.lock"}

FAST_TEST_ORDER = ("unit", "component", "contract")
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
        "tests/integration/protocols/",
    ):
        targets.add("integration-protocol")

    if (
        _starts(path, "src/wind_hub_core/rpc/", "tests/integration/rpc/")
        or "/grpc/" in path
        or path
        in {
            "src/wind_hub_server/adapter/outbound/collector_directory.py",
            # Collector 只读查询 / Sink 控制面的应用服务——直接塑造 RPC 响应。
            "src/wind_hub_collector/application/service/query.py",
            "src/wind_hub_collector/application/service/sink.py",
        }
    ):
        targets.add("integration-rpc")

    if (
        _starts(
            path,
            "src/wind_hub_collector/adapter/outbound/sink/",
            "tests/integration/sinks/",
        )
        or path == "src/wind_hub_collector/application/service/sink.py"
    ):
        targets.add("integration-sinks")
        if path.startswith("src/wind_hub_collector/adapter/outbound/sink/"):
            targets.add("system-reload")

    if _starts(
        path,
        "src/wind_hub_collector/application/runtime/",
        "tests/system/acquisition/",
    ) or path in {
        "src/wind_hub_collector/assembly.py",
        "src/wind_hub_collector/main.py",
        "src/wind_hub_collector/application/service/task.py",
    }:
        targets.add("system-acquisition")

    if _starts(path, "tests/system/command/") or path in {
        "src/wind_hub_commander/dispatcher.py",
        "src/wind_hub_commander/runtime.py",
        "src/wind_hub_commander/application/command.py",
        "src/wind_hub_server/application/device/command.py",
    }:
        targets.add("system-command")

    if (
        _starts(path, "tests/system/diagnostics/")
        or path.endswith("/application/diagnostic.py")
        or path.endswith("/application/device/diagnostic.py")
        or path == "src/wind_hub_server/infra/network_probe.py"
    ):
        targets.add("system-diagnostics")

    if _starts(path, "tests/system/reload/") or path in {
        "src/wind_hub_server/application/config/service.py",
        "src/wind_hub_server/application/config/files.py",
        "src/wind_hub_collector/application/service/config.py",
        "src/wind_hub_commander/runtime.py",
        "src/wind_hub_commander/application/config.py",
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

    if (
        _starts(
            path,
            "tests/system/task_control/",
            # worker 权威状态是 placement safety 判定的输入。
            "src/wind_hub_server/application/worker/",
        )
        or path in {
            "src/wind_hub_collector/application/service/task.py",
            "src/wind_hub_server/application/task/placement.py",
            # reconciler 负责 placement 收敛，collector 身份校验是其安全前提。
            "src/wind_hub_server/application/task/reconcile.py",
            "src/wind_hub_server/application/task/collector.py",
            "src/wind_hub_server/application/task/control.py",
            "src/wind_hub_ctl/client.py",
            "src/wind_hub_ctl/main.py",
        }
    ):
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


def _backend_fast_tests(paths: list[str]) -> tuple[bool, set[str]]:
    """决定是否运行后端静态检查以及 Fast 测试层级。"""
    static_required = False
    targets: set[str] = set()

    for path in paths:
        if path in BACKEND_GLOBAL_FILES or path.startswith(BACKEND_PREFIX):
            static_required = True
            targets.update(FAST_TEST_ORDER)
            continue

        if path.startswith("configs/"):
            targets.add("contract")
            continue

        if path.startswith("tests/unit/"):
            static_required = True
            targets.add("unit")
            continue

        if path.startswith("tests/component/"):
            static_required = True
            targets.add("component")
            continue

        if path.startswith("tests/contract/"):
            static_required = True
            targets.add("contract")
            continue

        if path.startswith("tests/"):
            # integration/system/reliability/performance 测试本身仍需 Ruff，
            # 但不因此触发 unit/component/contract 全量运行。
            static_required = True

    return static_required, targets


def _frontend_fast_required(path: str) -> bool:
    """判断是否需要 Vitest + type-check/build。"""
    if not path.startswith(FRONTEND_PREFIX):
        return False

    return not (
        path.startswith("src/wind-hub-admin/tests/e2e/")
        or path == "src/wind-hub-admin/playwright.config.ts"
    )


def classify(paths: list[str]) -> dict[str, str]:
    """返回 Gate 所需的稳定字符串输出。"""
    normalized = [path.strip().replace("\\", "/") for path in paths if path.strip()]

    backend_static, fast_targets = _backend_fast_tests(normalized)
    frontend_fast = any(_frontend_fast_required(path) for path in normalized)

    pr_targets: set[str] = set()
    for path in normalized:
        pr_targets.update(_backend_pr_targets(path))

    frontend_e2e = any(_frontend_e2e_required(path) for path in normalized)
    tooling = any(path.startswith(TOOLING_PREFIXES) for path in normalized)
    governance = any(
        path in GOVERNANCE_FILES or path.startswith(GOVERNANCE_PREFIXES)
        for path in normalized
    )

    ordered_fast = [target for target in FAST_TEST_ORDER if target in fast_targets]
    ordered_pr = [target for target in PR_TARGET_ORDER if target in pr_targets]

    product = (
        backend_static
        or bool(ordered_fast)
        or frontend_fast
        or bool(ordered_pr)
        or frontend_e2e
    )

    return {
        "product": _bool(product),
        "backend_static": _bool(backend_static),
        "backend_fast_targets": ",".join(ordered_fast),
        "backend_fast_required": _bool(bool(ordered_fast)),
        "frontend_fast": _bool(frontend_fast),
        "backend_pr_targets": ",".join(ordered_pr),
        "backend_pr_required": _bool(bool(ordered_pr)),
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
