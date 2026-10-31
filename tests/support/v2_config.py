"""System harness：生成 Collector v2 三路径配置树（service / instances / templates）。

生产 Collector CLI 只消费三路径：

- ``services/collector.yaml``：服务级运行参数；
- ``instances/``：``site.yaml`` + 设备实例文档 ``devices.yaml``；
- ``templates/``：不可变版本化模板（protocol / point_table / device_model / task）。

旧 system 测试的扁平 Core 目录仍由 Commander / Web Server / 协议从站消费，
本模块只负责把 Collector 需要的那一半从出厂模板或扁平站点派生出来，
不添加任何生产兼容层。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED = REPO_ROOT / "configs"

#: 与 tests.support.control.DEFAULT_ADMIN_TOKEN 保持一致；system harness 同时
#: 把它注入 Collector 与 Web Server 进程，设备级 START/STOP 必须携带该令牌。
SYSTEM_ADMIN_TOKEN = "system-test-admin-token"


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected mapping document")
    return data


def _empty_tree(base: Path) -> Path:
    """复制出厂三路径树（含 modbus_wtg 模板）作为 v2 测试树根。"""
    root = base / "v2"
    for name in ("services", "instances", "templates"):
        shutil.copytree(SHIPPED / name, root / name)
    return root


def write_v2_modbus_tree(
    base: Path,
    port: int,
    *,
    device_id: str = "modbus-1",
    site_id: str = "test",
    desired_state: str = "STOP",
    nats_url: str = "nats://127.0.0.1:4222",
    slave_id: int = 1,
) -> Path:
    """单 Modbus 设备的 v2 三路径树（复用出厂 modbus_wtg 1.0.0 模板）。

    采集点为出厂模板 ``active_power``（holding@0, int32）；system 控制面
    round-trip 不断言寄存器语义，数据面验证由 acquisition 套件负责。
    """
    root = _empty_tree(base)
    _dump(
        root / "instances" / "site.yaml",
        {"schema_version": 1, "site_id": site_id, "name": f"System {site_id}"},
    )
    _dump(
        root / "instances" / "devices.yaml",
        {
            "schema_version": 1,
            "devices": {
                device_id: {
                    "model": {"id": "modbus_wtg", "version": "1.0.0"},
                    "endpoint": {"host": "127.0.0.1", "port": port},
                    "overrides": {
                        "protocol_options": {"slave_id": slave_id},
                        "task": {"desired_state": desired_state},
                    },
                }
            },
        },
    )
    service = _load(root / "services" / "collector.yaml")
    service["messaging"]["servers"] = [nats_url]
    service.setdefault("runtime", {}).update(
        {"connect_timeout": 2.0, "read_timeout": 2.0, "shutdown_timeout": 5.0}
    )
    service["watchdog"] = {"interval": 0.5, "health_timeout": 0.5}
    _dump(root / "services" / "collector.yaml", service)
    return root


def emit_v2_tree_from_flat_site(
    base: Path,
    flat_site: Path,
    *,
    nats_url: str = "nats://127.0.0.1:4222",
) -> Path:
    """把 generate_virtual_site 产出的扁平站点派生为 v2 三路径树。

    扁平站点的每个 point_table / device_model 成为 1.0.0 版本模板；
    设备文档逐条转换为 model 引用 + endpoint + protocol_options 覆盖；
    采集任务每设备唯一（v2 不再有 Task→设备组扇出），统一引用
    ``device_all`` 1.0.0（point_group=all）。
    """
    root = _empty_tree(base)

    points_doc = _load(flat_site / "points.yaml")
    models_doc = _load(flat_site / "device_models.yaml")
    devices_doc = _load(flat_site / "devices.yaml")
    system_doc = _load(flat_site / "system.yaml")
    tasks_path = flat_site / "tasks.yaml"
    tasks_doc = _load(tasks_path) if tasks_path.exists() else {"tasks": {}}

    point_tables = points_doc["point_tables"]
    device_models = models_doc["device_models"]

    # 每个协议一个 protocol 模板（template_id 任意，protocol 字段必须是
    # Core 协议名）；point_table / model 沿用扁平 ID 作为 template_id。
    protocols = {model["protocol"] for model in device_models.values()}
    for protocol in sorted(protocols):
        _dump(
            root / "templates" / "protocols" / f"{protocol}_default" / "1.0.0.yaml",
            {
                "schema_version": 1,
                "template_id": f"{protocol}_default",
                "template_version": "1.0.0",
                "protocol": protocol,
            },
        )

    # 扁平任务按具名 point_group 采点，而 v2 每设备唯一任务固定引用 all。
    # 派生模板给每个点补 all 成员（原有成员保留、去重），使 v2 设备任务
    # 覆盖扁平站点任务采集的同一点集，不删除任何既有点组归属。
    for table_id, table in point_tables.items():
        protocol = table["protocol"]
        derived_points = {}
        for point_id, point in table["points"].items():
            groups = list(point.get("point_groups", []))
            if "all" not in groups:
                point = {**point, "point_groups": [*groups, "all"]}
            derived_points[point_id] = point
        _dump(
            root / "templates" / "point_tables" / table_id / "1.0.0.yaml",
            {
                "schema_version": 1,
                "template_id": table_id,
                "template_version": "1.0.0",
                "protocol": protocol,
                "points": derived_points,
            },
        )

    # 扁平任务按 device_group_id 引用点组与节拍；v2 型号直接引用任务模板。
    # 为每个唯一 (point_group, interval) 组合生成一个任务模板，再按
    # 「使用该型号的设备所属分组」为型号选定模板；无 tasks.yaml 时回退
    # all/1.0s 单一模板。
    flat_tasks = tasks_doc.get("tasks", {})
    group_spec: dict[str, tuple[str, float, str]] = {
        str(task["device_group_id"]): (
            str(task.get("point_group", "all")),
            float(task.get("interval", 1.0)),
            str(task.get("desired_state", "STOP")),
        )
        for task in flat_tasks.values()
    }
    combo_ids: dict[tuple[str, float, str], str] = {}
    for spec in group_spec.values():
        if spec not in combo_ids:
            combo_ids[spec] = f"task_{len(combo_ids):02d}"

    def _model_task_ref(model_id: str) -> dict[str, str]:
        for device in devices_doc["devices"].values():
            if device.get("model_id") != model_id:
                continue
            for group_id in device.get("device_group_ids", []):
                spec = group_spec.get(str(group_id))
                if spec is not None:
                    return {"id": combo_ids[spec], "version": "1.0.0"}
        return {"id": "device_all", "version": "1.0.0"}

    for model_id, model in device_models.items():
        protocol = model["protocol"]
        document: dict[str, Any] = {
            "schema_version": 1,
            "template_id": model_id,
            "template_version": "1.0.0",
            "name": model_id,
            "device_type_id": model.get("device_type_id", "turbine"),
            "protocol": {"id": f"{protocol}_default", "version": "1.0.0"},
            "point_table": {"id": model["point_table_id"], "version": "1.0.0"},
            "task": _model_task_ref(model_id),
        }
        if model.get("protocol_options"):
            document["protocol_options"] = model["protocol_options"]
        _dump(
            root / "templates" / "device_models" / model_id / "1.0.0.yaml",
            document,
        )

    for spec, task_id in combo_ids.items():
        point_group, interval, desired_state = spec
        _dump(
            root / "templates" / "tasks" / task_id / "1.0.0.yaml",
            {
                "schema_version": 1,
                "template_id": task_id,
                "template_version": "1.0.0",
                "point_group": point_group,
                "interval": interval,
                "desired_state": desired_state,
            },
        )
    if not combo_ids:
        _dump(
            root / "templates" / "tasks" / "device_all" / "1.0.0.yaml",
            {
                "schema_version": 1,
                "template_id": "device_all",
                "template_version": "1.0.0",
                "point_group": "all",
                "interval": 1.0,
                "desired_state": "STOP",
            },
        )

    # 扁平站点的 units / business_points 与模板根解析器同源；扁平文档缺
    # schema_version 头（旧 Core 适配器不要它），复制时补齐 v2 必需的头。
    units_doc = _load(flat_site / "units.yaml")
    _dump(root / "templates" / "units.yaml", {"schema_version": 1, **units_doc})
    business_doc = _load(flat_site / "business_points.yaml")
    _dump(
        root / "templates" / "business_points.yaml",
        {"schema_version": 1, **business_doc},
    )

    site = system_doc.get("site", {})
    _dump(
        root / "instances" / "site.yaml",
        {
            "schema_version": 1,
            "site_id": site.get("site_id", "virtual_windfarm"),
            "name": site.get("name", "Virtual Wind Farm"),
        },
    )

    v2_devices: dict[str, Any] = {}
    for device_id, device in devices_doc["devices"].items():
        entry: dict[str, Any] = {
            "model": {"id": device["model_id"], "version": "1.0.0"},
            "endpoint": dict(device["endpoint"]),
        }
        if device.get("protocol_options"):
            entry["overrides"] = {"protocol_options": dict(device["protocol_options"])}
        if device.get("enabled") is False:
            entry["enabled"] = False
        v2_devices[device_id] = entry
    _dump(root / "instances" / "devices.yaml", {"schema_version": 1, "devices": v2_devices})

    service = _load(root / "services" / "collector.yaml")
    messaging = system_doc.get("messaging")
    if messaging:
        service["messaging"] = {
            "servers": messaging.get("servers", [nats_url]),
            "delivery": messaging.get("delivery", "realtime"),
            "max_staleness_ms": messaging.get("max_staleness_ms", 60_000),
        }
    if system_doc.get("ads"):
        service["ads"] = system_doc["ads"]
    runtime = system_doc.get("runtime", {})
    service.setdefault("runtime", {}).update(
        {
            "connect_timeout": runtime.get("connect_timeout", 10.0),
            "read_timeout": runtime.get("read_timeout", 5.0),
            "write_timeout": runtime.get("write_timeout", 5.0),
            "shutdown_timeout": runtime.get("shutdown_timeout", 30.0),
        }
    )
    if runtime.get("watchdog_interval") or runtime.get("health_timeout"):
        service["watchdog"] = {
            "interval": runtime.get("watchdog_interval", 1.0),
            "health_timeout": runtime.get("health_timeout", 1.0),
        }
    # v2 把发布队列容量配置迁到 metrics.pending_capacity（见 collector
    # assembly 的 SamplePublisher 构造）；映射扁平 runtime.queue_maxsize，
    # 否则固定默认值在目标负载下背压丢点。
    queue_maxsize = runtime.get("queue_maxsize")
    if queue_maxsize:
        service.setdefault("metrics", {})["pending_capacity"] = int(queue_maxsize)
    _dump(root / "services" / "collector.yaml", service)
    return root


# ---------------------------------------------------------------------------
# Component harness：从扁平点表规格直接派生 v2 三路径树
# ---------------------------------------------------------------------------

ADS_LOGICAL_TYPES = {
    "BOOL": "bool",
    "SINT": "int8",
    "USINT": "uint8",
    "INT": "int16",
    "UINT": "uint16",
    "DINT": "int32",
    "UDINT": "uint32",
    "LINT": "int64",
    "ULINT": "uint64",
    "REAL": "float32",
    "LREAL": "float64",
    "STRING": "str",
}

DEFAULT_SERVICE_DOC: dict[str, Any] = {
    "schema_version": 1,
    "runtime": {
        "shutdown_timeout": 5.0,
        "connect_timeout": 2.0,
        "read_timeout": 2.0,
        "write_timeout": 2.0,
        "thread_pool": {"max_workers": 8},
    },
    "watchdog": {"interval": 0.5, "health_timeout": 0.5},
    "messaging": {
        "servers": ["nats://127.0.0.1:4222"],
        "delivery": "realtime",
        "max_staleness_ms": 60_000,
    },
    "metrics": {
        "enabled": True,
        "windows": [60],
        "publish_interval": 10,
        "max_staleness": 300,
        "event_capacity": 1000,
        "pending_capacity": 120,
    },
}


def _business_points(point_tables: dict[str, Any]) -> dict[str, Any]:
    business: dict[str, Any] = {}
    for table in point_tables.values():
        protocol = table.get("protocol", "modbus")
        for point_id, point in table.get("points", {}).items():
            data_type = point.get("data_type")
            if data_type is None:
                continue
            if protocol == "ads":
                data_type = ADS_LOGICAL_TYPES.get(str(data_type).upper(), data_type)
            business.setdefault(
                point_id, {"data_type": data_type, "unit": point.get("unit", "none")}
            )
    return {"schema_version": 1, "business_points": business}


def write_v2_component_tree(
    base: Path,
    *,
    devices: dict[str, dict[str, Any]],
    point_tables: dict[str, Any],
    device_models: dict[str, Any] | None = None,
    site_id: str = "test",
    task_interval: float = 1.0,
    task_point_group: str | None = None,
    default_desired_state: str = "STOP",
    nats_url: str = "nats://127.0.0.1:4222",
    service: dict[str, Any] | None = None,
) -> Path:
    """从扁平点表/设备规格派生自包含 v2 三路径树（组件测试用）。

    Args:
        devices: ``{device_id: {model, endpoint, overrides, enabled}}``；
            model 为 ``{"id", "version"}`` 引用（version 默认 1.0.0）。
            便捷写法允许省略 model，此时要求只存在一个设备型号。
        point_tables: 扁平点表 ``{table_id: {protocol?, points}}``，
            点定义与 write_config_tree 同形（point_groups/address/data_type）。
        device_models: ``{model_id: {protocol, point_table, protocol_options?}}``；
            省略时按点表与设备自动派生单型号 ``modbus``。
        task_interval / task_point_group: 每设备任务模板参数；point_group
            省略时使用点表中第一个 point_group。
        default_desired_state: 任务模板缺省 desired_state（设备覆盖优先）。
        service: 覆盖/合并进服务文档（messaging.servers 默认被 nats_url 覆盖）。
    """
    from tests.support.units import MINIMAL_UNITS_DOCUMENT

    tables = {name: dict(table) for name, table in point_tables.items()}
    protocols = sorted({table.setdefault("protocol", "modbus") for table in tables.values()})
    models_spec = device_models
    if models_spec is None:
        if len(tables) != 1:
            raise ValueError("device_models required when multiple point tables exist")
        (only_table,) = tables
        only_protocol = tables[only_table]["protocol"]
        models_spec = {
            "modbus": {
                "protocol": only_protocol,
                "point_table": only_table,
            }
        }
    model_protocols = {spec["protocol"] for spec in models_spec.values()}
    protocols = sorted(set(protocols) | model_protocols)

    root = base / "v2"
    templates = root / "templates"
    for protocol in protocols:
        _dump(
            templates / "protocols" / f"{protocol}_default" / "1.0.0.yaml",
            {
                "schema_version": 1,
                "template_id": f"{protocol}_default",
                "template_version": "1.0.0",
                "protocol": protocol,
            },
        )
    for table_id, table in tables.items():
        _dump(
            templates / "point_tables" / table_id / "1.0.0.yaml",
            {
                "schema_version": 1,
                "template_id": table_id,
                "template_version": "1.0.0",
                "protocol": table["protocol"],
                "points": table["points"],
            },
        )
    for model_id, spec in models_spec.items():
        document = {
            "schema_version": 1,
            "template_id": model_id,
            "template_version": "1.0.0",
            "name": model_id,
            "device_type_id": spec.get("device_type_id", "turbine"),
            "protocol": {"id": f"{spec['protocol']}_default", "version": "1.0.0"},
            "point_table": {"id": spec["point_table"], "version": "1.0.0"},
            "task": {"id": "device_all", "version": "1.0.0"},
        }
        if spec.get("protocol_options"):
            document["protocol_options"] = spec["protocol_options"]
        _dump(templates / "device_models" / model_id / "1.0.0.yaml", document)

    if task_point_group is None:
        groups = {
            group
            for table in tables.values()
            for point in table["points"].values()
            for group in point.get("point_groups", [])
        }
        task_point_group = sorted(groups)[0] if groups else "all"
    _dump(
        templates / "tasks" / "device_all" / "1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "device_all",
            "template_version": "1.0.0",
            "point_group": task_point_group,
            "interval": task_interval,
            "desired_state": default_desired_state,
        },
    )
    _dump(templates / "units.yaml", {"schema_version": 1, **MINIMAL_UNITS_DOCUMENT})
    _dump(templates / "business_points.yaml", _business_points(tables))

    _dump(
        root / "instances" / "site.yaml",
        {"schema_version": 1, "site_id": site_id, "name": f"Component {site_id}"},
    )
    v2_devices: dict[str, Any] = {}
    for device_id, raw in devices.items():
        entry = dict(raw)
        if "model" not in entry:
            if len(models_spec) != 1:
                raise ValueError(f"device {device_id}: model reference required")
            entry["model"] = {"id": next(iter(models_spec)), "version": "1.0.0"}
        elif isinstance(entry["model"], str):
            entry["model"] = {"id": entry["model"], "version": "1.0.0"}
        v2_devices[device_id] = entry
    _dump(root / "instances" / "devices.yaml", {"schema_version": 1, "devices": v2_devices})

    service_doc = dict(DEFAULT_SERVICE_DOC)
    if service:
        service_doc.update(service)
    service_doc.setdefault("messaging", {})["servers"] = [nats_url]
    _dump(root / "services" / "collector.yaml", service_doc)
    return root
