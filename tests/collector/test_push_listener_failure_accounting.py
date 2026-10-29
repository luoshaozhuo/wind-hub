"""ROUND2 专项：push 监听链路失效后 Collector 的可观测性。

IEC104 LISTEN 采集路径：驱动在监听器回调持续异常时会将其判失效（failed），
此后数据链路永久静默。Collector 的执行状态维度必须记账该失败，不能只有
实例簿记 RUNNING 而无任何失败信号（运维侧将看到"运行中但永不到数"）。

实例生命周期 RUNNING/STOPPED 与采集执行状态分维度是既有设计（掉线不改
RUNNING）；本测试只要求执行状态（consecutive_failures）反映 push 链路死亡。
"""

from __future__ import annotations

import asyncio

import pytest

from collector.application.config import RuntimeParams
from collector.application.device_runtime import DeviceRuntime
from collector.application.task_instance import TaskInstanceState
from collector.application.task_runtime import TaskRuntime
from collector.domain.acquisition import AcquisitionEngine
from tests.support.iec104_fake import (
    floating_data_msg,
    install_fake_connect,
    make_driver,
    make_point,
)
from tests.support.new_collector import make_collector_config, make_session

pytestmark = [pytest.mark.component, pytest.mark.asyncio]


class _RaisingDispatch:
    """模拟 NATS 派发端口持续抛出意外异常。

    生产路径的 SamplePublisher 会吞掉 publish 失败并计 dropped（不使监听
    失效）；本测试覆盖 dispatch 自身上抛的路径（如未预期的编程错误），
    驱动监听器据此判失效，采集执行状态必须记账。
    """

    async def dispatch(self, task_id: str, values: object) -> None:
        del task_id, values
        raise RuntimeError("sample dispatch failed")


async def test_push_listener_death_is_accounted_in_acquisition_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = install_fake_connect(monkeypatch)
    driver = make_driver([make_point("p1", 100)])
    await driver.connect()

    # 隔离总召：本专项只关心监听链路，GI 与失败记账无关。
    async def _no_interrogation(request: object) -> tuple[()]:
        del request
        return ()

    monkeypatch.setattr(driver, "interrogate", _no_interrogation)

    config = make_collector_config(protocol="iec104")
    session = make_session(config, driver)
    devices = DeviceRuntime({"dev1": session}, RuntimeParams(connect_timeout=0.2))
    engine = AcquisitionEngine()
    engine.attach_sample_publisher(_RaisingDispatch())
    engine.attach_device_state(devices)
    tasks = TaskRuntime(dict(config.tasks), devices, engine)
    engine.attach_acquisition_state(tasks)

    try:
        await tasks.sync_instances()
        await tasks.start_instance("t1:dev1")
        assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING

        # 设备上送一批数据；sink 派发异常使驱动监听器判失效。
        conn.deliver([floating_data_msg(1, 100, 1.0)])
        for _ in range(30):
            handle = tasks._acquisition_handles["t1:dev1"]  # noqa: SLF001
            if getattr(handle, "failed", False):
                break
            await asyncio.sleep(0.01)

        assert getattr(
            tasks._acquisition_handles["t1:dev1"],
            "failed",
            False,  # noqa: SLF001
        ), "driver listener must be invalidated after persistent callback failure"

        state = tasks.acquisition_states().get("t1:dev1")
        assert state is not None
        # 期望：push 链路死亡必须反映到采集执行状态（失败计数），
        # 不得仅留下 RUNNING 簿记而无任何失败信号。
        assert (
            state.consecutive_failures > 0
        ), "push listener death is not accounted in acquisition execution state"
    finally:
        await devices.close_all()
        await driver.close()
