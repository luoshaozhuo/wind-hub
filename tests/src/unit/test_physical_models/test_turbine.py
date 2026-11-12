"""风机模型：气动功率/转矩、转速、变桨、超速与切出保护、能量守恒、确定性。"""

from __future__ import annotations

import copy
import math

import pytest

from data_generator.application.model.turbine import Turbine
from data_generator.application.model.weather import WeatherModel
from data_generator.domain.model import GeneratorError

pytestmark = [pytest.mark.unit]

BETZ = 16 / 27


def _weather_sequence(make_weather_config, steps: int, dt: float = 0.05):
    model = WeatherModel("site-a", make_weather_config())
    return [model.advance(dt, step * dt) for step in range(1, steps + 1)]


def _run(turbine: Turbine, weather, controls=None, dt: float = 0.05, voltage: float = 690.0):
    return [turbine.advance(dt, climate, controls or {}, voltage) for climate in weather]


def test_shipped_parameters_construct(make_params) -> None:
    turbine = Turbine(make_params("turbine"))
    assert turbine.omega == pytest.approx(1.5)  # 额定转速作为初始转速


def test_parameter_validation_rejects_illegal_bounds(make_params) -> None:
    cases = {
        "rated_power_w": 0.0,
        "rated_apparent_power_va": 1.0,  # 小于有功额定
        "cut_in_speed_m_s": 13.0,  # 高于额定风速
        "max_rotor_speed_rad_s": 1.0,  # 不大于额定转速
        "cp_opt": 0.99,  # 超过 Betz
        "generator_efficiency": 1.2,
        "pitch_feather_rad": 0.0,  # 不大于最小桨距
    }
    for key, bad_value in cases.items():
        params = make_params("turbine")
        params[key] = bad_value
        with pytest.raises(GeneratorError):
            Turbine(params)


def test_cp_curve_requires_two_monotonic_rows(make_params) -> None:
    params = make_params("turbine")
    params["cp_curve"] = [{"lambda": 7.5, "cp": 0.42}]
    with pytest.raises(GeneratorError):
        Turbine(params)
    params = make_params("turbine")
    params["cp_curve"] = [
        {"lambda": 4.0, "cp": 0.22},
        {"lambda": 4.0, "cp": 0.30},
    ]
    with pytest.raises(GeneratorError):
        Turbine(params)


def test_rated_reference_point_must_achieve_rated_power(make_params) -> None:
    params = make_params("turbine")
    params["rotor_radius_m"] = 1.0  # 扫风面积不足以支撑额定功率
    with pytest.raises(GeneratorError):
        Turbine(params)


def test_deterministic_bit_identical_runs(make_params, make_weather_config) -> None:
    weather_a = _weather_sequence(make_weather_config, 200)
    weather_b = _weather_sequence(make_weather_config, 200)
    outputs_a = _run(Turbine(make_params("turbine")), weather_a)
    outputs_b = _run(Turbine(make_params("turbine")), weather_b)
    assert outputs_a == outputs_b
    assert all(math.isfinite(value) for output in outputs_a for value in output.values())


def test_zero_wind_produces_zero_power_and_torque(make_params, make_weather) -> None:
    turbine = Turbine(make_params("turbine"))
    output = turbine.advance(0.05, make_weather(wind_speed=0.0), {}, 690.0)
    assert output["active_power"] == 0.0
    assert output["shaft_torque"] == 0.0
    assert output["running_status"] == 0
    assert output["power_coefficient"] <= BETZ


def test_aerodynamic_chain_is_physical_at_rated_wind(make_params, make_weather) -> None:
    turbine = Turbine(make_params("turbine"))
    output = turbine.advance(0.05, make_weather(wind_speed=12.0), {}, 690.0)
    assert output["aerodynamic_power"] > 0
    assert output["available_power"] > 0
    assert 0 <= output["power_coefficient"] <= BETZ
    assert output["sim_rotor_speed"] > 0
    # 发电机转速 = 齿轮比 × 转子转速。
    assert output["sim_generator_speed"] == pytest.approx(100.0 * output["sim_rotor_speed"])
    assert output["pitch_angle_1"] >= 0.0


def test_electrical_energy_balance_active_plus_losses_equals_shaft(
    make_params, make_weather
) -> None:
    turbine = Turbine(make_params("turbine"))
    weather = [make_weather(wind_speed=10.0) for _ in range(120)]
    for output in _run(turbine, weather):
        # shaft_torque 已是高速轴转矩 n·T；电气轴功率 = n·T·ω_rotor。
        shaft = output["shaft_torque"] * output["sim_rotor_speed"]
        # 生产实现：active = eta*shaft，losses = (1-eta)*shaft，二者恒等闭合。
        assert output["active_power"] + output["converter_losses"] == pytest.approx(
            shaft, abs=1e-6 * max(1.0, abs(shaft))
        )
        assert output["converter_losses"] >= 0.0
        assert output["active_power"] <= 2_000_000.0 * (1 + 1e-9)


def test_cut_out_trip_latches_feathers_and_zeros_power(make_params, make_weather) -> None:
    turbine = Turbine(make_params("turbine"))
    tripped = turbine.advance(0.05, make_weather(wind_speed=26.0), {}, 690.0)
    assert tripped["fault_status"] == 1
    assert tripped["running_status"] == 0
    assert tripped["active_power"] == 0.0
    # 顺桨：变桨速率限制 0.12 rad/s，全程至少需要 1.57/0.12 ≈ 13.1s。
    previous = tripped["pitch_angle_1"]
    for _step in range(400):
        output = turbine.advance(0.05, make_weather(wind_speed=26.0), {}, 690.0)
        # 每步桨距角增量不得超过 pitch_rate_rad_s·dt（换算为度）。
        assert output["pitch_angle_1"] - previous <= math.degrees(0.12 * 0.05) + 1e-9
        assert output["pitch_angle_1"] >= previous  # 单调顺桨
        previous = output["pitch_angle_1"]
    assert output["pitch_angle_1"] == pytest.approx(math.degrees(1.57), abs=1.0)
    # 风速恢复正常后故障仍终身闭锁。
    recovered = turbine.advance(0.05, make_weather(wind_speed=10.0), {}, 690.0)
    assert recovered["fault_status"] == 1
    assert recovered["active_power"] == 0.0


def test_initial_overspeed_trips_and_brakes_within_max_rotor_speed(
    make_params, make_weather
) -> None:
    params = make_params("turbine")
    params["initial_rotor_speed_rad_s"] = 2.5  # 超过 max_rotor_speed_rad_s=2.0
    turbine = Turbine(params)
    output = turbine.advance(0.05, make_weather(wind_speed=10.0), {}, 690.0)
    assert output["fault_status"] == 1
    assert output["sim_rotor_speed"] <= 2.0
    assert output["sim_generator_torque"] == 0.0  # 机械制动而非电气截幅


def test_curtailment_mode_zero_limit_drives_power_to_zero(make_params, make_weather) -> None:
    turbine = Turbine(make_params("turbine"))
    controls = {"control_mode": 2, "power_limit_setpoint": 0.0}
    weather = [make_weather(wind_speed=11.0) for _ in range(200)]
    outputs = _run(turbine, weather, controls)
    assert outputs[0]["control_mode"] == 2
    assert outputs[-1]["active_power"] == pytest.approx(0.0, abs=1.0)
    assert all(0 <= output["pitch_angle_1"] <= math.degrees(1.57) + 1e-9 for output in outputs)


def test_grid_disconnect_stops_electrical_output(make_params, make_weather) -> None:
    turbine = Turbine(make_params("turbine"))
    output = turbine.advance(0.05, make_weather(wind_speed=11.0), {"_grid_connected": 0.0}, 690.0)
    assert output["active_power"] == 0.0
    assert output["reactive_power"] == 0.0
    assert output["running_status"] == 0


def test_unknown_control_mode_rejected(make_params, make_weather) -> None:
    turbine = Turbine(make_params("turbine"))
    with pytest.raises(GeneratorError):
        turbine.advance(0.05, make_weather(), {"control_mode": 7}, 690.0)


def test_reactive_power_stays_inside_apparent_capacity(make_params, make_weather) -> None:
    turbine = Turbine(make_params("turbine"))
    controls = {"reactive_power_setpoint": 1e9}  # 远超容量，必须被钳位
    output = turbine.advance(0.05, make_weather(wind_speed=10.0), controls, 690.0)
    assert math.hypot(output["active_power"], output["reactive_power"]) <= 2_200_000.0 + 1.0


def test_independent_instances_do_not_share_state(make_params, make_weather) -> None:
    first = Turbine(copy.deepcopy(make_params("turbine")))
    second = Turbine(copy.deepcopy(make_params("turbine")))
    first.advance(0.05, make_weather(wind_speed=20.0), {}, 690.0)
    assert second.omega == pytest.approx(1.5)  # 第二台不受第一台跳闸影响
