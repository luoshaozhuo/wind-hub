"""Non-gRPC Commander composition; protocol factories are injected explicitly."""

from __future__ import annotations

from pathlib import Path

from commander.application.service_lifecycle import CommanderService
from commander.infrastructure.adapters.outbound.gateway import GatewaySimulationSession
from commander.infrastructure.adapters.outbound.nats_audit import NatsAuditTransport
from commander.infrastructure.config.config_commit import recover_config_commit
from commander.infrastructure.config.service_settings import load_service_settings
from commander.infrastructure.persistence.event_repository import EventRepository
from core.application.protocol_registry import ProtocolRegistry


class CommanderSettingsSource:
    """Select a validated immutable Service path for each reload."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __call__(self):
        return load_service_settings(self.path)


def build_commander(
    *,
    service_config: Path,
    protocol_registry: ProtocolRegistry,
    settings_source: CommanderSettingsSource | None = None,
) -> CommanderService:
    """Build an inert service after recovering interrupted configuration commits."""
    config_path = service_config.resolve(strict=True)
    recover_config_commit(config_path.parent.parent)
    source = settings_source or CommanderSettingsSource(config_path)
    settings = source()
    audit_transport = NatsAuditTransport(
        settings.nats_servers,
        stream=settings.audit_stream,
        subject=settings.audit_subject,
    )
    return CommanderService(
        protocol_registry=protocol_registry,
        simulation_factory=GatewaySimulationSession,
        native_session_factory=protocol_registry.create,
        audit_transport=audit_transport,
        settings_loader=source,
        journal_factory=lambda current: EventRepository(
            current.sqlite_path,
            max_storage_bytes=current.max_storage_bytes,
            retention_seconds=current.retention_seconds,
        ),
    )
