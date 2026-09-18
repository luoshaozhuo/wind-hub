"""``wind-hub probe discover`` — 协议点表发现，输出 points.yaml 草稿。

按设备协议分派（决策 1）：

- **ADS**：符号浏览——上传 TwinCAT 符号表，转换为点表草稿；
- **Modbus**：无符号概念，只在显式 ``--unsafe`` 时做保持寄存器盲扫；
- **IEC104**：不支持点表发现，直接报错退出（决策 9）。

输出为**草稿**而非可直接生效的配置（决策 4）：默认写 stdout，
``--output`` 指定文件路径。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from wind_hub.adapter.inbound.cli.output import print_error
from wind_hub.adapter.inbound.cli.probe import discover as probe_discover
from wind_hub.adapter.inbound.cli.probe.models import DiscoveredPoint
from wind_hub.adapter.inbound.cli.probe.output import render_yaml_draft
from wind_hub.config.loader import load_config
from wind_hub.config.schema import DeviceConfig
from wind_hub.domain.model.errors import WindHubError

# 符号数超过该阈值时提示用 --filter 收敛（决策 7：大 PLC 符号可能上万，
# 一次性输出的草稿既难人工确认也难审阅）。
_LARGE_SYMBOL_COUNT = 10000


def discover(
    device: str = typer.Option(..., "--device", help="设备 ID"),
    config: Path = typer.Option(
        Path("configs"), "--config", help="配置目录（含 system/devices/points/routing.yaml）"
    ),
    output: Path | None = typer.Option(None, "--output", help="输出 YAML 文件路径（默认 stdout）"),
    filter_prefix: str | None = typer.Option(
        None, "--filter", help='只输出指定前缀的符号（如 "MAIN."）'
    ),
    unsafe: bool = typer.Option(
        False, "--unsafe", help="启用 Modbus 寄存器扫描（有风险，可能触发从站保护）"
    ),
    scan_range: str | None = typer.Option(
        None, "--range", help="Modbus 扫描范围（如 0-1000，可逗号分隔多段）"
    ),
) -> None:
    """协议点表发现：连接设备，输出 points.yaml 兼容的 YAML 草稿。"""
    try:
        device_cfg = _find_device(config, device)
        points = asyncio.run(
            _discover(device_cfg, unsafe=unsafe, scan_range=scan_range, filter_prefix=filter_prefix)
        )
    except WindHubError as exc:
        print_error(f"点表发现失败：{exc}")
        raise typer.Exit(1) from exc
    except ValueError as exc:
        # --range 解析失败等参数错误
        print_error(f"参数错误：{exc}")
        raise typer.Exit(1) from exc

    if len(points) > _LARGE_SYMBOL_COUNT:
        print_error(
            f"警告：发现 {len(points)} 个符号，超过 {_LARGE_SYMBOL_COUNT}；"
            "建议用 --filter 收敛后再生成草稿"
        )

    draft = render_yaml_draft(device_id=device, points=points, protocol=device_cfg.protocol)
    if output is not None:
        output.write_text(draft, encoding="utf-8")
        typer.echo(f"草稿已写入 {output}（{len(points)} 个点位，需人工确认）")
    else:
        typer.echo(draft, nl=False)


def _find_device(config_dir: Path, device_id: str) -> DeviceConfig:
    """加载配置并取出指定设备；配置非法或设备不存在时抛 WindHubError。"""
    cfg = load_config(config_dir)
    for device_cfg in cfg.devices.devices:
        if device_cfg.device_id == device_id:
            return device_cfg
    known = [d.device_id for d in cfg.devices.devices]
    raise WindHubError(f"设备 '{device_id}' 不存在（已配置：{known}）")


async def _discover(
    device_cfg: DeviceConfig,
    *,
    unsafe: bool,
    scan_range: str | None,
    filter_prefix: str | None,
) -> list[DiscoveredPoint]:
    """按协议分派到具体的发现实现。"""
    protocol = device_cfg.protocol
    if protocol == "ads":
        return await probe_discover.discover_ads(device_cfg, filter_prefix=filter_prefix)
    if protocol == "modbus":
        if not unsafe:
            raise WindHubError(
                "Modbus 不支持符号发现；寄存器扫描有风险，"
                "如确需扫描请显式加 --unsafe 并用 --range 指定范围（如 0-1000）"
            )
        if scan_range is None:
            raise WindHubError("Modbus 扫描必须用 --range 指定范围（如 0-1000）")
        ranges = probe_discover.parse_scan_ranges(scan_range)
        return await probe_discover.discover_modbus(device_cfg, ranges)
    # iec104（决策 9）
    raise WindHubError("IEC104 不支持点表发现。请手动配置点表。")
