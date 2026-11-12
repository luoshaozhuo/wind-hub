"""储能模型：SOC/OCV/内阻/效率/容量边界、能量守恒与并离网行为。"""

from __future__ import annotations

import math

import pytest

from data_generator.application.model.bess import BESS
from data_generator.domain.model import GeneratorError

pytestmark = [pytest.mark.unit]

DT = 0.05
RATED_J = 1_000_000.0 * 3600.0  # rated_energy_wh 换算焦耳


def _advance(bess: BESS, make_weather, controls=None):
    return bess.advance(DT, make_weather(), controls or {}, 690.0)


def test_shipped_parameters_construct_at_half_soc(make_params) -> None:
    bess = BESS(make_params("battery"))
    assert bess.energy == pytest.approx(0.5 * RATED_J)
    assert bess._ocv(0.5) == pytest.approx(800.0)


def test_parameter_validation_rejects_illegal_bounds(make_params) -> None:
    cases = [
        ("rated_energy_wh", 0.0),
        ("pcs_rated_apparent_power_va", 1.0),  # 小于有功容量
        ("charge_efficiency", 1.1),
        ("discharge_efficiency", 0.0),
        ("max_battery_current_a", 0.0),
        ("initial_soc", 0.05),  # 低于 soc_min
        ("initial_soc", 0.95),  # 高于 soc_max
        ("internal_resistance_ohm", -0.1),
        ("initial_soh", 0.0),
    ]
    for key, bad_value in cases:
        params = make_params("battery")
        params[key] = bad_value
        with pytest.raises(GeneratorError):
            BESS(params)


def test_strategy_reactive_flag_and_ocv_curve_contract(make_params) -> None:
    params = make_params("battery")
    params["control_strategy"] = "manual"
    with pytest.raises(GeneratorError):
        BESS(params)
    params = make_params("battery")
    params["reactive_support_enabled"] = "yes"
    with pytest.raises(GeneratorError):
        BESS(params)
    params = make_params("battery")
    params["ocv_curve"] = [{"soc": 0.5, "voltage_v": 800.0}]
    with pytest.raises(GeneratorError):
        BESS(params)
    params = make_params("battery")
    params["ocv_curve"] = [
        {"soc": 0.9, "voltage_v": 900.0},
        {"soc": 0.1, "voltage_v": 700.0},
    ]  # 逆序
    with pytest.raises(GeneratorError):
        BESS(params)
    params = make_params("battery")
    params["ocv_curve"] = [
        {"soc": 0.2, "voltage_v": 720.0},
        {"soc": 0.8, "voltage_v": 880.0},
    ]  # 未覆盖完整 SOC 区间
    with pytest.raises(GeneratorError):
        BESS(params)


def test_ocv_interpolation_is_monotonic(make_params) -> None:
    bess = BESS(make_params("battery"))
    voltages = [bess._ocv(soc) for soc in (0.1, 0.3, 0.5, 0.7, 0.9)]
    assert voltages == sorted(voltages)
    # 线性插值中点。
    assert bess._ocv(0.3) == pytest.approx(750.0)


def test_discharge_decreases_soc_and_charge_increases_it(make_params, make_weather) -> None:
    bess = BESS(make_params("battery"))
    start = bess.energy
    for _ in range(40):
        output = _advance(bess, make_weather, {"active_power_setpoint": 200_000.0})
        assert output["active_power"] > 0
        assert output["battery_current"] > 0
    assert bess.energy < start
    # 反向充电。
    charged = bess.energy
    for _ in range(40):
        output = _advance(bess, make_weather, {"active_power_setpoint": -200_000.0})
        assert output["active_power"] < 0
        assert output["battery_current"] < 0
    assert bess.energy > charged


def test_soc_is_bounded_by_configured_interval(make_params, make_weather) -> None:
    # 固定 1s 步长跑完整容量区间：0.4×3.6e9 J / 500kW ≈ 2880s。
    bess = BESS(make_params("battery"))
    for _ in range(3200):
        bess.advance(1.0, make_weather(), {"active_power_setpoint": 500_000.0}, 690.0)
        soc = bess.energy / RATED_J
        assert 0.1 - 1e-7 <= soc <= 0.9 + 1e-7  # 每一步都不得越界
    assert bess.energy / RATED_J == pytest.approx(0.1, abs=1e-4)
    for _ in range(6400):
        bess.advance(1.0, make_weather(), {"active_power_setpoint": -500_000.0}, 690.0)
        soc = bess.energy / RATED_J
        assert 0.1 - 1e-7 <= soc <= 0.9 + 1e-7
    assert bess.energy / RATED_J == pytest.approx(0.9, abs=1e-4)


def test_terminal_voltage_and_resistive_loss_identity(make_params, make_weather) -> None:
    bess = BESS(make_params("battery"))
    output = _advance(bess, make_weather, {"active_power_setpoint": 300_000.0})
    ocv = bess._ocv(0.5)
    # 生产实现：terminal = OCV - R·I，loss = I²·R。
    assert output["battery_voltage"] == pytest.approx(
        ocv - 0.05 * output["battery_current"], rel=1e-9
    )
    assert output["battery_loss"] == pytest.approx(output["battery_current"] ** 2 * 0.05, rel=1e-9)
    assert output["battery_loss"] >= 0.0


def test_energy_bookkeeping_per_step_conservation(make_params, make_weather) -> None:
    """ΔE 必须严格等于 -dt·OCV(pre)·I（内阻耗能单列 loss，不重复扣能量）。"""
    bess = BESS(make_params("battery"))
    for demand in (250_000.0, -250_000.0, 400_000.0):
        for _ in range(50):
            before = bess.energy
            pre_soc = before / RATED_J
            output = _advance(bess, make_weather, {"active_power_setpoint": demand})
            expected = before - DT * bess._ocv(pre_soc) * output["battery_current"]
            assert bess.energy == pytest.approx(expected, rel=1e-9, abs=1.0)


def test_efficiency_asymmetry_between_charge_and_discharge(make_params, make_weather) -> None:
    # 放电：dc = ac/eta；充电：dc = ac·eta。同幅值交流功率下电流幅值不同。
    discharging = BESS(make_params("battery"))
    charging = BESS(make_params("battery"))
    out_d = _advance(discharging, make_weather, {"active_power_setpoint": 100_000.0})
    out_c = _advance(charging, make_weather, {"active_power_setpoint": -100_000.0})
    assert abs(out_d["battery_current"]) != pytest.approx(abs(out_c["battery_current"]))
    assert out_d["battery_current"] > 0 and out_c["battery_current"] < 0


def test_soc_target_strategy_deadband_holds_zero_power(make_params, make_weather) -> None:
    params = make_params("battery")
    params["control_strategy"] = "soc_target"
    bess = BESS(params)
    # 初始 SOC 0.5，目标 50%，deadband 0.5%：死区内请求必须为零。
    output = _advance(bess, make_weather, {"soc_target": 50.0})
    assert output["active_power"] == 0.0
    output = _advance(bess, make_weather, {"soc_target": 80.0})
    assert output["active_power"] < 0  # 目标更高 → 充电


def test_grid_disconnect_forces_zero_power(make_params, make_weather) -> None:
    bess = BESS(make_params("battery"))
    output = _advance(
        bess, make_weather, {"active_power_setpoint": 300_000.0, "_grid_connected": 0.0}
    )
    assert output["active_power"] == 0.0
    assert output["reactive_power"] == 0.0
    assert output["pcs_status"] == 0


def test_reactive_power_requires_capability_flag(make_params, make_weather) -> None:
    params = make_params("battery")
    params["reactive_support_enabled"] = False
    bess = BESS(params)
    output = _advance(bess, make_weather, {"reactive_power_setpoint": 100_000.0})
    assert output["reactive_power"] == 0.0


def test_deterministic_trajectories(make_params, make_weather) -> None:
    first = BESS(make_params("battery"))
    second = BESS(make_params("battery"))
    controls = {"active_power_setpoint": 180_000.0}
    seq_a = [_advance(first, make_weather, controls) for _ in range(120)]
    seq_b = [_advance(second, make_weather, controls) for _ in range(120)]
    assert seq_a == seq_b
    assert all(math.isfinite(value) for row in seq_a for value in row.values())


def test_soh_monotonic_degradation_and_temperature_finite(make_params, make_weather) -> None:
    params = make_params("battery")
    params["aging_coefficient_per_ampere_second"] = 1e-9
    bess = BESS(params)
    initial = bess.soh
    for _ in range(200):
        output = _advance(bess, make_weather, {"active_power_setpoint": 400_000.0})
    assert bess.soh < initial
    assert math.isfinite(output["battery_temperature"])
