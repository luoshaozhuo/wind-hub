// 设备领域纯函数：model 解析、连接参数合成、覆盖差异计算。
import type { DeviceInst, DeviceModelDef, DeviceVerification, UnitDef } from './types'

export interface DevicesSnapshot {
  deviceModels: DeviceModelDef[]
  devices: DeviceInst[]
  units: Record<string, UnitDef>
}

/** 目标网络标识等“身份类”扩展键不参与恢复默认。 */
export const DEVICE_IDENTITY_EXTENSION_KEYS = new Set(['target_net_id'])

export function emptyVerification(): DeviceVerification {
  return {
    state: 'idle',
    network: 'unknown',
    protocol: 'unknown',
    points: 'unknown',
    point_total: 0,
    point_success: 0,
    point_failed: 0,
    verified_at: '',
    latency_ms: 0,
    errors: [],
  }
}

export function modelOf(
  snapshot: Pick<DevicesSnapshot, 'deviceModels'>,
  d: DeviceInst,
): DeviceModelDef | undefined {
  return snapshot.deviceModels.find((m) => m.id === d.model)
}

export function protocolOfDevice(snapshot: DevicesSnapshot, d: DeviceInst): string {
  return modelOf(snapshot, d)?.protocol || ''
}

export function tableOfDevice(snapshot: DevicesSnapshot, d: DeviceInst): string {
  return modelOf(snapshot, d)?.point_table || ''
}

export function unitSymbol(snapshot: DevicesSnapshot, unitId: string): string {
  return snapshot.units[unitId]?.symbol ?? unitId
}

/** model connection_defaults + 设备覆盖 + 端口覆盖的合成结果。 */
export function effectiveConnection(
  snapshot: DevicesSnapshot,
  d: DeviceInst,
): Record<string, unknown> {
  const model = modelOf(snapshot, d)
  return {
    ...(model?.connection_defaults || {}),
    ...(d.extensions || {}),
    port: d.port ?? model?.connection_defaults?.port,
  }
}

/** 相对 model 默认值真正有差异的覆盖项（身份类键始终保留）。 */
export function deviceConnectionOverrides(
  snapshot: DevicesSnapshot,
  d: DeviceInst,
): Record<string, unknown> {
  const model = modelOf(snapshot, d)
  const defaults = model?.connection_defaults || {}
  const result: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(d.extensions || {})) {
    if (DEVICE_IDENTITY_EXTENSION_KEYS.has(key) || defaults[key] !== value) result[key] = value
  }
  return result
}

/** 就地清除设备连接覆盖（保留身份类键），返回清除项数；调用方需持有可变草稿。 */
export function resetDeviceConnectionOverrides(d: DeviceInst): number {
  const removable = Object.keys(d.extensions || {}).filter(
    (key) => !DEVICE_IDENTITY_EXTENSION_KEYS.has(key),
  )
  d.extensions = Object.fromEntries(
    Object.entries(d.extensions || {}).filter(([key]) => DEVICE_IDENTITY_EXTENSION_KEYS.has(key)),
  )
  const hadPortOverride = d.port !== undefined
  d.port = undefined
  return removable.length + (hadPortOverride ? 1 : 0)
}
