// Mock Service 层：状态机、成败判定、错误码、日志生成、跨模块联动。
// Handler 只 parse + 调用这里 + 包 HTTP；复杂行为全部收敛在本文件。
// 所有成败由 scenarios.ts 单一事实源决定，禁止随机数。
import type { components } from '../api/generated/schema'
import {
  devicesYaml,
  modelOf,
  pointsOfTable,
  portOfDevice,
  tasksYaml,
  unitsSeed,
  type MockDevice,
  type MockPointDef,
  type MockSink,
  type MockTask,
} from './data'
import { MOCK_ERROR_CODES, deviceRuntimeState, sinkScenarios } from './scenarios'
import {
  mockState,
  nextRevision,
  pollOperation,
  registerOperation,
  sinkRuntimeOf,
  taskPhase,
  taskRuntimeOf,
} from './state'
import { epochSecNow, hash32, pointBoolAt, pointValueAt, setCommandOverride } from './values'
import { appendLog } from './logs'

type DeviceDto = components['schemas']['DeviceResponse']
type TaskDto = components['schemas']['TaskResponse']
type TaskInstanceDto = components['schemas']['TaskInstanceResponse']
type SinkDto = components['schemas']['SinkResponse']
type SinkTestResult = components['schemas']['SinkTestResponse']
type CommandResult = components['schemas']['DeviceCommandResponse']
type PingResult = components['schemas']['PingResponse']
type PortProbe = components['schemas']['PortProbeResponse']
type ProtocolReadResult = components['schemas']['ProtocolReadResponse']
type DeviceDataItem = components['schemas']['DeviceDataItemResponse']
type TrendSeries = components['schemas']['TrendSeriesResponse']
type ConfigApplyDto = components['schemas']['ConfigApplyResponse']
type ConfigReviewDto = components['schemas']['ConfigReviewResponse']
type OperationDto = components['schemas']['OperationResponse']

// ---------------------------------------------------------------------------
// 延迟档位：分档固定值，不统一、不随机
// ---------------------------------------------------------------------------
export const LATENCY = {
  uiLocal: 60,
  verifyStep: 120,
  verifyBulk: 400,
  command: 160,
  taskTransition: 200,
  configApply: 150,
  sinkCheckStep: 40,
  diagnostic: 120,
  qualityCheck: 250,
  scanBatch: 8,
} as const

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** 稳定错误包络（与后端 webapi/errors.py 同形）。 */
export function errorBody(code: string, message: string, details: Record<string, unknown> = {}) {
  return { error: { code, message, details } }
}

function nowIso(): string {
  return new Date().toISOString()
}

// ---------------------------------------------------------------------------
// DTO 派生：Device / Task / Sink
// ---------------------------------------------------------------------------
export function deviceDto(d: MockDevice): DeviceDto {
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  const model = modelOf(d)
  const connected = d.enabled && runtime.network === 'ok' && !runtime.protocolError
  return {
    device_id: d.device_id,
    model: d.model,
    device_type: model?.device_type ?? null,
    device_group: d.device_group || null,
    host: d.host,
    port: portOfDevice(d),
    port_override: d.port,
    protocol: model?.protocol ?? '',
    point_table: model?.point_table ?? '',
    enabled: d.enabled,
    connected,
    consecutive_failures: !d.enabled
      ? 0
      : runtime.network === 'unreachable'
        ? 8
        : runtime.protocolError
          ? 5
          : 0,
    last_error: !d.enabled
      ? null
      : runtime.network === 'unreachable'
        ? 'Host unreachable'
        : (runtime.protocolError?.message ?? null),
    extensions: { ...d.extensions },
    extension_overrides: {},
  }
}

export function findDevice(deviceId: string): MockDevice | undefined {
  return mockState.devices.find((d) => d.device_id === deviceId)
}

export function targetsOfTask(t: MockTask): MockDevice[] {
  if (t.device) return mockState.devices.filter((d) => d.device_id === t.device)
  if (t.device_group)
    return mockState.devices.filter((d) => d.device_group === t.device_group && d.enabled)
  return []
}

/** Task 无效原因：空串表示有效（sink / point group / 目标设备 / 点绑定）。 */
export function taskInvalidReason(t: MockTask): string {
  if (!t.sinks.length) return 'Task has no Sink target'
  for (const name of t.sinks) {
    const sink = mockState.sinks.find((s) => s.name === name)
    if (!sink) return `Sink '${name}' does not exist`
    if (!sink.enabled) return `Sink '${name}' is disabled`
  }
  const targets = targetsOfTask(t)
  if (!targets.length) return 'Task target resolves to no devices'
  for (const d of targets) {
    const model = modelOf(d)
    if (!model) return `Device ${d.device_id} has no valid model`
    if (!pointsOfTable(model.point_table).some((p) => p.point_groups.includes(t.point_group))) {
      return `Device ${d.device_id} Point Table has no points in group '${t.point_group}'`
    }
  }
  return ''
}

/** Task 实例级状态：RUNNING 任务内不可达/协议失败 → failed，点失败 → warning。 */
export function taskInstanceState(t: MockTask, d: MockDevice): string {
  if (taskPhase(t.task_id) !== 'running') return 'stopped'
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  if (runtime.network !== 'ok' || runtime.protocolError) return 'failed'
  if (runtime.failingPointIndexes.length) return 'warning'
  return 'running'
}

export function taskDto(t: MockTask): TaskDto {
  const phase = taskPhase(t.task_id)
  const targets = targetsOfTask(t)
  const states = targets.map((d) => taskInstanceState(t, d))
  const running = states.filter((s) => s === 'running').length
  const failed = states.filter((s) => s === 'failed').length
  const warning = states.filter((s) => s === 'warning').length
  return {
    task_id: t.task_id,
    device: t.device,
    device_group: t.device_group,
    point_group: t.point_group,
    interval: t.interval,
    enabled: t.enabled,
    runtime_state: phase,
    placement_state: phase === 'running' || phase === 'starting' ? 'placed' : 'unplaced',
    instance_count: phase === 'running' ? states.length : 0,
    running_instances: running + warning,
    failed_instances: failed,
    stopped_instances: phase === 'running' ? 0 : states.length,
    assigned_worker_id: phase === 'running' || phase === 'starting' ? 'collector-1' : null,
    // TaskResponse.targets 在现有前端契约中表示 Sink 目标名（stores/config.ts 映射为 sinks）
    targets: [...t.sinks],
  }
}

export function taskInstances(t: MockTask): TaskInstanceDto[] {
  const phase = taskPhase(t.task_id)
  if (phase !== 'running') return []
  return targetsOfTask(t).map((d, i) => ({
    instance_id: `${t.task_id}#${i}`,
    task_id: t.task_id,
    device_id: d.device_id,
    point_group: t.point_group,
    interval: t.interval,
    state: taskInstanceState(t, d),
    assigned_worker_id: 'collector-1',
    targets: [...t.sinks],
  }))
}

export function sinkHealthy(s: MockSink): boolean {
  if (!s.enabled) return false
  return !sinkRuntimeOf(s.name).error
}

export function sinkDto(s: MockSink): SinkDto {
  const runtime = sinkRuntimeOf(s.name)
  return {
    name: s.name,
    type: s.type,
    enabled: s.enabled,
    healthy: sinkHealthy(s),
    message: !s.enabled ? 'disabled' : runtime.error || null,
    queue_depth: runtime.queue_depth,
    point_count: s.points.length,
    connection: { ...s.connection },
    points: s.points.map((p) => ({ ...p })),
  }
}

// ---------------------------------------------------------------------------
// Devices：Verify 三阶段（ping → protocol check → point table operation）
// 中间态经 Operation polling 可观察（pending/running → success/partial/failed）。
// ---------------------------------------------------------------------------
export function pingHost(host: string): PingResult {
  const known = mockState.devices.find((d) => d.host === host)
  const reachable = known ? deviceRuntimeState(known.device_id, true).network === 'ok' : true
  if (reachable) appendLog('INFO', 'diagnostics', host, 'ping ok')
  else appendLog('ERROR', 'diagnostics', known?.device_id ?? host, 'ping failed: host unreachable')
  return { host, reachable, latency_ms: reachable ? 2 + (hash32(host) % 7) : 0 }
}

const PORT_SERVICES: Record<number, string> = { 502: 'modbus', 2404: 'iec104', 48898: 'ads' }

export function probePorts(host: string, ports: number[]): PortProbe[] {
  const known = mockState.devices.find((d) => d.host === host)
  const protocol = known ? modelOf(known)?.protocol : ''
  const reachable = known ? deviceRuntimeState(known.device_id, true).network === 'ok' : true
  return ports.map((port, index) => {
    const open = known ? reachable && PORT_SERVICES[port] === protocol : index === 0
    return {
      port,
      state: open ? 'open' : 'closed',
      latency_ms: open ? 2 + (hash32(`${host}:${port}`) % 6) : 0,
    }
  })
}

export function protocolCheckDevice(
  deviceId: string,
): { connected: boolean } | { error: { code: string; message: string } } {
  const d = findDevice(deviceId)
  if (!d)
    return { error: { code: MOCK_ERROR_CODES.NOT_FOUND, message: `device ${deviceId} not found` } }
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  const connected = runtime.network === 'ok' && !runtime.protocolError
  if (!connected) {
    appendLog(
      'ERROR',
      modelOf(d)?.protocol ?? 'device',
      d.device_id,
      `protocol check failed: ${runtime.protocolError?.code ?? 'HOST_UNREACHABLE'}`,
    )
  }
  return { connected }
}

/** 点表测试 Operation：多轮 polling 后按场景落定 success / partial / failed。 */
export function createPointTableOperation(
  deviceId: string,
): OperationDto | { error: { code: string; message: string } } {
  const d = findDevice(deviceId)
  if (!d)
    return { error: { code: MOCK_ERROR_CODES.NOT_FOUND, message: `device ${deviceId} not found` } }
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  const points = pointsOfTable(modelOf(d)?.point_table ?? '')
  if (runtime.network !== 'ok' || runtime.protocolError) {
    const code =
      runtime.network !== 'ok' ? MOCK_ERROR_CODES.HOST_UNREACHABLE : runtime.protocolError!.code
    const message = runtime.network !== 'ok' ? 'Host unreachable' : runtime.protocolError!.message
    appendLog(
      'ERROR',
      modelOf(d)?.protocol ?? 'device',
      d.device_id,
      `point table test failed: ${code}`,
    )
    return registerOperation('point-table-test', 2, 'failed', null, { code, message })
  }
  const failing = new Set(runtime.failingPointIndexes)
  const rows = points.map((p, i) => ({
    point_id: p.point_id,
    success: !failing.has(i),
    error: failing.has(i) ? 'POINT_READ_FAILED' : null,
  }))
  const failedCount = rows.filter((r) => !r.success).length
  const terminal = failedCount === 0 ? 'success' : failedCount < rows.length ? 'partial' : 'failed'
  if (terminal !== 'success') {
    appendLog(
      'WARNING',
      modelOf(d)?.protocol ?? 'device',
      d.device_id,
      `point table test ${terminal}: ${failedCount}/${rows.length} point(s) failed`,
    )
  } else {
    appendLog('INFO', modelOf(d)?.protocol ?? 'device', d.device_id, 'point table test passed')
  }
  return registerOperation('point-table-test', 3, terminal, {
    device_id: d.device_id,
    points: rows,
  })
}

// ---------------------------------------------------------------------------
// Devices：点值（Data / Trend / Diagnostics Read 共用 pointValueAt，天然一致）
// ---------------------------------------------------------------------------
export function engineeringValue(p: MockPointDef, raw: number | boolean): number | boolean {
  if (typeof raw === 'boolean') return raw
  return Number((raw * p.scale + p.offset).toFixed(4))
}

export function deviceDataItems(d: MockDevice): DeviceDataItem[] {
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  const model = modelOf(d)
  const points = pointsOfTable(model?.point_table ?? '')
  const epochSec = epochSecNow()
  const offline = runtime.network !== 'ok' || !!runtime.protocolError
  return points.map((p, i) => {
    const failed = offline || runtime.failingPointIndexes.includes(i)
    const raw = failed
      ? null
      : p.data_type === 'bool'
        ? pointBoolAt(d.device_id, p.point_id, i, epochSec)
        : pointValueAt(d.device_id, p.point_id, i, epochSec)
    return {
      point_id: p.point_id,
      variable_name: p.variable_name,
      point_groups: [...p.point_groups],
      data_type: p.data_type,
      unit: p.unit,
      unit_symbol: unitsSeed[p.unit]?.symbol ?? p.unit,
      value: raw === null ? null : engineeringValue(p, raw),
      quality: failed ? 'bad' : 'good',
      source: model?.protocol ?? '',
      timestamp: new Date(epochSec * 1000).toISOString(),
      description: p.description || null,
    }
  })
}

export function deviceTrend(
  d: MockDevice,
  pointIds: string[],
  windowSeconds: number,
  limitPerPoint: number,
): TrendSeries[] {
  const model = modelOf(d)
  const points = pointsOfTable(model?.point_table ?? '')
  const step = Math.max(1, Math.floor(windowSeconds / 60))
  const endSec = epochSecNow()
  return pointIds.map((pointId) => {
    const index = points.findIndex((p) => p.point_id === pointId)
    const point = index >= 0 ? points[index] : undefined
    const count = Math.min(limitPerPoint, Math.floor(windowSeconds / step) + 1)
    const samples = point
      ? Array.from({ length: count }, (_, i) => {
          const sec = endSec - (count - 1 - i) * step
          // 尾点（sec === endSec）与 Data 当前值经同一 pointValueAt 派生
          const raw =
            point.data_type === 'bool'
              ? pointBoolAt(d.device_id, pointId, index, sec)
              : pointValueAt(d.device_id, pointId, index, sec)
          return {
            timestamp: new Date(sec * 1000).toISOString(),
            value: engineeringValue(point, raw),
            quality: 'good',
            source: model?.protocol ?? '',
          }
        })
      : []
    return {
      point_id: pointId,
      variable_name: point?.variable_name ?? pointId,
      unit: point?.unit ?? '',
      unit_symbol: point ? (unitsSeed[point.unit]?.symbol ?? point.unit) : '',
      samples,
    }
  })
}

// ---------------------------------------------------------------------------
// Diagnostics：Protocol Read / Write、Point Read、Subnet Scan
// ---------------------------------------------------------------------------
export function protocolReadPoint(
  deviceId: string,
  pointId: string,
): ProtocolReadResult | { error: { code: string; message: string } } {
  const d = findDevice(deviceId)
  if (!d)
    return { error: { code: MOCK_ERROR_CODES.NOT_FOUND, message: `device ${deviceId} not found` } }
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  const model = modelOf(d)
  const points = pointsOfTable(model?.point_table ?? '')
  const index = points.findIndex((p) => p.point_id === pointId)
  if (index < 0)
    return { error: { code: MOCK_ERROR_CODES.NOT_FOUND, message: `point ${pointId} not found` } }
  if (runtime.network !== 'ok') {
    const code =
      runtime.network === 'disabled'
        ? MOCK_ERROR_CODES.DEVICE_DISABLED
        : MOCK_ERROR_CODES.READ_TIMEOUT
    return {
      error: {
        code,
        message:
          runtime.network === 'disabled'
            ? 'Device is disabled'
            : 'Read request timed out after 3000 ms',
      },
    }
  }
  if (runtime.protocolError) return { error: runtime.protocolError }
  if (runtime.failingPointIndexes.includes(index)) {
    return {
      error: {
        code: MOCK_ERROR_CODES.POINT_READ_FAILED,
        message: 'Device returned no data for this point',
      },
    }
  }
  const point = points[index]
  const epochSec = epochSecNow()
  const raw =
    point.data_type === 'bool'
      ? pointBoolAt(d.device_id, pointId, index, epochSec)
      : pointValueAt(d.device_id, pointId, index, epochSec)
  return {
    device_id: d.device_id,
    point_id: pointId,
    value: engineeringValue(point, raw),
    quality: 'good',
    source: model?.protocol ?? '',
    timestamp: new Date(epochSec * 1000).toISOString(),
  }
}

/** Command / Protocol Write 共用的底层写操作：跨页面联动核心。 */
export function writeDevicePoint(
  deviceId: string,
  pointId: string,
  value: number | boolean,
): CommandResult {
  const d = findDevice(deviceId)!
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  const model = modelOf(d)
  const protocol = model?.protocol ?? 'device'
  const base = {
    command_id: `mock-cmd-${hash32(`${deviceId}:${pointId}:${epochSecNow()}`) % 100000}`,
    requested: value,
    sent_at: nowIso(),
    finished_at: nowIso(),
  }
  const fail = (code: string, message: string, latency: number): CommandResult => {
    appendLog('ERROR', protocol, d.device_id, `command failed: ${code} (${pointId})`)
    return {
      ...base,
      success: false,
      error: `${code}: ${message}`,
      readback: null,
      readback_error: message,
      readback_quality: 'bad',
      readback_timestamp: null,
      latency_ms: latency,
    }
  }
  if (runtime.network !== 'ok') {
    return runtime.network === 'disabled'
      ? fail(MOCK_ERROR_CODES.DEVICE_DISABLED, 'Device is disabled', 6)
      : fail(MOCK_ERROR_CODES.COMMAND_TIMEOUT, 'No response within 3000 ms', 3000)
  }
  if (runtime.protocolError)
    return fail(runtime.protocolError.code, runtime.protocolError.message, 46)
  if (runtime.commandRejected) {
    return fail(MOCK_ERROR_CODES.COMMAND_REJECTED, 'Write request rejected by device', 3000)
  }
  // 成功：写入 override → Data / Trend / Diagnostics Read 立即读到新值；Logs 追加。
  // command 值是工程量，override 存原始量（value 经 scale/offset 还原），
  // 读取路径 engineeringValue(raw) 后正好回到 command 写入的值。
  const pointDef = pointsOfTable(model?.point_table ?? '').find((p) => p.point_id === pointId)
  const rawValue =
    typeof value === 'number' && pointDef && pointDef.scale !== 0
      ? (value - pointDef.offset) / pointDef.scale
      : value
  setCommandOverride(d.device_id, pointId, rawValue)
  appendLog('INFO', protocol, d.device_id, `command applied: ${pointId} → ${String(value)}`)
  return {
    ...base,
    success: true,
    error: null,
    readback: value,
    readback_error: null,
    readback_quality: 'good',
    readback_timestamp: nowIso(),
    latency_ms: 160 + (hash32(d.device_id) % 6) * 7,
  }
}

export interface SubnetHostRow {
  ip: string
  ping: boolean
  ads: boolean
  modbus: boolean
  iec104: boolean
  object: string
}

export function subnetScanResults(network: string): SubnetHostRow[] {
  const prefix = network.replace(/\.\d+\/\d+$/, '')
  return Array.from({ length: 32 }, (_, i) => {
    const ip = `${prefix}.${i * 8 + 1}`
    const known = mockState.devices.find((d) => d.host === ip)
    const reachable = known
      ? deviceRuntimeState(known.device_id, true).network === 'ok'
      : i % 23 === 0 || i % 37 === 0
    const protocol = known ? modelOf(known)?.protocol : ''
    return {
      ip,
      ping: reachable,
      ads: reachable && (protocol === 'ads' || (!known && i % 37 === 0)),
      modbus: reachable && (protocol === 'modbus' || (!known && i % 23 === 0)),
      iec104: reachable && protocol === 'iec104',
      object: known?.device_id ?? '',
    }
  })
}

// ---------------------------------------------------------------------------
// Tasks：Start / Stop 状态机（STOPPED→STARTING→RUNNING / RUNNING→STOPPING→STOPPED）
// ---------------------------------------------------------------------------
export type TaskStartOutcome =
  { ok: true; task: TaskDto } | { ok: false; status: number; code: string; message: string }

export function startTaskById(taskId: string): TaskStartOutcome {
  const t = mockState.tasks.find((x) => x.task_id === taskId)
  if (!t)
    return {
      ok: false,
      status: 404,
      code: MOCK_ERROR_CODES.NOT_FOUND,
      message: `task ${taskId} not found`,
    }
  const runtime = taskRuntimeOf(t.task_id)
  if (!t.enabled) {
    runtime.phase = 'stopped'
    return {
      ok: false,
      status: 409,
      code: MOCK_ERROR_CODES.TASK_DISABLED,
      message: 'Task is disabled',
    }
  }
  const invalid = taskInvalidReason(t)
  if (invalid) {
    runtime.phase = 'stopped'
    appendLog('ERROR', 'task', t.task_id, `start failed: ${invalid}`)
    return {
      ok: false,
      status: 409,
      code: MOCK_ERROR_CODES.CONFIG_VALIDATION_FAILED,
      message: invalid,
    }
  }
  const unavailable = targetsOfTask(t).find(
    (d) => deviceRuntimeState(d.device_id, d.enabled).network === 'unreachable',
  )
  if (unavailable) {
    // 启动失败：回滚 STOPPED + 稳定错误 + Logs
    runtime.phase = 'stopped'
    appendLog(
      'ERROR',
      'task',
      t.task_id,
      `start failed: device ${unavailable.device_id} unreachable`,
    )
    return {
      ok: false,
      status: 409,
      code: MOCK_ERROR_CODES.TASK_DEVICE_UNAVAILABLE,
      message: `device ${unavailable.device_id} unreachable`,
    }
  }
  runtime.phase = 'starting'
  runtime.readyAt = Date.now() + 8000
  appendLog('INFO', 'task', t.task_id, 'starting')
  return { ok: true, task: taskDto(t) }
}

export function stopTaskById(taskId: string): TaskStartOutcome {
  const t = mockState.tasks.find((x) => x.task_id === taskId)
  if (!t)
    return {
      ok: false,
      status: 404,
      code: MOCK_ERROR_CODES.NOT_FOUND,
      message: `task ${taskId} not found`,
    }
  const runtime = taskRuntimeOf(t.task_id)
  runtime.phase = 'stopping'
  runtime.readyAt = Date.now() + 4000
  appendLog('INFO', 'task', t.task_id, 'stopping')
  return { ok: true, task: taskDto(t) }
}

// ---------------------------------------------------------------------------
// Sinks：分阶段 Verify + Write Test（计数器 / Quality / Logs 联动）
// ---------------------------------------------------------------------------
interface SinkCheckStep {
  layer: string
  name: string
  target: string
  detail: string
  blocks: boolean
}

function sinkEndpoint(s: MockSink): { host: string; port: number } {
  if (s.type === 'kafka') {
    const first = String(s.connection.bootstrap_servers || 'localhost:9092')
      .split(',')[0]
      .trim()
    const [host, portText] = first.split(':')
    return { host: host || 'localhost', port: Number(portText || 9092) }
  }
  if (s.type === 'db') {
    const match = String(s.connection.dsn || '').match(
      /^[a-zA-Z0-9+.-]+:\/\/(?:[^@/]+@)?([^:/]+)(?::(\d+))?/,
    )
    return { host: match?.[1] || 'localhost', port: Number(match?.[2] || 5432) }
  }
  return { host: '', port: 0 }
}

function resolvedHost(host: string): string {
  if (host === 'localhost') return '127.0.0.1'
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) return host
  return '192.168.10.25'
}

export function sinkCheckPlan(s: MockSink): SinkCheckStep[] {
  if (s.type === 'file') {
    const path = String(s.connection.path || '')
    const parent = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) || '/' : '.'
    return [
      {
        layer: 'filesystem',
        name: 'Parent Path',
        target: parent,
        detail: 'Resolve parent directory',
        blocks: true,
      },
      {
        layer: 'filesystem',
        name: 'Permission',
        target: parent,
        detail: 'Check create/append permission',
        blocks: true,
      },
      {
        layer: 'filesystem',
        name: 'Open / Append',
        target: path,
        detail: 'Open target for append without business payload',
        blocks: true,
      },
    ]
  }
  const ep = sinkEndpoint(s)
  const ip = resolvedHost(ep.host)
  const common: SinkCheckStep[] = [
    {
      layer: 'network',
      name: 'DNS',
      target: ep.host,
      detail: `Resolve ${ep.host} → ${ip}`,
      blocks: true,
    },
    {
      layer: 'network',
      name: 'ICMP',
      target: ip,
      detail: 'ICMP reachability check (advisory only)',
      blocks: false,
    },
    {
      layer: 'network',
      name: 'TCP Port',
      target: `${ip}:${ep.port}`,
      detail: 'Open TCP connection to remote endpoint',
      blocks: true,
    },
  ]
  if (s.type === 'kafka') {
    return [
      ...common,
      {
        layer: 'protocol',
        name: 'Broker Session',
        target: `${ep.host}:${ep.port}`,
        detail: 'Establish Kafka producer / broker session',
        blocks: true,
      },
      {
        layer: 'target',
        name: 'Metadata',
        target: String(s.connection.topic || ''),
        detail: 'Request broker metadata',
        blocks: true,
      },
      {
        layer: 'target',
        name: 'Topic',
        target: String(s.connection.topic || ''),
        detail: 'Verify configured topic is addressable',
        blocks: true,
      },
    ]
  }
  return [
    ...common,
    {
      layer: 'protocol',
      name: 'PostgreSQL Session',
      target: `${ep.host}:${ep.port}`,
      detail: 'Establish PostgreSQL protocol session',
      blocks: true,
    },
    {
      layer: 'protocol',
      name: 'Authentication',
      target: ep.host,
      detail: 'Authenticate configured DSN credentials',
      blocks: true,
    },
    {
      layer: 'target',
      name: 'SELECT 1',
      target: String(s.connection.table || 'points'),
      detail: 'Execute lightweight read-only query',
      blocks: true,
    },
  ]
}

/** Sink Verify：分阶段 passed / warning / failed / skipped；失败步骤阻断后续。 */
export async function verifySinkByName(
  name: string,
): Promise<SinkTestResult | { error: { code: string; message: string } }> {
  const s = mockState.sinks.find((x) => x.name === name)
  if (!s) return { error: { code: MOCK_ERROR_CODES.NOT_FOUND, message: `sink ${name} not found` } }
  const scenario = sinkScenarios[s.name]
  const runtime = sinkRuntimeOf(s.name)
  const steps: Record<string, unknown>[] = []
  let blocked = false
  for (const step of sinkCheckPlan(s)) {
    await sleep(LATENCY.sinkCheckStep)
    if (blocked) {
      steps.push({
        layer: step.layer,
        name: step.name,
        state: 'skipped',
        target: step.target,
        latency_ms: 0,
        detail: 'Skipped after upstream failure',
        error_code: '',
      })
      continue
    }
    if (!s.enabled) {
      steps.push({
        layer: step.layer,
        name: step.name,
        state: 'skipped',
        target: step.target,
        latency_ms: 0,
        detail: 'Sink is disabled',
        error_code: MOCK_ERROR_CODES.SINK_DISABLED,
      })
      blocked = true
      continue
    }
    // Kafka 的 ICMP 仅 advisory：无回复但 TCP 为准 → warning 不阻塞
    if (step.name === 'ICMP' && s.type === 'kafka') {
      steps.push({
        layer: step.layer,
        name: step.name,
        state: 'warning',
        target: step.target,
        latency_ms: 1000,
        detail: 'No ICMP reply; continue because TCP is authoritative',
        error_code: 'ICMP_NO_REPLY',
      })
      continue
    }
    if (scenario && s.enabled && step.name === scenario.failingCheck) {
      steps.push({
        layer: step.layer,
        name: step.name,
        state: 'failed',
        target: step.target,
        latency_ms: scenario.latencyMs,
        detail: scenario.message,
        error_code: scenario.code,
      })
      if (step.blocks) blocked = true
      continue
    }
    steps.push({
      layer: step.layer,
      name: step.name,
      state: 'passed',
      target: step.target,
      latency_ms: 2 + steps.length * 4,
      detail: `${step.detail} passed`,
      error_code: '',
    })
  }
  const failedStep = steps.find((c) => c.state === 'failed') as { error_code?: string } | undefined
  const failed = !!failedStep || !s.enabled
  const passed = steps.filter((c) => c.state === 'passed').length
  runtime.latency_ms = Math.max(0, ...steps.map((c) => Number(c.latency_ms)))
  runtime.error = failed ? String(failedStep?.error_code ?? MOCK_ERROR_CODES.SINK_DISABLED) : ''
  appendLog(
    failed ? 'ERROR' : 'INFO',
    'sink',
    s.name,
    failed
      ? `verification failed: ${runtime.error}`
      : `verification passed (${passed}/${steps.length})`,
  )
  return {
    success: !failed,
    latency_ms: runtime.latency_ms,
    message: failed
      ? `verification failed: ${runtime.error}`
      : `verification passed (${passed}/${steps.length})`,
    steps,
  }
}

/** Write Test：成功更新 last_write_at/writes_total/latency；失败更新 failures/dropped/error 并影响 Quality 与 Logs。 */
export async function writeTestSinkByName(
  name: string,
): Promise<SinkTestResult | { error: { code: string; message: string } }> {
  const s = mockState.sinks.find((x) => x.name === name)
  if (!s) return { error: { code: MOCK_ERROR_CODES.NOT_FOUND, message: `sink ${name} not found` } }
  await sleep(LATENCY.command)
  const runtime = sinkRuntimeOf(s.name)
  if (!s.enabled) {
    runtime.failures_total += 1
    runtime.error = MOCK_ERROR_CODES.SINK_DISABLED
    appendLog('ERROR', 'sink', s.name, 'write test failed: sink is disabled')
    return {
      success: false,
      latency_ms: 4,
      message: 'Sink is disabled; enable it before writing.',
      steps: [],
    }
  }
  const scenario = sinkScenarios[s.name]
  if (scenario) {
    runtime.failures_total += 1
    runtime.dropped_points += 1
    runtime.error = scenario.code
    appendLog('ERROR', 'sink', s.name, `write test failed: ${scenario.message}`)
    return {
      success: false,
      latency_ms: scenario.latencyMs,
      message: `${scenario.code}: ${scenario.message}`,
      steps: [],
    }
  }
  runtime.last_write_at = nowIso()
  runtime.writes_total += 1
  runtime.latency_ms = 18
  runtime.error = ''
  runtime.queue_depth = 0
  appendLog('INFO', 'sink', s.name, 'write test accepted (synthetic PointValue)')
  return {
    success: true,
    latency_ms: 18,
    message: 'Synthetic PointValue accepted by the sink.',
    steps: [{ step: 'write', state: 'passed', latency_ms: 18 }],
  }
}

// ---------------------------------------------------------------------------
// Settings / Admin State
// ---------------------------------------------------------------------------
export function updateSettings(body: components['schemas']['SettingsRequest']): ConfigApplyDto {
  mockState.settings = { ...mockState.settings, ...body }
  const revision = pushConfigRevision('settings', 'update settings via API')
  appendLog('INFO', 'config', 'system.yaml', `settings updated (revision ${revision})`)
  return { success: true, errors: [], revision, rollback_performed: false }
}

export function replaceAdminState(
  body: components['schemas']['AdminStateRequest'],
): ConfigApplyDto {
  for (const item of body.devices) {
    const d = findDevice(item.device_id)
    if (d) {
      d.enabled = item.enabled
      d.host = item.host
      d.port = item.port ?? null
      d.device_group = item.device_group ?? d.device_group
    }
  }
  for (const item of body.tasks) {
    const t = mockState.tasks.find((x) => x.task_id === item.task_id)
    if (t) {
      t.enabled = item.enabled
      t.interval = item.interval ?? null
      t.point_group = item.point_group
      t.sinks = [...(item.sinks ?? t.sinks)]
    }
  }
  for (const item of body.sinks) {
    const s = mockState.sinks.find((x) => x.name === item.name)
    if (s) {
      s.enabled = item.enabled
      s.connection = { ...item.connection }
      if (!item.enabled) sinkRuntimeOf(s.name).error = ''
    }
  }
  // 结构化 state → YAML working/applied 同步（Devices / Tasks 页立即可见）
  mockState.workingTexts['devices.yaml'] = devicesYaml(mockState.devices)
  mockState.appliedTexts['devices.yaml'] = mockState.workingTexts['devices.yaml']
  mockState.workingTexts['tasks.yaml'] = tasksYaml(mockState.tasks)
  mockState.appliedTexts['tasks.yaml'] = mockState.workingTexts['tasks.yaml']
  const revision = pushConfigRevision('admin-state', 'replace admin state via API')
  appendLog('INFO', 'config', 'admin-state', `admin state saved (revision ${revision})`)
  return { success: true, errors: [], revision, rollback_performed: false }
}

// ---------------------------------------------------------------------------
// Config：Validate / Apply / Import / Restore / Backup（三层状态）
// ---------------------------------------------------------------------------
export function pushConfigRevision(source: string, comment: string): number {
  const revision = nextRevision()
  mockState.revisions.unshift({ revision, created_at: nowIso(), source, comment })
  mockState.revisionSnapshots.set(revision, { ...mockState.appliedTexts })
  return revision
}

/** 确定性校验规则：空文档 / Tab 缩进 / 重复 device_id / 未知引用。 */
export function validateConfigText(name: string, text: string): ConfigReviewDto {
  const errors: string[] = []
  if (!text.trim()) {
    errors.push(`${name}: document is empty`)
  } else {
    const tabLine = text.split('\n').findIndex((line) => line.includes('\t'))
    if (tabLine >= 0)
      errors.push(
        `${name}:${tabLine + 1}: YAML syntax error — tab characters are not allowed for indentation`,
      )
    if (name === 'devices.yaml') {
      const ids = [...text.matchAll(/device_id:\s*(\S+)/g)].map((m) => m[1])
      const seen = new Set<string>()
      for (const id of ids) {
        if (seen.has(id)) errors.push(`${name}: duplicate device_id "${id}"`)
        seen.add(id)
      }
      for (const model of [...text.matchAll(/^\s+model:\s*(\S+)/gm)].map((m) => m[1])) {
        if (
          !Object.keys(modelOf({ model } as MockDevice) ?? {}).length &&
          !modelNameExists(model)
        ) {
          errors.push(`${name}: unknown device model "${model}"`)
        }
      }
    }
    if (name === 'tasks.yaml') {
      for (const sinkName of [...text.matchAll(/^\s+-\s*(\S+)$/gm)].map((m) => m[1])) {
        if (!mockState.sinks.some((s) => s.name === sinkName))
          errors.push(`${name}: unknown sink "${sinkName}"`)
      }
    }
    if (name === 'points.yaml') {
      for (const parent of [...text.matchAll(/extends:\s*(\S+)/g)].map((m) => m[1])) {
        if (
          !Object.keys(mockState.workingTexts).includes('points.yaml') ||
          !pointTableExists(parent)
        ) {
          errors.push(`${name}: unknown point table "${parent}" in extends`)
        }
      }
    }
  }
  const applied = mockState.appliedTexts[name] ?? ''
  return {
    name,
    valid: errors.length === 0,
    changed: text !== applied,
    errors,
    diff: { applied_lines: applied.split('\n').length, working_lines: text.split('\n').length },
  }
}

function modelNameExists(model: string): boolean {
  return (
    mockState.devices.some((d) => d.model === model) ||
    ['beckhoff_wtg', 'modbus_wtg', 'iec104_wtg', 'pcs_modbus_a'].includes(model)
  )
}

function pointTableExists(table: string): boolean {
  return mockState.workingTexts['points.yaml']?.includes(`  ${table}:`) ?? false
}

/** system.yaml 应用后回写 settings（Settings / Header / Overview 同步反映）。 */
function applySystemYamlToState(text: string): void {
  const pick = (re: RegExp) => text.match(re)?.[1]?.trim()
  const unquote = (v: string | undefined) => v?.replace(/^"|"$/g, '') ?? ''
  const siteId = unquote(pick(/site_id:\s*("?[^\s"]+"?)/))
  const siteName = unquote(pick(/^\s+name:\s*("?[^"\n]+"?)\s*$/m))
  if (siteId) mockState.settings.site_id = siteId
  if (siteName) mockState.settings.site_name = siteName
  const ams = unquote(pick(/local_ams_net_id:\s*("?[^\s"]+"?)/))
  const ip = unquote(pick(/local_ip:\s*("?[^\s"]+"?)/))
  if (ams) mockState.settings.ads_local_ams_net_id = ams
  if (ip) mockState.settings.ads_local_ip = ip
  const apiPort = pick(/port:\s*(\d+)/)
  if (apiPort) mockState.settings.api_port = Number(apiPort)
}

/** devices.yaml 应用后回写设备 host / enabled（Devices 页立即反映）。 */
function applyDevicesYamlToState(text: string): void {
  const blocks = text.split(/(?=^\s*-\s+device_id:)/m).filter((b) => /device_id:/.test(b))
  for (const block of blocks) {
    const id = block.match(/device_id:\s*(\S+)/)?.[1]
    if (!id) continue
    const device = findDevice(id)
    if (!device) continue
    const host = block.match(/host:\s*"?([0-9.]+)"?/)?.[1]
    const enabledText = block.match(/enabled:\s*(true|false)/)?.[1]
    if (host) device.host = host
    if (enabledText) device.enabled = enabledText === 'true'
  }
}

export function applyConfigText(
  name: string,
  text: string,
  comment: string,
  source: string,
): ConfigApplyDto {
  const review = validateConfigText(name, text)
  if (!review.valid) {
    appendLog('ERROR', 'config', name, `${source} rejected: ${review.errors[0]}`)
    return { success: false, errors: review.errors, revision: null, rollback_performed: false }
  }
  mockState.workingTexts[name] = text
  mockState.appliedTexts[name] = text
  if (name === 'system.yaml') applySystemYamlToState(text)
  if (name === 'devices.yaml') applyDevicesYamlToState(text)
  const revision = pushConfigRevision(source, comment || `${source} ${name}`)
  appendLog('INFO', 'config', name, `${source} applied as revision ${revision}`)
  return { success: true, errors: [], revision, rollback_performed: false }
}

export type ImportOutcome =
  { ok: true; body: ConfigApplyDto } | { ok: false; status: number; code: string; message: string }

export function importConfigText(name: string, text: string, comment: string): ImportOutcome {
  if (!text.trim()) {
    return {
      ok: false,
      status: 422,
      code: MOCK_ERROR_CODES.CONFIG_VALIDATION_FAILED,
      message: `${name}: document is empty`,
    }
  }
  if (text === mockState.appliedTexts[name]) {
    // 相同内容 → No Changes（成功但无新 revision）
    return {
      ok: true,
      body: { success: true, errors: [], revision: null, rollback_performed: false },
    }
  }
  return { ok: true, body: applyConfigText(name, text, comment, 'import') }
}

export function restoreConfigRevision(
  revision: number,
): ConfigApplyDto | { error: { code: string; message: string } } {
  const snapshot = mockState.revisionSnapshots.get(revision)
  if (!snapshot)
    return {
      error: { code: MOCK_ERROR_CODES.NOT_FOUND, message: `revision ${revision} not found` },
    }
  // Restore 生成新 revision，不篡改旧 revision
  mockState.appliedTexts = { ...snapshot }
  mockState.workingTexts = { ...snapshot }
  const systemText = snapshot['system.yaml']
  if (systemText) applySystemYamlToState(systemText)
  const devicesText = snapshot['devices.yaml']
  if (devicesText) applyDevicesYamlToState(devicesText)
  const newRevision = pushConfigRevision('restore', `restore revision ${revision}`)
  appendLog('INFO', 'config', 'history', `restored revision ${revision} as ${newRevision}`)
  return { success: true, errors: [], revision: newRevision, rollback_performed: false }
}

/** Backup 基于 Applied State（不是 Working Copy）。 */
export function configBackupText(): string {
  return Object.entries(mockState.appliedTexts)
    .map(([name, text]) => `# ===== ${name} =====\n${text}`)
    .join('\n')
}

export { pollOperation }
