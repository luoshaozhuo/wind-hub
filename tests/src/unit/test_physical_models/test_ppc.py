"""厂级 PPC：AGC/AVC/FFR 分配、爬坡与容量限制、控制隔离。"""

from __future__ import annotations

import copy

import pytest

from data_generator.application.model.ppc import PPC, optional_voltage_control
from data_generator.domain.model import GeneratorError

pytestmark = [pytest.mark.unit]

DEVICES = ("WTG_SIM01", "PV_SIM01", "BESS_SIM01")
DT = 0.05


def _feedback(active=0.0, reactive=0.0, apparent=2_000_000.0):
    return {
        device: {
            "active_power": active,
            "reactive_power": reactive,
            "rated_apparent_power": apparent,
        }
        for device in DEVICES
    }


def _limits(low=-500_000.0, high=500_000.0):
    return {device: (low, high) for device in DEVICES}


def _grid(frequency=50.0, voltage=690.0):
    return {
        "grid_frequency": frequency,
        "frequency_nominal": 50.0,
        "grid_voltage": voltage,
    }


def test_shipped_parameters_construct(make_params) -> None:
    ppc = PPC(make_params("plant"))
    assert ppc.devices == tuple(sorted(DEVICES))
    assert set(ppc.weights) == set(DEVICES)


def test_participant_and_weight_validation(make_params) -> None:
    params = make_params("plant")
    params["participating_device_ids"] = []
    with pytest.raises(GeneratorError):
        PPC(params)
    params = make_params("plant")
    params["participating_device_ids"] = ["D1", "D1"]
    with pytest.raises(GeneratorError):
        PPC(params)
    params = make_params("plant")
    params["allocation_weights"] = {"WTG_SIM01": 1.0, "PV_SIM01": 1.0}  # 缺 BESS
    with pytest.raises(GeneratorError):
        PPC(params)
    params = make_params("plant")
    params["allocation_weights"] = {device: 0.0 for device in DEVICES}
    with pytest.raises(GeneratorError):
        PPC(params)


def test_avc_mode_and_gain_validation(make_params) -> None:
    params = make_params("plant")
    params["avc_mode"] = "bogus"
    with pytest.raises(GeneratorError):
        PPC(params)
    params = make_params("plant")
    params["avc_mode"] = "voltage"
    params["avc_voltage_gain_var_v"] = 0.0
    with pytest.raises(GeneratorError):
        PPC(params)


def test_missing_participant_feedback_rejected(make_params) -> None:
    ppc = PPC(make_params("plant"))
    previous = _feedback()
    del previous["BESS_SIM01"]
    with pytest.raises(GeneratorError):
        ppc.advance(DT, previous, _grid(), {}, _limits())


def test_agc_off_tracks_previous_plant_power(make_params) -> None:
    ppc = PPC(make_params("plant"))
    previous = _feedback(active=100_000.0)
    dispatch, output = ppc.advance(DT, previous, _grid(), {"agc_enable": 0}, _limits())
    # 无 AGC 时命令等于上一步厂级有功，三台均分且总和守恒。
    assert output["agc_allocated_power"] == pytest.approx(300_000.0)
    assert sum(item["active_power_setpoint"] for item in dispatch.values()) == pytest.approx(
        300_000.0
    )
    assert set(dispatch) == set(DEVICES)


def test_agc_setpoint_distributes_equally_and_respects_ramp(make_params) -> None:
    ppc = PPC(make_params("plant"))
    previous = _feedback(active=0.0)
    controls = {"agc_enable": 1, "agc_power_setpoint": 900_000.0}
    # 爬坡 50kW/s × 0.05s = 2500W/步，首步每台不得超过该增量。
    dispatch, _ = ppc.advance(DT, previous, _grid(), controls, _limits())
    for device in DEVICES:
        assert dispatch[device]["active_power_setpoint"] == pytest.approx(2500.0)
    # 多步后逼近指令，总分配永不超过指令（容量充足时无 unserved）。
    for _ in range(2000):
        previous = {
            device: {**previous[device], "active_power": dispatch[device]["active_power_setpoint"]}
            for device in DEVICES
        }
        dispatch, output = ppc.advance(DT, previous, _grid(), controls, _limits())
    assert output["agc_allocated_power"] == pytest.approx(900_000.0)
    assert output["unserved_power_command"] == pytest.approx(0.0, abs=1.0)


def test_allocation_clamped_to_device_limits_with_unserved_power(make_params) -> None:
    ppc = PPC(make_params("plant"))
    previous = _feedback(active=100_000.0)
    controls = {"agc_enable": 1, "agc_power_setpoint": 5_000_000.0}
    # 容量上限每台 500kW，厂级 1.5MW 封顶，超出部分计入 unserved。
    dispatch, output = ppc.advance(DT, previous, _grid(), controls, _limits())
    assert sum(item["active_power_setpoint"] for item in dispatch.values()) <= 1_500_000.0 + 1.0
    assert output["unserved_power_command"] > 0.0
    for device in DEVICES:
        low, high = _limits()[device]
        assert low <= dispatch[device]["active_power_setpoint"] <= high


def test_ffr_sign_deadband_and_enable_gate(make_params) -> None:
    ppc = PPC(make_params("plant"))
    previous = _feedback()
    base = {"agc_enable": 0, "ffr_enable": 1}
    # 死区 0.05Hz：死区内 FFR 为零。
    _, inside = ppc.advance(DT, previous, _grid(frequency=50.03), base, _limits())
    assert inside["ffr_power_command"] == 0.0
    assert inside["ffr_active"] == 0
    # 高频 → 下调功率（负指令）；低频 → 上调功率（正指令）。
    _, high = ppc.advance(DT, previous, _grid(frequency=50.2), base, _limits())
    assert high["ffr_power_command"] == pytest.approx(-300_000.0 * (0.2 - 0.05))
    _, low = ppc.advance(DT, previous, _grid(frequency=49.8), base, _limits())
    assert low["ffr_power_command"] == pytest.approx(300_000.0 * (0.2 - 0.05))
    # 使能关闭后 FFR 必须静默。
    _, off = ppc.advance(DT, previous, _grid(frequency=50.2), {**base, "ffr_enable": 0}, _limits())
    assert off["ffr_power_command"] == 0.0


def test_invalid_enable_flags_rejected(make_params) -> None:
    ppc = PPC(make_params("plant"))
    with pytest.raises(GeneratorError):
        ppc.advance(DT, _feedback(), _grid(), {"agc_enable": 2}, _limits())


def test_avc_reactive_allocation_proportional_to_capacity(make_params) -> None:
    ppc = PPC(make_params("plant"))
    previous = _feedback(active=0.0, apparent=1_000_000.0)
    controls = {"avc_enable": 1, "avc_reactive_setpoint": 900_000.0}
    dispatch, output = ppc.advance(DT, previous, _grid(), controls, _limits())
    allocations = [item["reactive_power_setpoint"] for item in dispatch.values()]
    # 等容量 → 等分配，总和等于钳位后的请求。
    assert allocations[0] == pytest.approx(allocations[1]) == pytest.approx(allocations[2])
    assert sum(allocations) == pytest.approx(900_000.0)
    assert output["avc_allocated_reactive_power"] == pytest.approx(900_000.0)


def test_avc_reactive_clamped_by_remaining_apparent_margin(make_params) -> None:
    ppc = PPC(make_params("plant"))
    # 每台有功 800kW、视在 1MW → 单台无功上限 600kvar，厂级 1.8Mvar。
    previous = _feedback(active=800_000.0, apparent=1_000_000.0)
    controls = {"avc_enable": 1, "avc_reactive_setpoint": 5_000_000.0}
    dispatch, _ = ppc.advance(
        DT, previous, _grid(), controls, _limits(low=-900_000.0, high=900_000.0)
    )
    assert sum(item["reactive_power_setpoint"] for item in dispatch.values()) == pytest.approx(
        1_800_000.0
    )


def test_voltage_avc_uses_gain_times_error(make_params) -> None:
    assert optional_voltage_control(
        {"avc_voltage_gain_var_v": 1000.0}, 700.0, 690.0
    ) == pytest.approx(10_000.0)
    params = make_params("plant")
    params["avc_mode"] = "voltage"
    params["avc_voltage_gain_var_v"] = 1000.0
    ppc = PPC(params)
    previous = _feedback()
    controls = {"avc_enable": 1, "avc_voltage_setpoint": 700.0}
    _, output = ppc.advance(DT, previous, _grid(voltage=690.0), controls, _limits())
    # 跟踪误差 = 跟踪请求 - 上一步无功（零）。
    assert output["avc_tracking_error"] == pytest.approx(10_000.0)


def test_dispatch_does_not_mutate_previous_feedback(make_params) -> None:
    ppc = PPC(make_params("plant"))
    previous = _feedback(active=120_000.0)
    snapshot = copy.deepcopy(previous)
    ppc.advance(
        DT,
        previous,
        _grid(),
        {"agc_enable": 1, "agc_power_setpoint": 600_000.0},
        _limits(),
    )
    assert previous == snapshot  # 一步延迟反馈输入保持不可变
