"""probe diagnose 的分层诊断编排（决策 2/3/4/9/10，仅 Linux）。

流程（决策 2）：本机信息 → 网段匹配（决策 4）→ 网络层（ARP/ICMP，
复用 step18 的 scan_arp / scan_icmp）→ 传输层（复用 step19 的
scan_port，端口四态，决策 9）→ 协议层（复用现有驱动的 connect() +
一次 read()，决策 3，不新写握手逻辑）。上层失败即短路，下层步骤标
记 ``skipped``（决策 2）。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import shutil
import socket

import wind_hub.adapter.outbound.protocol  # noqa: F401  # 触发协议驱动自注册
from wind_hub.adapter.inbound.cli.probe.diagnose_models import (
    DiagnoseResult,
    DiagnoseStep,
    StepStatus,
)
from wind_hub.adapter.inbound.cli.probe.local_info import get_local_info, is_same_subnet
from wind_hub.adapter.inbound.cli.probe.ports import scan_port
from wind_hub.adapter.inbound.cli.probe.ports_models import PortResult, PortState
from wind_hub.adapter.inbound.cli.probe.scan_methods import check_linux, scan_arp, scan_icmp
from wind_hub.config.schema import DeviceConfig, PointConfig
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.model.point import PointRef
from wind_hub.domain.port.outbound import ProtocolPort
from wind_hub.infra.registry import protocol_registry

logger = logging.getLogger(__name__)


def _create_driver(device_cfg: DeviceConfig) -> ProtocolPort:
    """通过注册表创建协议驱动（模块级 seam，测试可替换）。

    Raises:
        ConfigError: 协议名未注册。
    """
    return protocol_registry.create(device_cfg.protocol, device_cfg)


async def _resolve_target(host: str) -> str:
    """把 endpoint host 解析为 IPv4 字符串；已是 IP 则原样返回。

    Raises:
        ConfigError: 主机名无法解析。
    """
    try:
        return str(ipaddress.IPv4Address(host))
    except ipaddress.AddressValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, family=socket.AF_INET)
    except OSError as exc:
        raise ConfigError(f"无法解析主机名 '{host}': {exc}") from exc
    return str(infos[0][4][0])


def _suggest_from_port_state(state: PortState, ping_ok: bool) -> str | None:
    """按端口四态 + ping 结果给处置建议（决策 9，不读 iptables）。

    - ``TIMEOUT`` + ping 通 → 端口无响应但主机可达，疑似包过滤；
    - ``CLOSED`` → 主机能达但端口未监听；
    - ``UNREACHABLE`` → 本机路由缺失；
    - ``OPEN`` / 其他组合 → 无需建议（或由网络层结论解释）。
    """
    if state is PortState.TIMEOUT:
        if ping_ok:
            return "端口无响应但 ping 可达，可能被防火墙过滤（检查设备侧/中间防火墙规则）"
        return None
    if state is PortState.CLOSED:
        return "服务未启动或端口未监听（确认设备侧协议服务已启用、端口配置正确）"
    if state is PortState.UNREACHABLE:
        return "路由不存在（检查本机网关/路由配置，或确认目标网段是否有路由可达）"
    return None


def _build_conclusion(steps: list[DiagnoseStep]) -> tuple[StepStatus, str]:
    """汇聚步骤状态为整体结论（决策 6）：任一 fail → fail，否则有
    warn → warn，否则 ok。跳过的步骤不参与汇聚。"""
    active = [s for s in steps if not s.skipped]
    fails = [s for s in active if s.status is StepStatus.FAIL]
    if fails:
        first = fails[0]
        text = f"诊断未通过：{first.name} —— {first.detail}"
        if first.suggestion:
            text += f"\n建议：{first.suggestion}"
        return StepStatus.FAIL, text
    warns = [s for s in active if s.status is StepStatus.WARN]
    if warns:
        first = warns[0]
        return (
            StepStatus.WARN,
            f"基本联通，但存在 {len(warns)} 个可疑项：{first.name} —— {first.detail}",
        )
    return StepStatus.OK, "所有诊断层通过，设备联通性正常。"


# ---------------------------------------------------------------------------
# 各层步骤构建
# ---------------------------------------------------------------------------


def _arp_step(
    mac: str | None,
    *,
    loopback: bool,
    same_subnet: bool,
    icmp_ok: bool | None,
) -> DiagnoseStep:
    """网络层 · ARP 步骤。

    ARP 只在同网段有意义；跨网段无记录属正常现象，所以只有「同网段
    或回环」时才把缺失升级为 fail（结合 ICMP 结果定级）。
    """
    if loopback:
        return DiagnoseStep("ARP", StepStatus.OK, "目标为本机回环，无需 ARP")
    if mac is not None:
        return DiagnoseStep("ARP", StepStatus.OK, f"已解析 MAC {mac}")
    if icmp_ok:
        return DiagnoseStep(
            "ARP", StepStatus.WARN, "ARP 表无记录，但 ICMP 可达（跨网段通信属正常）"
        )
    if icmp_ok is None:
        return DiagnoseStep("ARP", StepStatus.WARN, "ARP 表无记录（ping 不可用，无法确认）")
    if same_subnet:
        return DiagnoseStep(
            "ARP",
            StepStatus.FAIL,
            "同网段但 ARP 无记录且 ping 不通",
            suggestion="确认目标 IP 正确、设备已上电且接入同一网段",
        )
    return DiagnoseStep(
        "ARP",
        StepStatus.FAIL,
        "无法解析（不在同一网段）",
        suggestion="检查本机网络配置或路由",
    )


def _icmp_step(icmp_ok: bool | None) -> DiagnoseStep:
    """网络层 · ICMP 步骤；``icmp_ok=None`` 表示 ping 二进制不可用。"""
    if icmp_ok is None:
        return DiagnoseStep(
            "ICMP",
            StepStatus.WARN,
            "系统无 ping 命令，ICMP 诊断不可用",
            suggestion="安装 iputils-ping，或直接用 probe ports 做 TCP 层诊断",
        )
    if icmp_ok:
        return DiagnoseStep("ICMP", StepStatus.OK, "可达（ping 通）")
    detail = "不可达（ping 无响应）"
    return DiagnoseStep("ICMP", StepStatus.FAIL, detail)


def _tcp_step(port_result: PortResult, *, ping_ok: bool) -> DiagnoseStep:
    """传输层 · TCP 端口步骤（端口四态 → 步骤三态，决策 9）。"""
    name = f"TCP {port_result.port}"
    state = port_result.state
    if state is PortState.OPEN:
        service = port_result.service_guess
        suffix = f"（{service}）" if service else ""
        return DiagnoseStep(name, StepStatus.OK, f"端口开放{suffix}")
    details = {
        PortState.CLOSED: "连接被拒（RST）",
        PortState.TIMEOUT: "无响应（超时）",
        PortState.UNREACHABLE: "网络/主机不可达",
    }
    return DiagnoseStep(
        name,
        StepStatus.FAIL,
        details[state],
        suggestion=_suggest_from_port_state(state, ping_ok),
    )


async def _protocol_step(
    device_cfg: DeviceConfig,
    points: list[PointConfig],
    connect_timeout: float = 5.0,
) -> DiagnoseStep:
    """协议层 · 用现有驱动 connect() + 一次 read()（决策 3）。

    connect 包 ``asyncio.wait_for``（step21 任务 0.1）：驱动自带指数
    退避重试（ADS 最坏 ~1 分钟），与「诊断要快」冲突，故在外层加超
    时，不改驱动。无论成败都在 finally 里 close()（决策 3：诊断完
    成后释放连接）。读取失败但握手成功只记 ``WARN``——联通性已证
    实，问题多半在点表。
    """
    label = device_cfg.protocol.upper()
    try:
        driver = _create_driver(device_cfg)
    except ConfigError as exc:
        return DiagnoseStep(label, StepStatus.FAIL, f"无法创建协议驱动：{exc}")
    if points:
        try:
            driver.set_points_mapping(points)
        except Exception as exc:  # 点表解析失败是配置问题，不是联通性问题
            return DiagnoseStep(
                label,
                StepStatus.WARN,
                f"点表解析失败：{exc}",
                suggestion="检查 points.yaml 中该设备的地址/数据类型配置",
            )
    try:
        await asyncio.wait_for(driver.connect(), timeout=connect_timeout)
    except TimeoutError:
        # 驱动重试预算耗尽前由外层超时截断（step21 任务 0.1）
        with contextlib.suppress(Exception):
            await driver.close()
        return DiagnoseStep(
            label,
            StepStatus.FAIL,
            f"协议握手超时（{connect_timeout:g}s 内未完成，可能设备不可达或配置错误）",
            suggestion="确认设备地址/端口可达（可先用 probe ports 验证传输层）",
        )
    except Exception as exc:  # 各驱动异常类型不一，诊断只关心成败
        # connect 失败可能留下半初始化的会话；close 按接口约定是幂等的
        with contextlib.suppress(Exception):
            await driver.close()
        return DiagnoseStep(
            label,
            StepStatus.FAIL,
            f"协议握手失败：{exc}",
            suggestion="检查协议参数（net id / unit id / common addr 等）与设备状态",
        )
    try:
        if not points:
            return DiagnoseStep(
                label,
                StepStatus.WARN,
                "协议握手成功（设备无配置点，未验证 read）",
                suggestion="为设备配置点表后可进一步验证 read",
            )
        ref = PointRef(device_id=device_cfg.device_id, point_id=points[0].point_id)
        try:
            values = await driver.read([ref])
        except Exception as exc:  # 同上：诊断只关心成败
            return DiagnoseStep(
                label,
                StepStatus.WARN,
                f"协议握手成功，但读取 {ref.point_id} 失败：{exc}",
                suggestion="检查点表配置（地址/数据类型）",
            )
        value = values[0].value if values else "<无返回值>"
        return DiagnoseStep(label, StepStatus.OK, f"握手成功；读取 {ref.point_id} = {value}")
    finally:
        with contextlib.suppress(Exception):
            await driver.close()


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


async def diagnose_device(
    device_cfg: DeviceConfig,
    points: list[PointConfig] | None = None,
    timeout: float = 1.0,
    connect_timeout: float = 5.0,
) -> DiagnoseResult:
    """诊断单个设备，返回分层结果（决策 2/8）。

    Args:
        timeout: 网络层/传输层单步超时（决策 10）。
        connect_timeout: 协议层握手超时（step21 任务 0.1，截断驱动的
            重试预算）。

    Raises:
        ConfigError: 非 Linux 平台、本机信息获取失败或目标地址非法。
    """
    check_linux()
    local = await get_local_info()
    target_ip = await _resolve_target(device_cfg.endpoint.host)
    port = device_cfg.endpoint.port
    point_list = list(points) if points else []

    # 决策 4：网段匹配。回环地址永远视为「同网段」（就是本机）。
    loopback = ipaddress.IPv4Address(target_ip).is_loopback
    subnet_detail: str | None
    if loopback:
        same_subnet, subnet_detail = True, "本机回环"
    else:
        same_subnet, subnet_detail = is_same_subnet(local.ips, target_ip)

    steps: list[DiagnoseStep] = []

    # ---- 网络层（决策 2）：ARP + ICMP ----
    arp_map = await scan_arp(target_ip)
    icmp_ok: bool | None = None
    if shutil.which("ping") is not None:
        alive = await scan_icmp([target_ip], timeout, 1)
        icmp_ok = target_ip in alive
    else:
        logger.warning("'ping' not found — ICMP diagnose step degraded to warn")
    mac = arp_map.get(target_ip)
    steps.append(_arp_step(mac, loopback=loopback, same_subnet=same_subnet, icmp_ok=icmp_ok))
    steps.append(_icmp_step(icmp_ok))
    network_ok = loopback or mac is not None or bool(icmp_ok)

    # ---- 传输层（决策 2）：TCP 端口 ----
    port_open = False
    if network_ok:
        port_result = await scan_port(target_ip, port, timeout)
        steps.append(_tcp_step(port_result, ping_ok=bool(icmp_ok)))
        port_open = port_result.state is PortState.OPEN
    else:
        steps.append(DiagnoseStep(f"TCP {port}", StepStatus.OK, "网络层失败，跳过", skipped=True))

    # ---- 协议层（决策 3）：驱动 connect + 一次 read ----
    label = device_cfg.protocol.upper()
    if port_open:
        steps.append(await _protocol_step(device_cfg, point_list, connect_timeout))
    else:
        detail = "传输层失败，跳过" if network_ok else "网络层失败，跳过"
        steps.append(DiagnoseStep(label, StepStatus.OK, detail, skipped=True))

    # ---- 汇总结论（决策 6/4）----
    overall, conclusion = _build_conclusion(steps)
    if not same_subnet and overall is StepStatus.FAIL:
        local_nets = ", ".join(cidr for cidr, _ in local.ips) or "（无可用网段）"
        conclusion = (
            f"本机与目标设备不在同一网段。\n"
            f"  本机: {local_nets}\n"
            f"  目标: {target_ip}\n"
            f"{conclusion}"
        )

    return DiagnoseResult(
        local_info=local,
        device_id=device_cfg.device_id,
        device_ip=target_ip,
        device_port=port,
        protocol=device_cfg.protocol,
        steps=steps,
        overall=overall,
        conclusion=conclusion,
        same_subnet=same_subnet,
        subnet_detail=subnet_detail,
    )
