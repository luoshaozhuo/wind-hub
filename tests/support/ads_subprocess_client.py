"""独立进程 ADS 通知客户端（SIGKILL 泄漏测试用）。

用法::

    python -m tests.support.ads_subprocess_client <server_ip> <count>

在独立 OS 进程中用 pyads 直连 AdsMemoryServer，注册 ``count`` 个 Device
Notification，打印 ``READY <count>`` 后常驻，直到被外部信号杀死。
刻意不注册任何清理钩子：SIGKILL 场景下客户端无法注销 PLC 侧通知句柄，
服务端残留只能由测试侧观测。
"""

from __future__ import annotations

import sys
import time
from ctypes import c_ubyte

import pyads  # type: ignore[import-untyped]

_GROUP = 0x4020
_BLOCK_SIZE = 8


def main() -> None:
    server_ip = sys.argv[1]
    count = int(sys.argv[2])
    net_id = f"{server_ip}.2.1" if not server_ip.replace(".", "").isdigit() else "127.0.0.1.2.1"
    # AdsMemoryServer 各 loopback 共用同一目标 Net ID。
    net_id = "127.0.0.1.2.1"

    connection = pyads.Connection(net_id, 851, server_ip)
    connection.open()
    handles = []
    try:
        attr = pyads.NotificationAttrib(
            _BLOCK_SIZE,
            trans_mode=pyads.ADSTRANS_SERVERCYCLE,
            cycle_time=10000,
        )
        for index in range(count):
            buffer_type = c_ubyte * _BLOCK_SIZE

            @connection.notification(buffer_type)  # type: ignore[untyped-decorator]
            def callback(  # noqa: ANN001
                notification,
                data,
                timestamp,
            ) -> None:
                del notification, data, timestamp

            handle = connection.add_device_notification(
                (_GROUP, index * _BLOCK_SIZE),
                attr,
                callback,
            )
            handles.append(handle)
        # 输出服务端分配的 Notification Handle，供测试侧证明重启客户端
        # 后服务端分配的是新句柄、旧句柄不会被复用。
        handle_ids = ",".join(str(handle[0]) for handle in handles)
        print(f"READY {len(handles)} HANDLES {handle_ids}", flush=True)
        while True:
            time.sleep(0.1)
    finally:
        # SIGKILL 不会执行这里；正常退出路径保留以便手工联调。
        for handle in handles:
            connection.del_device_notification(handle)
        connection.close()


if __name__ == "__main__":
    main()
