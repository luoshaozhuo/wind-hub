"""Local process manager: exact configured target, no shell invocation."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .runtime import ServiceTarget


async def _execute(
    args: list[str],
    timeout: float,
    *,
    max_output_bytes: int = 4096,
) -> dict[str, Any]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RuntimeError(f"process manager unavailable: {args[0]}") from exc
    try:
        output, errors = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            process.kill()
        await asyncio.shield(process.communicate())
        if isinstance(exc, asyncio.CancelledError):
            raise
        # 外部管理器子进程超时：远端动作可能已生效，按表 6-2/10-2 报 UNKNOWN/3，
        # 不得翻成 FAILURE/1。
        raise TimeoutError("process manager timed out") from exc
    return {
        "returncode": process.returncode,
        "stdout": output[:max_output_bytes].decode("utf-8", errors="replace"),
        "stderr": errors[:4096].decode("utf-8", errors="replace"),
    }


async def _isula(target: ServiceTarget, action: str) -> dict[str, Any]:
    binary = target.isula_binary or "/usr/bin/isula"
    if not binary.startswith("/") or not target.target:
        raise ValueError("iSulad requires absolute executable and configured target")
    deadline = asyncio.get_running_loop().time() + target.timeout

    async def status() -> dict[str, Any]:
        assert target.target is not None  # validated by caller before entering the closure
        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
        outcome = await _execute(
            [binary, "inspect", target.target],
            remaining,
            max_output_bytes=1048576,
        )
        if outcome["returncode"] != 0:
            raise RuntimeError(f"isula inspect failed: {outcome['stderr']}")
        try:
            payload = json.loads(outcome["stdout"])
        except (json.JSONDecodeError, ValueError) as exc:
            # daemon 输出不可解析是管理器/业务失败（FAILURE/1），不是本地参数错误（2）。
            raise RuntimeError("isula inspect returned invalid JSON") from exc
        if isinstance(payload, list):
            if not payload:
                raise RuntimeError("isula inspect returned no objects")
            payload = payload[0]
        if not isinstance(payload, dict) or not isinstance(payload.get("State"), dict):
            raise RuntimeError("isula inspect returned no State object")
        state = payload["State"]
        return {
            "manager": "isulad",
            "target": target.target,
            "running": bool(state.get("Running", False)),
            "state": str(state.get("Status", "UNKNOWN")),
        }

    if action == "status":
        return await status()
    operations = ("stop", "start") if action == "restart" else (action,)
    completed: list[dict[str, Any]] = []
    for operation in operations:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"isula {operation} timeout")
        result = await _execute([binary, operation, target.target], remaining)
        if result["returncode"] != 0:
            raise RuntimeError(f"isula {operation} failed: {result['stderr']}")
        expected = operation == "start"
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"isula {operation} was not confirmed")
            current = await status()
            if current["running"] is expected:
                completed.append({"operation": operation, "confirmed": True, **current})
                break
            await asyncio.sleep(min(0.2, remaining))
    return {"manager": "isulad", "target": target.target, "stages": completed}


async def _systemd(target: ServiceTarget, action: str) -> dict[str, Any]:
    try:
        from dbus_next import BusType
        from dbus_next.aio import MessageBus
    except ImportError as exc:
        raise RuntimeError("systemd requires dbus-next") from exc
    if not target.target or not target.target.endswith(".service"):
        raise ValueError("systemd target must be a configured .service unit")
    if target.systemd_scope == "user":
        bus = await MessageBus().connect()
    else:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    def as_runtime_error(exc: Exception, action: str) -> RuntimeError:
        # D-Bus 错误（单元消失、权限拒绝等）是进程管理业务失败，由调用方映射 FAILURE/1，
        # 不允许以未翻译异常落到参数错误（2）。
        return RuntimeError(f"systemd {action} failed: {exc}")

    try:
        obj = await bus.introspect("org.freedesktop.systemd1", "/org/freedesktop/systemd1")
        proxy = bus.get_proxy_object("org.freedesktop.systemd1", "/org/freedesktop/systemd1", obj)
        manager = proxy.get_interface("org.freedesktop.systemd1.Manager")

        async def state() -> str:
            try:
                unit_path = await manager.call_get_unit(target.target)
            except Exception as exc:
                # 停止后 systemd 可立即回收未加载单元（停止与重启之间的真实竞态）；
                # 对被配置的 .service 目标，未加载等价于 inactive。
                if "not loaded" in str(exc) or "not found" in str(exc):
                    return "inactive"
                raise as_runtime_error(exc, "get-unit") from exc
            description = await bus.introspect("org.freedesktop.systemd1", unit_path)
            unit = bus.get_proxy_object("org.freedesktop.systemd1", unit_path, description)
            properties = unit.get_interface("org.freedesktop.DBus.Properties")
            value = await properties.call_get("org.freedesktop.systemd1.Unit", "ActiveState")
            return str(value.value)

        if action == "status":
            current = await asyncio.wait_for(state(), target.timeout)
            return {
                "manager": "systemd",
                "target": target.target,
                "active_state": current,
                "status": "ACTIVE" if current == "active" else "INACTIVE",
            }

        operations = ("stop", "start") if action == "restart" else (action,)
        deadline = asyncio.get_running_loop().time() + target.timeout
        stages: list[dict[str, Any]] = []
        for operation in operations:
            method = manager.call_start_unit if operation == "start" else manager.call_stop_unit
            try:
                job = await asyncio.wait_for(
                    method(target.target, "replace"),
                    max(0.1, deadline - asyncio.get_running_loop().time()),
                )
            except Exception as exc:
                raise as_runtime_error(exc, operation) from exc
            expected = "active" if operation == "start" else "inactive"
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"systemd {operation} not confirmed")
                current = await asyncio.wait_for(state(), remaining)
                if current == expected:
                    stages.append(
                        {
                            "operation": operation,
                            "job": str(job),
                            "active_state": current,
                            "confirmed": True,
                        }
                    )
                    break
                if current == "failed":
                    raise RuntimeError(f"systemd unit entered failed state during {operation}")
                await asyncio.sleep(min(0.2, remaining))
        return {
            "manager": "systemd",
            "target": target.target,
            "stages": stages,
            "confirmed": True,
        }
    finally:
        bus.disconnect()


async def manage(target: ServiceTarget, action: str) -> dict[str, Any]:
    """Run only explicit manager operations on configured targets."""
    if action not in {"start", "stop", "restart", "status"}:
        raise ValueError("unsupported process operation")
    if not target.manager:
        raise ValueError("service has no configured process manager")
    if target.manager == "isulad":
        return await _isula(target, action)
    if target.manager == "systemd":
        return await _systemd(target, action)
    from .containerd_client import containerd_manage

    return await containerd_manage(target, action)
