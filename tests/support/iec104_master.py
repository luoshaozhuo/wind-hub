"""IEC104 测试主站——真实 TCP + 真实 APCI/ASDU 编解码的最小 master。

供需要验证 IEC104 从站（Sink / slave server）协议行为的测试使用：
STARTDT 握手、总召（C_IC_NA_1）、单点命令。只用仓库自己的 codec，
不经过被测的 server 实现，保证验证链路独立。
"""

from __future__ import annotations

import asyncio
import contextlib

from wind_hub_core.protocol.iec104.codec import (
    ASDU,
    CauseOfTransmission,
    IFrame,
    InterrogationCommand,
    SingleCommand,
    TypeID,
    UFrameType,
    decode_apdu,
    decode_asdu,
    encode_asdu,
    encode_i_frame,
    encode_u_frame,
)


class IEC104MasterClient:
    """最小 IEC104 主站：connect → STARTDT → 总召 / 单点命令。"""

    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self._host = host
        self._port = port
        self._send_seq = 0
        self._recv_seq = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        """建立 TCP 连接（尚未 STARTDT）。"""
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port
        )

    async def close(self) -> None:
        """关闭 TCP 连接。"""
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()

    async def startdt(self) -> None:
        """发送 STARTDT_ACT 并等待 STARTDT_CON。"""
        await self._send_u(UFrameType.STARTDT_ACT)
        while True:
            raw = await self._read_frame()
            assert raw is not None
            frame = decode_apdu(raw)
            if getattr(frame, "frame_type", None) == UFrameType.STARTDT_CON:
                return

    async def interrogate(self) -> list[ASDU]:
        """发起站总召并收集到 ACTIVATION_TERMINATION 为止的全部 ASDU。"""
        await self._send_i(
            ASDU(
                type_id=TypeID.C_IC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
                objects=[InterrogationCommand(ioa=0)],
            )
        )
        return await self._read_asdus(
            (TypeID.C_IC_NA_1, CauseOfTransmission.ACTIVATION_TERMINATION)
        )

    async def send_single_command(self, ioa: int, value: bool) -> tuple[ASDU, bool]:
        """发送 C_SC_NA_1 单点命令，返回（应答 ASDU, 是否否定应答）。"""
        await self._send_i(
            ASDU(
                type_id=TypeID.C_SC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
                objects=[SingleCommand(ioa=ioa, value=value)],
            )
        )
        while True:
            raw = await self._read_frame()
            assert raw is not None
            frame = decode_apdu(raw)
            if not isinstance(frame, IFrame):
                continue
            self._recv_seq = (frame.send_seq + 1) & 0x7FFF
            asdu, _ = decode_asdu(frame.asdu)
            return asdu, bool(frame.asdu[2] & 0x80)

    async def _send_i(self, asdu: ASDU) -> None:
        assert self._writer is not None
        seq = self._send_seq
        self._send_seq = (self._send_seq + 1) & 0x7FFF
        self._writer.write(encode_i_frame(seq, self._recv_seq, encode_asdu(asdu)))
        await self._writer.drain()

    async def _send_u(self, frame_type: UFrameType) -> None:
        assert self._writer is not None
        self._writer.write(encode_u_frame(frame_type))
        await self._writer.drain()

    async def _read_frame(self) -> bytes | None:
        assert self._reader is not None
        try:
            header = await self._reader.readexactly(2)
        except asyncio.IncompleteReadError:
            return None
        rest = await self._reader.readexactly(header[1])
        return header + rest

    async def _read_asdus(self, stop: tuple[TypeID, CauseOfTransmission]) -> list[ASDU]:
        asdus: list[ASDU] = []
        while True:
            frame_bytes = await self._read_frame()
            assert frame_bytes is not None
            frame = decode_apdu(frame_bytes)
            if not isinstance(frame, IFrame):
                continue
            self._recv_seq = (frame.send_seq + 1) & 0x7FFF
            asdu, _ = decode_asdu(frame.asdu)
            asdus.append(asdu)
            if asdu.type_id == stop[0] and asdu.cause == stop[1]:
                return asdus
