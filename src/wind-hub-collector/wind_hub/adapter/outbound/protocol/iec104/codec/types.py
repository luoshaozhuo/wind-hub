"""IEC 60870-5-104 type identifiers and enumerations."""

from __future__ import annotations

from enum import IntEnum, IntFlag


class TypeID(IntEnum):
    """IEC 60870-5-104 ASDU type identifiers.

    Only the types relevant to wind-farm applications are included.
    """

    # --- monitor-direction: single-point information -----------------------
    M_SP_NA_1 = 1
    """单点信息 (Single-point information)"""

    M_SP_TB_1 = 30
    """单点信息 带 CP56Time2a 时标"""

    # --- monitor-direction: double-point information -----------------------
    M_DP_NA_1 = 3
    """双点信息 (Double-point information)"""

    M_DP_TB_1 = 31
    """双点信息 带 CP56Time2a 时标"""

    # --- monitor-direction: measured values --------------------------------
    M_ME_NA_1 = 9
    """归一化测量值 (Measured value, normalized value)"""

    M_ME_NB_1 = 11
    """标度化测量值 (Measured value, scaled value)"""

    M_ME_NC_1 = 13
    """短浮点测量值 (Measured value, short floating-point)"""

    M_ME_TD_1 = 34
    """归一化测量值 带 CP56Time2a 时标"""

    M_ME_TF_1 = 36
    """短浮点测量值 带 CP56Time2a 时标"""

    # --- control-direction: commands ---------------------------------------
    C_SC_NA_1 = 45
    """单点遥控 (Single command)"""

    C_DC_NA_1 = 46
    """双点遥控 (Double command)"""

    C_SE_NC_1 = 50
    """短浮点设点 (Set-point command, short floating-point)"""

    # --- control-direction: interrogation ----------------------------------
    C_IC_NA_1 = 100
    """站总召 / 组总召 (Interrogation command)"""

    C_CI_NA_1 = 103
    """电能总召 (Counter interrogation command)"""


class CauseOfTransmission(IntEnum):
    """IEC 60870-5-104 cause of transmission (传送原因).

    Only commonly used causes are enumerated.  The low 6 bits of the
    COT byte carry the cause value; bit 6 (value 64) indicates a
    test-mode flag and bit 7 (value 128) indicates a negative
    acknowledgement.
    """

    PERIODIC = 1
    """周期 / 循环 (periodic/cyclic)"""

    BACKGROUND = 2
    """背景扫描 (background scan)"""

    SPONTANEOUS = 3
    """自发 / 突发 (spontaneous)"""

    INITIALIZED = 4
    """初始化 (initialized)"""

    REQUEST = 5
    """被请求 / 被轮询 (request/requested)"""

    ACTIVATION = 6
    """激活 (activation)"""

    ACTIVATION_CON = 7
    """激活确认 (activation confirmation)"""

    DEACTIVATION = 8
    """停止激活 (deactivation)"""

    DEACTIVATION_CON = 9
    """停止激活确认 (deactivation confirmation)"""

    ACTIVATION_TERMINATION = 10
    """激活终止 (activation termination)"""

    INTERROGATED_BY_STATION = 20
    """响应站召唤 (interrogated by station)"""

    UNKNOWN_TYPE = 44
    """未知类型标识 (unknown type identifier)"""

    UNKNOWN_COT = 45
    """未知传送原因 (unknown cause of transmission)"""

    UNKNOWN_CA = 46
    """未知公共地址 (unknown common address)"""

    UNKNOWN_IOA = 47
    """未知信息对象地址 (unknown information object address)"""


class UFrameType(IntEnum):
    """IEC 60870-5-104 U-frame (unnumbered control) function codes.

    Encoded in the low 8 bits of the control field::

        STARTDT_ACT : 0000 0111 (0x07)
        STARTDT_CON : 0000 1011 (0x0B)
        STOPDT_ACT  : 0001 0011 (0x13)
        STOPDT_CON  : 0010 0011 (0x23)
        TESTFR_ACT  : 0100 0011 (0x43)
        TESTFR_CON  : 1000 0011 (0x83)
    """

    STARTDT_ACT = 0x07
    """数据传输激活 (STARTDT act)"""

    STARTDT_CON = 0x0B
    """数据传输激活确认 (STARTDT con)"""

    STOPDT_ACT = 0x13
    """数据传输停止 (STOPDT act)"""

    STOPDT_CON = 0x23
    """数据传输停止确认 (STOPDT con)"""

    TESTFR_ACT = 0x43
    """测试帧激活 (TESTFR act)"""

    TESTFR_CON = 0x83
    """测试帧确认 (TESTFR con)"""


class QualityFlag(IntFlag):
    """IEC 60870-5-104 quality descriptor (品质描述词) — 1 byte.

    Bit layout::

        bit 7   reserved (0)
        bit 6   reserved (0)
        bit 5   reserved (0)
        bit 4   IV — invalid (无效)
        bit 3   NT — not topical (非当前值)
        bit 2   SB — substituted (取代)
        bit 1   BL — blocked (闭锁)
        bit 0   OV — overflow (溢出)
    """

    OV = 0x01
    """溢出 (overflow)"""

    BL = 0x02
    """闭锁 (blocked)"""

    SB = 0x04
    """取代 (substituted)"""

    NT = 0x08
    """非当前 (not topical)"""

    IV = 0x10
    """无效 (invalid)"""
