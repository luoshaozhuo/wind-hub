"""IEC104 mock server — 最小从站，支持 STARTDT 握手与总召响应。

绑定固定端口（见 :data:`IEC104_PORT`），与
``tests/fixtures/configs/devices_test.yaml`` 的 IEC104 设备端点一致。

处理能力（与驱动对接所需的最小集）：
    - ``STARTDT_ACT`` → ``STARTDT_CON``
    - ``C_IC_NA_1``（QOI=20，总召）→ ``ACT_CON`` + 数据帧 + ``ACT_TERM``
    - ``TESTFR_ACT`` → ``TESTFR_CON``
    - ``STOPDT_ACT`` → ``STOPDT_CON``

数据点预设为 ``{100: 1500.5, 200: 50.0}``，编码为 ``M_ME_NC_1``（float32）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct

logger = logging.getLogger(__name__)

START_CHAR = 0x68
IEC104_PORT = 12404

_STARTDT_CON = bytes([0x68, 0x04, 0x0B, 0x00, 0x00, 0x00])
_TESTFR_CON = bytes([0x68, 0x04, 0x83, 0x00, 0x00, 0x00])
_STOPDT_CON = bytes([0x68, 0x04, 0x23, 0x00, 0x00, 0x00])


def _build_apdu(body: bytes) -> bytes:
    """APDU = 0x68 | len | body."""
    return bytes([START_CHAR, len(body)]) + body


def _encode_i_frame(send_seq: int, recv_seq: int, asdu: bytes) -> bytes:
    """Encode an I-frame: control(4B) + asdu."""
    ctrl = struct.pack("<HH", (send_seq << 1) & 0xFFFF, (recv_seq << 1) & 0xFFFF)
    return _build_apdu(ctrl + asdu)


def _make_m_me_nc_1_asdu(ioa: int, value: float, common_addr: int) -> bytes:
    """TypeID=13 (M_ME_NC_1), COT=INTERROGATED_BY_STATION, single info object."""
    header = bytearray()
    header.append(0x0D)  # TypeID
    header.append(0x01)  # VSQ = count=1, SQ=0
    header.append(0x14)  # COT = 20 (interrogated by station)
    header.append(0x00)  # OA
    header.extend(struct.pack("<H", common_addr))  # CA

    body = bytearray()
    body.extend(struct.pack("<I", ioa)[:3])  # IOA (3 bytes LE)
    body.extend(struct.pack("<f", value))  # float32
    body.append(0x00)  # QDS = good
    return bytes(header) + bytes(body)


def _make_c_ic_na_1_asdu(cause: int, ioa: int, common_addr: int) -> bytes:
    """TypeID=100 (C_IC_NA_1) for activation con / term."""
    header = bytearray()
    header.append(0x64)  # TypeID
    header.append(0x01)  # VSQ
    header.append(cause & 0x3F)  # COT
    header.append(0x00)  # OA
    header.extend(struct.pack("<H", common_addr))  # CA

    body = bytearray()
    body.extend(struct.pack("<I", ioa)[:3])  # IOA
    body.append(0x14)  # QOI = 20 (station)
    return bytes(header) + bytes(body)


class IEC104MockServer:
    """异步生命周期的 IEC104 从站，可反复起停（供故障恢复测试）。"""

    def __init__(
        self,
        port: int = IEC104_PORT,
        common_addr: int = 1,
        data_points: dict[int, float] | None = None,
    ) -> None:
        self._port = port
        self._common_addr = common_addr
        self._data_points = data_points or {100: 1500.5, 200: 50.0}
        self._server: asyncio.AbstractServer | None = None
        # 活动客户端连接——``stop()`` 必须主动断开它们：asyncio 的
        # ``Server.close()`` 只停止接受新连接，不断开既有连接；不主动断开
        # 的话故障注入（断连恢复测试）对客户端不可见。
        self._clients: set[asyncio.StreamWriter] = set()

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=self._port,
        )

    async def stop(self) -> None:
        for writer in list(self._clients):
            writer.close()
        for writer in list(self._clients):
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._clients.add(writer)
        send_seq = 0
        recv_seq = 0
        try:
            while True:
                frame = await self._read_frame(reader)
                if frame is None:
                    break
                ctrl = frame[2:6]
                asdu_data = frame[6:]

                if (ctrl[0] & 0x01) == 0:  # I-frame
                    peer_send = (struct.unpack_from("<H", ctrl, 0)[0] >> 1) & 0x7FFF
                    recv_seq = (peer_send + 1) & 0x7FFF
                    # 响应可能包含多帧（总召响应 = ACT_CON + N 数据 + ACT_TERM），
                    # 发送序号由处理器实际发出的帧数推进，不能按请求数 +1。
                    send_seq = await self._handle_i_frame(
                        writer, asdu_data, send_seq, recv_seq
                    )
                elif (ctrl[0] & 0x03) == 0x01:  # S-frame
                    continue
                elif (ctrl[0] & 0x03) == 0x03:  # U-frame
                    await self._handle_u_frame(writer, ctrl[0])
        except Exception:
            logger.exception("IEC104 mock server: error handling client")
        finally:
            self._clients.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytearray | None:
        try:
            data = await reader.readexactly(2)
        except asyncio.IncompleteReadError:
            return None
        if data[0] != START_CHAR:
            return None
        apdu_len = data[1]
        try:
            rest = await reader.readexactly(apdu_len)
        except asyncio.IncompleteReadError:
            return None
        return bytearray(data) + bytearray(rest)

    async def _handle_u_frame(self, writer: asyncio.StreamWriter, func_code: int) -> None:
        if func_code == 0x07:  # STARTDT_ACT
            writer.write(_STARTDT_CON)
            await writer.drain()
        elif func_code == 0x13:  # STOPDT_ACT
            writer.write(_STOPDT_CON)
            await writer.drain()
        elif func_code == 0x43:  # TESTFR_ACT
            writer.write(_TESTFR_CON)
            await writer.drain()

    async def _handle_i_frame(
        self,
        writer: asyncio.StreamWriter,
        asdu_data: bytes,
        send_seq: int,
        recv_seq: int,
    ) -> int:
        """处理 I-frame 并返回更新后的发送序号。"""
        if len(asdu_data) < 6:
            return send_seq
        type_id = asdu_data[0]
        cause = asdu_data[2] & 0x3F
        if type_id == 0x64 and cause == 0x06:  # C_IC_NA_1 activation
            return await self._send_interrogation_response(writer, send_seq, recv_seq)
        return send_seq

    async def _send_interrogation_response(
        self,
        writer: asyncio.StreamWriter,
        send_seq: int,
        recv_seq: int,
    ) -> int:
        """发送总召响应（ACT_CON + 全部数据点 + ACT_TERM），返回新发送序号。"""
        act_con = _make_c_ic_na_1_asdu(0x07, 0, self._common_addr)
        writer.write(_encode_i_frame(send_seq, recv_seq, act_con))
        await writer.drain()
        send_seq = (send_seq + 1) & 0x7FFF

        for ioa, value in self._data_points.items():
            meas = _make_m_me_nc_1_asdu(ioa, value, self._common_addr)
            writer.write(_encode_i_frame(send_seq, recv_seq, meas))
            await writer.drain()
            send_seq = (send_seq + 1) & 0x7FFF

        act_term = _make_c_ic_na_1_asdu(0x0A, 0, self._common_addr)
        writer.write(_encode_i_frame(send_seq, recv_seq, act_term))
        await writer.drain()
        return (send_seq + 1) & 0x7FFF


# 供测试代码引用（避免 Any 泄漏到断言处）。
def make_server() -> IEC104MockServer:
    return IEC104MockServer()


__all__ = ["IEC104MockServer", "IEC104_PORT", "make_server"]
