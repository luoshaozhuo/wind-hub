"""``wind-hub replay`` —— 把归档文件（csv/jsonl）重放到目标 sink。

独立子命令：不依赖运行中的引擎，单独加载配置、按名称创建 sink 实例，读回
归档文件逐条重放，结束时打印统计。用于把 File Sink 落盘的数据离线回灌到
MQ / DB。

用法：
    wind-hub replay --input ./data/archive.jsonl --sink kafka_main
    wind-hub replay --input ./data/archive.csv --sink kafka_main --rate 1000
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from wind_hub.adapter.inbound.cli.output import print_error, print_kv
from wind_hub.adapter.outbound.sink.file.replay import parse_records, replay_to_sink
from wind_hub.assembly import _create_sink
from wind_hub.config.loader import load_config
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.port.outbound import SinkPort

app = typer.Typer(name="replay", help="重放归档文件到目标 sink")


@app.callback(invoke_without_command=True)
def replay(
    input_path: Path = typer.Option(..., "--input", help="输入文件路径（csv/jsonl）"),
    sink: str = typer.Option(..., "--sink", help="目标 Sink 名称"),
    rate: int = typer.Option(0, "--rate", help="每秒重放条数（0=不限速）"),
    fmt: str = typer.Option("auto", "--format", help="auto / csv / jsonl（默认按扩展名推断）"),
    config: Path = typer.Option("configs", "--config", help="配置目录（含 system.yaml）"),
) -> None:
    """读回归档文件并按速率重放到指定 sink，打印统计。"""
    asyncio.run(_run(input_path, sink, rate, fmt, config))


async def _run(input_path: Path, sink_name: str, rate: int, fmt: str, config: Path) -> None:
    try:
        resolved_fmt = _resolve_format(input_path, fmt)
        target = _resolve_sink(config, sink_name)
        records = parse_records(input_path, resolved_fmt)
    except (OSError, ConfigError) as exc:
        print_error(f"回放准备失败：{exc}")
        raise typer.Exit(1) from exc

    await target.open()
    try:
        stats = await replay_to_sink(target, records, rate)
    finally:
        await target.close()

    print_kv(
        {
            "input": str(input_path),
            "sink": sink_name,
            "format": resolved_fmt,
            "total": stats.total,
            "success": stats.success,
            "failed": stats.failed,
            "elapsed_seconds": round(stats.elapsed_seconds, 3),
        }
    )


def _resolve_format(input_path: Path, fmt: str) -> str:
    """把 ``--format`` 归一到 ``csv`` / ``jsonl``；``auto`` 按扩展名推断。"""
    if fmt in ("csv", "jsonl"):
        return fmt
    if fmt != "auto":
        raise ConfigError(f"--format must be auto/csv/jsonl, got {fmt!r}")
    ext = input_path.suffix.lower()
    if ext == ".csv":
        return "csv"
    if ext in (".jsonl", ".json", ".ndjson"):
        return "jsonl"
    raise ConfigError(
        f"无法从扩展名 {ext!r} 推断格式，请用 --format csv|jsonl（文件：{input_path}）"
    )


def _resolve_sink(config_dir: Path, sink_name: str) -> SinkPort:
    """按名称从配置解析 sink 并创建独立实例（不复用采集的实例）。"""
    cfg = load_config(config_dir)
    for sink_cfg in cfg.system.sinks:
        if sink_cfg.name == sink_name:
            return _create_sink(sink_cfg)
    available = sorted(s.name for s in cfg.system.sinks)
    raise ConfigError(f"未知 sink '{sink_name}'（可用：{available}）")
