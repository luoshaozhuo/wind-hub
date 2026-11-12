"""电网等值模型：频率模式、电压降、并离网状态与参数校验。"""

from __future__ import annotations

import math

import pytest

from data_generator.application.model.grid import Grid
from data_generator.domain.model import GeneratorError

pytestmark = [pytest.mark.unit]


def _participants(active=300_000.0, reactive=100_000.0):
    return [{"active_power": active, "reactive_power": reactive}]


def test_shipped_external_mode_constructs_at_nominal(make_params) -> None:
    grid = Grid(make_params("grid"))
    assert grid.frequency == pytest.approx(50.0)
    assert grid.mode == "external"


def test_parameter_validation_rejects_illegal_bounds(make_params) -> None:
    for key in ("nominal_voltage_v", "source_voltage_v", "nominal_frequency_hz"):
        params = make_params("grid")
        params[key] = 0.0
        with pytest.raises(GeneratorError):
            Grid(params)
    params = make_params("grid")
    params["grid_r_ohm"] = -0.1
    with pytest.raises(GeneratorError):
        Grid(params)
    params = make_params("grid")
    params["frequency_mode"] = "bogus"
    with pytest.raises(GeneratorError):
        Grid(params)


def test_dynamic_mode_requires_inertia_and_base_power(make_params) -> None:
    params = make_params("grid")
    params["frequency_mode"] = "dynamic"
    with pytest.raises(GeneratorError):
        Grid(params)
    params["equivalent_inertia_s"] = 5.0
    params["frequency_base_power_w"] = 5_000_000.0
    params["frequency_damping_w_hz"] = 100_000.0
    assert Grid(params).mode == "dynamic"


def test_external_mode_frequency_follows_input(make_params) -> None:
    grid = Grid(make_params("grid"))
    output = grid.advance(0.05, _participants(), {"grid_frequency_input": 49.8})
    assert output["grid_frequency"] == pytest.approx(49.8)
    assert output["frequency_deviation"] == pytest.approx(-0.2)
    assert output["frequency_rocof"] == pytest.approx((49.8 - 50.0) / 0.05)


def test_grid_disconnect_zeros_voltage_power_and_reports_fault(make_params) -> None:
    grid = Grid(make_params("grid"))
    output = grid.advance(0.05, _participants(), {"grid_connected": 0})
    assert output["grid_voltage"] == 0.0
    assert output["grid_current"] == 0.0
    assert output["active_power"] == 0.0
    assert output["reactive_power"] == 0.0
    assert output["grid_status"] == 0
    assert output["fault_status"] == 1
    assert output["sim_power_factor"] == 1.0


def test_reconnect_restores_power_and_clears_fault(make_params) -> None:
    grid = Grid(make_params("grid"))
    grid.advance(0.05, _participants(), {"grid_connected": 0})
    output = grid.advance(0.05, _participants(), {"grid_connected": 1})
    assert output["grid_status"] == 1
    assert output["fault_status"] == 0
    assert output["active_power"] == pytest.approx(300_000.0)


def test_line_impedance_voltage_drop_sign(make_params) -> None:
    params = make_params("grid")
    grid = Grid(params)
    # 正有功 + R>0：母线电压高于源电压（V = Vs + (R·P + X·Q)/Vn）。
    output = grid.advance(0.05, _participants(400_000.0, 0.0), {})
    expected = 690.0 + 0.05 * 400_000.0 / 690.0
    assert output["grid_voltage"] == pytest.approx(expected)
    assert output["voltage_deviation"] == pytest.approx(expected - 690.0)


def test_dynamic_frequency_rises_with_positive_imbalance_and_damps(make_params) -> None:
    params = make_params("grid")
    params["frequency_mode"] = "dynamic"
    params["equivalent_inertia_s"] = 5.0
    params["frequency_base_power_w"] = 5_000_000.0
    params["frequency_damping_w_hz"] = 200_000.0
    grid = Grid(params)
    jumped = grid.advance(0.05, _participants(), {"power_imbalance_w": 1_000_000.0})
    assert jumped["grid_frequency"] > 50.0
    # 不平衡归零后，阻尼使频率向额定值回落。
    previous = jumped["grid_frequency"]
    for _ in range(200):
        output = grid.advance(0.05, _participants(), {"power_imbalance_w": 0.0})
        assert abs(output["grid_frequency"] - 50.0) <= abs(previous - 50.0) + 1e-9
        previous = output["grid_frequency"]
    assert output["grid_frequency"] == pytest.approx(50.0, abs=0.05)
    assert math.isfinite(output["frequency_rocof"])


def test_invalid_connection_command_and_source_voltage_rejected(make_params) -> None:
    grid = Grid(make_params("grid"))
    with pytest.raises(GeneratorError):
        grid.advance(0.05, _participants(), {"grid_connected": 2})
    with pytest.raises(GeneratorError):
        grid.advance(0.05, _participants(), {"grid_voltage_source": 0.0})


def test_apparent_power_identity_and_power_factor(make_params) -> None:
    grid = Grid(make_params("grid"))
    output = grid.advance(0.05, _participants(300_000.0, 400_000.0), {})
    assert output["sim_apparent_power"] == pytest.approx(500_000.0)
    assert output["sim_power_factor"] == pytest.approx(0.6)
