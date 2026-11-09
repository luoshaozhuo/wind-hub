"""Sink, Industrial Gateway, and Data Generator CLI commands."""

from __future__ import annotations

from typing import Any

import typer

from .service_rpc import invoke

sink = typer.Typer(help="Sink delivery management.")
gateway = typer.Typer(help="Industrial Gateway variables and runtime.")
data_generator = typer.Typer(help="Data Generator scenarios and generations.")


def _call(
    service: str, method: str, fields: dict[str, Any], *, validate_sink: bool = False
) -> None:
    from .main import _run, runtime

    async def task() -> dict[str, Any]:
        # 参数校验必须在 _run 协程内抛出，才能走统一信封/退出码 2，而非裸 traceback。
        target = _sink_target(service) if validate_sink else service
        return await invoke(runtime(), target, method, fields)

    _run(f"{service} {method}", task())


def _sink_target(service: str) -> str:
    if service not in {"sink_realtime", "sink_reliable"}:
        raise ValueError("--service must be sink_realtime or sink_reliable")
    return service


def _run_sink_common(service: str, method: str, fields: dict[str, Any]) -> None:
    """CommonService call whose sink service selection must stay inside the envelope."""
    from .common_commands import request_common
    from .main import _run

    async def task() -> dict[str, Any]:
        return await request_common(_sink_target(service), method, fields)

    _run(f"{service} {method}", task())


@sink.command("deliveries")
def sink_deliveries(
    channel: str = "",
    page_size: int = 100,
    page_token: str = "",
    service: str = typer.Option("sink_realtime", "--service"),
) -> None:
    _call(
        service,
        "ListDeliveries",
        {"channel": channel, "page_size": page_size, "page_token": page_token},
        validate_sink=True,
    )


@sink.command("delivery")
def sink_delivery(
    delivery_id: str, service: str = typer.Option("sink_realtime", "--service")
) -> None:
    _call(service, "GetDelivery", {"id": delivery_id}, validate_sink=True)


async def _delivery_action(
    service: str,
    delivery_id: str,
    method: str,
    revision: str | None,
) -> dict[str, Any]:
    from .main import runtime

    if revision is None:
        result = await invoke(runtime(), service, "GetDelivery", {"id": delivery_id})
        delivery = result.get("delivery", {})
        revision = delivery.get("revision") if isinstance(delivery, dict) else None
    if not isinstance(revision, str) or not revision:
        raise ValueError("delivery revision unavailable; pass --expected-revision")
    return await invoke(
        runtime(),
        service,
        method,
        {
            "id": delivery_id,
            "expected_revision": revision,
        },
    )


@sink.command("start")
def sink_start(
    delivery_id: str,
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
    yes: bool = typer.Option(False, "--yes"),
    service: str = typer.Option("sink_realtime", "--service"),
) -> None:
    from .main import _run
    from .safety import confirm_mutation

    confirm_mutation(f"Start delivery {delivery_id}?", yes, command="sink start")

    async def action() -> dict[str, Any]:
        return await _delivery_action(
            _sink_target(service), delivery_id, "StartDelivery", expected_revision
        )

    _run("sink start", action())


@sink.command("stop")
def sink_stop(
    delivery_id: str,
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
    yes: bool = typer.Option(False, "--yes"),
    service: str = typer.Option("sink_realtime", "--service"),
) -> None:
    from .main import _run
    from .safety import confirm_mutation

    confirm_mutation(f"Stop delivery {delivery_id}?", yes, command="sink stop")

    async def action() -> dict[str, Any]:
        return await _delivery_action(
            _sink_target(service), delivery_id, "StopDelivery", expected_revision
        )

    _run("sink stop", action())


@gateway.command("list")
def gateway_list(page_size: int = 100, page_token: str = "") -> None:
    _call("gateway", "ListGateways", {"page_size": page_size, "page_token": page_token})


@gateway.command("status")
def gateway_status(gateway_id: str) -> None:
    _call("gateway", "GetGateway", {"id": gateway_id})


@gateway.command("variables")
def gateway_variables(
    gateway_id: str,
    page_size: int = 100,
    page_token: str = "",
) -> None:
    _call(
        "gateway",
        "ListVariables",
        {
            "gateway_id": gateway_id,
            "page_size": page_size,
            "page_token": page_token,
        },
    )


@gateway.command("read")
def gateway_read(gateway_id: str, variable_ids: list[str] = typer.Argument(...)) -> None:
    _call(
        "gateway",
        "ReadVariables",
        {
            "gateway_id": gateway_id,
            "variable_ids": variable_ids,
        },
    )


@gateway.command("update")
def gateway_update(
    gateway_id: str,
    updates: list[str] = typer.Argument(..., help="variable_id=typed_value"),
    owner_id: str = typer.Option(..., "--owner-id", help="Authorized lease owner identity."),
    lease_ms: int = typer.Option(30000, "--lease-ms", min=1),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Update Gateway-owned variables under a server-authorized target lease."""
    from .main import _run, runtime
    from .safety import confirm_mutation
    from .scalar_value import parse_json_scalar

    try:
        if not owner_id.strip():
            raise ValueError("--owner-id must be non-empty")
        values: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in updates:
            variable_id, separator, encoded = item.partition("=")
            if not separator or not variable_id or variable_id in seen:
                raise ValueError("updates require distinct variable_id=typed_value pairs")
            seen.add(variable_id)
            values.append({"variable_id": variable_id, "value": parse_json_scalar(encoded)})
        if len(values) > 1000:
            raise ValueError("UpdateVariables supports at most 1000 variables")
    except ValueError as exc:
        runtime().error("gateway update", exc)
        raise typer.Exit(code=2) from exc
    confirm_mutation(
        f"Update {len(values)} Gateway variables on {gateway_id}?",
        yes,
        command="gateway update",
    )

    async def update() -> dict[str, Any]:
        ctx = runtime()
        variable_ids = [item["variable_id"] for item in values]
        lease = await invoke(
            ctx,
            "gateway",
            "ClaimTargets",
            {
                "gateway_id": gateway_id,
                "owner_id": owner_id,
                "variable_ids": variable_ids,
                "action": "ACQUIRE",
                "lease_ms": lease_ms,
            },
        )
        token = lease.get("lease_token")
        if lease.get("accepted") is not True or not isinstance(token, str) or not token:
            raise PermissionError(
                f"Gateway denied target lease: {lease.get('error_code', 'DENIED')}"
            )

        response: dict[str, Any] | None = None
        update_error: BaseException | None = None
        try:
            response = await invoke(
                ctx,
                "gateway",
                "UpdateVariables",
                {"gateway_id": gateway_id, "values": values, "lease_token": token},
            )
            results = response.get("results")
            if not isinstance(results, list) or len(results) != len(values):
                raise TimeoutError(
                    "UpdateVariables result incomplete; outcome UNKNOWN; do not retry"
                )
            actual = [
                item.get("variable_id") if isinstance(item, dict) else None
                for item in results
            ]
            if actual != variable_ids:
                raise TimeoutError(
                    "UpdateVariables result mismatch; outcome UNKNOWN; do not retry"
                )
        except BaseException as exc:
            update_error = exc
        finally:
            try:
                released = await invoke(
                    ctx,
                    "gateway",
                    "ClaimTargets",
                    {
                        "gateway_id": gateway_id,
                        "owner_id": owner_id,
                        "variable_ids": variable_ids,
                        "action": "RELEASE",
                        "lease_token": token,
                    },
                )
                if released.get("accepted") is not True:
                    raise RuntimeError(
                        "Gateway lease release not confirmed: "
                        f"{released.get('error_code', 'UNKNOWN')}"
                    )
            except Exception as release_exc:
                if update_error is not None:
                    raise RuntimeError(
                        "Gateway update outcome UNKNOWN and lease release unconfirmed; "
                        "do not retry"
                    ) from release_exc
                raise
        if update_error is not None:
            raise update_error
        if response is None:
            raise RuntimeError("Gateway returned no update response")
        return response

    _run("gateway update", update())


async def _gateway_action(
    gateway_id: str,
    method: str,
    revision: str | None,
) -> dict[str, Any]:
    from .main import runtime

    if revision is None:
        result = await invoke(runtime(), "gateway", "GetGateway", {"id": gateway_id})
        gateway = result.get("gateway", {})
        revision = gateway.get("revision") if isinstance(gateway, dict) else None
    if not isinstance(revision, str) or not revision:
        raise ValueError("gateway revision unavailable; pass --expected-revision")
    return await invoke(
        runtime(),
        "gateway",
        method,
        {
            "id": gateway_id,
            "expected_revision": revision,
        },
    )


@gateway.command("start")
def gateway_start(
    gateway_id: str,
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    from .main import _run
    from .safety import confirm_mutation

    confirm_mutation(f"Start gateway {gateway_id}?", yes, command="gateway start")
    _run("gateway start", _gateway_action(gateway_id, "StartGateway", expected_revision))


@gateway.command("stop")
def gateway_stop(
    gateway_id: str,
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    from .main import _run
    from .safety import confirm_mutation

    confirm_mutation(f"Stop gateway {gateway_id}?", yes, command="gateway stop")
    _run("gateway stop", _gateway_action(gateway_id, "StopGateway", expected_revision))


@data_generator.command("devices")
def generator_devices(page_size: int = 100, page_token: str = "") -> None:
    _call("data-generator", "ListDevices", {"page_size": page_size, "page_token": page_token})


@data_generator.command("device")
def generator_device(device_id: str) -> None:
    _call("data-generator", "GetDevice", {"id": device_id})


@data_generator.command("scenarios")
def generator_scenarios(page_size: int = 100, page_token: str = "") -> None:
    _call(
        "data-generator",
        "ListScenarios",
        {
            "page_size": page_size,
            "page_token": page_token,
        },
    )


async def _generation_revision(revision: str | None) -> str:
    if revision:
        return revision
    from .common_commands import _disk_revision

    return await _disk_revision("data-generator")


@data_generator.command("start")
def generator_start(
    scenario_id: str,
    scenario_version: str | None = typer.Option(None, "--scenario-version"),
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    from .main import _run, runtime
    from .safety import confirm_mutation

    confirm_mutation(f"Start scenario {scenario_id}?", yes, command="data-generator start")

    async def start() -> dict[str, Any]:
        values: dict[str, Any] = {
            "scenario_id": scenario_id,
            "expected_revision": await _generation_revision(expected_revision),
        }
        if scenario_version is not None:
            values["scenario_version"] = scenario_version
        return await invoke(runtime(), "data-generator", "StartGeneration", values)

    _run("data-generator start", start())


@data_generator.command("stop")
def generator_stop(
    generation_id: str,
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    from .main import _run, runtime
    from .safety import confirm_mutation

    confirm_mutation(f"Stop generation {generation_id}?", yes, command="data-generator stop")

    async def stop() -> dict[str, Any]:
        return await invoke(
            runtime(),
            "data-generator",
            "StopGeneration",
            {
                "generation_id": generation_id,
                "expected_revision": await _generation_revision(expected_revision),
            },
        )

    _run("data-generator stop", stop())


@data_generator.command("status")
def generator_status(generation_id: str) -> None:
    _call("data-generator", "GetGeneration", {"id": generation_id})


@sink.command("metrics")
def sink_metrics(
    scope: str = "",
    window_ms: int = 60000,
    service: str = typer.Option("sink_realtime", "--service"),
) -> None:
    _run_sink_common(service, "GetMetrics", {"scope": scope, "window_ms": window_ms})


@sink.command("events")
def sink_events(
    page_size: int = 100,
    page_token: str = "",
    service: str = typer.Option("sink_realtime", "--service"),
) -> None:
    _run_sink_common(
        service,
        "GetEvents",
        {
            "page_size": page_size,
            "page_token": page_token,
        },
    )


@gateway.command("metrics")
def gateway_metrics(scope: str = "", window_ms: int = 60000) -> None:
    from .common_commands import _run_common

    _run_common("gateway", "GetMetrics", {"scope": scope, "window_ms": window_ms})


@gateway.command("events")
def gateway_events(page_size: int = 100, page_token: str = "") -> None:
    from .common_commands import _run_common

    _run_common(
        "gateway",
        "GetEvents",
        {
            "page_size": page_size,
            "page_token": page_token,
        },
    )


@data_generator.command("metrics")
def generator_metrics(scope: str = "", window_ms: int = 60000) -> None:
    from .common_commands import _run_common

    _run_common(
        "data-generator",
        "GetMetrics",
        {
            "scope": scope,
            "window_ms": window_ms,
        },
    )


@data_generator.command("events")
def generator_events(page_size: int = 100, page_token: str = "") -> None:
    from .common_commands import _run_common

    _run_common(
        "data-generator",
        "GetEvents",
        {
            "page_size": page_size,
            "page_token": page_token,
        },
    )
