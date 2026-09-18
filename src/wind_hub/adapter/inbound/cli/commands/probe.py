"""``wind-hub probe`` — 现场辅助探测命令组。

本模块只持有命令组本身并完成子命令注册；每个探测能力一个模块
（step17-21：discover / scan / ports / diagnose / verify）。
"""

from __future__ import annotations

import typer

from wind_hub.adapter.inbound.cli.commands.probe_diagnose import diagnose
from wind_hub.adapter.inbound.cli.commands.probe_discover import discover
from wind_hub.adapter.inbound.cli.commands.probe_ports import ports
from wind_hub.adapter.inbound.cli.commands.probe_scan import scan
from wind_hub.adapter.inbound.cli.commands.probe_verify import verify

app = typer.Typer(
    name="probe",
    help="现场辅助探测（点表发现/验证、网段/端口扫描、联通性诊断）",
    no_args_is_help=True,
)

app.command("discover", help="协议点表发现（ADS 符号浏览；Modbus 需 --unsafe）")(discover)
app.command("scan", help="网段 IP 占用扫描（仅 Linux）")(scan)
app.command("ports", help="指定 IP 端口扫描（仅 Linux，TCP 四态）")(ports)
app.command("diagnose", help="协议联通性诊断（仅 Linux，分层短路）")(diagnose)
app.command("verify", help="点表只读验证（批量读，绝不 write/subscribe）")(verify)
