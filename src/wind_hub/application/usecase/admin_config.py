"""Admin 配置、Settings 与 Definitions 用例。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from io import BytesIO
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from wind_hub.application.event_log import EventLogStore
from wind_hub.application.usecase.config import ConfigUseCase
from wind_hub.config.loader import load_config
from wind_hub.domain.model.errors import ConfigError

CONFIG_FILES = (
    "system.yaml",
    "units.yaml",
    "device_models.yaml",
    "points.yaml",
    "devices.yaml",
    "tasks.yaml",
    "reporting.yaml",
)


class AdminConfigUseCase:
    """Config 页面与 Settings 页面共享的文件级配置生命周期。"""

    def __init__(self, config: ConfigUseCase, logs: EventLogStore) -> None:
        self._config = config
        self._logs = logs
        self._history_dir = config.config_dir / ".history"
        self._history_dir.mkdir(exist_ok=True)

    def list_files(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in CONFIG_FILES:
            path = self._config.config_dir / name
            if path.exists():
                stat = path.stat()
                rows.append(
                    {
                        "name": name,
                        "size": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC),
                    }
                )
        return rows

    def read_file(self, name: str) -> str:
        path = self._path(name)
        if not path.exists():
            raise KeyError(name)
        return path.read_text(encoding="utf-8")

    def validate(self, name: str, text: str) -> dict[str, Any]:
        try:
            self._validate_candidate(name, text)
        except Exception as exc:
            return {"ok": False, "errors": [str(exc)]}
        return {"ok": True, "errors": []}

    async def apply(
        self,
        name: str,
        text: str,
        source: str = "editor",
    ) -> dict[str, Any]:
        self._validate_candidate(name, text)
        path = self._path(name)
        old = path.read_text(encoding="utf-8") if path.exists() else ""

        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

        result = await self._config.reload()
        if not result.success:
            path.write_text(old, encoding="utf-8")
            await self._config.reload()
            raise ConfigError(
                "; ".join(result.errors) or "configuration apply failed"
            )

        revision = self._create_revision(
            f"{source} applied {name}"
        )
        self._logs.append(
            "INFO",
            "config",
            name,
            f"{source} applied as revision {revision}",
        )
        return {
            "success": True,
            "revision": revision,
            "duration_ms": result.duration_ms,
            "impact": self.impact(name),
        }

    def impact(self, name: str) -> list[str]:
        mapping = {
            "system.yaml": ["Runtime / Sink / interface configuration"],
            "devices.yaml": ["Device registry", "Task target expansion"],
            "device_models.yaml": ["Device model resolution", "Bound devices"],
            "points.yaml": [
                "Point tables",
                "Device point mappings",
                "Task point groups",
            ],
            "tasks.yaml": ["Task definitions", "Task instances"],
            "units.yaml": ["Unit metadata"],
            "reporting.yaml": ["IEC104 reporting proxy"],
        }
        return mapping.get(name, ["Configuration reload"])

    def history(self) -> list[dict[str, Any]]:
        index = self._read_history_index()
        return sorted(index, key=lambda row: int(row["revision"]), reverse=True)

    async def restore(self, revision: int) -> dict[str, Any]:
        """恢复历史 Applied 快照，并把恢复结果登记为一个新 revision。"""
        folder = self._history_dir / f"{revision:06d}"
        if not folder.is_dir():
            raise KeyError(str(revision))

        with tempfile.TemporaryDirectory(
            prefix="wind-hub-restore-"
        ) as temp:
            rollback = Path(temp)
            for name in CONFIG_FILES:
                current = self._config.config_dir / name
                if current.exists():
                    shutil.copy2(current, rollback / name)

            for name in CONFIG_FILES:
                src = folder / name
                target = self._config.config_dir / name
                if src.exists():
                    shutil.copy2(src, target)
                elif target.exists():
                    target.unlink()

            result = await self._config.reload()
            if not result.success:
                for name in CONFIG_FILES:
                    saved = rollback / name
                    target = self._config.config_dir / name
                    if saved.exists():
                        shutil.copy2(saved, target)
                    elif target.exists():
                        target.unlink()
                await self._config.reload()
                raise ConfigError(
                    "; ".join(result.errors)
                    or "restore failed"
                )

        new_revision = self._create_revision(
            f"restored from revision {revision}"
        )
        self._logs.append(
            "INFO",
            "config",
            str(new_revision),
            f"restored from revision {revision}",
        )
        return {
            "success": True,
            "revision": new_revision,
            "restored_from": revision,
            "duration_ms": result.duration_ms,
        }

    def backup_zip(self) -> bytes:
        """打包当前 Applied Configuration，不包含 history。"""
        buffer = BytesIO()
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for name in CONFIG_FILES:
                path = self._config.config_dir / name
                if path.exists():
                    archive.writestr(
                        name,
                        path.read_text(encoding="utf-8"),
                    )
        return buffer.getvalue()

    def settings(self) -> dict[str, Any]:
        system = self._config.current_config.system
        ads = system.ads
        api = system.interfaces.api
        return {
            "site_id": system.site.site_id if system.site else "",
            "site_name": system.site.name if system.site else "",
            "ads": {
                "local_ip": ads.local_ip if ads else "",
                "local_ams_net_id": ads.local_ams_net_id if ads else "",
                "username": ads.route_repair.username if ads else "",
                "password": "",
            },
            "api": {
                "host": api.host,
                "port": api.port,
                "enabled": api.enabled,
            },
            "runtime": system.runtime.model_dump(),
        }

    async def update_settings(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        path = self._path("system.yaml")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw.setdefault("site", {})
        if "site_id" in payload:
            raw["site"]["site_id"] = payload["site_id"]
        if "site_name" in payload:
            raw["site"]["name"] = payload["site_name"]

        if "ads" in payload:
            ads = raw.setdefault("ads", {})
            incoming = payload["ads"] or {}
            for key in ("local_ip", "local_ams_net_id"):
                if key in incoming:
                    ads[key] = incoming[key]
            repair = ads.setdefault("route_repair", {})
            if "username" in incoming:
                repair["username"] = incoming["username"]
            if incoming.get("password") not in (None, ""):
                repair["password"] = incoming["password"]

        if "api" in payload:
            api = raw.setdefault("interfaces", {}).setdefault("api", {})
            api.update(
                {
                    key: value
                    for key, value in payload["api"].items()
                    if key in {"host", "port", "enabled"}
                }
            )

        text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
        return await self.apply("system.yaml", text, source="settings")

    def definitions(self) -> dict[str, Any]:
        cfg = self._config.current_config
        groups = sorted(
            {
                d.device_group
                for d in cfg.devices.devices
                if d.device_group is not None
            }
        )
        point_groups = sorted(
            {
                group
                for table in cfg.point_tables.tables.values()
                for point in table.points
                for group in point.point_groups
            }
        )
        return {
            "device_types": {
                key: value.model_dump()
                for key, value in cfg.device_types.items()
            },
            "device_models": {
                key: value.model_dump()
                for key, value in cfg.device_models.items()
            },
            "device_groups": groups,
            "point_tables": {
                key: value.model_dump()
                for key, value in cfg.point_tables.tables.items()
            },
            "point_groups": point_groups,
            "units": {
                key: value.model_dump()
                for key, value in cfg.units.units.items()
            },
        }

    def _validate_candidate(self, name: str, text: str) -> None:
        self._path(name)
        with tempfile.TemporaryDirectory(
            prefix="wind-hub-config-"
        ) as temp:
            target = Path(temp)
            for cfg_name in CONFIG_FILES:
                src = self._config.config_dir / cfg_name
                if src.exists():
                    shutil.copy2(src, target / cfg_name)
            (target / name).write_text(text, encoding="utf-8")
            load_config(target)

    def _path(self, name: str) -> Path:
        if name not in CONFIG_FILES:
            raise ConfigError(f"unsupported config file: {name}")
        return self._config.config_dir / name

    def _create_revision(self, message: str) -> int:
        index = self._read_history_index()
        revision = max(
            [int(row["revision"]) for row in index],
            default=0,
        ) + 1
        folder = self._history_dir / f"{revision:06d}"
        folder.mkdir(parents=True, exist_ok=False)
        for name in CONFIG_FILES:
            src = self._config.config_dir / name
            if src.exists():
                shutil.copy2(src, folder / name)
        index.append(
            {
                "revision": revision,
                "created_at": datetime.now(UTC).isoformat(),
                "message": message,
            }
        )
        (self._history_dir / "index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return revision

    def _read_history_index(self) -> list[dict[str, Any]]:
        path = self._history_dir / "index.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
