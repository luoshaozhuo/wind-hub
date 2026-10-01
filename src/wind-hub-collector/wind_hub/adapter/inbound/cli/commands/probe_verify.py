"""``wind-hub probe verify`` — 点表只读验证。

遍历配置里所有设备的所有点（``--device`` 可指定单个设备，决策 1），
批量读验证可返回性（决策 2），失败点与质量 BAD 点分开报告（决策
5）。exit code 语义（决策 7）：``0`` 全部可读且质量全 GOOD /
``1`` 有读失败点 / ``2`` 无失败但有 BAD 质量点——与 diagnose 一
致，便于现场脚本串联。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
import yaml

from wind_hub.adapter.inbound.cli.output import print_error, print_json
from wind_hub.adapter.inbound.cli.probe.verify import verify_all
from wind_hub.adapter.inbound.cli.probe.verify_models import (
    DeviceVerifyResult,
    VerifyResult,
)
from wind_hub.config.loader import load_config
from wind_hub.domain.model.errors import WindHubError

# 分隔线宽度（与 diagnose 报告一致）。
_RULE = "═" * 63
_SECTION = "─" * 55


def verify(
    device: str | None = typer.Option(None, "--device", help="只验证指定设备（默认所有设备）"),
    config: Path = typer.Option(
        Path("configs"), "--config", help="现场配置目录（<site>/，含 system/devices/tasks.yaml；公共定义在同级 common/）"
    ),
    connect_timeout: float = typer.Option(5.0, "--connect-timeout", help="连接超时（秒）"),
    read_timeout: float = typer.Option(10.0, "--read-timeout", help="读取超时（秒）"),
    concurrency: int = typer.Option(1, "--concurrency", help="设备并发数（默认 1 串行）"),
    output_json: bool = typer.Option(False, "--json", help="JSON 输出"),
    output_yaml: bool = typer.Option(False, "--yaml", help="YAML 输出"),
) -> None:
    """点表只读验证：批量读所有设备的所有点，报告失败点与 BAD 质量点。"""
    if output_json and output_yaml:
        print_error("--json 与 --yaml 只能二选一")
        raise typer.Exit(1)
    try:
        cfg = load_config(config)
        result = asyncio.run(
            verify_all(
                cfg,
                device_id=device,
                concurrency=concurrency,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            )
        )
    except WindHubError as exc:
        print_error(f"点表验证失败：{exc}")
        raise typer.Exit(1) from exc

    if output_json or output_yaml:
        _print_structured(result, as_json=output_json)
    else:
        _print_report(result)

    # 决策 7：exit code 语义
    if result.total_fail > 0:
        raise typer.Exit(1)
    if result.total_bad_quality > 0:
        raise typer.Exit(2)


def _print_structured(result: VerifyResult, *, as_json: bool) -> None:
    """JSON / YAML 机器可读输出（决策 8）。"""
    payload = result.to_dict()
    if as_json:
        print_json(payload)
    else:
        typer.echo(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), nl=False)


def _print_device(device: DeviceVerifyResult) -> None:
    """单设备分节：连接状态 + 统计 + 失败点 / BAD 质量点分开列（决策 5）。"""
    typer.echo(_SECTION)
    typer.echo(f"设备: {device.device_id} ({device.protocol})")
    typer.echo(_SECTION)
    if device.connect_ok:
        typer.echo("连接:        ✅ OK")
    else:
        typer.echo(f"连接:        ❌ FAIL（{device.connect_error}）")
    typer.echo(f"点表数量:    {device.total}")
    typer.echo(f"可读:        {device.ok_count}")
    typer.echo(f"失败:        {device.fail_count}")
    typer.echo(f"质量 BAD:    {device.bad_quality_count}")

    failed = [p for p in device.points if not p.ok]
    typer.echo("\n失败点:")
    if failed:
        for point in failed:
            typer.echo(f"  ❌ {point.point_id:<24}读取失败: {point.error}")
    else:
        typer.echo("  (无)")

    bad = [p for p in device.points if p.quality_bad]
    typer.echo("\n质量 BAD 点:")
    if bad:
        for point in bad:
            typer.echo(f"  ⚠️ {point.point_id:<24}quality=BAD, value={point.value}")
    else:
        typer.echo("  (无)")


def _print_report(result: VerifyResult) -> None:
    """默认文本报告（决策 8）：分设备分节 + 汇总 + 结论。"""
    typer.echo(_RULE)
    typer.echo("点表只读验证")
    typer.echo(_RULE)
    for device in result.devices:
        typer.echo("")
        _print_device(device)

    typer.echo("")
    typer.echo(_RULE)
    typer.echo("汇总")
    typer.echo(_RULE)
    typer.echo(f"总设备:      {len(result.devices)}")
    typer.echo(f"总点表:      {result.total_points}")
    typer.echo(f"可读:        {result.total_ok}")
    typer.echo(f"失败:        {result.total_fail}")
    typer.echo(f"质量 BAD:    {result.total_bad_quality}")
    typer.echo(f"耗时:        {result.duration_ms:.0f} ms")

    typer.echo("")
    if result.total_fail > 0:
        typer.echo(f"结论: ❌ 有失败点（{result.total_fail} 个）")
    elif result.total_bad_quality > 0:
        typer.echo(f"结论: ⚠️ 全部可读，但有 {result.total_bad_quality} 个点质量 BAD")
    else:
        typer.echo("结论: ✅ 所有点可读，质量全 GOOD")
