"""Domain model — routing rule definitions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

DeliveryType = Literal["always", "interval", "every_n", "on_change"]
"""投递策略类型：``always``（每批都投递，缺省）/ ``interval``（按最小间隔
限流）/ ``every_n``（每 n 批投递一次）/ ``on_change``（值变化才投递）。"""


class DeliveryConfig(BaseModel):
    """投递策略——Router 决定「发到哪些 sink」之后，策略决定「何时真正投递」。

    挂在规则上（规则级策略）：命中该规则、发往同一 sink 的批次共用一套
    节拍状态。缺省 ``always`` 等价于旧行为（路由即投递）。
    """

    type: DeliveryType = "always"
    """策略类型。"""

    interval: float | None = None
    """``interval`` 类型的最小投递间隔（秒）——距上次投递不足该时长的
    批次整批抑制。"""

    n: int | None = None
    """``every_n`` 类型的批间隔——首批投递，之后每 n 批投递一次。"""

    @model_validator(mode="after")
    def _check_params(self) -> DeliveryConfig:
        if self.type == "interval" and (self.interval is None or self.interval <= 0):
            raise ValueError("delivery type 'interval' requires interval > 0")
        if self.type == "every_n" and (self.n is None or self.n < 1):
            raise ValueError("delivery type 'every_n' requires n >= 1")
        return self


class RouteTarget(BaseModel):
    """Destination for routed data — a named Sink plus its delivery policy.

    Delivery policy lives on the *target*, not the rule: one point routed
    to Kafka (``always``), PostgreSQL (``interval``) and a file
    (``every_n``) gets an independent delivery cadence per sink.
    ``delivery=None`` is equivalent to ``always``.
    """

    sink: str
    """Name of the SinkPort implementation to forward data to."""

    delivery: DeliveryConfig | None = None
    """Delivery policy for this (rule, sink) pair; ``None`` means always."""


class RouteMatch(BaseModel):
    """路由匹配条件——按设备业务类别与点采集分组两个维度匹配。

    - ``all: true`` 匹配全部数据，与 ``device_group`` / ``point_group`` 互斥；
    - ``device_group`` 匹配 :attr:`~wind_hub.config.schema.DeviceConfig.device_group`
      （设备业务类别，如 ``'turbine'``）；
    - ``point_group`` 匹配 :attr:`~wind_hub.config.schema.PointConfig.group`
      （点采集分组，如 ``'fast'``）；
    - 两个维度同时配置时为 AND；只配置一个时只按该维度匹配。
    """

    model_config = ConfigDict(extra="forbid")

    all: bool = False
    """``True`` 表示匹配全部数据（兜底规则）。"""

    device_group: str | None = None
    """按设备业务类别匹配；``None`` 表示不限设备类别。"""

    point_group: str | None = None
    """按点采集分组匹配；``None`` 表示不限分组。"""

    @model_validator(mode="after")
    def _validate_match(self) -> RouteMatch:
        if self.all and (self.device_group is not None or self.point_group is not None):
            raise ValueError(
                "route match: 'all: true' is mutually exclusive with "
                "'device_group' / 'point_group'"
            )
        if not self.all and self.device_group is None and self.point_group is None:
            raise ValueError(
                "route match must define at least one condition: "
                "'all' / 'device_group' / 'point_group'"
            )
        return self


class RouteRule(BaseModel):
    """A default routing rule that matches points and sends them to sinks.

    The router evaluates rules in descending priority order; the first
    matching rule determines the target sink list.
    """

    name: str
    """Human-readable rule name for debugging and observability."""

    match: RouteMatch
    """匹配条件（``all`` / ``device_group`` / ``point_group``）。"""

    targets: list[RouteTarget]
    """Ordered list of route targets (sink + optional delivery policy)
    to deliver matching data to."""

    priority: int = 0
    """Rule priority — higher values are evaluated first.
    Used to resolve conflicts when multiple rules could match."""


class RouteDecision(BaseModel):
    """A single routing decision, used for debugging and observability.

    Records *why* a particular (device_id, point_id) was routed to
    certain sinks.
    """

    device_id: str
    """Device identifier."""

    point_id: str
    """Point identifier."""

    targets: list[str]
    """Sink names this point is routed to."""

    matched_rule: str | None = None
    """Name of the RouteRule that matched, or ``None`` when the
    decision came from a per-point sink override."""

    source: str
    """How the targets were determined:
    ``'point_override'``, ``'rule'``, or ``'unmatched'``."""
