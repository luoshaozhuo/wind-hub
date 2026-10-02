"""单条 IEC104 主站 TCP 连接的从站 session。

session 持续读取 APDU，处理 STARTDT/TESTFR/STOPDT U-frame 握手，并把已解码
I-frame ASDU 交给共享 IEC104SlaveHandlers。send_asdu 负责用当前 N(S)/N(R)
封装 I-frame，其中 N(R) 同时确认主站最近收到的 I-frame。

该实现只提供当前代理所需的最小流控语义，不负责跨 session 状态共享；每条连接
独立维护序号和 STARTDT 状态。
"""

from __future__ import annotations

import asyncio
import logging

from wind_hub.adapter.inbound.iec104_slave.handlers import IEC104SlaveHandlers
from wind_hub_core.protocol.iec104.codec import (
    ASDU,
    CauseOfTransmission,
    IFrame,
    SFrame,
    TypeID,
    UFrame,
    UFrameType,
    decode_apdu,
    decode_asdu,
    encode_asdu,
    encode_i_frame,
    encode_u_frame,
)

logger = logging.getLogger(__name__)

START_CHAR = 0x68
MAX_SEQ = 0x7FFF

# 可映射为远程控制 Command 的控制方向 TypeID。
_COMMAND_TYPE_IDS: frozenset[TypeID] = frozenset(
    {TypeID.C_SC_NA_1, TypeID.C_DC_NA_1, TypeID.C_SE_NC_1}
)


class IEC104SlaveSession:
    """绑定单个 reader/writer 的 IEC104 从站 TCP session。

    Args:
        reader: 当前 TCP 连接的 StreamReader。
        writer: 当前 TCP 连接的 StreamWriter。
        handlers: 共享的请求处理器。
        common_address: 从站公共地址。
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        handlers: IEC104SlaveHandlers,
        common_address: int,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._handlers = handlers
        self._common_address = common_address

        self._send_seq = 0
        self._recv_seq = 0
        self._started = False

    async def run(self) -> None:
        """运行帧循环，直到对端断开或读取失败。

        socket 解码/处理异常向上抛给 Server，由 Server 记录并统一释放 writer。
        """
        while True:
            frame_bytes = await self._read_frame()
            if frame_bytes is None:
                return

            frame = decode_apdu(frame_bytes)
            if isinstance(frame, UFrame):
                await self._handle_u_frame(frame)
            elif isinstance(frame, SFrame):
                # S-frame 不携带业务数据，只更新对端确认序号。
                self._recv_seq = (frame.recv_seq + 1) & MAX_SEQ
            elif isinstance(frame, IFrame):
                self._recv_seq = (frame.send_seq + 1) & MAX_SEQ
                if not self._started:
                    continue
                asdu, _ = decode_asdu(frame.asdu)
                await self._dispatch(asdu)

    async def send_asdu(self, asdu: ASDU, negative: bool = False) -> None:
        """把 ASDU 封装为 I-frame 并发送。

        Args:
            asdu: 待发送的 ASDU。
            negative: True 时设置 COT 的 P/N 位，表示否定确认。

        Side Effects:
            递增本 session 的 N(S)，并通过 writer 写入 socket。
        """
        raw = encode_asdu(asdu)
        if negative:
            raw = raw[:2] + bytes([raw[2] | 0x80]) + raw[3:]
        seq = self._send_seq
        self._send_seq = (self._send_seq + 1) & MAX_SEQ
        await self._write(encode_i_frame(seq, self._recv_seq, raw))

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _read_frame(self) -> bytes | None:
        """读取一个完整 APDU。

        Returns:
            完整 APDU 字节；EOF、起始字符非法或半帧结束时返回 None。
        """
        try:
            header = await self._reader.readexactly(2)
        except asyncio.IncompleteReadError:
            return None
        if header[0] != START_CHAR:
            return None
        try:
            rest = await self._reader.readexactly(header[1])
        except asyncio.IncompleteReadError:
            return None
        return header + rest

    async def _handle_u_frame(self, frame: UFrame) -> None:
        if frame.frame_type == UFrameType.STARTDT_ACT:
            self._started = True
            logger.debug(
                "IEC104 slave: STARTDT (common_address=%d) from peer", self._common_address
            )
            await self._write(encode_u_frame(UFrameType.STARTDT_CON))
        elif frame.frame_type == UFrameType.STOPDT_ACT:
            self._started = False
            await self._write(encode_u_frame(UFrameType.STOPDT_CON))
        elif frame.frame_type == UFrameType.TESTFR_ACT:
            await self._write(encode_u_frame(UFrameType.TESTFR_CON))
        # 这些 *_CON 通常由从站发出；本 session 作为从站收到时不参与状态推进，直接忽略。

    async def _dispatch(self, asdu: ASDU) -> None:
        if asdu.type_id == TypeID.C_IC_NA_1 and asdu.cause == CauseOfTransmission.ACTIVATION:
            await self._handlers.handle_interrogation(asdu, self)
        elif asdu.type_id in _COMMAND_TYPE_IDS and asdu.cause == CauseOfTransmission.ACTIVATION:
            await self._handlers.handle_command(asdu, self)

    async def _write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()
