from __future__ import annotations

from wind_hub_collector.application.usecase.task import TaskInstanceDetail
from wind_hub_collector.assembly import CollectorApp


async def start_task_instance(
    rt: CollectorApp,
    instance_id: str,
) -> TaskInstanceDetail:
    return await rt.tasks.start_instance(instance_id)
