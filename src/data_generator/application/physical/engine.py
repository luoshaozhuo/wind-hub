"""Deterministic coordinator; device equations stay in separate modules."""

from __future__ import annotations

from collections.abc import Mapping

from data_generator.application.physical.bess import BESS
from data_generator.application.physical.common import as_mapping, finite_outputs, number
from data_generator.application.physical.grid import Grid
from data_generator.application.physical.ppc import PPC
from data_generator.application.physical.pv import PV
from data_generator.application.physical.turbine import Turbine
from data_generator.application.physical.weather import Weather, WeatherModel
from data_generator.domain.model import GeneratorError, Scalar, validate_scalar
from data_generator.domain.scenario import Scenario


class PhysicalEngine:
    """One engine per Generation; instances and random state never cross generations."""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.weather = {
            name: WeatherModel(name, as_mapping(raw, f"weather.{name}"))
            for name, raw in scenario.weather.items()
        }
        self.models: dict[str, Turbine | PV | BESS | Grid | PPC] = {}
        self.kinds: dict[str, str] = {}
        self.params: dict[str, Mapping[str, object]] = {}
        self.weather_refs: dict[str, str] = {}
        self.controller_modes: dict[str, str] = {}
        self.inputs: dict[str, dict[str, float]] = {}
        self.last: dict[str, dict[str, float | int]] = {}
        self._ppc_ids: list[str] = []
        self._grid_ids: list[str] = []
        factories = {"wtg": Turbine, "pv": PV, "bess": BESS, "grid": Grid, "ppc": PPC}
        for device in sorted(scenario.devices):
            binding = scenario.bindings.get(device)
            if not isinstance(binding, Mapping):
                raise GeneratorError(f"physical scenario device binding absent: {device}")
            model_ref = binding.get("model_ref")
            raw = scenario.models.get(str(model_ref))
            if raw is None:
                raise GeneratorError(f"physical model reference missing: {model_ref}")
            definition = as_mapping(raw, f"models.{model_ref}")
            if definition.get("kind") == "random":
                continue
            if definition.get("kind") != "physical":
                raise GeneratorError("unsupported configured model kind")
            kind = str(definition.get("device_kind"))
            factory = factories.get(kind)
            if factory is None:
                raise GeneratorError(f"unsupported physical model kind: {kind}")
            params = as_mapping(definition.get("parameters"), f"models.{model_ref}.parameters")
            instance = factory(params)
            self.models[device] = instance
            self.kinds[device] = kind
            self.params[device] = params
            self.inputs[device] = {}
            self.controller_modes[device] = str(definition.get("controller_mode", "local"))
            if self.controller_modes[device] not in {"local", "ppc"}:
                raise GeneratorError("invalid model control ownership")
            if kind in {"wtg", "pv", "bess"}:
                weather_ref = definition.get("weather_ref")
                if not isinstance(weather_ref, str) or weather_ref not in self.weather:
                    raise GeneratorError(f"missing physical weather reference: {device}")
                self.weather_refs[device] = weather_ref
            elif kind == "grid":
                self._grid_ids.append(device)
            else:
                self._ppc_ids.append(device)
            self.last[device] = {
                "active_power": 0.0,
                "reactive_power": 0.0,
                "rated_apparent_power": (
                    float(params.get("rated_apparent_power_va", 0)) if kind == "wtg"
                    else float(params.get("inverter_rated_apparent_power_va", 0)) if kind == "pv"
                    else float(params.get("pcs_rated_apparent_power_va", 0)) if kind == "bess"
                    else 0.0
                ),
            }
        if len(self._grid_ids) > 1 or len(self._ppc_ids) > 1:
            raise GeneratorError("one grid equivalent and one PPC per physical scenario")
        for ppc_id in self._ppc_ids:
            ppc = self.models[ppc_id]
            if not isinstance(ppc, PPC) or any(
                self.kinds.get(device_id) not in {"wtg", "pv", "bess"}
                for device_id in ppc.devices
            ):
                raise GeneratorError("PPC participants must be physical generation or storage devices")
        self.last_grid: dict[str, float | int] = {
            "grid_frequency": 50.0,
            "grid_voltage": 690.0,
            "frequency_nominal": 50.0,
        }
        if self._grid_ids:
            grid = self.params[self._grid_ids[0]]
            self.last_grid = {
                "grid_frequency": number(grid, "nominal_frequency_hz"),
                "grid_voltage": number(grid, "nominal_voltage_v"),
                "frequency_nominal": number(grid, "nominal_frequency_hz"),
            }

    def set_inputs(self, device: str, values: Mapping[str, float]) -> None:
        """Stage-3 RPC may call this only after authorization and atomic validation."""
        if device not in self.models:
            raise GeneratorError("unknown physical device")
        for key, value in values.items():
            if type(value) not in (int, float):
                raise GeneratorError("model input must be numeric")
            validate_scalar(float(value), "float64")
            self.inputs[device][key] = float(value)

    def _device_capacity(self, device: str) -> tuple[float, float]:
        p = self.params[device]
        kind = self.kinds[device]
        if kind == "wtg":
            return 0.0, number(p, "rated_power_w")
        if kind == "pv":
            return 0.0, number(p, "inverter_rated_power_w")
        return -number(p, "pcs_rated_active_power_w"), number(p, "pcs_rated_active_power_w")

    def advance(self, dt: float, step: int) -> dict[tuple[str, str], Scalar]:
        """Weather once, PPC from previous feedback, devices, then present-step grid."""
        if dt <= 0 or step < 1:
            raise GeneratorError("invalid physical step")
        climates: dict[str, Weather] = {
            name: model.advance(dt, step * dt) for name, model in sorted(self.weather.items())
        }
        dispatch: dict[str, dict[str, float]] = {}
        results: dict[str, dict[str, float | int]] = {}
        for device in self._ppc_ids:
            ppc = self.models[device]
            if not isinstance(ppc, PPC):
                raise GeneratorError("invalid PPC model instance")
            limits = {name: self._device_capacity(name) for name in ppc.devices}
            dispatch, results[device] = ppc.advance(
                dt, self.last, self.last_grid, self.inputs[device], limits
            )
        grid_online = all(
            int(self.inputs[device].get("grid_connected", 1)) == 1
            for device in self._grid_ids
        )
        voltage = float(self.last_grid.get("grid_voltage", 690.0))
        if voltage <= 0:
            voltage = 690.0
        for device in sorted(self.models):
            kind = self.kinds[device]
            if kind not in {"wtg", "pv", "bess"}:
                continue
            instance = self.models[device]
            controls = dict(self.inputs[device])
            controls["_grid_connected"] = float(grid_online)
            if self.controller_modes[device] == "ppc":
                if device not in dispatch:
                    raise GeneratorError("PPC control configured but no assignment was delivered")
                controls.update(dispatch[device])
                if kind == "wtg":
                    controls["control_mode"] = 1.0
            climate = climates[self.weather_refs[device]]
            if isinstance(instance, (Turbine, PV, BESS)):
                results[device] = instance.advance(dt, climate, controls, voltage)
        participants = [results[d] for d in sorted(results) if self.kinds[d] in {"wtg", "pv", "bess"}]
        for device in self._grid_ids:
            grid = self.models[device]
            if not isinstance(grid, Grid):
                raise GeneratorError("invalid grid model instance")
            results[device] = grid.advance(dt, participants, self.inputs[device])
            self.last_grid = {
                **results[device], "frequency_nominal": number(self.params[device], "nominal_frequency_hz")
            }
        for device, result in results.items():
            self.last[device] = {**self.last.get(device, {}), **result}
        final: dict[tuple[str, str], Scalar] = {}
        for (device, point), metadata in self.scenario.points.items():
            if device not in self.models:
                continue
            output = results.get(device, {})
            if point not in output:
                raise GeneratorError(f"physical model does not provide output: {device}.{point}")
            kind = str(metadata["data_type"])
            value = output[point]
            final[(device, point)] = validate_scalar(value, kind)
        return final
