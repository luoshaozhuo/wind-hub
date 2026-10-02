from __future__ import annotations

from wind_hub.application.usecase.task import TaskInstanceDetail
from wind_hub.assembly import AssembledRuntime


async def start_task_instance(
    rt: AssembledRuntime,
    instance_id: str,
) -> TaskInstanceDetail:
    return await rt.tasks.start_instance(instance_id)


async def start_all_task_instances(rt: AssembledRuntime) -> None:
    await rt.tasks.start_all_instances()
