"""Non-gRPC Commander composition; protocol factories are injected explicitly."""

from __future__ import annotations

from pathlib import Path

from commander.application.service_lifecycle import CommanderService
from commander.infrastructure.adapters.outbound.gateway import GatewaySimulationSession
from commander.infrastructure.config_commit import recover_config_commit
from commander.infrastructure.event_repository import EventRepository
from commander.infrastructure.nats_audit import NatsAuditTransport
from commander.infrastructure.service_settings import load_service_settings
from core.application.protocol_registry import ProtocolRegistry


def build_commander(
    *,
    service_config: Path,
    protocol_registry: ProtocolRegistry,
) -> CommanderService:
    """Build an inert service after recovering interrupted configuration commits."""
    config_path = service_config.resolve(strict=True)
    recover_config_commit(config_path.parent.parent)
    settings = load_service_settings(config_path)
    audit_transport = NatsAuditTransport(
        settings.nats_servers,
        stream=settings.audit_stream,
        subject=settings.audit_subject,
    )
    return CommanderService(
        protocol_registry=protocol_registry,
        simulation_factory=GatewaySimulationSession,
        audit_transport=audit_transport,
        settings_loader=lambda: load_service_settings(config_path),
        journal_factory=lambda current: EventRepository(
            current.sqlite_path,
            max_storage_bytes=current.max_storage_bytes,
            retention_seconds=current.retention_seconds,
        ),
    )
