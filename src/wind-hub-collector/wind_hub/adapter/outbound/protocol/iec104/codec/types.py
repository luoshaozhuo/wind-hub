"""IEC 60870-5-104 TypeID、COT、U-frame 与品质位枚举。"""

from __future__ import annotations

from enum import IntEnum, IntFlag


class TypeID(IntEnum):
    """本项目使用的 IEC104 ASDU TypeID。

    只枚举风电场采集、总召和遥控当前实际使用的类型。
    """

    # 监视方向：单点信息
    M_SP_NA_1 = 1
    """单点信息 (Single-point information)"""

    M_SP_TB_1 = 30
    """单点信息 带 CP56Time2a 时标"""

    # 监视方向：双点信息
    M_DP_NA_1 = 3
    """双点信息 (Double-point information)"""

    M_DP_TB_1 = 31
    """双点信息 带 CP56Time2a 时标"""

    # 监视方向：测量值
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

    # 控制方向：遥控/设点
    C_SC_NA_1 = 45
    """单点遥控 (Single command)"""

    C_DC_NA_1 = 46
    """双点遥控 (Double command)"""

    C_SE_NC_1 = 50
    """短浮点设点 (Set-point command, short floating-point)"""

    # 控制方向：召唤
    C_IC_NA_1 = 100
    """站总召 / 组总召 (Interrogation command)"""

    C_CI_NA_1 = 103
    """电能总召 (Counter interrogation command)"""


class CauseOfTransmission(IntEnum):
    """IEC104 Cause Of Transmission（传送原因）。

    COT byte 低 6 bit 为原因值；bit6 为 test，bit7 为 negative acknowledgement。
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
    """IEC104 U-frame 控制功能码。

    数值直接对应控制域低 8 bit 的标准编码。
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
    """IEC104 一字节品质描述词。

    bit4~bit0 分别表示 IV、NT、SB、BL、OV；高 3 bit 保留。
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
