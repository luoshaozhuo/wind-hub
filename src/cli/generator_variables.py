"""Data Generator variable discovery and strictly typed model-input CLI controls."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import typer

from .service_rpc import invoke

variables = typer.Typer(help="Discover and update authorized simulation model inputs.")


async def _catalog(generation_id: str, device_id: str) -> dict[str, dict[str, Any]]:
    """Read the service-authoritative catalog, never infer permissions locally."""
    from .main import runtime

    result: dict[str, dict[str, Any]] = {}
    token = ""
    visited: set[str] = set()
    for _ in range(100):
        page = await invoke(
            runtime(), "data-generator", "ListVariables",
            {
                "generation_id": generation_id,
                "device_id": device_id,
                "page_size": 500,
                "page_token": token,
            },
        )
        for item in page.get("variables", []):
            if not isinstance(item, dict) or not isinstance(item.get("point_id"), str):
                raise ValueError("server supplied malformed model variable")
            point_id = item["point_id"]
            if point_id in result:
                raise ValueError("duplicate model variable from server")
            result[point_id] = item
        following = page.get("next_page_token", "")
        if not following:
            return result
        if not isinstance(following, str) or following in visited:
            raise ValueError("invalid model catalog page token")
        visited.add(following)
        token = following
    raise ValueError("model catalog exceeds 100 pages")


def _encode_checked(raw: str, metadata: dict[str, Any]) -> dict[str, Any]:
    from .scalar_value import parse_json_scalar

    encoded = parse_json_scalar(raw)
    kind = str(metadata.get("data_type", ""))
    if kind == "bool":
        if "bool_value" not in encoded:
            raise ValueError("Boolean input requires JSON true/false")
    elif kind.startswith(("int", "uint")):
        field = "sint64_value" if "sint64_value" in encoded else "uint64_value"
        if field not in encoded:
            raise ValueError("integer input requires exact JSON integer")
        value = int(encoded[field])
        if kind.startswith("uint") and value < 0:
            raise ValueError("unsigned input cannot be negative")
    elif kind in {"float32", "float64"}:
        if "double_value" in encoded:
            value = float(encoded["double_value"])
        elif "sint64_value" in encoded or "uint64_value" in encoded:
            value = float(next(iter(encoded.values())))
            encoded = {"double_value": value}
        else:
            raise ValueError("floating input requires JSON number")
        if not math.isfinite(value):
            raise ValueError("nonfinite model input")
    elif kind == "str":
        if "string_value" not in encoded:
            raise ValueError("string input requires JSON string")
    else:
        raise ValueError(f"unsupported controllable model data type: {kind}")
    if kind != "bool" and kind != "str":
        for key in ("minimum", "maximum"):
            if key in metadata:
                bound = float(metadata[key])
                if key == "minimum" and float(value) < bound:
                    raise ValueError("input below model minimum")
                if key == "maximum" and float(value) > bound:
                    raise ValueError("input above model maximum")
    return encoded


@variables.command("list")
def list_variables(
    generation_id: str = typer.Option(..., "--generation-id"),
    device_id: str = typer.Option(..., "--device-id"),
    page_size: int = typer.Option(100, "--page-size", min=1, max=500),
    page_token: str = typer.Option("", "--page-token"),
) -> None:
    from .main import _run, runtime

    _run(
        "data-generator variables list",
        invoke(runtime(), "data-generator", "ListVariables", {
            "generation_id": generation_id,
            "device_id": device_id,
            "page_size": page_size,
            "page_token": page_token,
        }),
    )


@variables.command("update")
def update_variables(
    generation_id: str = typer.Option(..., "--generation-id"),
    device_id: str = typer.Option(..., "--device-id"),
    request_id: str = typer.Option(..., "--request-id"),
    point_id: str | None = typer.Option(None, "--point-id"),
    value: str | None = typer.Option(None, "--value"),
    values_file: Path | None = typer.Option(None, "--values-file"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    from .main import _run, runtime
    from .safety import confirm_mutation

    confirm_mutation(
        f"Update model inputs for Generation {generation_id}, device {device_id}?",
        yes,
        command="data-generator variables update",
    )

    async def update() -> dict[str, Any]:
        if not generation_id.strip() or not device_id.strip() or not request_id.strip():
            raise ValueError("generation, device and request_id must be nonempty")
        batch: list[tuple[str, str]] = []
        if values_file is not None:
            if point_id is not None or value is not None:
                raise ValueError("--values-file cannot be combined with --point-id or --value")
            if values_file.stat().st_size > 65536:
                raise ValueError("model-input file exceeds 64 KiB")
            source = json.loads(values_file.read_text(encoding="utf-8"))
            if not isinstance(source, list):
                raise ValueError("model-input file must contain a JSON array")
            for item in source:
                if not isinstance(item, dict) or set(item) != {"point_id", "value"}:
                    raise ValueError("each model input must contain only point_id and value")
                batch.append(
                    (str(item["point_id"]), json.dumps(item["value"], allow_nan=False))
                )
        elif point_id is not None and value is not None:
            batch.append((point_id, value))
        else:
            raise ValueError("provide either --values-file or --point-id and --value")
        if not 1 <= len(batch) <= 100:
            raise ValueError("input batch size must be within 1..100")
        catalog = await _catalog(generation_id, device_id)
        seen: set[str] = set()
        updates: list[dict[str, Any]] = []
        for identifier, raw in batch:
            if not identifier or identifier in seen:
                raise ValueError("model input IDs must be distinct and nonempty")
            seen.add(identifier)
            metadata = catalog.get(identifier)
            if metadata is None or metadata.get("writable") is not True:
                raise PermissionError(f"model input not writable: {identifier}")
            updates.append({
                "point_id": identifier,
                "value": _encode_checked(raw, metadata),
            })
        return await invoke(
            runtime(), "data-generator", "UpdateVariables",
            {
                "generation_id": generation_id,
                "device_id": device_id,
                "request_id": request_id,
                "values": updates,
            },
        )

    _run("data-generator variables update", update())
