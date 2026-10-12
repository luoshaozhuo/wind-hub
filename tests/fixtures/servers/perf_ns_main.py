"""在独立 net namespace 中以子进程运行三协议压测 server 的入口。

用法::

    ip netns exec <ns> python -m tests.fixtures.servers.perf_ns_main \
        --protocol modbus --host 10.99.0.2 --port 10502 --num-points 500

与 ``modbus_ns_main`` 同理：同命名空间的本机投递走 lo 回环、绕过 veth
接口的 qdisc，netem 不生效——压测/soak 的 server 必须活在 veth 对端的
namespace 里。就绪后在 stdout 打印一行 ``READY <host>:<port>``。
"""

from __future__ import annotations

import argparse
import asyncio

from tests.performance.servers import (
    start_ads_server,
    start_iec104_server,
    start_modbus_server,
)


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", choices=["modbus", "iec104", "ads"], required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--num-points", type=int, default=600)
    parser.add_argument("--device-id", default="perf-iec104")
    args = parser.parse_args()

    if args.protocol == "modbus":
        cm = start_modbus_server(args.host, args.port)
    elif args.protocol == "iec104":
        cm = start_iec104_server(
            args.host, args.port, num_points=args.num_points, device_id=args.device_id
        )
    else:
        cm = start_ads_server(args.host, args.port, num_vars=args.num_points)

    async with cm:
        print(f"READY {args.host}:{args.port}", flush=True)
        await asyncio.Event().wait()  # 直到被调用方 terminate


if __name__ == "__main__":
    asyncio.run(_main())
