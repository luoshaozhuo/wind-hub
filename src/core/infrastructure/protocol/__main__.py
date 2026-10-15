"""协议 Driver 手工调试入口（仅供开发使用，不依赖 Collector/Commander）。

使用方法
--------
1. 在仓库虚拟环境中安装项目（推荐 `pip install -e .`），或在仓库根目录：
       PYTHONPATH=src python -m core.infrastructure.protocol
   使用 VS Code/PyCharm 时，以模块 `core.infrastructure.protocol` 启动，
   工作目录设为仓库根目录，并将 src 加入 Python 模块搜索路径。
2. 修改下方“手工调试配置”的协议、服务端地址与测点参数。
3. 在 `driver.connect()`、`driver.read_one()` 等位置设置断点。
4. 默认只读；确需下发指令时明确将 WRITE_ENABLED 改为 True，
   并确认连接的是允许写入的测试设备。未知写结果绝不自动重试。

配置示例
--------
Modbus: PROTOCOL="modbus"，POINT_CONFIG={"register_type": "holding",
        "address": 100, "data_type": "uint16"}。
ADS:    PROTOCOL="ads"，PROTOCOL_OPTIONS={"target_net_id": "1.2.3.4.1.1",
        "twincat_version": "3"}，
        POINT_CONFIG={"index_group": 0x4020, "index_offset": 0,
                      "data_type": "DINT"}。
IEC104: PROTOCOL="iec104"，POINT_CONFIG={"ioa": 100,
        "type_id": "M_ME_NC_1"}；遥控写入使用对应 C_* 命令类型。

本入口构造单个动态 Point，无需登记点表；如需一次调试多个点，
可复制 read_one/write_one 调用并直接修改 Point 对象。
"""

from __future__ import annotations

import asyncio
from typing import cast

from core.application.port.protocol import ProtocolPort
from core.application.protocol_contract import ProtocolCapability, ProtocolPoint, ProtocolWrite
from core.domain import ConnectionEndpoint, PointTable, PointTableId, Protocol
from core.infrastructure.protocol.ads.driver import ADSDriver
from core.infrastructure.protocol.ads.mapping import ads_point
from core.infrastructure.protocol.iec104.driver import IEC104Driver
from core.infrastructure.protocol.iec104.mapping import iec104_point
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from core.infrastructure.protocol.modbus.mapping import modbus_point

# ── 手工调试配置：修改此处即可 ────────────────────────────────────────────
PROTOCOL = "modbus"  # "modbus" / "ads" / "iec104"
HOST = "127.0.0.1"
PORT = 1502
PROTOCOL_OPTIONS: dict[str, str | int | float | bool] = {}

POINT_ID = "debug_point"
POINT_CONFIG: dict[str, str | int] = {
    "register_type": "holding",
    "address": 100,
    "data_type": "uint16",
}

READ_ENABLED = True
HEALTH_ENABLED = True
WRITE_ENABLED = False  # 安全默认值：禁止向真实设备下发写入
WRITE_VALUE: bool | int | float | str = 1
# ─────────────────────────────────────────────────────────────────────


def build_driver() -> tuple[ProtocolPort[ProtocolPoint], ProtocolPoint]:
    """按选定协议构造真实 Driver 和未登记动态点。"""
    table = PointTable(PointTableId("protocol_debug"), Protocol(PROTOCOL), {})
    endpoint = ConnectionEndpoint(HOST, PORT)

    if PROTOCOL == "modbus":
        point = modbus_point(
            POINT_ID,
            register_type=str(POINT_CONFIG["register_type"]),
            address=int(POINT_CONFIG["address"]),
            data_type=str(POINT_CONFIG["data_type"]),
        )
        driver = ModbusDriver(endpoint, table, PROTOCOL_OPTIONS)
    elif PROTOCOL == "ads":
        point = ads_point(
            POINT_ID,
            index_group=int(POINT_CONFIG["index_group"]),
            index_offset=int(POINT_CONFIG["index_offset"]),
            data_type=str(POINT_CONFIG["data_type"]),
        )
        driver = ADSDriver(endpoint, table, PROTOCOL_OPTIONS)
    elif PROTOCOL == "iec104":
        point = iec104_point(
            POINT_ID,
            ioa=int(POINT_CONFIG["ioa"]),
            type_id=str(POINT_CONFIG["type_id"]),
        )
        driver = IEC104Driver(endpoint, table, PROTOCOL_OPTIONS)
    else:
        raise ValueError(f"unsupported protocol: {PROTOCOL!r}")
    return cast("ProtocolPort[ProtocolPoint]", driver), point


async def run() -> None:
    """仅执行显式启用的调试操作，不重连、不重试写入。"""
    driver, point = build_driver()
    if WRITE_ENABLED and ProtocolCapability.WRITE not in driver.capabilities():
        raise ValueError(f"{PROTOCOL} does not support writing")

    try:
        await driver.connect()
        print(f"connected: {PROTOCOL} {HOST}:{PORT}")

        if HEALTH_ENABLED:
            print("health:", await driver.health())

        if READ_ENABLED:
            print("read_one:", await driver.read_one(point))

        if WRITE_ENABLED:
            write = ProtocolWrite(point, WRITE_VALUE)
            result = await driver.write_one(write)
            print("write_one:", result)
    finally:
        await driver.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
