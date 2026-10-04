"""在独立 net namespace 中以子进程运行 ModbusMockServer 的入口。

用法::

    ip netns exec <ns> python -m tests.fixtures.servers.modbus_ns_main \
        --host 10.99.0.2 --port 15020

仅供 ``tests/reliability/network`` 使用：网络降级注入要求采集流量真实
穿越 veth pair（同命名空间的本机投递走 lo 回环、绕过 qdisc，netem 不
生效），因此 server 必须活在 veth 对端的 namespace 里。就绪后在
stdout 打印一行 ``READY <host>:<port>`` 供 fixture 等待。
"""

from __future__ import annotations

import argparse
import asyncio

from tests.fixtures.servers.modbus_server import ModbusMockServer


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    server = ModbusMockServer(port=args.port, host=args.host)
    await server.start()
    print(f"READY {args.host}:{args.port}", flush=True)
    await asyncio.Event().wait()  # 直到被 fixture terminate


if __name__ == "__main__":
    asyncio.run(_main())
