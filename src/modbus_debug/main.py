import asyncio
import struct

from pymodbus.client import AsyncModbusTcpClient


HOST = "192.168.100.102"
PORT = 502
UNIT_ID = 1


def decode_int32(registers: list[int]) -> int:
    """两个16位寄存器按高字在前解析为有符号32位整数。"""
    raw = struct.pack(">HH", registers[0], registers[1])
    return struct.unpack(">i", raw)[0]


def encode_int32(value: int) -> list[int]:
    """有符号32位整数编码为两个16位寄存器，高字在前。"""
    raw = struct.pack(">i", value)
    return list(struct.unpack(">HH", raw))


async def main() -> None:
    client = AsyncModbusTcpClient(HOST, port=PORT, timeout=3.0)

    try:
        if not await client.connect():
            print("连接失败")
            return

        print("连接成功")

        # 1. 风速
        response = await client.read_input_registers(
            357,
            count=1,
            device_id=UNIT_ID,
        )

        if response.isError():
            print("读取风速失败:", response)
        else:
            wind_speed = response.registers[0]
            if wind_speed >= 0x8000:
                wind_speed -= 0x10000

            wind_speed *= 0.01
            print(f"风速: {wind_speed:.2f} m/s")

        # 2. 有功功率
        response = await client.read_input_registers(
            178,
            count=2,
            device_id=UNIT_ID,
        )

        if response.isError():
            print("读取有功功率失败:", response)
        else:
            raw_power = decode_int32(response.registers)
            active_power = raw_power * 0.01

            print("功率原始寄存器:", response.registers)
            print(f"有功功率: {active_power:.2f} kW")

        # 3. 写有功指令
        # 注意：这里示例写 1000。
        # 是否允许写、地址是否正确、是否有倍率，必须现场确认后再执行。
        value = 1000
        registers = encode_int32(value)

        print(f"准备写有功指令: {value}")
        print("写入寄存器:", registers)

        confirm = input("确认写入？输入 yes: ")

        if confirm == "yes":
            response = await client.write_registers(
                2001,
                registers,
                device_id=UNIT_ID,
            )

            if response.isError():
                print("写入失败:", response)
            else:
                print("写入成功")
        else:
            print("取消写入")

    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())