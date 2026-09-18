"""DeliveryDispatcher —— 投递策略评估（阶段 B）。

架构位置：domain/routing。Router 只回答「发到哪些 sink」，本组件回答
「这一批是否真的投递」——按 RouteTarget 上声明的 :class:`DeliveryConfig`
对路由结果做节拍/去重过滤，产出最终交给 SinkDispatchPort 的批次。

策略语义（键空间与口径）：

- ``always``：不过滤（缺省，等价于旧行为）；
- ``interval``：按 ``(规则, sink)`` 计时——距上次投递不足 ``interval``
  秒的批次整批抑制；首批立即投递；
- ``every_n``：按 ``(规则, sink)`` 计批——首批投递，之后每 n 批投递一次；
- ``on_change``：按 ``(sink, 设备, 点)`` 记录上次投递值，值发生变化
  （严格不等）才投递；首见即投递。

点位级 sinks 覆盖（point_override）的点不属于任何规则——按 ``always``
处理。本组件不接触 SinkPort / Sink 队列；过滤结果交回调用方
（AcquisitionEngine）派发，因此策略热替换不涉及 Sink/Protocol 重建。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from wind_hub.domain.model.point import PointValue
from wind_hub.domain.model.route import DeliveryConfig, RouteRule
from wind_hub.domain.routing.router import Router


def policies_from_rules(rules: list[RouteRule]) -> dict[tuple[str, str], DeliveryConfig]:
    """从规则列表抽取 ``{(规则名, sink名): 投递策略}``——投递策略属于
    RouteTarget，未声明策略的 target 不出现（评估时按 ``always`` 处理）。"""
    return {
        (rule.name, target.sink): target.delivery
        for rule in rules
        for target in rule.targets
        if target.delivery is not None
    }


class DeliveryDispatcher:
    """投递策略评估器——对 Router 的路由结果按规则策略过滤。

    注入依赖：

    - ``router`` — 当前路由表实例，用于反查每个点命中的规则
      （点位级覆盖 / 未匹配返回 ``None`` → ``always``）；热替换路由表时
      Runtime 会整体重建本组件，因此这里持有的是不可变的当期实例；
    - ``policies`` — ``{(规则名, sink名): DeliveryConfig}``——策略状态
      以 ``(规则, sink)`` 为键，同一条规则发往不同 sink 的节拍相互独立；
    - ``clock`` — 单调时钟（测试可注入假时钟）。

    节拍状态（interval 时间戳 / every_n 计数 / on_change 上次值）为本组件
    内部状态；热重载整体替换组件时状态随之重置（策略语义可接受的口径）。
    """

    def __init__(
        self,
        router: Router,
        policies: dict[tuple[str, str], DeliveryConfig],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._router = router
        self._policies = dict(policies)
        self._clock = clock
        self._interval_last: dict[tuple[str, str], float] = {}
        self._every_n_counts: dict[tuple[str, str], int] = {}
        self._on_change_last: dict[tuple[str, str, str], object] = {}

    def evaluate(self, routed: dict[str, list[PointValue]]) -> dict[str, list[PointValue]]:
        """按各点命中规则的策略过滤路由结果，返回最终投递批次。

        ``interval`` / ``every_n`` 以 ``(规则, sink)`` 为粒度、每批评估一次；
        ``on_change`` 逐点评估。返回结构与输入相同（``{sink: [PointValue]}``），
        被完全抑制的 sink 不出现在结果中。
        """
        # 按 (sink, 命中规则) 聚合——同规则同 sink 的点共享一次节拍评估
        groups: dict[tuple[str, str | None], list[PointValue]] = {}
        for sink, values in routed.items():
            for v in values:
                key = (sink, self._router.rule_for(v.device_id, v.point_id))
                groups.setdefault(key, []).append(v)

        out: dict[str, list[PointValue]] = {}
        for (sink, rule_name), values in groups.items():
            policy = (
                self._policies.get((rule_name, sink)) if rule_name is not None else None
            )
            kept = self._apply(sink, rule_name or "", policy, values)
            if kept:
                out.setdefault(sink, []).extend(kept)
        return out

    # ------------------------------------------------------------------
    # 私有
    # ------------------------------------------------------------------

    def _apply(
        self,
        sink: str,
        rule_name: str,
        policy: DeliveryConfig | None,
        values: list[PointValue],
    ) -> list[PointValue]:
        if policy is None or policy.type == "always":
            return values
        if policy.type == "interval":
            interval = policy.interval or 0.0  # 配置校验保证 > 0
            key = (rule_name, sink)
            now = self._clock()
            last = self._interval_last.get(key)
            if last is not None and now - last < interval:
                return []
            self._interval_last[key] = now
            return values
        if policy.type == "every_n":
            n = policy.n or 1  # 配置校验保证 >= 1
            key = (rule_name, sink)
            count = self._every_n_counts.get(key, 0)
            self._every_n_counts[key] = count + 1
            return values if count % n == 0 else []
        # on_change：逐点与上次投递值比较（严格不等才投递）
        kept: list[PointValue] = []
        for v in values:
            vkey = (sink, v.device_id, v.point_id)
            if vkey not in self._on_change_last or self._on_change_last[vkey] != v.value:
                self._on_change_last[vkey] = v.value
                kept.append(v)
        return kept
