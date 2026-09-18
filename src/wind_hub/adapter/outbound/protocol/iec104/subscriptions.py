"""IEC104 subscription registry.

Manages subscribe callbacks for spontaneous updates and interrogation
data.  Callbacks are dispatched in independent asyncio tasks so they
never block the receive loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from wind_hub.domain.model.point import PointRef, PointValue

logger = logging.getLogger(__name__)


class SubscriptionRegistry:
    """Manages point-value callbacks.

    Two levels of subscription:

    * **Global** — :meth:`subscribe` with an empty *points* list
      registers a callback that receives **every** ``PointValue``.
    * **Per-IOA** — each IOA can have zero or more callbacks.

    Every callback is wrapped in its own ``asyncio.ensure_future``
    when dispatched, so a slow or crashing callback cannot delay or
    poison the receive loop.
    """

    def __init__(self) -> None:
        self._global: list[Callable[[PointValue], Awaitable[None]]] = []
        self._ioa: dict[int, list[Callable[[PointValue], Awaitable[None]]]] = {}

    # ==================================================================
    # subscribe
    # ==================================================================

    def subscribe(
        self,
        points: list[PointRef],
        callback: Callable[[PointValue], Awaitable[None]],
        ioa_resolver: Callable[[PointRef], int | None],
    ) -> None:
        """Register a subscription.

        Args:
            points:
                Points to subscribe to.  An empty list creates a
                **global** subscription that receives every value.
            callback:
                Async callable invoked with each ``PointValue``.
            ioa_resolver:
                Callable that maps a ``PointRef`` to an IOA (int)
                or ``None`` if the point is unknown.
        """
        if not points:
            self._global.append(callback)
            logger.info(
                "IEC104: registered global subscription (total=%d)",
                len(self._global),
            )
            return

        for ref in points:
            ioa = ioa_resolver(ref)
            if ioa is None:
                logger.warning(
                    "IEC104: subscribe: unknown point '%s' — skipping",
                    ref.point_id,
                )
                continue
            self._ioa.setdefault(ioa, []).append(callback)
        logger.info(
            "IEC104: registered subscriptions for %d IOAs",
            len(points),
        )

    # ==================================================================
    # dispatch
    # ==================================================================

    async def dispatch(self, pv: PointValue, ioa: int) -> None:
        """Distribute *pv* to all matching subscribers.

        - Global subscribers are called first.
        - IOA-specific subscribers are called second.
        - Each callback runs in its own task — slow callbacks do not
          delay other callbacks.
        - Callback exceptions are logged but never re-raised.
        """
        callbacks: list[Callable[[PointValue], Awaitable[None]]] = []

        # Global callbacks.
        callbacks.extend(self._global)

        # IOA-specific callbacks.
        callbacks.extend(self._ioa.get(ioa, []))

        if not callbacks:
            return

        for cb in callbacks:
            asyncio.ensure_future(self._invoke_callback(cb, pv, ioa))

    async def _invoke_callback(
        self,
        cb: Callable[[PointValue], Awaitable[None]],
        pv: PointValue,
        ioa: int,
    ) -> None:
        """Invoke one callback and log failures."""
        try:
            await cb(pv)
        except Exception:
            logger.exception(
                "IEC104: subscriber callback raised (IOA %d, point '%s')",
                ioa,
                pv.point_id,
            )

    # ==================================================================
    # stats
    # ==================================================================

    @property
    def global_count(self) -> int:
        """Number of global subscribers."""
        return len(self._global)

    @property
    def ioa_count(self) -> int:
        """Total number of IOA-specific subscriber entries."""
        return sum(len(v) for v in self._ioa.values())

    @property
    def unique_ioa_count(self) -> int:
        """Number of distinct IOAs with at least one subscriber."""
        return len(self._ioa)
