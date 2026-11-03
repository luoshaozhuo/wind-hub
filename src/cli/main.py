"""Typer entrypoint and one-loop interactive wind-hub shell."""

from __future__ import annotations

import asyncio
import io
import shlex
import signal
from concurrent.futures import CancelledError as FutureCancelled
from concurrent.futures import Future
from contextlib import redirect_stdout
from pathlib import Path
from typing import Annotated, Any

import click
import grpc
import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import DummyHistory
from typer._click import exceptions as typer_click_exceptions

# Typer 0.27 vendors Click; its internal Exit is not in the package's public API.
from typer._click.core import Exit as _TyperClickExit  # type: ignore[attr-defined]

from .business_commands import collector, commander
from .common_commands import config as remote_config
from .diagnose_commands import diagnose
from .directory_diff import compare_directories
from .history import SafeHistory
from .network_probe import industrial_probe, ping_probe, scan_cidr, tcp_probe
from .plant_commands import data_generator, gateway, sink
from .process_manager import manage
from .runtime import Runtime

app = typer.Typer(no_args_is_help=False, add_completion=False)
service_app = typer.Typer(help="Configured service targets and public gRPC status.")
config_app = typer.Typer(help="CLI-local configuration comparison.")
net_app = typer.Typer(help="One-shot local network probes.")
app.add_typer(service_app, name="service")
app.add_typer(config_app, name="config")
for _remote_command in remote_config.registered_commands:
    config_app.registered_commands.append(_remote_command)
app.add_typer(net_app, name="net")
app.add_typer(collector, name="collector")
app.add_typer(commander, name="commander")
app.add_typer(sink, name="sink")
app.add_typer(gateway, name="gateway")
app.add_typer(data_generator, name="data-generator")
app.add_typer(diagnose, name="diagnose")

_config: Path | None = None
_format: str | None = None
_runtime: Runtime | None = None
_shell_loop: asyncio.AbstractEventLoop | None = None
_active_rpc: Future[Any] | None = None


def runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime(_config, _format)
    return _runtime


def _run(command: str, task: Any) -> None:
    global _active_rpc

    async def invoke() -> None:
        exit_code = 0
        cancelled_in_shell = False
        try:
            data = await task
            exit_code = runtime().present(command, data)
        except asyncio.CancelledError:
            runtime().interrupted(command)
            exit_code = 130
            # Shell 模式下跨线程 future 已取消（工作线程据此抛 Exit(130)）；
            # 在事件循环任务里再抛 Exit 只会成为无人 retrieve 的孤儿异常。
            cancelled_in_shell = _shell_loop is not None
        except Exception as exc:
            runtime().error(command, exc)
            exit_code = 2
            if isinstance(exc, NotImplementedError):
                exit_code = 3
            elif isinstance(exc, TimeoutError | asyncio.TimeoutError):
                # 设计表 10-2：本地总预算超时等同 DEADLINE，结果 UNKNOWN，不是参数错误。
                exit_code = 3
            elif isinstance(exc, grpc.aio.AioRpcError):
                if exc.code() in {
                    grpc.StatusCode.UNAVAILABLE,
                    grpc.StatusCode.DEADLINE_EXCEEDED,
                    grpc.StatusCode.UNIMPLEMENTED,
                    grpc.StatusCode.CANCELLED,
                }:
                    exit_code = 3
                elif exc.code() in {
                    grpc.StatusCode.ABORTED,
                    grpc.StatusCode.ALREADY_EXISTS,
                    grpc.StatusCode.FAILED_PRECONDITION,
                    # 设计表 10-2：对象消失是业务失败（1），不是参数/配置错误（2）。
                    grpc.StatusCode.NOT_FOUND,
                }:
                    exit_code = 1
            elif isinstance(exc, RuntimeError):
                # 进程管理层 fail-closed（身份不符、FIFO 预检失败等）是业务失败（1）。
                exit_code = 1
        finally:
            if _shell_loop is None:
                await runtime().close()
        if exit_code and not cancelled_in_shell:
            raise typer.Exit(code=exit_code)

    if _shell_loop is None:
        asyncio.run(invoke())
    else:
        future = asyncio.run_coroutine_threadsafe(invoke(), _shell_loop)
        _active_rpc = future
        try:
            future.result()
        except FutureCancelled as exc:
            raise typer.Exit(code=130) from exc
        finally:
            _active_rpc = None


@app.callback(invoke_without_command=True)
def entry(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config", help="CLI YAML configuration")] = None,
    output_format: Annotated[str | None, typer.Option("--format", help="table or json")] = None,
) -> None:
    global _config, _format, _runtime
    if output_format is not None and output_format not in {"table", "json"}:
        raise typer.BadParameter("must be table or json")
    selected = config or (Path("cli.yaml") if Path("cli.yaml").is_file() else None)
    _config, _format = selected, output_format
    if _shell_loop is None:
        _runtime = Runtime(selected, output_format)
    if selected is not None:
        try:
            runtime().config()
        except (ValueError, OSError) as exc:
            raise typer.BadParameter(str(exc), param_hint="--config") from exc
    if ctx.invoked_subcommand is None:
        asyncio.run(shell())


def _command_words() -> list[str]:
    """Build completion and summary help from the actual registered Typer tree."""
    root = typer.main.get_command(app)
    names: list[str] = []

    def visit(prefix: tuple[str, ...], group: Any) -> None:
        commands = getattr(group, "commands", None)
        if isinstance(commands, dict):
            for name, child in commands.items():
                visit((*prefix, name), child)
        elif prefix:
            names.append(" ".join(prefix))

    visit((), root)
    return sorted(set(names))


@app.command("help")
def help_command() -> None:
    """List available stage-one command groups."""
    print(chr(10).join(_command_words()))


@app.command("exit")
def exit_command() -> None:
    """Exit a shell session (one-shot calls simply return)."""


@service_app.command("list")
def service_list() -> None:
    try:
        if _config is None:
            raise ValueError("service list requires --config CLI_YAML")
        targets = runtime().config().services
        runtime().present(
            "service list",
            [
                {
                    "service": name,
                    "endpoint": target.grpc_endpoint,
                    "rpc_via": target.rpc_via,
                    "manager": target.manager,
                    "target": target.target,
                }
                for name, target in targets.items()
            ],
        )
    except (ValueError, OSError) as exc:
        runtime().error("service list", exc)
        raise typer.Exit(code=2) from exc


async def _manage_service(name: str, operation: str) -> dict[str, Any]:
    """Report actual manager transition and separately observed gRPC health."""
    context = runtime()
    target = context.target(name)
    process = await manage(target, operation)
    result: dict[str, Any] = {
        "service": name,
        "operation": operation,
        "process": process,
    }
    if operation == "stop":
        result["business"] = {"health": "UNAVAILABLE", "reason": "process stopped"}
        return result
    if target.grpc_endpoint is None or target.rpc_via is not None:
        result["business"] = {"health": "UNKNOWN", "reason": "no exclusive gRPC endpoint"}
        return result
    deadline = asyncio.get_running_loop().time() + target.timeout
    while True:
        try:
            result["business"] = await context.call_common(name, "GetServiceStatus")
            return result
        except grpc.aio.AioRpcError as exc:
            if exc.code() not in {
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
            }:
                raise
            if asyncio.get_running_loop().time() >= deadline:
                result["business"] = {"health": "UNKNOWN", "error": str(exc)}
                return result
            await asyncio.sleep(0.2)


@service_app.command("start")
def service_start(name: str, yes: bool = typer.Option(False, "--yes")) -> None:
    from .safety import confirm_mutation

    confirm_mutation(f"Start process {name}?", yes, command="service start")
    _run("service start", _manage_service(name, "start"))


@service_app.command("stop")
def service_stop(name: str, yes: bool = typer.Option(False, "--yes")) -> None:
    from .safety import confirm_mutation

    confirm_mutation(f"Stop process {name}?", yes, command="service stop")
    _run("service stop", _manage_service(name, "stop"))


@service_app.command("restart")
def service_restart(name: str, yes: bool = typer.Option(False, "--yes")) -> None:
    from .safety import confirm_mutation

    confirm_mutation(f"Restart process {name}?", yes, command="service restart")
    _run("service restart", _manage_service(name, "restart"))


@service_app.command("info")
def service_info(name: str) -> None:
    _run("service info", runtime().call_common(name, "GetServiceInfo"))


@service_app.command("status")
def service_status(name: Annotated[str | None, typer.Argument()] = None) -> None:
    """Keep process state and business readiness separately observable."""

    async def status() -> list[dict[str, Any]]:
        names = [name] if name is not None else list(runtime().config().services)

        async def check(item: str) -> dict[str, Any]:
            target = runtime().target(item)
            result: dict[str, Any] = {"service": item}
            if target.manager is not None:
                try:
                    result["process"] = await manage(target, "status")
                except (ValueError, OSError, RuntimeError, TimeoutError, grpc.RpcError) as exc:
                    result["process"] = {"status": "UNKNOWN", "error": str(exc)}
            else:
                result["process"] = {"status": "UNMANAGED"}
            if target.rpc_via is not None:
                result["business"] = {
                    "status": "UNKNOWN",
                    "reason": "RPC uses shared manager endpoint",
                    "rpc_via": target.rpc_via,
                }
            elif target.grpc_endpoint is None:
                result["business"] = {"status": "UNAVAILABLE", "reason": "no gRPC endpoint"}
            else:
                try:
                    result["business"] = await runtime().call_common(item, "GetServiceStatus")
                except (ValueError, OSError, RuntimeError, TimeoutError, grpc.RpcError) as exc:
                    result["business"] = {"health": "UNKNOWN", "error": str(exc)}
            return result

        return await asyncio.gather(*(check(item) for item in names))

    _run("service status", status())


@service_app.command("capabilities")
def service_capabilities(name: str) -> None:
    _run("service capabilities", runtime().call_common(name, "GetCapabilities"))


@config_app.command("diff")
def config_diff(left: Path, right: Path) -> None:
    try:
        cfg = runtime().config() if _config else None
        result = compare_directories(
            left,
            right,
            max_file_bytes=cfg.diff_max_file_bytes if cfg else 1048576,
            max_output_lines=cfg.diff_max_output_lines if cfg else 10000,
        )
        code = runtime().present("config diff", result)
        if code:
            raise typer.Exit(code=code)
    except (ValueError, OSError) as exc:
        runtime().error("config diff", exc)
        raise typer.Exit(code=2) from exc


@net_app.command("ping")
def net_ping(host: str, timeout: float | None = None) -> None:
    settings = runtime().config() if _config else None
    default = settings.probe_timeout if settings else 1.0
    _run("net ping", ping_probe(host, timeout if timeout is not None else default))


@net_app.command("tcp")
def net_tcp(host: str, port: int, timeout: float | None = None) -> None:
    default = runtime().config().probe_timeout if _config else 1.0
    _run("net tcp", tcp_probe(host, port, timeout if timeout is not None else default))


@net_app.command("industrial")
def net_industrial(host: str, timeout: float | None = None) -> None:
    cfg = runtime().config() if _config else None
    default = cfg.probe_timeout if cfg else 1.0
    ports = cfg.industrial_ports if cfg else None
    _run(
        "net industrial", industrial_probe(host, timeout if timeout is not None else default, ports)
    )


@net_app.command("scan")
def net_scan(cidr: str, timeout: float | None = None, concurrency: int | None = None) -> None:
    cfg = runtime().config() if _config else None
    default = cfg.probe_timeout if cfg else 1.0
    workers = cfg.max_concurrency if cfg else 64
    limit = cfg.max_hosts if cfg else 1024
    _run(
        "net scan",
        scan_cidr(
            cidr,
            timeout if timeout is not None else default,
            concurrency if concurrency is not None else workers,
            limit,
        ),
    )


def _dispatch_typer(arguments: list[str]) -> str:
    """Parse shell commands via the same Typer tree as one-shot execution."""
    command = typer.main.get_command(app)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        try:
            command.main(args=arguments, prog_name="wind-hub", standalone_mode=False)
        except (click.exceptions.Exit, typer.Exit, _TyperClickExit):
            pass  # Structured failure has already been emitted.
        except (
            click.ClickException,
            typer_click_exceptions.ClickException,
        ) as exc:
            raise ValueError(exc.format_message()) from exc
    return buffer.getvalue()


async def shell() -> None:
    """One event loop owns all grpc.aio channels across the shell session."""
    global _shell_loop
    _shell_loop = asyncio.get_running_loop()
    loop = asyncio.get_running_loop()
    previous_sigint = signal.getsignal(signal.SIGINT)
    try:
        loop.add_signal_handler(
            signal.SIGINT, lambda: _active_rpc.cancel() if _active_rpc else None
        )
        installed_handler = True
    except (NotImplementedError, RuntimeError):
        installed_handler = False
    session = runtime()
    cfg = session.config() if _config else None
    history = SafeHistory() if cfg is None or cfg.history else DummyHistory()
    words = _command_words()
    prompt: PromptSession[str] = PromptSession(
        history=history, completer=WordCompleter(words, sentence=True)
    )
    try:
        while True:
            try:
                line = await prompt.prompt_async(cfg.prompt if cfg else "wind-hub> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                continue
            try:
                arguments = shlex.split(line)
            except ValueError as exc:
                session.error("shell", exc)
                continue
            if not arguments:
                continue
            if arguments[0] in {"exit", "quit"}:
                break
            prefix = ["--config", str(_config)] if _config else []
            if _format is not None:
                prefix.extend(["--format", _format])
            # prompt_toolkit installs its own SIGINT handler while prompting.
            # Restore the foreground-RPC handler after every prompt, before
            # dispatching blocking Typer commands in a worker thread.
            if installed_handler:
                loop.add_signal_handler(
                    signal.SIGINT, lambda: _active_rpc.cancel() if _active_rpc else None
                )
            try:
                output = await asyncio.to_thread(_dispatch_typer, prefix + arguments)
                if output:
                    print(output, end="")
            except (ValueError, OSError, RuntimeError, grpc.RpcError) as exc:
                session.error(line, exc)
            except asyncio.CancelledError:
                session.interrupted(line)
    finally:
        if installed_handler:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous_sigint)
        _shell_loop = None
        await session.close()


def main() -> None:
    app()


if __name__ == "__main__":
    # `python -m cli.main` 通过 runpy 以 __main__ 身份再执行本文件；直接调用
    # main() 会让 __main__ 与子模块惰性导入的 cli.main 成为两个实例，全局
    # _config/_runtime 分裂（子模块命令拿到空 Runtime）。统一收敛到单例模块。
    from cli.main import main as _cli_main

    _cli_main()
