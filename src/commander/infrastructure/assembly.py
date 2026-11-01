"""Non-gRPC Commander composition; protocol factories are injected explicitly."""

from __future__ import annotations

from pathlib import Path

from commander.application.service_lifecycle import CommanderService
from commander.infrastructure.event_repository import EventRepository
from commander.infrastructure.nats_audit import NatsAuditTransport
from commander.infrastructure.service_settings import load_service_settings
from core.application.protocol_registry import ProtocolRegistry


def build_commander(
    *,
    config_root: Path,
    service_config: Path,
    protocol_registry: ProtocolRegistry,
) -> CommanderService:
    """Build an inert service; start() performs validation and recovery."""
    settings = load_service_settings(service_config)
    audit_transport = NatsAuditTransport(
        settings.nats_servers,
        stream=settings.audit_stream,
        subject=settings.audit_subject,
    )
    return CommanderService(
        config_root=config_root,
        service_config=service_config,
        protocol_registry=protocol_registry,
        audit_transport=audit_transport,
        settings_loader=lambda: load_service_settings(service_config),
        journal_factory=lambda current: EventRepository(
            current.sqlite_path,
            max_storage_bytes=current.max_storage_bytes,
            reserved_storage_bytes=current.reserved_storage_bytes,
            admission_stop_ratio=current.admission_stop_ratio,
            retention_seconds=current.retention_seconds,
        ),
    )
