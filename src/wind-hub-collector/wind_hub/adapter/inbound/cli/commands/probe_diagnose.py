"""``wind-hub probe diagnose`` — 协议联通性诊断（仅 Linux）。

分层诊断（决策 2）：网络层 → 传输层 → 协议层，上层失败短路。固定
exit code 语义（决策 7）：``0`` 全通 / ``1`` 有故障 / ``2`` 有可疑
项——便于现场脚本串联。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from wind_hub.adapter.inbound.cli.output import print_error, print_json
from wind_hub.adapter.inbound.cli.probe.diagnose import diagnose_device
from wind_hub.adapter.inbound.cli.probe.diagnose_models import (
    DiagnoseResult,
    DiagnoseStep,
    StepStatus,
)
from wind_hub.config.loader import load_config
from wind_hub.config.schema import DeviceConfig, PointConfig
from wind_hub.domain.model.errors import WindHubError

# 分隔线宽度（决策 5 的输出格式）。
_RULE = "═" * 63

# 步骤名列宽（输出对齐）。
_STEP_NAME_WIDTH = 18

# 状态图标。
_ICONS = {StepStatus.OK: "✅", StepStatus.WARN: "⚠️", StepStatus.FAIL: "❌"}
_SKIP_ICON = "⏭️"

# 步骤名 → 所属层（输出分组用；步骤构建顺序固定，见 diagnose_device）。
_LAYERS: list[tuple[str, tuple[str, ...]]] = [
    ("网络层", ("ARP", "ICMP")),
    ("传输层", ("TCP",)),
    ("协议层", ()),  # 剩余步骤（协议名）归入协议层
]


def diagnose(
    device: str = typer.Option(..., "--device", help="设备 ID"),
    config: Path = typer.Option(
        Path("configs"), "--config", help="现场配置目录（<site>/，含 system/devices/tasks.yaml；公共定义在同级 common/）"
    ),
    timeout: float = typer.Option(1.0, "--timeout", help="单步超时（秒）"),
    connect_timeout: float = typer.Option(
        5.0, "--connect-timeout", help="协议握手超时（秒，截断驱动重试）"
    ),
    output_json: bool = typer.Option(False, "--json", help="JSON 输出（用于脚本串联）"),
) -> None:
    """协议联通性诊断：本机信息 + 网络层/传输层/协议层分层报告。"""
    try:
        device_cfg, points = _load_device(config, device)
        result = asyncio.run(
            diagnose_device(
                device_cfg, points=points, timeout=timeout, connect_timeout=connect_timeout
            )
        )
    except WindHubError as exc:
        print_error(f"诊断失败：{exc}")
        raise typer.Exit(1) from exc

    if output_json:
        print_json(result.to_dict())
    else:
        _print_report(result)

    # 决策 7：固定 exit code 语义
    if result.overall is StepStatus.FAIL:
        raise typer.Exit(1)
    if result.overall is StepStatus.WARN:
        raise typer.Exit(2)


def _load_device(config_dir: Path, device_id: str) -> tuple[DeviceConfig, list[PointConfig]]:
    """加载配置并取出指定设备及其点表；配置非法或设备不存在时抛错。"""
    cfg = load_config(config_dir)
    for device_cfg in cfg.devices.devices:
        if device_cfg.device_id == device_id:
            return device_cfg, cfg.points_for_device(device_id)
    known = [d.device_id for d in cfg.devices.devices]
    raise WindHubError(f"设备 '{device_id}' 不存在（已配置：{known}）")


def _layer_of(step: DiagnoseStep) -> str:
    """按步骤名归层（网络层/传输层/协议层）。"""
    for title, prefixes in _LAYERS:
        if prefixes and step.name.startswith(prefixes):
            return title
    return _LAYERS[-1][0]


def _print_report(result: DiagnoseResult) -> None:
    """默认文本报告（决策 5）：本机信息 → 目标设备 → 分层结果 → 结论。"""
    local = result.local_info
    typer.echo(_RULE)
    typer.echo("本机信息")
    typer.echo(_RULE)
    typer.echo(f"Hostname:    {local.hostname}")
    typer.echo("IP addresses:")
    for cidr, ifname in local.ips:
        typer.echo(f"  {cidr:<20}({ifname})")
    if not local.ips:
        typer.echo("  （无非回环接口）")
    typer.echo(f"Default gateway: {local.default_gateway or '-'}")

    typer.echo("")
    typer.echo(_RULE)
    typer.echo("目标设备")
    typer.echo(_RULE)
    typer.echo(f"Device ID:  {result.device_id}")
    typer.echo(f"Protocol:   {result.protocol}")
    typer.echo(f"Address:    {result.device_ip}:{result.device_port}")
    if result.same_subnet:
        suffix = f" ({result.subnet_detail})" if result.subnet_detail else ""
        typer.echo(f"Same subnet: ✅ YES{suffix}")
    else:
        typer.echo("Same subnet: ❌ NO")

    typer.echo("")
    typer.echo(_RULE)
    typer.echo("诊断结果")
    typer.echo(_RULE)
    current_layer = ""
    for step in result.steps:
        layer = _layer_of(step)
        if layer != current_layer:
            current_layer = layer
            typer.echo(f"\n{layer}:")
        if step.skipped:
            typer.echo(f"  {step.name:<{_STEP_NAME_WIDTH}}{_SKIP_ICON}  skip    ({step.detail})")
            continue
        icon = _ICONS[step.status]
        typer.echo(
            f"  {step.name:<{_STEP_NAME_WIDTH}}{icon}  {step.status.value:<6}  {step.detail}"
        )
        if step.suggestion:
            typer.echo(f"  {'':<{_STEP_NAME_WIDTH}}       建议: {step.suggestion}")

    typer.echo("")
    typer.echo(_RULE)
    icon = _ICONS[result.overall]
    typer.echo(f"诊断结论: {icon} {result.overall.value.upper()}")
    typer.echo(_RULE)
    typer.echo(result.conclusion)
