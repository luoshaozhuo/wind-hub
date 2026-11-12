"""光伏模型：辐照/温度、MPPT、限功率、逆变器损耗与并离网行为。"""

from __future__ import annotations

import math

import pytest

from data_generator.application.model.pv import PV
from data_generator.domain.model import GeneratorError

pytestmark = [pytest.mark.unit]


def _run(pv: PV, make_weather, controls=None, steps: int = 60, dt: float = 0.05):
    return [
        pv.advance(dt, make_weather(irradiance=800.0), controls or {}, 690.0) for _ in range(steps)
    ]


def test_shipped_parameters_construct(make_params) -> None:
    pv = PV(make_params("solar"))
    assert pv.mppt_enabled is True
    assert pv.dc_voltage == pytest.approx(850.0)


def test_parameter_validation_rejects_illegal_bounds(make_params) -> None:
    cases = {
        "inverter_rated_power_w": 1_100_000.0,  # 大于视在容量
        "mppt_voltage_v": 1200.0,  # 不小于开路电压
        "inverter_efficiency": 1.01,
        "array_peak_power_w": 0.0,
        "stc_irradiance_w_m2": 0.0,
    }
    for key, bad_value in cases.items():
        params = make_params("solar")
        params[key] = bad_value
        with pytest.raises(GeneratorError):
            PV(params)


def test_mppt_enable_must_be_boolean_and_dc_setpoint_in_range(make_params) -> None:
    params = make_params("solar")
    params["mppt_enabled"] = "yes"
    with pytest.raises(GeneratorError):
        PV(params)
    params = make_params("solar")
    params["mppt_enabled"] = False
    params["dc_voltage_setpoint"] = 1200.0  # 超过开路电压
    with pytest.raises(GeneratorError):
        PV(params)


def test_zero_irradiance_means_zero_power_and_offline_status(make_params, make_weather) -> None:
    pv = PV(make_params("solar"))
    output = pv.advance(0.05, make_weather(irradiance=0.0), {}, 690.0)
    assert output["active_power"] == 0.0
    assert output["reactive_power"] == 0.0
    assert output["inverter_status"] == 0
    assert output["pv_available_power"] == 0.0


def test_stc_available_power_matches_peak_and_output_respects_rating(
    make_params, make_weather
) -> None:
    pv = PV(make_params("solar"))
    # NOCT 45°C：目标板温 = ambient + (45-20)·G/800；取 ambient=-6.25°C
    # 使 1000 W/m² 下目标板温恰为 STC 25°C，温度修正项严格为 1。
    output = pv.advance(
        0.05,
        make_weather(irradiance=1000.0, ambient_temperature=-6.25),
        {},
        690.0,
    )
    assert output["pv_available_power"] == pytest.approx(1_000_000.0)
    # 逆变器额定 900 kW：交流输出永远不超过额定。
    assert output["active_power"] <= 900_000.0 + 1e-6
    assert all(math.isfinite(value) for value in output.values())


def test_hotter_cell_reduces_available_power(make_params, make_weather) -> None:
    cool = PV(make_params("solar"))
    hot = PV(make_params("solar"))
    cool_cells = [
        cool.advance(0.05, make_weather(irradiance=1000.0, ambient_temperature=25.0), {}, 690.0)
        for _ in range(200)
    ]
    hot_cells = [
        hot.advance(0.05, make_weather(irradiance=1000.0, ambient_temperature=45.0), {}, 690.0)
        for _ in range(200)
    ]
    assert hot_cells[-1]["cell_temperature"] > cool_cells[-1]["cell_temperature"]
    assert hot_cells[-1]["pv_available_power"] < cool_cells[-1]["pv_available_power"]


def test_curtailment_zero_command_zeros_output_while_irradiance_remains(
    make_params, make_weather
) -> None:
    pv = PV(make_params("solar"))
    outputs = _run(pv, make_weather, {"active_power_setpoint": 0.0})
    assert outputs[-1]["active_power"] == 0.0
    assert outputs[-1]["pv_available_power"] > 0.0  # 上游光能未被浪费为热量


def test_dc_power_identity_and_nonnegative_loss(make_params, make_weather) -> None:
    pv = PV(make_params("solar"))
    for output in _run(pv, make_weather):
        # 生产实现：dc_power = active / eta，loss = dc_power - active。
        assert output["dc_power"] == pytest.approx(
            output["active_power"] / 0.97, abs=1e-6 * max(1.0, output["dc_power"])
        )
        assert output["dc_power"] >= output["active_power"] - 1e-6


def test_dc_voltage_converges_to_mppt_target_and_stays_below_open_circuit(
    make_params, make_weather
) -> None:
    pv = PV(make_params("solar"))
    outputs = _run(pv, make_weather, steps=400)
    assert outputs[-1]["dc_voltage"] == pytest.approx(850.0, rel=1e-3)
    assert all(0 < output["dc_voltage"] < 1100.0 for output in outputs)


def test_fixed_dc_voltage_mode_holds_setpoint(make_params, make_weather) -> None:
    params = make_params("solar")
    params["mppt_enabled"] = False
    params["dc_voltage_setpoint"] = 700.0
    pv = PV(params)
    # mppt_tau=5s：1200 步=60s=12τ，一阶滞后残差 e^-12 < 1e-5。
    outputs = _run(pv, make_weather, steps=1200)
    assert outputs[-1]["dc_voltage"] == pytest.approx(700.0, rel=1e-3)
    assert outputs[-1]["mppt_state"] == 0


def test_grid_disconnect_zeros_active_and_reactive(make_params, make_weather) -> None:
    pv = PV(make_params("solar"))
    output = pv.advance(0.05, make_weather(irradiance=800.0), {"_grid_connected": 0.0}, 690.0)
    assert output["active_power"] == 0.0
    assert output["reactive_power"] == 0.0
    assert output["inverter_status"] == 0


def test_reactive_power_clamped_to_apparent_capacity(make_params, make_weather) -> None:
    pv = PV(make_params("solar"))
    output = pv.advance(
        0.05,
        make_weather(irradiance=800.0),
        {"reactive_power_setpoint": 1e12},
        690.0,
    )
    assert math.hypot(output["active_power"], output["reactive_power"]) <= 1_000_000.0 + 1.0
