"""Unit tests for the compute_diff function."""

from __future__ import annotations

from wind_hub.application.config_service import compute_diff
from wind_hub.config.schema import (
    Config,
    DeviceConfig,
    DevicesConfig,
    PipelineConfig,
    PointAddress,
    PointConfig,
    PointTableConfig,
    PointTablesConfig,
    RoutingConfig,
    SinkConfig,
    SystemConfig,
)
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.route import RouteRule, RouteTarget


def _make_config(
    devices: list[DeviceConfig] | None = None,
    sinks: list[SinkConfig] | None = None,
    tables: dict[str, PointTableConfig] | None = None,
    rules: list[RouteRule] | None = None,
    processors: list[str] | None = None,
) -> Config:
    return Config(
        system=SystemConfig(
            sinks=sinks or [],
            pipeline=PipelineConfig(processors=processors or []),
        ),
        devices=DevicesConfig(devices=devices or []),
        point_tables=PointTablesConfig(tables=tables or {}),
        routing=RoutingConfig(rules=rules or []),
    )


def _ep(host: str = "10.0.0.1") -> Endpoint:
    return Endpoint(host=host, port=502)


def _device(device_id: str, protocol: str = "modbus", **kwargs) -> DeviceConfig:  # type: ignore[no-untyped-def]
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        point_table="t1",
        endpoint=kwargs.pop("endpoint", _ep()),
        **kwargs,
    )


def _table(*points: PointConfig) -> PointTableConfig:
    return PointTableConfig(points=list(points))


def _point(point_id: str = "p1", **kwargs) -> PointConfig:  # type: ignore[no-untyped-def]
    return PointConfig(point_id=point_id, address=PointAddress(type="hr"), **kwargs)


# ---------------------------------------------------------------------------
# 1. Empty diff — identical configs
# ---------------------------------------------------------------------------


def test_empty_diff_identical_configs() -> None:
    cfg = _make_config(
        devices=[_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
        tables={"t1": _table(_point("p1"))},
        rules=[RouteRule(name="default", targets=[RouteTarget(sink="s1")])],
        processors=["unit_convert"],
    )
    diff = compute_diff(cfg, cfg)
    assert not diff.has_any_changes
    assert diff.devices.added == []
    assert diff.devices.removed == []
    assert diff.devices.updated == []
    assert not diff.points_changed
    assert diff.point_tables_changed == []


# ---------------------------------------------------------------------------
# 2. Device added
# ---------------------------------------------------------------------------


def test_device_added() -> None:
    old = _make_config(
        devices=[_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    new = _make_config(
        devices=[
            _device("d1"),
            _device("d2", protocol="ads", endpoint=_ep("10.0.0.2")),
        ],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    diff = compute_diff(old, new)
    assert diff.devices.added == ["d2"]
    assert diff.devices.removed == []
    assert diff.devices.updated == []
    assert diff.devices.unchanged == ["d1"]


# ---------------------------------------------------------------------------
# 3. Device removed
# ---------------------------------------------------------------------------


def test_device_removed() -> None:
    old = _make_config(
        devices=[
            _device("d1"),
            _device("d2", protocol="ads", endpoint=_ep("10.0.0.2")),
        ],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    new = _make_config(
        devices=[_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    diff = compute_diff(old, new)
    assert diff.devices.removed == ["d2"]
    assert diff.devices.added == []
    assert diff.devices.unchanged == ["d1"]


# ---------------------------------------------------------------------------
# 4. Device updated — endpoint changed
# ---------------------------------------------------------------------------


def test_device_endpoint_changed() -> None:
    old = _make_config(
        devices=[_device("d1", endpoint=_ep("10.0.0.1"))],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    new = _make_config(
        devices=[_device("d1", endpoint=_ep("10.0.1.1"))],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    diff = compute_diff(old, new)
    assert diff.devices.updated == ["d1"]
    assert diff.devices.added == []
    assert diff.devices.removed == []


# ---------------------------------------------------------------------------
# 5. Device updated — protocol changed
# ---------------------------------------------------------------------------


def test_device_protocol_changed() -> None:
    old = _make_config(
        devices=[_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    new = _make_config(
        devices=[_device("d1", protocol="iec104")],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    diff = compute_diff(old, new)
    assert diff.devices.updated == ["d1"]


# ---------------------------------------------------------------------------
# 6. Device enabled changed
# ---------------------------------------------------------------------------


def test_device_enabled_changed() -> None:
    old = _make_config(
        devices=[_device("d1", enabled=True)],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    new = _make_config(
        devices=[_device("d1", enabled=False)],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    diff = compute_diff(old, new)
    assert diff.devices.updated == ["d1"]


# ---------------------------------------------------------------------------
# 7. Sink added / removed / updated
# ---------------------------------------------------------------------------


def test_sink_added() -> None:
    old = _make_config(sinks=[SinkConfig(name="s1", type="file")])
    new = _make_config(
        sinks=[
            SinkConfig(name="s1", type="file"),
            SinkConfig(name="s2", type="kafka"),
        ],
    )
    diff = compute_diff(old, new)
    assert diff.sinks.added == ["s2"]
    assert diff.sinks.unchanged == ["s1"]


def test_sink_removed() -> None:
    old = _make_config(
        sinks=[
            SinkConfig(name="s1", type="file"),
            SinkConfig(name="s2", type="kafka"),
        ],
    )
    new = _make_config(sinks=[SinkConfig(name="s1", type="file")])
    diff = compute_diff(old, new)
    assert diff.sinks.removed == ["s2"]
    assert diff.sinks.unchanged == ["s1"]


def test_sink_updated() -> None:
    old = _make_config(sinks=[SinkConfig(name="s1", type="file")])
    new = _make_config(sinks=[SinkConfig(name="s1", type="kafka")])
    diff = compute_diff(old, new)
    assert diff.sinks.updated == ["s1"]


# ---------------------------------------------------------------------------
# 8. Point tables changed
# ---------------------------------------------------------------------------


def test_table_content_changed() -> None:
    old = _make_config(tables={"t1": _table(_point("p1", data_type="float32"))})
    new = _make_config(tables={"t1": _table(_point("p1", data_type="float32", scale=2.0))})
    diff = compute_diff(old, new)
    assert diff.points_changed
    assert diff.point_tables_changed == ["t1"]


def test_table_added() -> None:
    old = _make_config(tables={"t1": _table(_point("p1"))})
    new = _make_config(tables={"t1": _table(_point("p1")), "t2": _table(_point("p9"))})
    diff = compute_diff(old, new)
    assert diff.points_changed
    assert diff.point_tables_changed == ["t2"]


def test_table_removed() -> None:
    old = _make_config(tables={"t1": _table(_point("p1")), "t2": _table(_point("p9"))})
    new = _make_config(tables={"t1": _table(_point("p1"))})
    diff = compute_diff(old, new)
    assert diff.points_changed
    assert diff.point_tables_changed == ["t2"]


def test_point_added_to_table() -> None:
    old = _make_config(tables={"t1": _table(_point("p1"))})
    new = _make_config(tables={"t1": _table(_point("p1"), _point("p2"))})
    diff = compute_diff(old, new)
    assert diff.points_changed
    assert diff.point_tables_changed == ["t1"]


def test_tables_unchanged() -> None:
    cfg = _make_config(tables={"t1": _table(_point("p1", data_type="float32"))})
    diff = compute_diff(cfg, cfg)
    assert not diff.points_changed
    assert diff.point_tables_changed == []


# ---------------------------------------------------------------------------
# 9. Rules changed
# ---------------------------------------------------------------------------


def test_rules_changed() -> None:
    old = _make_config(
        rules=[RouteRule(name="r1", targets=[RouteTarget(sink="s1")], priority=0)],
    )
    new = _make_config(
        # priority changed
        rules=[RouteRule(name="r1", targets=[RouteTarget(sink="s1")], priority=10)],
    )
    diff = compute_diff(old, new)
    assert diff.rules_changed


def test_rules_unchanged() -> None:
    cfg = _make_config(
        rules=[RouteRule(name="r1", targets=[RouteTarget(sink="s1")], priority=0)],
    )
    diff = compute_diff(cfg, cfg)
    assert not diff.rules_changed


# ---------------------------------------------------------------------------
# 10. Pipeline changed
# ---------------------------------------------------------------------------


def test_pipeline_changed() -> None:
    old = _make_config(processors=["proc_a"])
    new = _make_config(processors=["proc_a", "proc_b"])
    diff = compute_diff(old, new)
    assert diff.pipeline_changed


def test_pipeline_unchanged() -> None:
    cfg = _make_config(processors=["proc_a", "proc_b"])
    diff = compute_diff(cfg, cfg)
    assert not diff.pipeline_changed


# ---------------------------------------------------------------------------
# Edge: all diff types at once
# ---------------------------------------------------------------------------


def test_comprehensive_diff() -> None:
    old = _make_config(
        devices=[
            _device("d1", endpoint=_ep("10.0.0.1")),
            _device("d2", protocol="ads", endpoint=_ep("10.0.0.2")),
        ],
        sinks=[
            SinkConfig(name="s1", type="file"),
            SinkConfig(name="s2", type="kafka"),
        ],
        tables={"t1": _table(_point("p1"))},
        rules=[RouteRule(name="r1", targets=[RouteTarget(sink="s1")])],
        processors=["a"],
    )
    new = _make_config(
        devices=[
            _device("d1", protocol="iec104", endpoint=_ep("10.0.1.1")),
            _device("d3", endpoint=_ep("10.0.0.3")),
        ],
        sinks=[
            SinkConfig(name="s1", type="kafka"),
            SinkConfig(name="s3", type="db"),
        ],
        tables={"t1": _table(_point("p1"), _point("p2"))},
        rules=[RouteRule(name="r2", targets=[RouteTarget(sink="s1"), RouteTarget(sink="s3")])],
        processors=["a", "b"],
    )
    diff = compute_diff(old, new)
    assert diff.devices.added == ["d3"]
    assert diff.devices.removed == ["d2"]
    assert diff.devices.updated == ["d1"]
    assert diff.sinks.added == ["s3"]
    assert diff.sinks.removed == ["s2"]
    assert diff.sinks.updated == ["s1"]
    assert diff.points_changed
    assert diff.point_tables_changed == ["t1"]
    assert diff.rules_changed
    assert diff.pipeline_changed
    assert diff.has_any_changes
