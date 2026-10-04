"""CollectorRuntime — 运行时组件管理与生命周期编排。"""

from wind_hub_collector.application.runtime.acquisition_state import AcquisitionRuntimeState
from wind_hub_collector.application.runtime.device import (
    AcquisitionHandle,
    CollectorDeviceSession,
    PollingAcquisitionHandle,
)
from wind_hub_collector.application.runtime.device_runtime import DeviceRuntime
from wind_hub_collector.application.runtime.device_state import DeviceRuntimeState
from wind_hub_collector.application.runtime.dispatcher import SinkDispatcher
from wind_hub_collector.application.runtime.lifecycle import RuntimeLifecycle
from wind_hub_collector.application.runtime.runtime import CollectorRuntime
from wind_hub_collector.application.runtime.task_instance import (
    CollectionTaskInstance,
    TaskInstanceState,
    task_instance_id,
)

__all__ = [
    "AcquisitionHandle",
    "AcquisitionRuntimeState",
    "CollectionTaskInstance",
    "CollectorDeviceSession",
    "DeviceRuntime",
    "DeviceRuntimeState",
    "PollingAcquisitionHandle",
    "CollectorRuntime",
    "RuntimeLifecycle",
    "SinkDispatcher",
    "TaskInstanceState",
    "task_instance_id",
]
