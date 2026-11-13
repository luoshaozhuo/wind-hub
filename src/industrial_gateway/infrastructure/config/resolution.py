"""Resolve Gateway servers against the shared, version-pinned device catalog."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from configuration import SharedConfigPaths, SharedDocumentLoader
from configuration.shared_documents import SharedDeviceDefinition
from configuration.versioned_templates import config_hash
from core.application.errors import ConfigError
from core.domain import ConnectionEndpoint
from core.infrastructure.config import read_yaml
from core.infrastructure.protocol.modbus.config import parse_modbus_config
from industrial_gateway.infrastructure.config.schema import (
    GatewayServiceConfig,
    ServerConfig,
    service_path,
)
from industrial_gateway.infrastructure.protocol.modbus.codec import (
    RegisterArea,
    RegisterMap,
    RegisterMapping,
    WordOrder,
)


@dataclass(frozen=True, slots=True)
class ResolvedVariable:
    variable_id: str
    data_type: str
    address: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ResolvedServer:
    settings: ServerConfig
    protocol: str
    variables: Mapping[str, ResolvedVariable]
    registers: tuple[RegisterMapping, ...]
    unit_id: int | None


@dataclass(frozen=True, slots=True)
class ResolvedGateway:
    service: GatewayServiceConfig
    servers: Mapping[str, ResolvedServer]
    devices_path: Path
    templates_root: Path
    shared_revision: str
    project_root: Path
    revision: str


def _points(server: ServerConfig, device: SharedDeviceDefinition) -> ResolvedServer:
    protocol = str(device.templates.protocol.document.get("protocol", ""))
    if protocol != "modbus" and server.desired_state == "START":
        raise ConfigError(
            f"{server.server_id}: protocol {protocol!r} has no verified " "Gateway Server adapter"
        )
    tables = (
        *reversed(device.templates.inherited_tables),
        device.templates.point_table,
    )
    points: dict[str, ResolvedVariable] = {}
    for table in tables:
        removed = table.document.get("remove_points", [])
        if not isinstance(removed, list | tuple) or len(removed) != len(set(map(str, removed))):
            raise ConfigError(f"{table.path}: invalid remove_points")
        for point_id in removed:
            if not isinstance(point_id, str) or point_id not in points:
                raise ConfigError(f"{table.path}: unknown inherited point {point_id!r}")
            del points[point_id]
        raw_points = table.document.get("points", {})
        if not isinstance(raw_points, Mapping):
            raise ConfigError(f"{table.path}: points must be a mapping")
        for point_id, definition in raw_points.items():
            if not isinstance(point_id, str) or not isinstance(definition, Mapping):
                raise ConfigError(f"{table.path}: invalid point entry")
            original = points.get(point_id)
            merged: dict[str, Any] = {}
            if original is not None:
                merged.update({"data_type": original.data_type, "address": dict(original.address)})
            merged.update(definition)
            address = merged.get("address")
            data_type = merged.get("data_type")
            if not isinstance(address, Mapping) or not isinstance(data_type, str):
                raise ConfigError(f"{table.path}: incomplete point '{point_id}'")
            points[point_id] = ResolvedVariable(point_id, data_type, address)

    registers: list[RegisterMapping] = []
    unit_id: int | None = None
    if protocol == "modbus":
        protocol_options = device.templates.protocol.document.get("protocol_options", {})
        model_options = device.templates.model.document.get("protocol_options", {})
        if not isinstance(protocol_options, Mapping) or not isinstance(model_options, Mapping):
            raise ConfigError(f"{server.device_id}: invalid protocol options")
        options = {
            **protocol_options,
            **model_options,
            **device.protocol_options,
        }
        modbus = parse_modbus_config(
            ConnectionEndpoint(
                host=str(device.endpoint["host"]),
                port=device.endpoint.get("port"),
            ),
            options,
        )
        unit_id = modbus.slave_id
        if not 1 <= modbus.slave_id <= 247:
            raise ConfigError(f"{server.device_id}: invalid Server Unit ID")
        for variable in points.values():
            address = variable.address
            area = address.get("register_type")
            offset = address.get("address")
            word_order = address.get("word_order", modbus.word_order)
            if (
                area not in {"coil", "discrete_input", "holding", "input"}
                or type(offset) is not int
                or word_order not in {"big_endian", "little_endian"}
            ):
                raise ConfigError(
                    f"{server.device_id}.{variable.variable_id}: invalid Modbus address"
                )
            try:
                mapping = RegisterMapping(
                    server.device_id,
                    variable.variable_id,
                    cast(RegisterArea, area),
                    offset,
                    variable.data_type,
                    cast(WordOrder, word_order),
                )
            except ValueError as exc:
                raise ConfigError(f"{server.device_id}.{variable.variable_id}: {exc}") from exc
            count = address.get("count")
            if count is not None and (type(count) is not int or count != mapping.count):
                raise ConfigError(
                    f"{server.device_id}.{variable.variable_id}: " "register count mismatch"
                )
            registers.append(mapping)
        for variable in points.values():
            unit = variable.address.get("slave_id")
            if unit is not None and (type(unit) is not int or unit != modbus.slave_id):
                raise ConfigError(
                    f"{server.device_id}.{variable.variable_id}: "
                    "point table Unit ID conflicts with effective protocol options"
                )
        try:
            RegisterMap(registers)
        except ValueError as exc:
            raise ConfigError(f"{server.device_id}: {exc}") from exc

    for command_mapping in server.control.mappings:
        command_variable = points.get(command_mapping.variable_id)
        if command_variable is None:
            raise ConfigError(
                f"{server.device_id}: unknown control variable {command_mapping.variable_id}"
            )
        if protocol == "modbus" and command_variable.address.get("register_type") not in {
            "holding",
            "coil",
        }:
            raise ConfigError(f"{server.device_id}: control mapping is read only")
        if (
            server.control.target.kind == "commander"
            and command_mapping.target_point_id not in points
        ):
            raise ConfigError(
                f"{server.device_id}: Commander target point "
                f"{command_mapping.target_point_id} is not in the bound device"
            )
    # Modbus on_access captures protocol request writes independently of gRPC.
    # Other adapters must prove the same capability before enabling control.
    if server.control.enabled and protocol != "modbus":
        raise ConfigError(f"{server.server_id}: request-level control unavailable for {protocol}")
    return ResolvedServer(server, protocol, points, tuple(registers), unit_id)


def load_gateway(
    service_file: str | Path,
    *,
    project_root: str | Path,
    candidate_document: dict[str, Any] | None = None,
) -> ResolvedGateway:
    """Load explicitly named service and shared files without cwd defaults."""
    root = Path(project_root).resolve(strict=True)
    service_pathname = service_path(root, Path(service_file))
    service_document = (
        read_yaml(service_pathname) if candidate_document is None else candidate_document
    )
    service = GatewayServiceConfig.model_validate(service_document)
    device_file = service_path(root, service.config_sources.instances.devices)
    templates_root = service_path(root, service.config_sources.templates.root)
    if not device_file.is_file():
        raise ConfigError(f"missing configured devices instance: {device_file}")
    if not templates_root.is_dir():
        raise ConfigError(f"missing configured templates root: {templates_root}")
    shared = SharedDocumentLoader(
        SharedConfigPaths(instances=device_file.parent, templates=templates_root)
    ).load_devices(
        device_file,
        selected_device_ids={server.device_id for server in service.servers},
    )
    servers: dict[str, ResolvedServer] = {}
    for server in service.servers:
        device = shared.devices.get(server.device_id)
        if device is None or not device.enabled:
            raise ConfigError(f"{server.server_id}: missing or disabled device {server.device_id}")
        servers[server.server_id] = _points(server, device)
    return ResolvedGateway(
        service,
        servers,
        device_file,
        templates_root,
        shared.revision,
        root,
        config_hash({"service": service_document, "shared": shared.revision}),
    )
