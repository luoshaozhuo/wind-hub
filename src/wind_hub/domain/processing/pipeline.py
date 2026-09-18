"""Pipeline — chains ProcessorPort implementations in config order."""

from __future__ import annotations

from wind_hub.domain.model.point import PointValue
from wind_hub.domain.port.outbound import ProcessorPort


class Pipeline:
    """Processor chain — transforms a batch through ordered processors.

    Each processor receives the output of its predecessor.  If a
    processor fails, the error is logged and that processor is
    skipped; data flows through the remaining processors unchanged.
    """

    def __init__(self, processors: list[ProcessorPort]) -> None:
        self._processors = processors

    async def process(self, batch: list[PointValue]) -> list[PointValue]:
        """Run the batch through every processor in order.

        A failing processor is skipped; the batch continues to the
        next processor with the output produced so far.  Errors are
        logged at warning level.
        """
        import logging

        logger = logging.getLogger(__name__)
        current = batch

        for proc in self._processors:
            try:
                current = await proc.process(current)
            except Exception:
                logger.warning(
                    "Processor '%s' failed — skipping, batch continues",
                    proc.name,
                    exc_info=True,
                )
        return current

    @property
    def processor_count(self) -> int:
        """Number of processors in the pipeline."""
        return len(self._processors)
