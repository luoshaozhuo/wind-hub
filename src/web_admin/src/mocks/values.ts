// 确定性点值序列：hash + sin/cos + 固定基值 + 时间函数，禁止 Math.random()。
// Data 当前值、Trend 尾点、Diagnostics Read 全部经 pointValueAt/pointBoolAt
// 派生，因此三者天然一致；Command 成功后写入 override，三者同时跳变。

export function hash32(text: string): number {
  let hash = 2166136261
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

export function epochSecNow(): number {
  return Math.floor(Date.now() / 1000)
}

// Command 成功后写入的覆盖值：Data / Trend / Diagnostics 立即读到新值。
export interface CommandOverride {
  value: number | boolean
  atEpochSec: number
}

const commandOverrides = new Map<string, Map<string, CommandOverride>>()

export function setCommandOverride(
  deviceId: string,
  pointId: string,
  value: number | boolean,
): void {
  let perDevice = commandOverrides.get(deviceId)
  if (!perDevice) {
    perDevice = new Map()
    commandOverrides.set(deviceId, perDevice)
  }
  perDevice.set(pointId, { value, atEpochSec: epochSecNow() })
}

export function clearCommandOverrides(): void {
  commandOverrides.clear()
}

function baseValue(deviceId: string, pointIndex: number, epochSec: number): number {
  const h = hash32(`${deviceId}#${pointIndex}`)
  const base = 20 + (h % 50)
  const drift =
    Math.sin(epochSec / 7 + pointIndex * 1.3) * (3 + (h % 6)) +
    Math.sin(epochSec / 19 + pointIndex) * 0.9
  return Number((base + drift).toFixed(3))
}

/** 数值点在某个时刻的确定性值：同设备/点/时刻返回稳定值，随时间变化。 */
export function pointValueAt(
  deviceId: string,
  pointId: string,
  pointIndex: number,
  epochSec: number,
): number {
  const override = commandOverrides.get(deviceId)?.get(pointId)
  if (override && typeof override.value === 'number') {
    const settle = epochSec - override.atEpochSec
    if (settle >= 0) {
      // 覆盖后围绕新值小幅漂移，趋势在 Command 后可见跳变
      return Number((override.value + Math.sin(settle / 9 + pointIndex) * 0.4).toFixed(3))
    }
  }
  return baseValue(deviceId, pointIndex, epochSec)
}

/** Boolean / 状态点：确定性翻转，支持 command override。 */
export function pointBoolAt(
  deviceId: string,
  pointId: string,
  pointIndex: number,
  epochSec: number,
): boolean {
  const override = commandOverrides.get(deviceId)?.get(pointId)
  if (override && typeof override.value === 'boolean' && epochSec >= override.atEpochSec) {
    return override.value
  }
  const h = hash32(`${deviceId}#${pointIndex}`)
  return (h + Math.floor(epochSec / 5)) % 2 === 0
}
