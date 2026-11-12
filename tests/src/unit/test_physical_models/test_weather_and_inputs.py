"""WeatherModel 随机过程、外部输入与确定性契约。"""

from __future__ import annotations

import math

import pytest

from data_generator.application.model.inputs import (
    WEATHER_INPUTS,
    InputSpec,
    validate_input,
    validate_weather_input,
    writable_inputs,
)
from data_generator.application.model.weather import WeatherModel
from data_generator.domain.model import GeneratorError

pytestmark = [pytest.mark.unit]


def _weather_model(make_weather_config, **overrides):
    config = make_weather_config()
    config["wind"].update(overrides)
    return WeatherModel("site-a", config)


def test_zero_turbulence_pins_wind_speed_to_mean(make_weather_config) -> None:
    model = _weather_model(make_weather_config, turbulence_intensity=0.0)
    speeds = [model.advance(0.05, step * 0.05).wind_speed for step in range(1, 120)]
    assert speeds
    assert all(speed == pytest.approx(10.0) for speed in speeds)


def test_lognormal_wind_series_is_finite_and_nonnegative(make_weather_config) -> None:
    model = _weather_model(make_weather_config)
    weather = [model.advance(0.05, step * 0.05) for step in range(1, 600)]
    assert all(math.isfinite(item.wind_speed) and item.wind_speed >= 0 for item in weather)
    # 湍流强度 0.12 的长序列均值围绕 10 m/s（对数正态校正后），允许采样窗口偏差。
    mean = sum(item.wind_speed for item in weather) / len(weather)
    assert 8.0 < mean < 12.5
    assert all(item.air_density == pytest.approx(1.225) for item in weather)


def test_same_seed_produces_bit_identical_weather(make_weather_config) -> None:
    first = _weather_model(make_weather_config)
    second = _weather_model(make_weather_config)
    sequence_a = [first.advance(0.05, step * 0.05) for step in range(1, 200)]
    sequence_b = [second.advance(0.05, step * 0.05) for step in range(1, 200)]
    assert sequence_a == sequence_b


def test_different_weather_id_with_same_seed_diverges(make_weather_config) -> None:
    config = make_weather_config()
    first = WeatherModel("site-a", config)
    other = WeatherModel("site-b", config)
    assert first.advance(0.05, 0.05) != other.advance(0.05, 0.05)


def test_irradiance_is_zero_at_night_and_positive_at_noon(make_weather_config) -> None:
    model = _weather_model(make_weather_config)
    # 日出 06:00=21600s，日落 18:00=64800s；凌晨三点辐照必须为零。
    night = model.advance(0.05, 3 * 3600)
    assert night.irradiance == 0.0
    noon = model.advance(0.05, 12 * 3600)
    assert noon.irradiance > 0.0
    # 恒定云因子 0.8：正午辐照 = 晴空 × 0.8（日正弦小于 1，严格上界）。
    assert noon.irradiance < 1000.0


def test_invalid_timestep_and_clock_rejected(make_weather_config) -> None:
    model = _weather_model(make_weather_config)
    with pytest.raises(GeneratorError):
        model.advance(0.0, 1.0)
    with pytest.raises(GeneratorError):
        model.advance(0.05, -1.0)


def test_wind_config_rejects_zero_correlation_and_density(make_weather_config) -> None:
    with pytest.raises(GeneratorError):
        _weather_model(make_weather_config, wind_correlation_time_s=0.0)
    with pytest.raises(GeneratorError):
        _weather_model(make_weather_config, air_density=0.0)


def test_cloud_factor_above_one_and_bad_mode_rejected(make_weather_config) -> None:
    config = make_weather_config()
    config["irradiance"]["cloud_factor_target"] = 1.2
    with pytest.raises(GeneratorError):
        WeatherModel("site-a", config)
    config = make_weather_config()
    config["irradiance"]["cloud_mode"] = "bogus"
    with pytest.raises(GeneratorError):
        WeatherModel("site-a", config)


def test_seed_must_be_integer(make_weather_config) -> None:
    config = make_weather_config()
    config["wind"]["wind_seed"] = 1.5
    with pytest.raises(GeneratorError):
        WeatherModel("site-a", config)


def test_writable_inputs_cover_five_kinds_and_unknown_is_empty() -> None:
    assert set(writable_inputs("wtg")) == {
        "control_mode",
        "active_power_setpoint",
        "power_limit_setpoint",
        "reactive_power_setpoint",
    }
    for kind in ("pv", "bess", "grid", "ppc"):
        assert writable_inputs(kind)
    assert writable_inputs("unknown") == {}


def test_validate_input_enforces_type_and_bounds() -> None:
    spec = InputSpec("int16", "none", 0, 2)
    assert validate_input(2, spec) == 2
    with pytest.raises(GeneratorError):
        validate_input(3, spec)
    with pytest.raises(GeneratorError):
        validate_input(1.5, spec)  # 整型点拒绝浮点
    with pytest.raises(GeneratorError):
        validate_input(True, spec)  # bool 不得伪装 int


def test_validate_weather_input_scales_and_rejects_out_of_range() -> None:
    spec = WEATHER_INPUTS["turbulence_intensity"]
    assert validate_weather_input(0.5, spec) == 0.5
    with pytest.raises(GeneratorError):
        validate_weather_input(3.0, spec)
