// 与 wind-hub 配置 schema（src/wind_hub/config/schema.py、config/loader.py、
// adapter/outbound/protocol/*/config.py）对齐的前端 mock 类型定义。
// 字段名与取值集合均以后端真实代码为准，不另行发明。

export type Protocol = 'ads' | 'modbus' | 'iec104'
// schema.SUPPORTED_PROTOCOLS
export const PROTOCOLS: Protocol[] = ['ads', 'modbus', 'iec104']

// schema.ALLOWED_DATA_TYPES（协议无关）
export const DATA_TYPES = [
  'float32', 'float64',
  'int8', 'int16', 'int32', 'int64',
  'uint8', 'uint16', 'uint32', 'uint64',
  'bool', 'str',
]

// modbus/mapping.py 的 canonical register type（别名 discrete/input_register/
// holding_register 在 loader 归一化后等价，编辑表单只暴露 canonical 名）
export const MODBUS_REGISTER_TYPES = ['holding', 'input', 'coil', 'discrete_input']

// schema.ADS_READ_MODES（仅 ADS 型号可配置）
export const ADS_READ_MODES = ['sum', 'sequential']

export interface UnitDef {
  symbol: string
  name: string
}

export interface DeviceTypeDef {
  id: string
  name: string
}

// device_group 在 schema 中是实例/Task 上的自由字符串；此处为原型浏览维护
// 「group → 业务类型」的归属关系（与实例 model.device_type 保持一致）
export interface DeviceGroupDef {
  id: string
  device_type: string
}

// DeviceModelConfig
export interface DeviceModelDef {
  id: string
  device_type: string
  manufacturer: string
  protocol: Protocol
  point_table: string
  read_mode: string // 仅 ADS：sum / sequential；其他协议为空串
}

// PointTableConfig（points.yaml 的表定义；extends 为单继承父表）
export interface PointTableDef {
  id: string
  protocol: Protocol
  extends: string
}

export interface PointGroupDef {
  id: string
  name: string
}

// PointAddress：schema 为 extra="allow"，各协议字段见 config/loader.py 校验
// - ADS：symbol 单独合法；index_group + index_offset 必须成对；两者可共存（symbol 优先）
// - Modbus：type（register type）+ address（0-based）
// - IEC104：ioa ∈ [0, 0xFFFFFF]
export interface PointAddress {
  symbol?: string
  index_group?: string
  index_offset?: string
  type?: string
  address?: number
  ioa?: number
}

// PointConfig
export interface PointDef {
  point_id: string
  variable_name: string
  point_groups: string[]
  address: PointAddress
  data_type: string
  scale: number
  offset: number
  unit: string // unit ID（units.yaml 的键）
  description: string
}

// DeviceInstanceConfig（endpoint 展开为 host/port，extensions 省略）
export interface DeviceInst {
  device_id: string
  model: string
  device_group: string
  host: string
  port?: number
  enabled: boolean
  online: boolean // 运行态 mock，非配置字段
}

// CollectionTaskConfig；device / device_group 二选一（空串表示未配置）
// runtime 为运行态 mock（STOPPED / RUNNING），非配置字段
export interface TaskDef {
  task_id: string
  device: string
  device_group: string
  point_group: string
  interval: number | null
  sinks: string[]
  enabled: boolean
  runtime: string
}

// system.yaml 的 SinkConfig
export interface SinkDef {
  name: string
  type: string
  enabled: boolean
}
