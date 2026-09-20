"""CLI entry point — assembles the ``wind-hub`` Typer application."""

from __future__ import annotations

import typer

from wind_hub.adapter.inbound.cli.commands import (
    cmd,
    devices,
    jobs,
    point,
    probe,
    reload,
    replay,
    route,
    run,
    status,
    validate,
)


def build_cli() -> typer.Typer:
    """Build the ``wind-hub`` CLI application.

    Each sub-command lives in its own module (``commands/*.py``) and owns its
    own ``typer.Typer``; this function only wires them together.
    """
    app = typer.Typer(
        name="wind-hub",
        help="风电场主控通信模块",
        no_args_is_help=True,
    )
    app.add_typer(run.app, name="run")
    app.add_typer(validate.app, name="validate")
    app.add_typer(reload.app, name="reload")
    app.add_typer(status.app, name="status")
    app.add_typer(devices.app, name="devices")
    app.add_typer(jobs.app, name="jobs")
    app.add_typer(point.app, name="point")
    app.add_typer(cmd.app, name="cmd")
    app.add_typer(route.app, name="route")
    app.add_typer(replay.app, name="replay")
    app.add_typer(probe.app, name="probe")
    return app


def main() -> None:
    """Console-script entry point (``wind-hub = ...cli.app:main``)."""
    build_cli()()


if __name__ == "__main__":
    main()
