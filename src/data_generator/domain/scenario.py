"""Immutable configuration-bound scenario and service settings domain objects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.infrastructure.config.shared_snapshot import SharedDomainSnapshot
from data_generator.domain.model import GeneratorError, OutputMapping


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    version: str
    description: str
    source: dict[str, Any]
    devices: tuple[str, ...]
    points: dict[tuple[str, str], dict[str, Any]]
    mappings: tuple[OutputMapping, ...]
    gateways: dict[str, str]


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    service: dict[str, Any]
    scenarios: dict[str, Scenario]
    revision: str
    shared: SharedDomainSnapshot



def resolve_history(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise GeneratorError("history path must be relative")
    base = (root / "history").resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(base):
        raise GeneratorError("history path escapes authorized history directory")
    return candidate

