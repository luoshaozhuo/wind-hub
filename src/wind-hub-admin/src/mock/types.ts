// 与 wind-hub 配置 schema（src/wind_hub/config/schema.py、config/loader.py、
// adapter/outbound/protocol/*/config.py）对齐的前端 mock 类型定义。
// 字段名与取值集合均以后端真实代码为准，不另行发明。

export type Protocol = 'ads' | 'modbus' | 'iec104'
export const PROTOCOLS: Protocol[] = ['ads', 'modbus', 'iec104']

export const DATA_TYPES = [
  'float32', 'float64',
  'int8', 'int16', 'int32', 'int64',
  'uint8', 'uint16', 'uint32', 'uint64',
  'bool', 'str',
]

export const MODBUS_REGISTER_TYPES = ['holding', 'input', 'coil', 'discrete_input']
export const ADS_READ_MODES = ['sum', 'sequential']

export interface UnitDef {
  symbol: string
  name: string
}

export interface DeviceTypeDef {
  id: string
  name: string
}

export interface DeviceGroupDef {
  id: string
  device_type: string
}

export interface DeviceModelDef {
  id: string
  device_type: string
  manufacturer: string
  model?: string
  protocol: Protocol
  point_table: string
  read_mode: string
  properties: Record<string, unknown>
  connection_defaults: Record<string, unknown>
}

export interface PointTableDef {
  id: string
  protocol: Protocol
  extends: string
  system?: boolean
}

export interface PointGroupDef {
  id: string
  name: string
  system?: boolean
}

export interface PointAddress {
  symbol?: string
  index_group?: string
  index_offset?: string
  type?: string
  address?: number
  ioa?: number
}

export interface PointDef {
  point_id: string
  variable_name: string
  point_groups: string[]
  address: PointAddress
  data_type: string
  scale: number
  offset: number
  unit: string
  description: string
}

export interface DeviceInst {
  device_id: string
  model: string
  device_group: string
  host: string
  port?: number
  extensions: Record<string, unknown>
  enabled: boolean
  online: boolean
}

export interface TaskDef {
  task_id: string
  device: string
  device_group: string
  point_group: string
  interval: number | null
  sinks: string[]
  enabled: boolean
  runtime: string
  valid?: boolean
  invalid_reason?: string
}

export interface SinkDef {
  name: string
  type: string
  enabled: boolean
}

export type VerifyStepState = 'unknown' | 'checking' | 'success' | 'partial' | 'failed'
export type DeviceVerifyState = 'idle' | 'running' | 'success' | 'warning' | 'failed'

export interface DeviceVerifyError {
  stage: 'network' | 'protocol' | 'points'
  target: string
  message: string
}

export interface DeviceVerification {
  state: DeviceVerifyState
  network: VerifyStepState
  protocol: VerifyStepState
  points: VerifyStepState
  point_total: number
  point_success: number
  point_failed: number
  verified_at: string
  latency_ms: number
  errors: DeviceVerifyError[]
}
