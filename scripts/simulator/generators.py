"""确定性数据生成器：seeded 正弦 + 小幅噪声，所有设备共享点模型。

点模型（三协议一致的风机语义）：

- 遥测（point_group ``all``）：active_power / wind_speed / rotor_speed /
  generator_temp / pitch_angle（float32）、fault_code / turbine_status（int）
- 控制（point_group ``control``，可写）：power_setpoint（float32）/
  turbine_command（int）

同一 seed 在任何进程重启后产生同一相位/噪声序列——验收可复现。
"""

from __future__ import annotations

import math
import random
import struct
from dataclasses import dataclass

TELEMETRY_FLOAT_POINTS = (
    "active_power",
    "wind_speed",
    "rotor_speed",
    "generator_temp",
    "pitch_angle",
)
TELEMETRY_INT_POINTS = ("fault_code", "turbine_status")
CONTROL_FLOAT_POINTS = ("power_setpoint",)
CONTROL_INT_POINTS = ("turbine_command",)
ALL_POINTS = (
    *TELEMETRY_FLOAT_POINTS,
    *TELEMETRY_INT_POINTS,
    *CONTROL_FLOAT_POINTS,
    *CONTROL_INT_POINTS,
)

#: 点名 → (基值, 振幅, 角频率 rad/s)
_BASE = {
    "active_power": (1500.0, 300.0, 0.05),
    "wind_speed": (9.0, 2.5, 0.11),
    "rotor_speed": (14.0, 2.0, 0.07),
    "generator_temp": (62.0, 4.0, 0.03),
    "pitch_angle": (4.0, 1.5, 0.09),
}


@dataclass(frozen=True)
class PointSnapshot:
    """一个设备一轮生成的全部点值。"""

    floats: dict[str, float]
    ints: dict[str, int]


class DeviceGenerator:
    """单台设备的确定性波形发生器。"""

    def __init__(self, seed: int) -> None:
        rng = random.Random(seed)
        self._phases = {name: rng.uniform(0, 2 * math.pi) for name in _BASE}
        self._noise = random.Random(seed + 1)
        self._tick = 0
        # 控制点初值（写入后保持，不参与波形）。
        self._power_setpoint = 0.0
        self._turbine_command = 0

    def next(self, dt_s: float = 1.0) -> PointSnapshot:
        """推进一个周期并返回全点快照。"""
        self._tick += 1
        t = self._tick * dt_s
        floats: dict[str, float] = {}
        for name, (base, amp, omega) in _BASE.items():
            value = base + amp * math.sin(omega * t + self._phases[name])
            value += self._noise.uniform(-0.005, 0.005) * base
            floats[name] = round(value, 4)
        ints = {"fault_code": 0, "turbine_status": 3}
        floats["power_setpoint"] = self._power_setpoint
        ints["turbine_command"] = self._turbine_command
        return PointSnapshot(floats=floats, ints=ints)

    def set_control(self, name: str, value: float | int) -> None:
        """控制点写入（Commander 写命令落到模拟器）。"""
        if name == "power_setpoint":
            self._power_setpoint = float(value)
        elif name == "turbine_command":
            self._turbine_command = int(value)
        else:
            raise KeyError(name)


def float32_regs_be(value: float) -> list[int]:
    """float32 → 2 个 Modbus 寄存器（big-endian 字节序，配合
    ``word_order: big_endian`` 的设备配置）。"""
    return list(struct.unpack(">HH", struct.pack(">f", value)))


def regs_to_float32(regs: list[int]) -> float:
    """2 个 Modbus 寄存器（big-endian）→ float32（写命令解码用）。"""
    return struct.unpack(">f", struct.pack(">HH", regs[0], regs[1]))[0]
