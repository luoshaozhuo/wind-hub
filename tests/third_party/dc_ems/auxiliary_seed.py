"""在现有十风机数据库种子中添加共用 Gateway1 的虚拟站控/调频设备。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def append_auxiliary_devices(seed: dict[str, Any]) -> dict[str, Any]:
    """复用首台 Gateway 的 Modbus 地址，增加两种 DCEMS 逻辑设备类型。"""
    if not seed.get("devices") or not seed.get("edgeServers") or not seed.get("edgeaddrset"):
        raise ValueError("DCEMS seed needs turbines, servers and an address set")
    if any(str(item.get("deviceType")) in {"1", "2"} for item in seed["devices"]):
        raise ValueError("auxiliary devices already configured")
    if any(int(item["setId"]) in {2, 3} for item in seed["edgeaddrset"]):
        raise ValueError("auxiliary address-set identity already occupied")
    if not all(str(item.get("deviceType")) == "0" for item in seed["devices"]):
        raise ValueError("expected wind turbines only before auxiliary extension")

    document = deepcopy(seed)
    source_device = document["devices"][0]
    source_point = document["edgeaddrset"][0]
    for identity, name, kind, address_set in (
        ("DCEMS_SUBSTATION", "DCEMS substation", "1", 2),
        ("DCEMS_PFR", "DCEMS primary frequency", "2", 3),
    ):
        device = deepcopy(source_device)
        device.update(
            id=None,
            deviceId=identity,
            deviceName=name,
            deviceType=kind,
            serverId=1,
            addrSet=address_set,
            lineNo=1,
        )
        document["devices"].append(device)

    for set_id, name, address, data_type, scale in (
        (2, "powAct", "1.3.2", "F", 0.001),
        (2, "powReact", "1.3.4", "F", 0.001),
        (3, "isFreMode", "1.1.0", "B", None),
    ):
        point = deepcopy(source_point)
        point.update(
            id=None,
            setId=set_id,
            varPath=name,
            addr=address,
            dataType=data_type,
            highBit=0,
            coe=scale,
            plus=None,
            isCtrl=False,
            isCal=False,
            isWatch=True,
            pointName=name,
        )
        document["edgeaddrset"].append(point)

    home_rows = [
        row
        for row in document["pagepoints"]
        if row.get("pageId") == "HomePage" and row.get("locationId") == "allTableList"
    ]
    if not home_rows:
        raise ValueError("HomePage monitoring template is missing")
    next_order = max(int(row["orderNo"]) for row in home_rows) + 1
    for var_path, point_name, unit in (
        ("emsSyzAct", "升压站有功", "kW"),
        ("emsSyzReact", "升压站无功", "kVar"),
    ):
        row = deepcopy(home_rows[0])
        row.update(
            id=None,
            varPath=var_path,
            pointName=point_name,
            unit=unit,
            orderNo=next_order,
        )
        next_order += 1
        document["pagepoints"].append(row)
    return document
