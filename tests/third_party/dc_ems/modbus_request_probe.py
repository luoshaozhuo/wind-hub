"""透明 Modbus TCP 代理：记录 Collector 实际发出的读取请求。

仅用于隔离验收，不修改报文、不连接任何生产或共享资源。每个监听端口
对应一台源端设备，上游固定为同一宿主机 Gateway2 的真实端口。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import struct
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def _parse_request(pdu: bytes) -> tuple[int, int, int]:
    """返回 (功能码, 起始地址, 数量)；非读写请求返回 (功能码, -1, -1)。"""
    function = pdu[0]
    if function in (1, 2, 3, 4, 15, 16) and len(pdu) >= 5:
        start, quantity = struct.unpack(">HH", pdu[1:5])
        return function, start, quantity
    return function, -1, -1


class Probe:
    """一组固定映射的透明代理，并记录客户端到上游的请求序列。"""

    def __init__(self, listen_base: int, target_base: int, count: int, output: Path) -> None:
        self._listen_base = listen_base
        self._target_base = target_base
        self._count = count
        self._output = output
        self._servers: dict[int, asyncio.AbstractServer] = {}
        self._stop = asyncio.Event()
        self._records: list[dict[str, object]] = []
        self._counts: Counter[tuple[int, int, int]] = Counter()
        self._stream = output.open("a", encoding="utf-8")

    def _record(
        self, listen_port: int, function: int, address: int, quantity: int, raw_hex: str
    ) -> None:
        self._counts[(function, address, quantity)] += 1
        record = {
            "at": datetime.now(UTC).isoformat(),
            "listen_port": listen_port,
            "function": function,
            "address": address,
            "quantity": quantity,
            "raw_hex": raw_hex,
        }
        self._records.append(record)
        self._stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._stream.flush()

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            # 上游断开时向 DCEMS 侧发 RST，使其立即感知连接失败，
            # 与直连 Gateway1 时的失败语义保持一致。
            writer.transport.abort()

    async def _client(
        self,
        listen_port: int,
        target_port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", target_port
            )
        except OSError:
            # Gateway1 未监听时立即中止 DCEMS 连接，不留下等待响应的半开连接。
            writer.transport.abort()
            return
        reverse = asyncio.create_task(self._pipe(upstream_reader, writer))
        try:
            while True:
                header = await reader.readexactly(6)
                length = struct.unpack(">H", header[4:6])[0]
                # MBAP length covers the unit id plus the PDU.
                body = await reader.readexactly(length)
                frame = header + body
                function, address, quantity = _parse_request(body[1:])
                self._record(listen_port, function, address, quantity, frame.hex())
                upstream_writer.write(frame)
                await upstream_writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            reverse.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reverse
            upstream_writer.close()

    def close(self) -> None:
        self._stream.close()

    def summary(self) -> dict[str, object]:
        return {
            "requests": len(self._records),
            "by_function_address_quantity": [
                {"function": key[0], "address": key[1], "quantity": key[2], "count": value}
                for key, value in sorted(self._counts.items())
            ],
        }

    async def _start_listener(self, offset: int) -> None:
        listen = self._listen_base + offset
        target = self._target_base + offset

        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
            listen_port: int = listen,
            target_port: int = target,
        ) -> None:
            await self._client(listen_port, target_port, reader, writer)

        self._servers[offset] = await asyncio.start_server(handler, "127.0.0.1", listen)

    @staticmethod
    async def _reachable(port: int) -> bool:
        """探测 Gateway 端口是否在监听，决定代理是否对外提供服务。"""
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.5
            )
        except (OSError, TimeoutError):
            return False
        writer.close()
        return True

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for stop_signal in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(stop_signal, self._stop.set)
        # 只在上游 Gateway 可达时代理监听：上游关闭时本地端口同样拒绝连接，
        # 使 DCEMS 观察到与直连完全一致的 refused 失败语义（D6 断连用例）。
        while not self._stop.is_set():
            for offset in range(self._count):
                reachable = await self._reachable(self._target_base + offset)
                if reachable and offset not in self._servers:
                    await self._start_listener(offset)
                elif not reachable and offset in self._servers:
                    self._servers.pop(offset).close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=0.2)
        for server in self._servers.values():
            server.close()
        self._servers.clear()


def main() -> None:
    """命令行入口：记录 JSONL 并在退出时打印汇总。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-base", type=int, default=15231)
    parser.add_argument("--target-base", type=int, default=15221)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    probe = Probe(args.listen_base, args.target_base, args.count, args.output)

    async def run() -> None:
        await probe.run()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        probe.close()
        summary = args.output.with_name(args.output.stem + "-summary.json")
        summary.write_text(json.dumps(probe.summary(), indent=2) + "\n", encoding="utf-8")
        print(json.dumps(probe.summary(), indent=2), file=sys.stdout)


if __name__ == "__main__":
    main()
