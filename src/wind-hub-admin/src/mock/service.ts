// Mock Service 层（§2.2）：页面只调用这里的操作。
// 职责：延迟、状态转换、成功/失败、错误码、事件记录、日志生成、跨页面状态联动。
// 所有成败由 deviceScenarios / sinkScenarios 单一事实源决定，禁止随机数（§0.6）。
import {
  modelOf,
  pointsOfTable,
  protocolOfDevice,
  refreshTaskValidity,
  store,
  tableOfDevice,
  unitSymbol,
} from './data'
import { MOCK_ERROR_CODES, mockError } from './errors'
import type { MockError } from './errors'
import { runQualityCheck } from './quality'
import {
  appendLog,
  bumpDeviceDataTick,
  deviceRuntimeState,
  pointBoolAt,
  pointValueAt,
  setCommandOverride,
  sinkScenarios,
} from './runtime'
import type {
  DeviceInst,
  DeviceVerification,
  PointDef,
  SinkDef,
  SinkVerificationCheck,
  TaskDef,
} from './types'
import { formatTimestamp } from '../utils/format'

// ---------------------------------------------------------------------------
// 延迟档位（§27）：分档固定值，不统一 300ms、不随机
// ---------------------------------------------------------------------------
export const LATENCY = {
  uiLocal: 140,
  verifyStep: 280,
  verifyBulk: 1100,
  command: 420,
  taskTransition: 420,
  configApply: 380,
  sinkCheckStep: 90,
  diagnostic: 350,
  qualityCheck: 700,
  scanBatch: 18,
} as const

export function sleep(ms: number) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function nowText() {
  return formatTimestamp(new Date())
}

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

function verificationOf(device: DeviceInst): DeviceVerification {
  if (!store.deviceVerification[device.device_id]) {
    store.deviceVerification[device.device_id] = emptyVerification()
  }
  return store.deviceVerification[device.device_id]
}

function protocolDescription(d: DeviceInst) {
  const model = modelOf(d)
  if (model?.protocol === 'ads') return `ADS · AMS ${d.host}.1.1 · Port 801`
  if (model?.protocol === 'modbus') return `Modbus TCP · ${d.host}:${d.port || 502}`
  if (model?.protocol === 'iec104') return `IEC 60870-5-104 · ${d.host}:${d.port || 2404}`
  return model?.protocol || 'Unknown'
}

// ---------------------------------------------------------------------------
// Devices：Verify（§4）—— 终态结果由 deviceRuntimeState 决定
// ---------------------------------------------------------------------------
export function buildDeviceVerification(d: DeviceInst): DeviceVerification {
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  const result = emptyVerification()
  result.verified_at = nowText()

  if (runtime.network !== 'ok') {
    result.state = 'failed'
    result.network = 'failed'
    const err = runtime.network === 'disabled'
      ? mockError(MOCK_ERROR_CODES.DEVICE_DISABLED, 'network', d.host, 'Device is disabled')
      : mockError(MOCK_ERROR_CODES.HOST_UNREACHABLE, 'network', d.host, 'Host unreachable')
    result.errors.push({ stage: 'network', target: err.target, message: err.message })
    return result
  }

  result.network = 'success'
  result.latency_ms = 2 + (d.device_id.length % 7)

  if (runtime.protocolError) {
    result.state = 'failed'
    result.protocol = 'failed'
    result.errors.push({ stage: 'protocol', target: protocolDescription(d), message: runtime.protocolError.message })
    return result
  }

  result.protocol = 'success'
  const points = pointsOfTable(tableOfDevice(d))
  result.point_total = points.length
  const failedPoints = runtime.failingPointIndexes
    .map(i => points[i])
    .filter((p): p is PointDef => !!p)

  result.point_failed = failedPoints.length
  result.point_success = Math.max(0, result.point_total - result.point_failed)
  for (const p of failedPoints) {
    result.errors.push({
      stage: 'points',
      target: p.variable_name || p.point_id,
      message: modelOf(d)?.protocol === 'ads'
        ? 'ADS symbol not found / read failed'
        : 'Read request returned invalid response',
    })
  }

  if (result.point_failed === 0) {
    result.points = 'success'
    result.state = 'success'
  } else if (result.point_success > 0) {
    result.points = 'partial'
    result.state = 'warning'
  } else {
    result.points = 'failed'
    result.state = 'failed'
  }
  return result
}

// 单台 Verify：Idle → Network Checking → Protocol Checking → Point Checking → 终态，
// 每步写入 store.deviceVerification（页面直接渲染同一对象）。
export async function verifyDevice(d: DeviceInst): Promise<DeviceVerification> {
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  const v = verificationOf(d)
  Object.assign(v, emptyVerification(), { state: 'running' as const })

  v.network = 'checking'
  await sleep(LATENCY.verifyStep)
  if (runtime.network !== 'ok') {
    v.network = 'failed'
    v.state = 'failed'
    v.verified_at = nowText()
    const message = runtime.network === 'disabled' ? 'Device is disabled' : 'Host unreachable'
    v.errors.push({ stage: 'network', target: d.host, message })
    appendLog('ERROR', modelOf(d)?.protocol || 'device', d.device_id, `verification failed: ${message}`)
    return v
  }

  v.network = 'success'
  v.latency_ms = 2 + (d.device_id.length % 7)
  v.protocol = 'checking'
  await sleep(LATENCY.verifyStep + 20)
  if (runtime.protocolError) {
    v.protocol = 'failed'
    v.state = 'failed'
    v.verified_at = nowText()
    v.errors.push({ stage: 'protocol', target: protocolDescription(d), message: runtime.protocolError.message })
    appendLog('ERROR', modelOf(d)?.protocol || 'device', d.device_id, `verification failed: ${runtime.protocolError.code}`)
    return v
  }

  v.protocol = 'success'
  v.points = 'checking'
  await sleep(LATENCY.verifyStep + 100)
  Object.assign(v, (() => {
    const r = buildDeviceVerification(d)
    r.network = 'success'
    return r
  })())
  if (v.state !== 'success') {
    appendLog('WARN', modelOf(d)?.protocol || 'device', d.device_id,
      `verification ${v.state}: ${v.point_failed}/${v.point_total} point(s) failed`)
  }
  return v
}

// Verify All：与单台规则一致（同一 buildDeviceVerification），批量标记 checking 后统一落定。
export async function verifyAllDevices(devices: DeviceInst[]): Promise<{ failed: number; warning: number }> {
  for (const d of devices) {
    Object.assign(verificationOf(d), emptyVerification(), {
      state: 'running' as const,
      network: 'checking' as const,
      protocol: 'checking' as const,
      points: 'checking' as const,
    })
  }
  await sleep(LATENCY.verifyBulk)
  let failed = 0
  let warning = 0
  for (const d of devices) {
    const result = buildDeviceVerification(d)
    Object.assign(verificationOf(d), result)
    if (result.state === 'failed') failed += 1
    else if (result.state === 'warning') warning += 1
  }
  appendLog(
    failed ? 'ERROR' : warning ? 'WARN' : 'INFO',
    'runtime', 'verification',
    `verify all completed: ${devices.length} checked · ${failed} failed · ${warning} warning`,
  )
  return { failed, warning }
}

// ---------------------------------------------------------------------------
// Devices：点值读取（§5 Data / §7 Trend / §9.4 Test Read / §17 Protocol Read 共用）
// ---------------------------------------------------------------------------
export interface PointValue {
  value: number | boolean
  read_state: 'success' | 'failed'
  error: string
}

// Data 页当前值：同一确定性序列；failingPointIndexes 的点保持错误（§5.6）。
export function readPointValue(d: DeviceInst, pointIndex: number, pointId: string, dataType: string): PointValue {
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  const epochSec = Math.floor(Date.now() / 1000)
  if (runtime.network !== 'ok') return { value: false, read_state: 'failed', error: runtime.network === 'disabled' ? 'Device is disabled' : 'Host unreachable' }
  if (runtime.protocolError) return { value: false, read_state: 'failed', error: runtime.protocolError.code }
  if (runtime.failingPointIndexes.includes(pointIndex)) return { value: false, read_state: 'failed', error: 'POINT_READ_FAILED' }
  return {
    value: dataType === 'bool'
      ? pointBoolAt(d.device_id, pointId, pointIndex, epochSec)
      : pointValueAt(d.device_id, pointId, pointIndex, epochSec),
    read_state: 'success',
    error: '',
  }
}

// Trend 序列：与 Data 共用 pointValueAt，尾点即当前值（§7）。
export function pointTrendSeries(
  d: DeviceInst,
  pointId: string,
  pointIndex: number,
  durationMs: number,
  stepMs = 1000,
): Array<[Date, number]> {
  const endSec = Math.floor(Date.now() / 1000)
  const count = Math.floor(durationMs / stepMs) + 1
  return Array.from({ length: count }, (_, i) => {
    const sec = endSec - (count - 1 - i) * Math.round(stepMs / 1000)
    return [new Date(sec * 1000), pointValueAt(d.device_id, pointId, pointIndex, sec)] as [Date, number]
  })
}

// ---------------------------------------------------------------------------
// Devices：Read Test（§5 单点诊断读）
// ---------------------------------------------------------------------------
export interface PointReadResult {
  state: 'success' | 'failed'
  timestamp: string
  latency_ms: number
  raw_data: string
  decoded_value: string
  engineering_value: string
  error_category: string
  error_code: string
  error_message: string
}

export function pointRawData(protocol: string | undefined, point: PointDef, index: number): string {
  if (protocol === 'modbus') return `registers: [0x${(0x4200 + index).toString(16).toUpperCase()}, 0x0000]`
  if (protocol === 'ads') return `bytes: ${[0x42, 0xc8, index & 0xff, 0x00].map(x => x.toString(16).padStart(2, '0').toUpperCase()).join(' ')}`
  return `ASDU: IOA=${point.address.ioa ?? '—'} value-bytes=42 C8 00 00`
}

export async function readDevicePoint(d: DeviceInst, point: PointDef, index: number): Promise<PointReadResult> {
  await sleep(LATENCY.diagnostic)
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  const protocol = modelOf(d)?.protocol
  const fail = (category: string, code: string, message: string, latency: number, withRaw = false): PointReadResult => ({
    state: 'failed',
    timestamp: nowText(),
    latency_ms: latency,
    raw_data: withRaw ? pointRawData(protocol, point, index) : '—',
    decoded_value: '—',
    engineering_value: '—',
    error_category: category,
    error_code: code,
    error_message: message,
  })

  if (runtime.network !== 'ok') {
    return fail('timeout', MOCK_ERROR_CODES.READ_TIMEOUT, 'Read request timed out after 3000 ms', 3000)
  }
  if (runtime.protocolError) {
    return fail('protocol', runtime.protocolError.code, runtime.protocolError.message, 46)
  }
  if (runtime.failingPointIndexes.includes(index)) {
    return fail('read', MOCK_ERROR_CODES.POINT_READ_FAILED, 'Device returned no data for this point', 28)
  }
  if (protocol === 'ads' && point.variable_name.toLowerCase().includes('missing')) {
    return fail('not_found', MOCK_ERROR_CODES.ADS_SYMBOL_NOT_FOUND, 'Configured ADS symbol was not found on the remote PLC', 28)
  }

  const epochSec = Math.floor(Date.now() / 1000)
  const rawValue = point.data_type === 'bool'
    ? (pointBoolAt(d.device_id, point.point_id, index, epochSec) ? 1 : 0)
    : pointValueAt(d.device_id, point.point_id, index, epochSec)
  const engineering = point.data_type === 'bool' ? rawValue : Number((rawValue * point.scale + point.offset).toFixed(4))
  return {
    state: 'success',
    timestamp: nowText(),
    latency_ms: 12 + (index % 8) * 3,
    raw_data: pointRawData(protocol, point, index),
    decoded_value: point.data_type === 'bool' ? (rawValue ? 'true' : 'false') : String(rawValue),
    engineering_value: point.data_type === 'bool'
      ? (rawValue ? 'true' : 'false')
      : `${engineering} ${unitSymbol(point.unit)}`.trim(),
    error_category: '',
    error_code: '',
    error_message: '',
  }
}

// ---------------------------------------------------------------------------
// Devices / Diagnostics：Command（§6 / §17.4 共享底层写操作）
// ---------------------------------------------------------------------------
export interface CommandOutcome {
  requested: number | boolean
  // 失败时 readback 回显当前值，可能是字符串（如枚举/文本点）
  readback: string | number | boolean
  sentAt: string
  latency: number
  success: boolean
  error: string
  error_code: string
}

export async function sendDeviceCommand(
  d: DeviceInst,
  point: PointDef,
  pointIndex: number,
  target: number | boolean,
): Promise<CommandOutcome> {
  await sleep(LATENCY.command)
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  const current = readPointValue(d, pointIndex, point.point_id, point.data_type)
  const base: CommandOutcome = {
    requested: target,
    readback: current.read_state === 'success' ? current.value : '—',
    sentAt: nowText(),
    latency: 0,
    success: false,
    error: '',
    error_code: '',
  }

  if (runtime.network !== 'ok') {
    const code = runtime.network === 'disabled' ? MOCK_ERROR_CODES.DEVICE_DISABLED : MOCK_ERROR_CODES.COMMAND_TIMEOUT
    const error = runtime.network === 'disabled' ? 'Device is disabled' : 'No response within 3000 ms (mock)'
    appendLog('ERROR', modelOf(d)?.protocol || 'device', d.device_id, `command failed: ${code} (${point.point_id})`)
    return { ...base, latency: 3000, error, error_code: code }
  }
  if (runtime.protocolError) {
    appendLog('ERROR', modelOf(d)?.protocol || 'device', d.device_id, `command failed: ${runtime.protocolError.code} (${point.point_id})`)
    return { ...base, latency: 46, error: runtime.protocolError.message, error_code: runtime.protocolError.code }
  }
  // 场景 E：wtg-044 设备写被拒绝
  if (runtime.commandRejected) {
    appendLog('ERROR', modelOf(d)?.protocol || 'device', d.device_id, `command rejected by device (${point.point_id} → ${String(target)})`)
    return { ...base, latency: 3000, error: 'Write request rejected by device (mock)', error_code: MOCK_ERROR_CODES.COMMAND_REJECTED }
  }

  const readback: number | boolean = typeof target === 'boolean' ? target : Number((target - 0.6).toFixed(2))
  // 跨页面联动（§6）：成功后 Data 点值与 Trend 追加立即反映 readback
  setCommandOverride(d.device_id, point.point_id, readback)
  bumpDeviceDataTick(d.device_id)
  appendLog('INFO', modelOf(d)?.protocol || 'device', d.device_id, `command applied: ${point.point_id} → ${String(readback)}`)
  return {
    ...base,
    readback,
    latency: 160 + (d.device_id.length % 6) * 7,
    success: true,
  }
}

// ---------------------------------------------------------------------------
// Tasks：Start / Stop（§8）
// ---------------------------------------------------------------------------
export type TaskStartResult =
  | { ok: true }
  | { ok: false; error: MockError }

export async function startTask(t: TaskDef): Promise<TaskStartResult> {
  t.runtime = 'STARTING'
  await sleep(LATENCY.taskTransition)
  refreshTaskValidity()
  if (t.valid === false) {
    t.runtime = 'STOPPED'
    return { ok: false, error: mockError(MOCK_ERROR_CODES.CONFIG_VALIDATION_FAILED, 'validate', t.task_id, t.invalid_reason || 'Task is invalid') }
  }
  if (!t.enabled) {
    t.runtime = 'STOPPED'
    return { ok: false, error: mockError(MOCK_ERROR_CODES.DEVICE_DISABLED, 'precheck', t.task_id, 'Task is disabled') }
  }
  // 目标设备可用性检查：任何目标设备网络不可达 → 启动失败（场景 B：wtg-041）
  const targets = t.device
    ? store.devices.filter(d => d.device_id === t.device)
    : store.devices.filter(d => d.device_group === t.device_group && d.enabled)
  const unavailable = targets.find(d => deviceRuntimeState(d.device_id, d.enabled).network === 'unreachable')
  if (unavailable) {
    t.runtime = 'STOPPED'
    appendLog('ERROR', 'task', t.task_id, `start failed: device ${unavailable.device_id} unreachable`)
    return {
      ok: false,
      error: mockError(MOCK_ERROR_CODES.TASK_DEVICE_UNAVAILABLE, 'connect', unavailable.device_id, `device ${unavailable.device_id} unreachable`),
    }
  }
  t.runtime = 'RUNNING'
  appendLog('INFO', 'task', t.task_id, 'started')
  return { ok: true }
}

export async function stopTask(t: TaskDef): Promise<void> {
  t.runtime = 'STOPPING'
  await sleep(LATENCY.taskTransition)
  t.runtime = 'STOPPED'
  appendLog('INFO', 'task', t.task_id, 'stopped')
}

// Task 实例级状态（§8.5）：RUNNING 任务内不可达设备 → FAILED，点失败设备 → WARNING。
export function taskInstanceState(t: TaskDef, d: DeviceInst): 'RUNNING' | 'WARNING' | 'FAILED' | 'STOPPED' {
  if (t.runtime !== 'RUNNING') return 'STOPPED'
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  if (runtime.network !== 'ok' || runtime.protocolError) return 'FAILED'
  if (runtime.failingPointIndexes.length) return 'WARNING'
  return 'RUNNING'
}

// ---------------------------------------------------------------------------
// Sinks：Verify / Write Test（§10）
// ---------------------------------------------------------------------------
function sinkEndpoint(s: SinkDef): { host: string; port: number } {
  if (s.type === 'kafka') {
    const first = String(s.params.bootstrap_servers || 'localhost:9092').split(',')[0].trim()
    const [host, portText] = first.split(':')
    return { host: host || 'localhost', port: Number(portText || 9092) }
  }
  if (s.type === 'db') {
    const match = String(s.params.dsn || '').match(/^[a-zA-Z0-9+.-]+:\/\/(?:[^@/]+@)?([^:/]+)(?::(\d+))?/)
    return { host: match?.[1] || 'localhost', port: Number(match?.[2] || 5432) }
  }
  return { host: '', port: 0 }
}
function resolvedHost(host: string) {
  if (host === 'localhost') return '127.0.0.1'
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) return host
  return '192.168.10.25'
}

// 三类 Sink 的验证链（§10.1–§10.3）
export function sinkCheckPlan(s: SinkDef): Array<{ layer: 'network' | 'protocol' | 'target' | 'filesystem'; name: string; target: string; detail: string; blocks: boolean }> {
  if (s.type === 'file') {
    const path = String(s.params.path || '')
    const parent = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) || '/' : '.'
    return [
      { layer: 'filesystem', name: 'Parent Path', target: parent, detail: 'Resolve parent directory', blocks: true },
      { layer: 'filesystem', name: 'Permission', target: parent, detail: 'Check create/append permission', blocks: true },
      { layer: 'filesystem', name: 'Open / Append', target: path, detail: 'Open target for append without business payload', blocks: true },
    ]
  }
  const ep = sinkEndpoint(s)
  const ip = resolvedHost(ep.host)
  const common = [
    { layer: 'network' as const, name: 'DNS', target: ep.host, detail: `Resolve ${ep.host} → ${ip}`, blocks: true },
    { layer: 'network' as const, name: 'ICMP', target: ip, detail: 'ICMP reachability check (advisory only)', blocks: false },
    { layer: 'network' as const, name: 'TCP Port', target: `${ip}:${ep.port}`, detail: 'Open TCP connection to remote endpoint', blocks: true },
  ]
  if (s.type === 'kafka') return [
    ...common,
    { layer: 'protocol', name: 'Broker Session', target: `${ep.host}:${ep.port}`, detail: 'Establish Kafka producer / broker session', blocks: true },
    { layer: 'target', name: 'Metadata', target: String(s.params.topic || ''), detail: 'Request broker metadata', blocks: true },
    { layer: 'target', name: 'Topic', target: String(s.params.topic || ''), detail: 'Verify configured topic is addressable', blocks: true },
  ]
  return [
    ...common,
    { layer: 'protocol', name: 'PostgreSQL Session', target: `${ep.host}:${ep.port}`, detail: 'Establish PostgreSQL protocol session', blocks: true },
    { layer: 'protocol', name: 'Authentication', target: ep.host, detail: 'Authenticate configured DSN credentials', blocks: true },
    { layer: 'target', name: 'SELECT 1', target: String(s.params.table || 'points'), detail: 'Execute lightweight read-only query', blocks: true },
  ]
}

export async function verifySink(s: SinkDef): Promise<void> {
  const plan = sinkCheckPlan(s)
  const checks: SinkVerificationCheck[] = []
  const scenario = sinkScenarios[s.name]
  s.runtime_state = 'testing'
  let blocked = false
  for (const step of plan) {
    await sleep(LATENCY.sinkCheckStep)
    if (blocked) {
      checks.push({ layer: step.layer, name: step.name, state: 'skipped', target: step.target, latency_ms: 0, detail: 'Skipped after upstream failure', error_code: '' })
      continue
    }
    // Kafka 的 ICMP 仅 advisory：无回复但 TCP 为准 → warning 不阻塞
    if (step.name === 'ICMP' && s.type === 'kafka') {
      checks.push({ layer: step.layer, name: step.name, state: 'warning', target: step.target, latency_ms: 1000, detail: 'No ICMP reply; continue because TCP is authoritative', error_code: 'ICMP_NO_REPLY' })
      continue
    }
    if (scenario && step.name === scenario.failingCheck) {
      checks.push({ layer: step.layer, name: step.name, state: 'failed', target: step.target, latency_ms: scenario.code === 'SINK_BROKER_TIMEOUT' ? 3000 : 32, detail: scenario.message, error_code: scenario.code })
      if (step.blocks) blocked = true
      continue
    }
    checks.push({ layer: step.layer, name: step.name, state: 'passed', target: step.target, latency_ms: 2 + checks.length * 4, detail: step.detail + ' passed', error_code: '' })
  }
  const passed = checks.filter(c => c.state === 'passed').length
  const failed = checks.some(c => c.state === 'failed')
  s.verification = { state: failed ? 'failed' : 'passed', checked_at: nowText(), passed, total: checks.length, checks }
  s.last_test_at = s.verification.checked_at
  s.latency_ms = Math.max(0, ...checks.map(c => c.latency_ms))
  s.runtime_state = s.enabled ? (failed ? 'failed' : 'healthy') : 'disabled'
  s.error = failed ? (checks.find(c => c.state === 'failed')?.error_code || 'Verification failed') : ''
  appendLog(
    failed ? 'ERROR' : 'INFO',
    'sink', s.name,
    failed ? `verification failed: ${s.error}` : `verification passed (${passed}/${checks.length})`,
  )
}

export interface WriteTestOutcome {
  ok: boolean
  title: string
  detail: string
  latency: number
}

export async function writeTestSink(s: SinkDef): Promise<WriteTestOutcome> {
  await sleep(LATENCY.command)
  if (!s.enabled) {
    s.failures_total += 1
    appendLog('ERROR', 'sink', s.name, 'write test failed: sink is disabled')
    return { ok: false, title: 'Write test failed', detail: 'Sink is disabled; enable it before writing.', latency: 4 }
  }
  const scenario = sinkScenarios[s.name]
  if (scenario) {
    // 失败路径（§10.6）：更新 runtime_state / error，并反映到 Quality Delivery Integrity
    s.failures_total += 1
    s.dropped_points += 1
    s.runtime_state = 'failed'
    s.error = scenario.code
    appendLog('ERROR', 'sink', s.name, `write test failed: ${scenario.message}`)
    return { ok: false, title: 'Write test failed', detail: scenario.message + ' (mock)', latency: scenario.code === 'SINK_BROKER_TIMEOUT' ? 3000 : 32 }
  }
  // 成功路径（§10.5）：更新 last_write_at / latency / Logs
  s.last_write_at = nowText()
  s.writes_total += 1
  s.latency_ms = 18
  if (s.runtime_state !== 'healthy') s.runtime_state = 'healthy'
  s.error = ''
  appendLog('INFO', 'sink', s.name, 'write test accepted (synthetic PointValue)')
  return { ok: true, title: 'Write test passed', detail: 'Synthetic PointValue accepted by the Sink (mock).', latency: 18 }
}

// ---------------------------------------------------------------------------
// Diagnostics（§17）：Ping / Port Probe / Subnet Scan / Protocol Read / Write
// ---------------------------------------------------------------------------
export function pingHost(host: string) {
  const known = store.devices.find(d => d.host === host)
  const reachable = known ? deviceRuntimeState(known.device_id, true).network === 'ok' : true
  return {
    IP: host,
    Reachable: reachable ? 'Yes' : 'No',
    RTT: reachable ? '8 ms' : '—',
    Loss: reachable ? '0%' : '100%',
    Object: known?.device_id || '—',
  }
}

export function probePorts(host: string, ports: number[]) {
  const known = store.devices.find(d => d.host === host)
  const protocol = known ? protocolOfDevice(known) : ''
  const reachable = known ? deviceRuntimeState(known.device_id, true).network === 'ok' : true
  const DEFAULT_SERVICES: Record<number, string> = {
    502: 'modbus', 2404: 'iec104', 48898: 'ads', 4840: 'opc-ua', 44818: 'ethernet-ip',
    80: 'http', 443: 'https', 22: 'ssh', 23: 'telnet',
  }
  return ports.map((port, index) => ({
    IP: host,
    Port: port,
    Service: DEFAULT_SERVICES[port] || 'custom',
    State: known
      ? (reachable && ((protocol === 'ads' && port === 48898) || (protocol === 'modbus' && port === 502) || (protocol === 'iec104' && port === 2404)) ? 'Open' : 'Closed')
      : (index === 0 ? 'Open' : 'Closed'),
    Latency: index === 0 ? '6 ms' : '—',
  }))
}

// Subnet scan：已配置设备用真实 mock 状态，未配置 IP 用固定 deterministic pattern（§17.5）
export function subnetHostResult(ip: string, index: number) {
  const known = store.devices.find(d => d.host === ip)
  const reachable = known ? deviceRuntimeState(known.device_id, true).network === 'ok' : index % 23 === 0 || index % 37 === 0
  const protocol = known ? protocolOfDevice(known) : ''
  return {
    IP: ip,
    Ping: reachable ? 'Yes' : 'No',
    ADS: reachable && (protocol === 'ads' || (!known && index % 37 === 0)) ? 'Open' : '—',
    Modbus: reachable && (protocol === 'modbus' || (!known && index % 23 === 0)) ? 'Open' : '—',
    IEC104: reachable && protocol === 'iec104' ? 'Open' : '—',
    Object: known?.device_id || '—',
  }
}

// 诊断结果行是面向表格的 string 字典（Diagnostics 页用 Object.keys 动态渲染列），
// 因此显式带 index signature。
export interface DiagnosticReadRow {
  [column: string]: string
  Target: string
  Host: string
  Point: string
  Address: string
  Raw: string
  Value: string
  Unit: string
  Result: string
  Error: string
  Latency: string
}

// Protocol Read：来自同一 Device Data mock（§17.3）
export async function runProtocolRead(devices: DeviceInst[], point: PointDef, addressText: string): Promise<DiagnosticReadRow[]> {
  await sleep(LATENCY.diagnostic)
  const epochSec = Math.floor(Date.now() / 1000)
  return devices.map((d, index) => {
    const runtime = deviceRuntimeState(d.device_id, d.enabled)
    const pointIndex = pointsOfTable(tableOfDevice(d)).findIndex(p => p.point_id === point.point_id)
    const base = {
      Target: d.device_id,
      Host: d.host,
      Point: point.point_id,
      Address: addressText,
      Unit: unitSymbol(point.unit),
    }
    if (runtime.network !== 'ok') {
      return { ...base, Raw: '—', Value: '—', Result: 'Failed', Error: 'Connection unavailable', Latency: '—' }
    }
    if (runtime.protocolError) {
      return { ...base, Raw: '—', Value: '—', Result: 'Failed', Error: runtime.protocolError.code, Latency: '—' }
    }
    if (runtime.failingPointIndexes.includes(pointIndex)) {
      return { ...base, Raw: '—', Value: '—', Result: 'Failed', Error: MOCK_ERROR_CODES.POINT_READ_FAILED, Latency: '—' }
    }
    const value = point.data_type === 'bool'
      ? pointBoolAt(d.device_id, point.point_id, pointIndex, epochSec)
      : pointValueAt(d.device_id, point.point_id, pointIndex, epochSec)
    return {
      ...base,
      Raw: pointRawData(protocolOfDevice(d), point, Math.max(0, pointIndex)),
      Value: String(value),
      Result: 'Success',
      Error: '—',
      Latency: (12 + index % 9) + ' ms',
    }
  })
}

// Protocol Write：与 Device Command 共享底层操作（§17.4），成功后 Data/Trend/Logs 联动
export async function runProtocolWrite(d: DeviceInst, point: PointDef, valueText: string, addressText: string): Promise<Record<string, string>> {
  const pointIndex = pointsOfTable(tableOfDevice(d)).findIndex(p => p.point_id === point.point_id)
  const parsed: number | boolean = valueText === 'true' ? true : valueText === 'false' ? false : Number(valueText)
  const outcome = await sendDeviceCommand(d, point, Math.max(0, pointIndex), parsed)
  return {
    Target: d.device_id,
    Address: addressText,
    Write: valueText,
    Result: outcome.success ? 'Success' : 'Failed',
    Readback: outcome.success ? String(outcome.readback) : '—',
    Error: outcome.success ? '—' : outcome.error,
    Latency: outcome.latency + ' ms',
  }
}

// Manual 模式目标不在 mock 设备注册表内，无场景状态可派生：
// 结果为由输入文本哈希决定的确定性合成回显（同一输入同一输出），不伪装成真实设备响应。
function manualSeed(host: string, addressText: string, extra: string) {
  let hash = 2166136261
  const seed = `${host}|${addressText}|${extra}`
  for (let i = 0; i < seed.length; i++) {
    hash ^= seed.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

export async function manualProtocolReadRow(host: string, addressText: string, dataType: string): Promise<DiagnosticReadRow> {
  await sleep(LATENCY.diagnostic)
  const h = manualSeed(host, addressText, dataType)
  const value = dataType === 'bool' ? (h % 2 === 1 ? 'true' : 'false') : String(Math.round((h % 10000)) / 100)
  return {
    Target: host,
    Host: host,
    Point: '—',
    Address: addressText,
    Raw: [0, 1, 2, 3].map(i => ((h >>> (i * 8)) & 0xff).toString(16).padStart(2, '0').toUpperCase()).join(' '),
    Value: value,
    Unit: '—',
    Result: 'Success',
    Error: '—',
    Latency: (10 + h % 9) + ' ms',
  }
}

export async function manualProtocolWriteRow(host: string, addressText: string, valueText: string): Promise<Record<string, string>> {
  await sleep(LATENCY.command)
  const h = manualSeed(host, addressText, valueText)
  return {
    Target: host,
    Address: addressText,
    Write: valueText,
    Result: 'Success',
    Readback: valueText,
    Error: '—',
    Latency: (16 + h % 11) + ' ms',
  }
}

// ---------------------------------------------------------------------------
// Config：Validate / Apply / Import / Restore（§18–§22）
// ---------------------------------------------------------------------------
export interface ConfigValidation {
  ok: boolean
  errors: string[]
}

// 确定性校验规则（§20）：空文档 / Tab 缩进 / 重复 device_id / 未知引用
export function validateConfig(file: string, text: string): ConfigValidation {
  const errors: string[] = []
  if (!text.trim()) {
    return { ok: false, errors: [`${file}: document is empty`] }
  }
  const tabLine = text.split('\n').findIndex(line => line.includes('\t'))
  if (tabLine >= 0) {
    errors.push(`${file}:${tabLine + 1}: YAML syntax error — tab characters are not allowed for indentation`)
  }
  if (file === 'devices.yaml') {
    const ids = [...text.matchAll(/device_id:\s*(\S+)/g)].map(m => m[1])
    const seen = new Set<string>()
    for (const id of ids) {
      if (seen.has(id)) errors.push(`${file}: duplicate device_id "${id}"`)
      seen.add(id)
    }
    const models = [...text.matchAll(/^\s+model:\s*(\S+)/gm)].map(m => m[1])
    for (const model of models) {
      if (!store.deviceModels.some(m => m.id === model)) errors.push(`${file}: unknown device model "${model}"`)
    }
  }
  if (file === 'tasks.yaml') {
    const sinks = [...text.matchAll(/sink:\s*(\S+)/g)].map(m => m[1])
    for (const name of sinks) {
      if (!store.sinks.some(s => s.name === name)) errors.push(`${file}: unknown sink "${name}"`)
    }
  }
  if (file === 'points.yaml') {
    const parents = [...text.matchAll(/extends:\s*(\S+)/g)].map(m => m[1])
    for (const parent of parents) {
      if (!store.pointTables.some(t => t.id === parent)) errors.push(`${file}: unknown point table "${parent}" in extends`)
    }
  }
  return { ok: errors.length === 0, errors }
}

// Apply 影响范围（§22）：按 target file 生成结构化影响描述
export function configApplyImpact(file: string): string[] {
  switch (file) {
    case 'system.yaml':
      return ['Runtime 参数重载', 'ADS 本机身份重建（所有 ADS 连接重初始化）', 'API 接口监听地址生效', `${store.sinks.length} 个 Sink 重连评估`]
    case 'devices.yaml':
      return [`${store.devices.length} 台 Device 重新解析`, `${store.tasks.length} 个 Task 目标重评估`, 'Diagnostics / Verify 使用新 endpoint']
    case 'points.yaml':
      return [`${store.pointTables.length} 个 Point Table 重新解析`, '继承链与设备点绑定重建', `${store.tasks.length} 个 Task 点绑定重评估`]
    case 'tasks.yaml':
      return [`${store.tasks.length} 个 Task 定义重载`, '运行中 Task 重建实例', 'Sink 引用有效性重评估']
    case 'device_models.yaml':
      return [`${store.deviceModels.length} 个 Device Model 重新解析`, '关联 Device 生效连接参数重算']
    case 'units.yaml':
      return [`${store.units.length} 个 Unit 重新解析`, '点单位显示刷新']
    default:
      return ['Reporting 配置重载（不影响采集链路）']
  }
}

// system.yaml 应用后回写结构化 mock state（§18/§19：Apply 后 Settings/Overview 同步反映）
export function applySystemYamlToState(text: string): void {
  const pick = (re: RegExp) => text.match(re)?.[1]?.trim()
  const unquote = (v: string | undefined) => v?.replace(/^"|"$/g, '') ?? ''
  const siteId = unquote(pick(/site_id:\s*("?[^\s"]+"?)/))
  const siteName = unquote(pick(/^\s+name:\s*("?[^"\n]+"?)\s*$/m))
  if (siteId) store.systemInfo.siteId = siteId
  if (siteName) store.systemInfo.siteName = siteName
  const ams = unquote(pick(/local_ams_net_id:\s*("?[^\s"]+"?)/))
  const ip = unquote(pick(/local_ip:\s*("?[^\s"]+"?)/))
  const username = unquote(pick(/username:\s*("?[^\s"]*"?)/))
  const password = unquote(pick(/password:\s*("?[^\s"]*"?)/))
  if (ams) store.systemInfo.ads.local_ams_net_id = ams
  if (ip) store.systemInfo.ads.local_ip = ip
  if (username) store.systemInfo.ads.username = username
  if (pick(/password:/) !== undefined) store.systemInfo.ads.password = password
  const apiHost = unquote(pick(/api:\s*\n\s*enabled:[^\n]*\n\s*host:\s*("?[^\s"]+"?)/))
  const apiPort = unquote(pick(/api:\s*\n\s*enabled:[^\n]*\n\s*host:[^\n]*\n\s*port:\s*(\d+)/))
  if (apiHost) store.systemInfo.apiHost = apiHost
  if (apiPort) store.systemInfo.apiPort = Number(apiPort)
}

// devices.yaml 应用后回写设备 host / enabled（§18：Devices 页立即反映）
export function applyDevicesYamlToState(text: string): void {
  const blocks = text.split(/(?=^\s*-\s+device_id:)/m).filter(b => /device_id:/.test(b))
  for (const block of blocks) {
    const id = block.match(/device_id:\s*(\S+)/)?.[1]
    if (!id) continue
    const device = store.devices.find(d => d.device_id === id)
    if (!device) continue
    const host = block.match(/host:\s*"?([0-9.]+)"?/)?.[1]
    const enabledText = block.match(/enabled:\s*(true|false)/)?.[1]
    if (host) device.host = host
    if (enabledText) device.enabled = enabledText === 'true'
  }
  refreshTaskValidity()
}

export function logConfigApplied(source: string, file: string, revision: number) {
  appendLog('INFO', 'config', file, `${source} applied as revision ${revision}`)
}

// ---------------------------------------------------------------------------
// Points：Test Read（§9.4）
// ---------------------------------------------------------------------------
export interface PointTestOutcome {
  ok: boolean
  error: string
  errorCode: string
  latency: number
  bytes?: Uint8Array
}

export async function testPointRead(device: DeviceInst, protocol: string, requestText: string): Promise<PointTestOutcome> {
  await sleep(LATENCY.qualityCheck)
  const runtime = deviceRuntimeState(device.device_id, device.enabled)
  if (runtime.network !== 'ok') {
    const disabled = runtime.network === 'disabled'
    return {
      ok: false,
      error: disabled ? 'Device is disabled' : 'Device is unreachable',
      errorCode: disabled ? MOCK_ERROR_CODES.DEVICE_DISABLED : MOCK_ERROR_CODES.HOST_UNREACHABLE,
      latency: disabled ? 6 : 3000,
    }
  }
  if (runtime.protocolError) {
    return { ok: false, error: runtime.protocolError.message, errorCode: runtime.protocolError.code, latency: 46 }
  }
  if (protocol === 'ads' && requestText.toLowerCase().includes('missing')) {
    return { ok: false, error: 'Configured ADS symbol was not found on the remote PLC', errorCode: MOCK_ERROR_CODES.ADS_SYMBOL_NOT_FOUND, latency: 28 }
  }
  // 确定性原始字节：由 device + 请求 + 数据类型哈希生成（同一输入同一字节流）
  let hash = 2166136261
  const seed = `${device.device_id}|${protocol}|${requestText}`
  for (let i = 0; i < seed.length; i++) {
    hash ^= seed.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  const bytes = new Uint8Array(8)
  for (let i = 0; i < bytes.length; i++) {
    hash ^= hash << 13
    hash ^= hash >>> 17
    hash ^= hash << 5
    bytes[i] = hash & 0xff
  }
  return { ok: true, error: '', errorCode: '', latency: 0, bytes }
}

// ---------------------------------------------------------------------------
// Quality：Auto Check（§16）重导出，页面不经由 quality.ts 直接碰计数器
// ---------------------------------------------------------------------------
export { runQualityCheck }
