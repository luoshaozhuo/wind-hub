// 设备协议连接表单领域纯函数：ADS / Modbus / IEC104 连接参数的默认值、
// 表单映射、覆盖序列化与校验的唯一维护位置。
// UI form model（ConnectionFormState）与 generated OpenAPI DTO、后端
// connection_defaults/extensions wire 结构显式分离；转换只经由本模块。
import type { DeviceModelDef, Protocol } from './types'

/**
 * 统一的连接表单模型：包含全部协议字段，渲染时按 protocol 取子集。
 * 字段名与后端 extensions / connection_defaults wire 键一致（不含 host/port
 * 身份字段，port 作为表单数值字段单独参与覆盖比较）。
 */
export interface ConnectionFormState {
  port: number
  timeout: number
  target_net_id: string
  twincat_version: string
  unit_id: number
  mode: string
  word_order: string
  common_addr: number
  k: number
  w: number
  t0: number
  t1: number
  t2: number
  t3: number
  max_reconnect_retries: number
  reconnect_max_retries: number
  reconnect_backoff_max: number
}

/** 各协议默认端口（TwinCAT 2 ADS / Modbus TCP / IEC 60870-5-104）。 */
export const PROTOCOL_DEFAULT_PORTS: Record<Protocol, number> = {
  ads: 801,
  modbus: 502,
  iec104: 2404,
}

/**
 * 各协议参与“设备级覆盖”的 extension 键（与 model connection_defaults 比较，
 * 有差异才落 overrides）。ADS target_net_id 属身份键，始终单独写入。
 */
export const PROTOCOL_OVERRIDE_KEYS: Record<Protocol, readonly string[]> = {
  ads: ['twincat_version', 'timeout'],
  modbus: ['unit_id', 'mode', 'timeout', 'word_order'],
  iec104: ['common_addr', 'k', 'w', 't0', 't1', 't2', 't3', 'max_reconnect_retries'],
}

/** 各协议序列化为 model connection_defaults 的键。 */
export const PROTOCOL_DEFAULT_KEYS: Record<Protocol, readonly string[]> = {
  ads: ['port', 'timeout', 'twincat_version', 'reconnect_max_retries', 'reconnect_backoff_max'],
  modbus: [
    'port',
    'timeout',
    'unit_id',
    'mode',
    'word_order',
    'reconnect_max_retries',
    'reconnect_backoff_max',
  ],
  iec104: ['port', 'common_addr', 'k', 'w', 't0', 't1', 't2', 't3', 'max_reconnect_retries'],
}

function num(value: unknown, fallback: number): number {
  const n = Number(value)
  return Number.isFinite(n) && value !== undefined && value !== null && value !== '' ? n : fallback
}

function str(value: unknown, fallback: string): string {
  return typeof value === 'string' && value !== '' ? value : fallback
}

/** 协议无关的全量默认值（port 按协议）。 */
export function defaultConnectionForm(protocol: Protocol): ConnectionFormState {
  return {
    port: PROTOCOL_DEFAULT_PORTS[protocol],
    timeout: 3,
    target_net_id: '',
    twincat_version: '2',
    unit_id: 1,
    mode: 'tcp',
    word_order: 'little_endian',
    common_addr: 1,
    k: 12,
    w: 8,
    t0: 30,
    t1: 15,
    t2: 10,
    t3: 20,
    max_reconnect_retries: 5,
    reconnect_max_retries: 5,
    reconnect_backoff_max: 30,
  }
}

/**
 * 从合成连接（model connection_defaults + 设备覆盖）填充表单，
 * 数值字段做 Number 规范化，空值回落协议默认值。
 */
export function formFromConnection(
  protocol: Protocol,
  connection: Record<string, unknown>,
): ConnectionFormState {
  const base = defaultConnectionForm(protocol)
  return {
    port: num(connection.port, base.port),
    timeout: num(connection.timeout, base.timeout),
    target_net_id: str(connection.target_net_id, ''),
    twincat_version: str(connection.twincat_version, base.twincat_version),
    unit_id: num(connection.unit_id, base.unit_id),
    mode: str(connection.mode, base.mode),
    word_order: str(connection.word_order, base.word_order),
    common_addr: num(connection.common_addr, base.common_addr),
    k: num(connection.k, base.k),
    w: num(connection.w, base.w),
    t0: num(connection.t0, base.t0),
    t1: num(connection.t1, base.t1),
    t2: num(connection.t2, base.t2),
    t3: num(connection.t3, base.t3),
    max_reconnect_retries: num(connection.max_reconnect_retries, base.max_reconnect_retries),
    reconnect_max_retries: num(connection.reconnect_max_retries, base.reconnect_max_retries),
    reconnect_backoff_max: num(connection.reconnect_backoff_max, base.reconnect_backoff_max),
  }
}

/** 从 model connection_defaults 填充表单（Manage Metadata 编辑模型默认值）。 */
export function formFromModelDefaults(model: DeviceModelDef): ConnectionFormState {
  return formFromConnection(model.protocol, model.connection_defaults || {})
}

/** 由 host 推导 ADS Target AMS Net ID（IPv4 host 追加 .1.1）。 */
export function amsNetIdFromHost(host: string): string {
  return `${host.trim()}.1.1`
}

/**
 * model 切换时把 model connection_defaults 应用到表单（就地修改）。
 * - port 以 model defaults 为准，无默认时回落协议默认端口（不保留旧协议端口）。
 * - ADS：target_net_id 为空且 host 非空时按 host 推导。
 */
export function applyModelDefaults(
  form: ConnectionFormState,
  model: DeviceModelDef,
  host = '',
): void {
  const defaults = model.connection_defaults || {}
  const fresh = formFromConnection(model.protocol, defaults)
  form.port = num(defaults.port, fresh.port)
  if (model.protocol === 'ads') {
    form.twincat_version = fresh.twincat_version
    form.timeout = fresh.timeout
    if (!form.target_net_id && host.trim()) form.target_net_id = amsNetIdFromHost(host)
  } else if (model.protocol === 'modbus') {
    form.unit_id = fresh.unit_id
    form.mode = fresh.mode
    form.timeout = fresh.timeout
    form.word_order = fresh.word_order
  } else if (model.protocol === 'iec104') {
    form.common_addr = fresh.common_addr
    form.k = fresh.k
    form.w = fresh.w
    form.t0 = fresh.t0
    form.t1 = fresh.t1
    form.t2 = fresh.t2
    form.t3 = fresh.t3
    form.max_reconnect_retries = fresh.max_reconnect_retries
  }
}

/** 仅切换协议时重置协议相关默认（port / ADS TwinCAT 版本），其余字段保留。 */
export function resetFormForProtocol(form: ConnectionFormState, protocol: Protocol): void {
  form.port = PROTOCOL_DEFAULT_PORTS[protocol]
  if (protocol === 'ads') form.twincat_version = '2'
}

/** 连接表单校验：返回错误消息，空串表示通过。 */
export function validateConnectionForm(form: ConnectionFormState, protocol: Protocol): string {
  if (protocol === 'ads' && !form.target_net_id.trim()) {
    return 'Target AMS Net ID is required for ADS'
  }
  return ''
}

export interface ConnectionOverrides {
  /** 相对 model 默认值有差异的 extension 覆盖（ADS 含身份键 target_net_id）。 */
  extensions: Record<string, unknown>
  /** 与 model 默认端口不同时的设备级端口覆盖。 */
  port: number | undefined
}

/**
 * 表单 → 设备级覆盖序列化：与 model connection_defaults 逐项比较，
 * 仅差异项落 extensions；ADS target_net_id 作为身份键始终写入。
 */
export function connectionOverridesFromForm(
  form: ConnectionFormState,
  model: DeviceModelDef,
): ConnectionOverrides {
  const defaults = model.connection_defaults || {}
  const extensions: Record<string, unknown> = {}
  for (const key of PROTOCOL_OVERRIDE_KEYS[model.protocol]) {
    if (defaults[key] !== form[key as keyof ConnectionFormState]) {
      extensions[key] = form[key as keyof ConnectionFormState]
    }
  }
  if (model.protocol === 'ads') extensions.target_net_id = form.target_net_id.trim()
  const port = num(defaults.port, 0) !== num(form.port, 0) ? form.port || undefined : undefined
  return { extensions, port }
}

/** 表单 → model connection_defaults 序列化（Manage Metadata 保存模型）。 */
export function connectionDefaultsFromForm(
  form: ConnectionFormState,
  protocol: Protocol,
): Record<string, unknown> {
  const result: Record<string, unknown> = {}
  for (const key of PROTOCOL_DEFAULT_KEYS[protocol]) {
    result[key] = form[key as keyof ConnectionFormState]
  }
  return result
}
