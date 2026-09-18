"""Unit tests for ADS 符号浏览（``cli/probe/discover.py`` 的 ADS 路径）。

pyads 网络层通过替换模块级 ``_upload_symbols`` 来 mock——无需真实 TwinCAT。
符号对象的 ``data_type`` 仿照 pyads：一个名为 ``PLCTYPE_*`` 的类。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from wind_hub.adapter.inbound.cli.probe import discover
from wind_hub.config.schema import DeviceConfig
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import ProtocolError


def _ads_device() -> DeviceConfig:
    return DeviceConfig(
        device_id="plc-001",
        protocol="ads",
        endpoint=Endpoint(
            host="10.0.3.1",
            port=48898,
            extensions={"target_net_id": "10.0.3.1.1.1", "target_port": 851},
        ),
    )


def _plctype(name: str) -> type:
    """仿 pyads 的 PLCTYPE_* 类（仅需 ``__name__``）。"""
    return type(name, (), {})


def _symbol(name: str, plctype: str, size: int, comment: str = "") -> Any:
    return SimpleNamespace(name=name, data_type=_plctype(plctype), size=size, comment=comment)


def _patch_symbols(monkeypatch: pytest.MonkeyPatch, symbols: list[Any]) -> None:
    monkeypatch.setattr(discover, "_upload_symbols", lambda _cfg, _host: symbols)


# ---------------------------------------------------------------------------
# 类型映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ads_type", "expected"),
    [
        ("BOOL", "bool"),
        ("SINT", "int8"),
        ("INT", "int16"),
        ("DINT", "int32"),
        ("LINT", "int64"),
        ("REAL", "float32"),
        ("LREAL", "float64"),
        ("STRING", "string"),
        ("DWORD", "uint32"),
        ("TOD", "unknown"),  # 未收录 → unknown
    ],
)
def test_map_ads_data_type(ads_type: str, expected: str) -> None:
    assert discover.map_ads_data_type(ads_type) == expected


# ---------------------------------------------------------------------------
# 符号浏览
# ---------------------------------------------------------------------------


async def test_discover_ads_maps_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_symbols(
        monkeypatch,
        [
            _symbol("MAIN.风机1.转速", "PLCTYPE_REAL", 4, "转速"),
            _symbol("MAIN.nCounter", "PLCTYPE_DINT", 4),
        ],
    )
    points = await discover.discover_ads(_ads_device())
    assert len(points) == 2
    assert points[0].symbol == "MAIN.风机1.转速"
    assert points[0].data_type == "float32"
    assert points[0].size == 4
    assert points[0].comment == "转速"
    assert points[0].address is None  # 符号寻址
    assert points[1].data_type == "int32"
    assert points[1].comment is None  # 空注释归一为 None


async def test_discover_ads_filters_system_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_symbols(
        monkeypatch,
        [
            _symbol("__INFO", "PLCTYPE_DWORD", 4),
            _symbol("__SYMBOLSIZE", "PLCTYPE_UDINT", 4),
            _symbol("MAIN.x", "PLCTYPE_BOOL", 1),
        ],
    )
    points = await discover.discover_ads(_ads_device())
    assert [p.symbol for p in points] == ["MAIN.x"]


async def test_discover_ads_filter_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_symbols(
        monkeypatch,
        [
            _symbol("MAIN.a", "PLCTYPE_BOOL", 1),
            _symbol("GVL.b", "PLCTYPE_BOOL", 1),
        ],
    )
    points = await discover.discover_ads(_ads_device(), filter_prefix="MAIN.")
    assert [p.symbol for p in points] == ["MAIN.a"]


async def test_discover_ads_unknown_type_kept_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_symbols(monkeypatch, [_symbol("MAIN.dt", "PLCTYPE_DATE_AND_TIME", 4)])
    points = await discover.discover_ads(_ads_device())
    assert points[0].data_type == "unknown"


async def test_discover_ads_upload_failure_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_cfg: Any, _host: str) -> list[Any]:
        raise OSError("connection refused")

    monkeypatch.setattr(discover, "_upload_symbols", _boom)
    with pytest.raises(ProtocolError, match="symbol upload failed"):
        await discover.discover_ads(_ads_device())
