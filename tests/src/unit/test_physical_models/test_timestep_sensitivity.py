"""固定时间步长敏感性：积分收敛性、守恒量与非法步长拒绝，而不仅是有限性检查。"""

from __future__ import annotations

import math

import pytest

from data_generator.application.model.bess import BESS
from data_generator.application.model.pv import PV
from data_generator.application.model.turbine import Turbine
from data_generator.domain.model import GeneratorError

pytestmark = [pytest.mark.unit]


def test_bess_soc_trajectory_converges_when_timestep_halved(make_params, make_weather) -> None:
    horizon_s = 10.0
    coarse_dt, fine_dt = 0.05, 0.025
    coarse = BESS(make_params("battery"))
    fine = BESS(make_params("battery"))
    controls = {"active_power_setpoint": 250_000.0}
    for _ in range(int(horizon_s / coarse_dt)):
        coarse.advance(coarse_dt, make_weather(), controls, 690.0)
    for _ in range(int(horizon_s / fine_dt)):
        fine.advance(fine_dt, make_weather(), controls, 690.0)
    rated_j = 1_000_000.0 * 3600.0
    soc_coarse = 100 * coarse.energy / rated_j
    soc_fine = 100 * fine.energy / rated_j
    # 同一 10s 放电历程：两种步长的 SOC 差异必须小于 0.05 个百分点。
    assert abs(soc_coarse - soc_fine) < 0.05
    assert soc_coarse < 50.0 and soc_fine < 50.0  # 方向正确：都在放电


def test_bess_energy_change_scales_linearly_with_timestep(make_params, make_weather) -> None:
    """恒功率放电下 ΔE 与 dt 成正比（显式欧拉积分的尺度守恒）。"""
    base = BESS(make_params("battery"))
    start = base.energy
    base.advance(0.05, make_weather(), {"active_power_setpoint": 300_000.0}, 690.0)
    delta_short = start - base.energy

    longer = BESS(make_params("battery"))
    start = longer.energy
    longer.advance(0.1, make_weather(), {"active_power_setpoint": 300_000.0}, 690.0)
    delta_long = start - longer.energy
    assert delta_long == pytest.approx(2 * delta_short, rel=1e-6)


def test_pv_thermal_response_matches_analytic_solution_at_both_timesteps(
    make_params, make_weather
) -> None:
    # 生产 response() 为精确指数离散：T(t)=T*+(T0-T*)e^(-t/τ)，
    # 任意步长在整步时刻都应与解析解吻合（误差仅来自浮点）。
    params = make_params("solar")
    tau = params["thermal_tau_s"]
    irradiance = 1000.0
    ambient = 25.0
    target = ambient + (params["noct_c"] - 20) * irradiance / 800.0
    t0 = params["stc_cell_temperature_c"]
    horizon_s = 45.0

    def run(dt: float) -> float:
        pv = PV(make_params("solar"))
        for _ in range(int(horizon_s / dt)):
            pv.advance(
                dt,
                make_weather(irradiance=irradiance, ambient_temperature=ambient),
                {},
                690.0,
            )
        return pv.cell_temperature

    analytic = target + (t0 - target) * math.exp(-horizon_s / tau)
    assert run(0.1) == pytest.approx(analytic, abs=1e-6)
    assert run(0.025) == pytest.approx(analytic, abs=1e-6)


def test_turbine_rotor_speed_is_stable_under_timestep_refinement(make_params, make_weather) -> None:
    horizon_s = 5.0
    coarse = Turbine(make_params("turbine"))
    fine = Turbine(make_params("turbine"))
    for _ in range(int(horizon_s / 0.05)):
        coarse.advance(0.05, make_weather(wind_speed=10.0), {}, 690.0)
    for _ in range(int(horizon_s / 0.025)):
        fine.advance(0.025, make_weather(wind_speed=10.0), {}, 690.0)
    # 显式欧拉转速积分对步长不敏感（5% 相对带），且两种步长都不触发超速保护。
    assert coarse.fault == 0 and fine.fault == 0
    assert abs(coarse.omega - fine.omega) / fine.omega < 0.05
    assert 0 < coarse.omega <= 2.0 and 0 < fine.omega <= 2.0


def test_turbine_and_pv_reject_nonpositive_timestep(make_params, make_weather) -> None:
    weather = make_weather()
    for bad_dt in (0.0, -0.05):
        with pytest.raises(GeneratorError):
            Turbine(make_params("turbine")).advance(bad_dt, weather, {}, 690.0)
        with pytest.raises(GeneratorError):
            PV(make_params("solar")).advance(bad_dt, weather, {}, 690.0)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT S2-DT-1（待 ChatGPT 修复生产代码）：BESS.advance 对 dt<=0 无校验，"
        "负步长会静默反向积分能量账本；Turbine/PV 经 common.response 守卫已拒绝。"
    ),
)
def test_bess_rejects_nonpositive_timestep(make_params, make_weather) -> None:
    bess = BESS(make_params("battery"))
    with pytest.raises(GeneratorError):
        bess.advance(0.0, make_weather(), {"active_power_setpoint": 100_000.0}, 690.0)
