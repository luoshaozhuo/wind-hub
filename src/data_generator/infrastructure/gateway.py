"""Gateway UpdateVariables adapter with per-target outcome accounting."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence

import grpc

from contracts import common_pb2, industrial_gateway_pb2, industrial_gateway_pb2_grpc
from data_generator.application.config import Scenario
from data_generator.domain.model import DataBatch, DeliveryResult, OutputTarget, Scalar


def scalar_message(value: Scalar) -> common_pb2.ScalarValue:
    result = common_pb2.ScalarValue()
    if type(value) is bool:
        result.bool_value = value
    elif type(value) is int:
        if value < 0:
            result.sint64_value = value
        else:
            result.uint64_value = value
    elif type(value) is float:
        result.double_value = value
    elif type(value) is str:
        result.string_value = value
    elif type(value) is bytes:
        result.bytes_value = value
    else:
        raise ValueError("unsupported scalar value")
    return result


class GatewayWriter:
    """One in-flight update per Gateway; unknown outcomes are not retried."""

    def __init__(self, timeout: float = 3.0, max_points: int = 1000) -> None:
        self._timeout = timeout
        self._max_points = max_points
        self._channels: dict[str, grpc.aio.Channel] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: set[asyncio.Task[object]] = set()
        self._unsettled: dict[str, asyncio.Task[object]] = {}

    async def send(self, scenario: Scenario, batch: DataBatch) -> list[DeliveryResult]:
        values = {(point.device_id, point.point_id): point.value for point in batch.points}
        groups: dict[str, list[tuple[OutputTarget, Scalar]]] = defaultdict(list)
        for mapping in scenario.mappings:
            key = (mapping.device_id, mapping.point_id)
            if key in values:
                groups[mapping.target.gateway_id].append((mapping.target, values[key]))
        results: list[DeliveryResult] = []
        for gateway_id, entries in groups.items():
            for start in range(0, len(entries), self._max_points):
                results.extend(await self._send_one(scenario, gateway_id,
                                                    entries[start:start+self._max_points]))
        return results

    async def _send_one(
        self, scenario: Scenario, gateway_id: str,
        entries: Sequence[tuple[OutputTarget, Scalar]],
    ) -> list[DeliveryResult]:
        async with self._locks[gateway_id]:
            prior = self._unsettled.get(gateway_id)
            if prior is not None and not prior.done():
                # Do not overlap a timed-out write with a later value for that target.
                return [DeliveryResult(target, "UNKNOWN", "UNSETTLED_IO")
                        for target, _ in entries]
            self._unsettled.pop(gateway_id, None)
            endpoint = scenario.gateways[gateway_id]
            if endpoint not in self._channels:
                self._channels[endpoint] = grpc.aio.insecure_channel(endpoint)
            stub = industrial_gateway_pb2_grpc.IndustrialGatewayServiceStub(
                self._channels[endpoint]
            )
            request = industrial_gateway_pb2.UpdateVariablesRequest(
                gateway_id=gateway_id,
                values=[industrial_gateway_pb2.VariableUpdate(
                    variable_id=target.variable_id, value=scalar_message(value)
                ) for target, value in entries],
            )
            task = asyncio.ensure_future(stub.UpdateVariables(request))
            self._unsettled[gateway_id] = task
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
            try:
                response = await asyncio.wait_for(asyncio.shield(task), self._timeout)
            except (TimeoutError, grpc.RpcError, asyncio.CancelledError):
                return [DeliveryResult(target, "UNKNOWN") for target, _ in entries]
            received = {result.variable_id: result for result in response.results}
            return [
                DeliveryResult(target,
                               "SUCCESS" if received[target.variable_id].success else "FAILED",
                               received[target.variable_id].error_code,
                               received[target.variable_id].message)
                if target.variable_id in received else DeliveryResult(target, "UNKNOWN")
                for target, _ in entries
            ]

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        self._unsettled.clear()

    async def close(self) -> None:
        await self.drain()
        for channel in self._channels.values():
            await channel.close()
        self._channels.clear()
