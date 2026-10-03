"""Modbus TCP Sink Server。

直接把 pymodbus ServerContext 适配到 ModbusSinkStore；Server 不维护第二份
寄存器状态。当前 Sink 为只读暴露，所有 Modbus 写功能码统一拒绝。
"""

from __future__ import annotations

from typing import Any

from pymodbus.constants import ExcCodes
from pymodbus.datastore import ModbusServerContext
from pymodbus.server import ModbusTcpServer

from wind_hub_collector.adapter.outbound.sink.modbus_pipeline import ModbusSinkDataPath
from wind_hub_collector.adapter.outbound.sink.modbus_store import ModbusSinkStore
from wind_hub_core.config.sinks import ModbusSinkConnection
from wind_hub_core.model.health import HealthStatus


_READ_FUNCTIONS = {
    1: "coil",
    2: "discrete",
    3: "holding",
    4: "input",
}


class StoreBackedModbusContext(ModbusServerContext):
    """直接读取 ModbusSinkStore 的 pymodbus ServerContext。"""

    def __init__(self, store: ModbusSinkStore) -> None:
        # ModbusTcpServer 3.15 通过 isinstance(context, ModbusServerContext)
        # 识别旧式 context；simdevices 为空且 old_simulator=True 时直接使用本对象。
        self.simdevices: list[Any] = []
        self.old_simulator = True
        self._store = store

    async def async_getValues(
        self,
        device_id: int,
        func_code: int,
        address: int,
        count: int = 1,
    ) -> list[int] | list[bool] | ExcCodes:
        """按功能码从唯一内存 store 读取值。"""
        register_type = _READ_FUNCTIONS.get(func_code)
        if register_type is None:
            return ExcCodes.ILLEGAL_FUNCTION
        try:
            if register_type in {"coil", "discrete"}:
                return self._store.read_bits(
                    device_id,
                    register_type,
                    address,
                    count,
                )
            return self._store.read_registers(
                device_id,
                register_type,
                address,
                count,
            )
        except KeyError:
            return ExcCodes.ILLEGAL_ADDRESS

    async def async_setValues(
        self,
        device_id: int,
        func_code: int,
        address: int,
        values: list[int] | list[bool],
    ) -> ExcCodes | None:
        """Sink 当前只读，拒绝全部主站写请求。"""
        del device_id, func_code, address, values
        return ExcCodes.ILLEGAL_FUNCTION

    def device_ids(self) -> list[int]:
        """返回当前暴露的 Modbus unit id。"""
        return list(self._store.unit_ids)


class ModbusTcpSinkServer:
    """Modbus Sink 的异步 TCP Server 生命周期。"""

    def __init__(
        self,
        connection: ModbusSinkConnection,
        data_path: ModbusSinkDataPath,
    ) -> None:
        self._connection = connection
        self._data_path = data_path
        self._server: ModbusTcpServer | None = None
        self._healthy = False

    @property
    def port(self) -> int:
        """返回配置监听端口。"""
        return self._connection.port

    async def start(self) -> None:
        """绑定 TCP 地址并后台启动 server；重复启动安全。"""
        if self._server is not None:
            return
        context = StoreBackedModbusContext(self._data_path.store)
        server = ModbusTcpServer(
            context,
            address=(self._connection.host, self._connection.port),
        )
        try:
            await server.serve_forever(background=True)
        except Exception:
            await server.shutdown()
            raise
        self._server = server
        self._healthy = True

    async def stop(self) -> None:
        """关闭 server 并释放监听端口；重复停止安全。"""
        server, self._server = self._server, None
        if server is None:
            self._healthy = False
            return
        try:
            await server.shutdown()
        finally:
            self._healthy = False

    def health(self) -> HealthStatus:
        """返回缓存的监听健康状态。"""
        if not self._healthy:
            return HealthStatus(healthy=False, message="not listening")
        return HealthStatus(healthy=True)


__all__ = ["StoreBackedModbusContext", "ModbusTcpSinkServer"]
