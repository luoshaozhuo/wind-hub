"""Realtime Delivery application lifecycle and ownership decisions."""

from __future__ import annotations

from sink_service.application.config import template_document as _template
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.latency_window import HistogramSnapshot, LatencyWindow
from sink_service.application.output_pool import _QuotaWriter
from sink_service.application.ports.realtime import RealtimeIngressPort, SinkOutputPoolPort
from sink_service.application.realtime import RealtimeEngine
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot


class RealtimeDeliveryPort:
    """A real Delivery owns NATS registration and latest-value output scheduling."""

    def __init__(
        self,
        definition: DeliveryDefinition,
        snapshot: ValidatedSnapshot,
        pool: SinkOutputPoolPort,
        ingress: RealtimeIngressPort,
    ) -> None:
        self._id = definition.delivery_id
        self._sink_id = definition.sink_id
        self._snapshot = snapshot
        self._pool = pool
        self._ingress = ingress
        self._engine: RealtimeEngine | None = None
        self._writer: _QuotaWriter | None = None
        self._stopping = False
        self._retained: dict[str, float] = {}
        self._retained_latency = LatencyWindow(
            window_ms=snapshot.service["metrics"]["metrics_window_ms"]
        )

    def inspect(self) -> dict[str, str]:
        """Return cached, non-invasive transport and ownership evidence."""
        return {
            "source": "CONNECTED" if self._ingress.connected else "DISCONNECTED",
            "delivery": "RUNNING" if self._engine is not None else "STOPPED",
            "sink": "OWNED" if self._writer is not None else "NOT_ATTACHED",
            "io_inflight": str(self._writer.io_inflight if self._writer is not None else False),
            "target": "NOT_PROBED",
        }

    @property
    def metrics(self) -> dict[str, float]:
        totals = dict(self._retained)
        engine = self._engine
        if engine is not None:
            for name, value in (
                ("received_total", engine.received_total),
                ("dropped_total", engine.dropped_total),
                ("delivered_total", engine.delivered_total),
                ("failed_total", engine.failed_total),
                ("write_success_total", engine.write_success_total),
            ):
                totals[name] = totals.get(name, 0.0) + float(value)
        totals["io_inflight"] = float(
            self._writer.io_inflight if self._writer is not None else False
        )
        return totals

    @property
    def latency_histograms(self) -> tuple[HistogramSnapshot, ...]:
        active = self._engine
        snapshots = [self._retained_latency.snapshot()]
        if active is not None:
            snapshots.append(active.latency_window.snapshot())
        return tuple(snapshots)

    @property
    def last_error(self) -> str:
        if self._engine is None:
            return ""
        if not self._ingress.connected:
            return "NATS realtime source disconnected"
        return self._engine.last_error

    async def start(self) -> None:
        if self._stopping:
            raise ConfigurationError("previous Delivery stop has not safely settled")
        if self._engine is not None:
            return
        sink = await self._pool.acquire(self._sink_id, self._snapshot)
        engine: RealtimeEngine | None = None
        try:
            instance = self._snapshot.deliveries[self._id]
            template = _template(
                self._snapshot,
                "deliveries",
                instance["template_id"],
                instance["template_version"],
            )
            output = template["output"]
            staleness = template.get("staleness", {})
            source = instance["source"]
            engine = RealtimeEngine(
                writer=sink,
                wind_farm_id=source["wind_farm_id"],
                collector_id=source["collector_id"],
                devices=frozenset(source["devices"]),
                points=frozenset(source["points"]),
                policy=output["policy"],
                period_ms=output["period_ms"],
                max_points=self._snapshot.service["runtime"]["max_pending_points"],
                max_age_ms=staleness.get("max_age_ms"),
                absolute_threshold=output.get("absolute_threshold", 0.0),
                relative_threshold=output.get("relative_threshold", 0.0),
                metrics_window_ms=self._snapshot.service["metrics"]["metrics_window_ms"],
                preserve_upstream_sample=(
                    _template(
                        self._snapshot,
                        "sinks",
                        self._snapshot.sinks[self._sink_id]["template_id"],
                        self._snapshot.sinks[self._sink_id]["template_version"],
                    )["kind"]
                    == "kafka"
                ),
            )
            await engine.start()
            await self._ingress.add(self._id, engine)
        except BaseException:
            try:
                if engine is not None:
                    await engine.stop()
            finally:
                await self._pool.release(self._sink_id)
            raise
        self._engine = engine
        self._writer = sink

    async def stop(self) -> None:
        engine = self._engine
        if engine is None:
            return
        self._stopping = True
        # Keep ownership until all work and the physical connection are settled.
        await self._ingress.remove(self._id)
        await engine.stop()
        if self._writer is not None:
            await self._writer.settle()
        await self._pool.release(self._sink_id)
        for name, value in (
            ("received_total", engine.received_total),
            ("dropped_total", engine.dropped_total),
            ("delivered_total", engine.delivered_total),
            ("failed_total", engine.failed_total),
            ("write_success_total", engine.write_success_total),
        ):
            self._retained[name] = self._retained.get(name, 0.0) + float(value)
        self._retained_latency.absorb(engine.latency_window)
        self._engine = None
        self._writer = None
        self._stopping = False


