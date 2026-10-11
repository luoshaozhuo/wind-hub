// Mock 单一事实源：固定故障场景注册表 + 派生运行时状态。
// Devices / Tasks / Overview / Quality / Diagnostics / Logs 全部经由这里的
// 查询函数派生；handler 与 service 不允许再写 if (device_id === 'wtg-xxx')。
// 故障设备固定、确定性、可复现（禁止随机数）。

export interface DeviceScenario {
  // network: unreachable = TCP/ICMP 不可达（wtg-041）
  network: 'ok' | 'unreachable'
  // protocolError: 协议层会话建立失败（wtg-043 ADS session unavailable）
  protocolError: { code: string; message: string } | null
  // failingPointIndexes: 点表内确定性读失败的点下标（wtg-026 → [0]）
  failingPointIndexes: number[]
  // commandRejected: 设备写被拒绝（wtg-044）
  commandRejected: boolean
  // degradation: latency = 持续高延迟（wtg-017）/ missing = 丢采集周期（wtg-003）
  degradation: 'latency' | 'missing' | null
}

export const deviceScenarios: Record<string, DeviceScenario> = {
  'wtg-041': {
    network: 'unreachable',
    protocolError: null,
    failingPointIndexes: [],
    commandRejected: false,
    degradation: null,
  },
  'wtg-043': {
    network: 'ok',
    protocolError: {
      code: 'ADS_SESSION_UNAVAILABLE',
      message: 'ADS session unavailable: route OK, port 801 not answering',
    },
    failingPointIndexes: [],
    commandRejected: false,
    degradation: null,
  },
  'wtg-026': {
    network: 'ok',
    protocolError: null,
    failingPointIndexes: [0],
    commandRejected: false,
    degradation: null,
  },
  'wtg-044': {
    network: 'ok',
    protocolError: null,
    failingPointIndexes: [],
    commandRejected: true,
    degradation: null,
  },
  'wtg-017': {
    network: 'ok',
    protocolError: null,
    failingPointIndexes: [],
    commandRejected: false,
    degradation: 'latency',
  },
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

/** 设备运行时状态：场景注册表 × enabled 的合成结果，全站唯一入口。 */
export function deviceRuntimeState(deviceId: string, enabled = true): DeviceRuntimeState {
  const scenario = deviceScenarios[deviceId]
  if (!enabled) return { ...NORMAL_RUNTIME, network: 'disabled' }
  if (!scenario) return NORMAL_RUNTIME
  return { ...scenario, failingPointIndexes: [...scenario.failingPointIndexes] }
}

/** 设备 connected 派生：enabled 且网络与协议会话均正常。 */
export function deviceConnected(deviceId: string, enabled = true): boolean {
  const runtime = deviceRuntimeState(deviceId, enabled)
  return runtime.network === 'ok' && !runtime.protocolError
}

export interface SinkScenario {
  failingCheck: string
  code: string
  message: string
  latencyMs: number
}

// Sink 固定故障场景：kafka_main broker timeout、db_main connection refused。
export const sinkScenarios: Record<string, SinkScenario> = {
  kafka_main: {
    failingCheck: 'Broker Session',
    code: 'SINK_BROKER_TIMEOUT',
    message: 'Broker session timed out after 3000 ms',
    latencyMs: 3000,
  },
  db_main: {
    failingCheck: 'TCP Port',
    code: 'SINK_CONNECTION_REFUSED',
    message: 'TCP connection refused by remote host',
    latencyMs: 32,
  },
}

// 统一 Mock 错误码：与真实错误包络 {"error": {"code", ...}} 一起使用。
export const MOCK_ERROR_CODES = {
  HOST_UNREACHABLE: 'HOST_UNREACHABLE',
  DEVICE_DISABLED: 'DEVICE_DISABLED',
  ADS_SESSION_UNAVAILABLE: 'ADS_SESSION_UNAVAILABLE',
  POINT_READ_FAILED: 'POINT_READ_FAILED',
  READ_TIMEOUT: 'READ_TIMEOUT',
  ADS_SYMBOL_NOT_FOUND: 'ADS_SYMBOL_NOT_FOUND',
  COMMAND_REJECTED: 'COMMAND_REJECTED',
  COMMAND_TIMEOUT: 'COMMAND_TIMEOUT',
  TASK_DISABLED: 'TASK_DISABLED',
  TASK_DEVICE_UNAVAILABLE: 'TASK_DEVICE_UNAVAILABLE',
  SINK_DISABLED: 'SINK_DISABLED',
  SINK_BROKER_TIMEOUT: 'SINK_BROKER_TIMEOUT',
  SINK_CONNECTION_REFUSED: 'SINK_CONNECTION_REFUSED',
  SINK_WRITE_FAILED: 'SINK_WRITE_FAILED',
  CONFIG_VALIDATION_FAILED: 'CONFIG_VALIDATION_FAILED',
  NOT_FOUND: 'NOT_FOUND',
} as const
