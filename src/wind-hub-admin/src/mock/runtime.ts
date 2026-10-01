// Mock State 层（§1/§2.1）：固定故障场景注册表 + 派生运行时状态 +
// 确定性点值/趋势序列 + 全局日志存储 + Quality/Health 计数器。
//
// 单一事实源：deviceScenarios 一处定义，Devices / Tasks / Quality /
// Diagnostics / Logs / Overview 全部经由这里的查询函数派生，
// 页面不允许再写 if (device_id === 'wtg-xxx') 的场景判断。
import { reactive } from 'vue'
import { formatTimestamp } from '../utils/format'

// ---------------------------------------------------------------------------
// 固定故障场景注册表（§3 / §25）
// ---------------------------------------------------------------------------
export interface DeviceScenario {
  // network: unreachable = TCP/ICMP 不可达（wtg-041）
  network: 'ok' | 'unreachable'
  // protocolError: 非空表示协议层会话建立失败（wtg-043 ADS）
  protocolError: { code: string; message: string } | null
  // failingPointIndexes: 点表内确定性读失败的点下标（wtg-026 → [0]）
  failingPointIndexes: number[]
  // commandRejected: 设备写被拒绝（wtg-044）
  commandRejected: boolean
  // degradation: 通信退化（latency = 高延迟 / missing = 丢周期），驱动 Quality Timeliness / Completeness
  degradation: 'latency' | 'missing' | null
}

export const deviceScenarios: Record<string, DeviceScenario> = {
  // 场景 B：网络不可达 —— Verify Network Failed / Ping Failed / Task Start Failure /
  // Quality Continuity + Timeliness issue / Logs network error
  'wtg-041': {
    network: 'unreachable',
    protocolError: null,
    failingPointIndexes: [],
    commandRejected: false,
    degradation: null,
  },
  // 场景 C：Ping 正常但 ADS 协议会话失败
  'wtg-043': {
    network: 'ok',
    protocolError: { code: 'ADS_SESSION_UNAVAILABLE', message: 'ADS session unavailable: route OK, port 801 not answering' },
    failingPointIndexes: [],
    commandRejected: false,
    degradation: null,
  },
  // 场景 D：单点读取失败（点表第 1 个点），Completeness degraded
  'wtg-026': {
    network: 'ok',
    protocolError: null,
    failingPointIndexes: [0],
    commandRejected: false,
    degradation: null,
  },
  // 场景 E：设备写被拒绝（读取与验证正常）
  'wtg-044': {
    network: 'ok',
    protocolError: null,
    failingPointIndexes: [],
    commandRejected: true,
    degradation: null,
  },
  // Quality Timeliness 退化样例：持续高读取延迟
  'wtg-017': {
    network: 'ok',
    protocolError: null,
    failingPointIndexes: [],
    commandRejected: false,
    degradation: 'latency',
  },
  // Quality Completeness 退化样例：间歇性采集周期缺失
  'wtg-003': {
    network: 'ok',
    protocolError: null,
    failingPointIndexes: [],
    commandRejected: false,
    degradation: 'missing',
  },
}

export interface DeviceRuntimeState {
  network: 'ok' | 'unreachable' | 'disabled'
  protocolError: { code: string; message: string } | null
  failingPointIndexes: number[]
  commandRejected: boolean
  degradation: 'latency' | 'missing' | null
}

const NORMAL_RUNTIME: DeviceRuntimeState = {
  network: 'ok',
  protocolError: null,
  failingPointIndexes: [],
  commandRejected: false,
  degradation: null,
}

// 设备运行时状态：场景注册表 × enabled 的合成结果，全站唯一入口。
export function deviceRuntimeState(deviceId: string, enabled = true): DeviceRuntimeState {
  const scenario = deviceScenarios[deviceId]
  if (!enabled) return { ...NORMAL_RUNTIME, network: 'disabled' }
  if (!scenario) return NORMAL_RUNTIME
  return { ...scenario, failingPointIndexes: [...scenario.failingPointIndexes] }
}

// Sink 固定故障场景（§3）：kafka_main broker timeout、db_main connection refused
export const sinkScenarios: Record<string, { failingCheck: string; code: string; message: string }> = {
  kafka_main: { failingCheck: 'Broker Session', code: 'SINK_BROKER_TIMEOUT', message: 'Broker session timed out after 3000 ms' },
  db_main: { failingCheck: 'TCP Port', code: 'SINK_CONNECTION_REFUSED', message: 'TCP connection refused by remote host' },
}

// ---------------------------------------------------------------------------
// 确定性哈希与点值序列（§5/§7）
// ---------------------------------------------------------------------------
export function hash32(text: string): number {
  let hash = 2166136261
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

// Command 成功后写入的覆盖值：Data / Trend / Diagnostics 立即读到新值（§6）。
export interface CommandOverride {
  value: number | boolean
  atEpochSec: number
}
export const commandOverrides = reactive<Record<string, Record<string, CommandOverride>>>({})

export function setCommandOverride(deviceId: string, pointId: string, value: number | boolean) {
  if (!commandOverrides[deviceId]) commandOverrides[deviceId] = {}
  commandOverrides[deviceId][pointId] = { value, atEpochSec: Math.floor(Date.now() / 1000) }
}

function baseValue(deviceId: string, pointIndex: number, epochSec: number): number {
  const h = hash32(`${deviceId}#${pointIndex}`)
  const base = 20 + (h % 50)
  const drift = Math.sin(epochSec / 7 + pointIndex * 1.3) * (3 + (h % 6))
    + Math.sin(epochSec / 19 + pointIndex) * 0.9
  return Number((base + drift).toFixed(3))
}

// 数值点在某个时刻的确定性值：Data 当前值、Trend 尾点、Diagnostics Read 全部用它，
// 因此三者天然一致（§7 当前值 ≈ 趋势尾点）。
export function pointValueAt(deviceId: string, pointId: string, pointIndex: number, epochSec: number): number {
  const override = commandOverrides[deviceId]?.[pointId]
  if (override && typeof override.value === 'number') {
    const settle = epochSec - override.atEpochSec
    if (settle >= 0) {
      // 覆盖后围绕新值小幅漂移，趋势在 Command 后可见跳变
      return Number((override.value + Math.sin(settle / 9 + pointIndex) * 0.4).toFixed(3))
    }
  }
  return baseValue(deviceId, pointIndex, epochSec)
}

export function pointBoolAt(deviceId: string, pointId: string, pointIndex: number, epochSec: number): boolean {
  const override = commandOverrides[deviceId]?.[pointId]
  if (override && typeof override.value === 'boolean' && epochSec >= override.atEpochSec) {
    return override.value
  }
  const h = hash32(`${deviceId}#${pointIndex}`)
  return (h + Math.floor(epochSec / 5)) % 2 === 0
}

// Data 页刷新计数（决定 updated_at 展示与“第 N 次刷新”语义），Auto Refresh 同步递增。
export const deviceDataTick = reactive<Record<string, number>>({})

export function bumpDeviceDataTick(deviceId: string) {
  deviceDataTick[deviceId] = (deviceDataTick[deviceId] || 0) + 1
}

// ---------------------------------------------------------------------------
// 全局 Mock 日志存储（§24）
// ---------------------------------------------------------------------------
export interface MockLogEntry {
  time: string
  level: 'ERROR' | 'WARN' | 'INFO'
  source: string
  object: string
  message: string
}

const LOG_CAP = 500
export const logStore = reactive<MockLogEntry[]>([])

export function appendLog(level: MockLogEntry['level'], source: string, object: string, message: string) {
  logStore.unshift({ time: formatTimestamp(new Date()), level, source, object, message })
  if (logStore.length > LOG_CAP) logStore.length = LOG_CAP
}

// 启动种子日志：确定性的历史内容，时间为相对当前时刻的固定偏移（无随机数）。
function seedLogs() {
  const samples: Array<[MockLogEntry['level'], string, string, string]> = [
    ['INFO', 'ads', 'wtg-040', 'connected (AMS route OK)'],
    ['INFO', 'modbus', 'wtg-002', 'connected 192.168.100.102:502'],
    ['WARN', 'task', 'turbine-ads-all', 'interval overrun 42 ms'],
    ['ERROR', 'modbus', 'wtg-003', 'read timeout after 5000 ms'],
    ['ERROR', 'ads', 'wtg-041', 'disconnected, retrying'],
    ['INFO', 'runtime', 'engine', 'config reloaded (3 tasks updated)'],
    ['WARN', 'sink', 'file_archive', 'queue usage 82%'],
    ['INFO', 'task', 'pcs-fast', 'started'],
    ['ERROR', 'sink', 'db_main', 'write failed: connection refused'],
    ['INFO', 'ads', 'wtg-041', 'reconnect failed, backoff 8 s'],
    ['INFO', 'task', 'turbine-modbus-all', 'cycle completed · 0 errors'],
    ['WARN', 'ads', 'wtg-017', 'read latency 412 ms above threshold'],
    ['INFO', 'task', 'turbine-ads-all', 'point batch read completed'],
    ['ERROR', 'sink', 'db_main', 'dropped 6 points after 3 retries'],
  ]
  // 最新在前（与 appendLog 的 unshift 一致），140 条保证分页规模（§28）
  for (let i = 139; i >= 0; i--) {
    const [level, source, object, message] = samples[i % samples.length]
    const time = new Date(Date.now() - (i * 47 + 11) * 1000)
    logStore.push({ time: formatTimestamp(time), level, source, object, message })
  }
}
seedLogs()

// ---------------------------------------------------------------------------
// Quality / Health 计数器（§16 Auto Check、§23）
// ---------------------------------------------------------------------------
// Auto Check 每次执行 +1，Quality 各窗口统计在此基础上确定性重算（无随机数）。
export const qualityCheckTick = reactive({ value: 0 })
