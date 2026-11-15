"""Resolve Delivery identities from the validated configuration snapshot."""

from __future__ import annotations

from dataclasses import dataclass

from sink_service.application.config import template_path
from sink_service.application.config_sources import config_sources
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot


@dataclass(frozen=True, slots=True)
class DeliveryDefinition:
    delivery_id: str
    sink_id: str
    channel: str
    desired_state: str


def delivery_definitions(snapshot: ValidatedSnapshot) -> tuple[DeliveryDefinition, ...]:
    """Resolve stable channel identities without importing an Adapter."""
    _, template_root = config_sources(snapshot.service)
    templates = snapshot.documents

    definitions: list[DeliveryDefinition] = []
    for delivery_id, value in sorted(snapshot.deliveries.items()):
        if not isinstance(value, dict):
            raise ConfigurationError(f"deliveries.{delivery_id}: invalid instance")
        path = template_path(
            template_root, "deliveries", value["template_id"], value["template_version"]
        )
        template = templates.get(path)
        if template is None:
            raise ConfigurationError(f"deliveries.{delivery_id}: missing template")
        channel = template.get("delivery_mode")
        if channel not in ("realtime", "reliable"):
            raise ConfigurationError(f"deliveries.{delivery_id}: invalid channel")
        definitions.append(
            DeliveryDefinition(
                delivery_id=delivery_id,
                sink_id=value["sink_id"],
                channel=str(channel),
                desired_state=value["desired_state"],
            )
        )
    return tuple(definitions)
