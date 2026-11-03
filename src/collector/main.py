"""Collector v1 process entrypoint and graceful lifecycle coordination."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import tempfile
from pathlib import Path

from .assembly import assemble_collector
from .infrastructure.config.process_lock import collector_state_owner
from .infrastructure.config.snapshot_repository import CollectorSnapshotRepository
from .infrastructure.config.validation import (
    resolve_source_paths,
    validate_collector_snapshot,
)

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wind-hub-collector")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state-directory", type=Path, default=None)
    parser.add_argument("--collector-id", default=None)
    parser.add_argument("--grpc-host", default="127.0.0.1")
    parser.add_argument("--grpc-port", type=int, default=50051)
    parser.add_argument("--shutdown-timeout", type=float, default=30.0)
    parser.add_argument("--check", action="store_true")
    return parser


async def run_collector(
    config_file: Path,
    state_directory: Path,
    *,
    collector_id: str | None = None,
    grpc_host: str = "127.0.0.1",
    grpc_port: int = 50051,
    shutdown_timeout: float = 30.0,
) -> int:
    if shutdown_timeout <= 0:
        raise ValueError("shutdown_timeout must be positive")
    with collector_state_owner(state_directory):
        app = await assemble_collector(
            config_file,
            state_directory=state_directory,
            collector_id=collector_id,
            grpc_host=grpc_host,
            grpc_port=grpc_port,
        )
        exit_requested = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, exit_requested.set)
            except NotImplementedError:
                logger.warning("signal handlers not supported by this event loop")
        try:
            await app.start()
            logger.info("Collector configuration service listening on %s", app.endpoint)
            await exit_requested.wait()
        finally:
            try:
                await asyncio.wait_for(app.stop(), timeout=shutdown_timeout)
            except TimeoutError:
                logger.error("Collector shutdown exceeded deadline; resources may remain")
                _exit_now(1)
            except BaseException:
                # Unsettled I/O already makes this process unsafe: leave without
                # running Runner task cancellation, which can wedge on orphaned
                # shielded protocol tasks (core modbus session close race).
                logger.exception("Collector shutdown failed; exiting without safe settle")
                _exit_now(1)
        return 0


def _exit_now(code: int) -> None:
    """Terminate immediately after a failed shutdown, flushing captured logs."""
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args()
    if args.check:
        paths = resolve_source_paths(args.config)
        # Validate the exact source references using the production snapshot layout.
        with tempfile.TemporaryDirectory(prefix="collector-check-") as directory:
            repository = CollectorSnapshotRepository(Path(directory) / "state")
            files = repository.collect_source(paths)
            logical_root = Path(directory) / "snapshot"
            logical_root.mkdir()
            repository._write_generation(logical_root, tuple(sorted(files.items())))
            validate_collector_snapshot(repository._paths(logical_root))
        logger.info("Collector configuration validated")
        return 0
    return asyncio.run(
        run_collector(
            args.config,
            args.state_directory or (args.config.resolve().parent.parent / ".collector-state"),
            collector_id=args.collector_id,
            grpc_host=args.grpc_host,
            grpc_port=args.grpc_port,
            shutdown_timeout=args.shutdown_timeout,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
