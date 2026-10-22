"""DC-EMS 示例风机点表的逻辑契约；不包含 Modbus 地址。"""
from __future__ import annotations

READ_POINTS = frozenset({
    "emsWorkMode", "emsWinSpd", "emsWinSpdAvg10Sec", "emsWinSpdAvg60Sec",
    "emsBladeAngle1", "emsBladeAngle2", "emsBladeAngle3", "emsMinBladeAngle",
    "emsGenRotateSpd", "emsGenTorque", "emsPowAct", "emsPowActCvt",
    "emsPowActAvg10Sec", "emsPowActAvg60Sec", "emsPowReact", "emsPowReactCvt",
    "emsPowReact10", "emsPowReact60", "emsGridPhaseCurrentA", "emsConvertVol",
    "emsTotalEnergy", "emsOilTemp", "emsCabinTemp", "emsEnvTemp", "emsFaultState",
    "emsLocalOnOff", "emsAutoLimitState", "emsLocalLimitState", "emsRemoteLimitState",
    "emsLimitStop", "emsLocalActPowerEnableState", "emsReactCanCtrl",
    "emsAutoLimitSetFeedback", "emsActPowerMaxSetFeedback",
    "emsReactPowerMaxSetFeedback", "emsReactPowerSet", "emsActPowerSet",
    "emsActPowerRead", "emsReactPowerSetStatus", "inertiaLimitValue",
})
COMMAND_POINTS = frozenset({
    "ctrlReActPow", "ctrlActPow", "emsActPowerSetInertia",
    "emsReactPowerSetStatusWrite", "emsActPowerChangeRate", "emsActCtrlMode",
    "ctrlStart", "ctrlStop", "ctrlReset",
})


def validate_turbine_points(points: set[str]) -> None:
    missing = READ_POINTS - points
    if missing:
        raise ValueError(f"missing turbine read points: {sorted(missing)}")
