// Mock 静态种子：definitions（units/types/groups/models/point tables/points）、
// 56 台设备（48 风机 + 8 PCS，ADS/Modbus/IEC104）、6 个 task、4 个 sink、
// settings、workers，以及 7 个配置文件的 YAML 文本（由结构化数据生成）。
// 故障场景不在此处定义 —— 见 scenarios.ts（单一事实源）。
import type { components } from '../api/generated/schema'

export type SettingsDto = components['schemas']['SettingsResponse']
export type DefinitionsDto = components['schemas']['DefinitionsResponse']
export type DeviceDto = components['schemas']['DeviceResponse']
export type TaskDto = components['schemas']['TaskResponse']
export type SinkDto = components['schemas']['SinkResponse']
export type WorkerDto = components['schemas']['WorkerResponse']
export type ConfigFileDto = components['schemas']['ConfigFileResponse']
export type ConfigRevisionDto = components['schemas']['ConfigRevisionResponse']

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
export const settingsSeed: SettingsDto = {
  site_id: 'wind_farm_a',
  site_name: '示例风场',
  api_enabled: true,
  api_host: '0.0.0.0',
  api_port: 8080,
  ads_local_ip: '192.168.151.244',
  ads_local_ams_net_id: '192.168.151.244.1.2',
  ads_username: 'Administrator',
  ads_password: '',
}

// ---------------------------------------------------------------------------
// Definitions：units / device types / groups / models / point tables / points
// ---------------------------------------------------------------------------
export interface MockPointDef {
  point_id: string
  variable_name: string
  point_groups: string[]
  address: Record<string, unknown>
  data_type: string
  scale: number
  offset: number
  unit: string
  description: string
}

export const unitsSeed: Record<string, { symbol: string; name: string }> = {
  none: { symbol: '', name: 'Dimensionless' },
  percent: { symbol: '%', name: 'Percent' },
  volt: { symbol: 'V', name: 'Volt' },
  kilovolt: { symbol: 'kV', name: 'Kilovolt' },
  ampere: { symbol: 'A', name: 'Ampere' },
  watt: { symbol: 'W', name: 'Watt' },
  kilowatt: { symbol: 'kW', name: 'Kilowatt' },
  megawatt: { symbol: 'MW', name: 'Megawatt' },
  hertz: { symbol: 'Hz', name: 'Hertz' },
  rpm: { symbol: 'rpm', name: 'Revolutions per minute' },
  meter_per_second: { symbol: 'm/s', name: 'Meter per second' },
  degree: { symbol: 'deg', name: 'Degree' },
  celsius: { symbol: '°C', name: 'Degree Celsius' },
  pascal: { symbol: 'Pa', name: 'Pascal' },
  bar: { symbol: 'bar', name: 'Bar' },
  second: { symbol: 's', name: 'Second' },
  millisecond: { symbol: 'ms', name: 'Millisecond' },
}

export const deviceTypesSeed: Record<string, { name: string }> = {
  turbine: { name: '风力发电机组' },
  pcs: { name: '储能变流器' },
  bms: { name: '电池管理系统' },
}

export const deviceGroupsSeed: string[] = [
  'turbine_modbus',
  'turbine_ads',
  'turbine_iec104',
  'storage_pcs',
]

export const pointGroupsSeed: string[] = [
  'all',
  'fast',
  'status',
  'control',
  'electrical',
  'mechanical',
  'monitor',
]

export interface MockDeviceModel {
  device_type: string
  manufacturer: string
  model: string
  protocol: 'ads' | 'modbus' | 'iec104'
  point_table: string
  read_mode?: string
  connection_defaults: Record<string, unknown>
}

export const deviceModelsSeed: Record<string, MockDeviceModel> = {
  beckhoff_wtg: {
    device_type: 'turbine',
    manufacturer: 'Beckhoff',
    model: 'TwinCAT 2 WTG',
    protocol: 'ads',
    point_table: 'beckhoff_wtg_v1',
    read_mode: 'sum',
    connection_defaults: { port: 48898, twincat_version: '2', timeout: 3.0, target_port: 801 },
  },
  modbus_wtg: {
    device_type: 'turbine',
    manufacturer: '',
    model: 'Modbus WTG',
    protocol: 'modbus',
    point_table: 'modbus_wtg_v1',
    connection_defaults: {
      port: 502,
      unit_id: 1,
      mode: 'tcp',
      timeout: 3.0,
      word_order: 'little_endian',
    },
  },
  iec104_wtg: {
    device_type: 'turbine',
    manufacturer: '',
    model: 'IEC 60870-5-104 WTG',
    protocol: 'iec104',
    point_table: 'iec104_wtg_v1',
    connection_defaults: { port: 2404, timeout: 5.0 },
  },
  pcs_modbus_a: {
    device_type: 'pcs',
    manufacturer: '',
    model: 'PCS Modbus A',
    protocol: 'modbus',
    point_table: 'pcs_modbus_v1',
    connection_defaults: { port: 502, unit_id: 1, mode: 'tcp', timeout: 3.0 },
  },
}

const pgroupIds = pointGroupsSeed
const unitIds = Object.keys(unitsSeed).filter((u) => u !== 'none')
const mixedTypes = ['float32', 'float32', 'int16', 'int32', 'bool', 'uint16']

function adsPoints(prefix: string, count: number): MockPointDef[] {
  return Array.from({ length: count }, (_, i) => {
    const name = `${prefix}_${String(i + 1).padStart(3, '0')}`
    return {
      point_id: name,
      variable_name: name,
      point_groups: [
        ...new Set([
          pgroupIds[i % pgroupIds.length],
          ...(i % 7 === 0 ? ['all'] : []),
          ...(i % 11 === 0 ? ['control'] : []),
        ]),
      ],
      address: {
        symbol: `MAIN.${name}`,
        index_group: '0x4020',
        index_offset: `0x${((i + 1) * 8).toString(16).toUpperCase()}`,
      },
      data_type: mixedTypes[i % mixedTypes.length],
      scale: i % 6 === 0 ? 0.1 : 1,
      offset: 0,
      unit: unitIds[i % unitIds.length],
      description: `${name.replace(/_/g, ' ')} measurement`,
    }
  })
}

function modbusPoints(prefix: string, count: number): MockPointDef[] {
  const rtypes = ['input', 'holding', 'holding', 'input']
  return Array.from({ length: count }, (_, i) => {
    const name = `${prefix}_${String(i + 1).padStart(3, '0')}`
    return {
      point_id: name,
      variable_name: name,
      point_groups: [
        pgroupIds[i % pgroupIds.length],
        ...(i % 7 === 0 ? ['all'] : []),
        ...(i % 11 === 0 ? ['control'] : []),
      ],
      address: { type: rtypes[i % rtypes.length], address: 100 + i * 2 },
      data_type: ['float32', 'int32', 'int16', 'uint16', 'bool'][i % 5],
      scale: i % 6 === 0 ? 0.01 : 1,
      offset: 0,
      unit: unitIds[i % unitIds.length],
      description: `${name.replace(/_/g, ' ')} measurement`,
    }
  })
}

function iec104Points(prefix: string, count: number): MockPointDef[] {
  return Array.from({ length: count }, (_, i) => {
    const name = `${prefix}_${String(i + 1).padStart(3, '0')}`
    return {
      point_id: name,
      variable_name: name,
      point_groups: [
        pgroupIds[i % pgroupIds.length],
        ...(i % 7 === 0 ? ['all'] : []),
        ...(i % 11 === 0 ? ['control'] : []),
      ],
      address: { ioa: 1001 + i, type: i % 5 === 4 ? 'M_SP_NA_1' : 'M_ME_NC_1' },
      data_type: ['float32', 'float32', 'int32', 'uint16', 'bool'][i % 5],
      scale: i % 6 === 0 ? 0.1 : 1,
      offset: 0,
      unit: unitIds[i % unitIds.length],
      description: `${name.replace(/_/g, ' ')} measurement`,
    }
  })
}

export interface MockPointTable {
  protocol: string
  extends: string
  remove_points: string[]
  points: MockPointDef[]
}

// 主要业务点表每张 100+ 点（验证分页 / 搜索 / Data / Trend / Diagnostics）。
export const pointTablesSeed: Record<string, MockPointTable> = {
  beckhoff_base_v1: {
    protocol: 'ads',
    extends: '',
    remove_points: [],
    points: adsPoints('base', 12),
  },
  beckhoff_wtg_v1: {
    protocol: 'ads',
    extends: 'beckhoff_base_v1',
    remove_points: [],
    points: adsPoints('wtg_ads', 120),
  },
  modbus_wtg_v1: {
    protocol: 'modbus',
    extends: '',
    remove_points: [],
    points: modbusPoints('wtg_mb', 120),
  },
  iec104_wtg_v1: {
    protocol: 'iec104',
    extends: '',
    remove_points: [],
    points: iec104Points('wtg_104', 100),
  },
  pcs_modbus_v1: {
    protocol: 'modbus',
    extends: '',
    remove_points: [],
    points: modbusPoints('pcs_mb', 100),
  },
}

/** 点表继承解析：extends 链合并，子表同名点覆盖。 */
export function pointsOfTable(tableId: string, stack: string[] = []): MockPointDef[] {
  if (stack.includes(tableId)) return []
  const table = pointTablesSeed[tableId]
  if (!table) return []
  const resolved = new Map<string, MockPointDef>()
  if (table.extends) {
    for (const p of pointsOfTable(table.extends, [...stack, tableId])) {
      resolved.set(p.point_id, {
        ...p,
        address: { ...p.address },
        point_groups: [...p.point_groups],
      })
    }
  }
  for (const id of table.remove_points) resolved.delete(id)
  for (const p of table.points) {
    resolved.set(p.point_id, { ...p, address: { ...p.address }, point_groups: [...p.point_groups] })
  }
  return [...resolved.values()]
}

export function buildDefinitions(): DefinitionsDto {
  const point_tables: DefinitionsDto['point_tables'] = {}
  for (const [id, table] of Object.entries(pointTablesSeed)) {
    const value: Record<string, unknown> = {
      remove_points: [...table.remove_points],
      points: table.points.map((p) => ({
        ...p,
        address: { ...p.address },
        point_groups: [...p.point_groups],
      })),
    }
    if (table.protocol !== 'generic') value.protocol = table.protocol
    if (table.extends) value.extends = table.extends
    point_tables[id] = value
  }
  const device_models: DefinitionsDto['device_models'] = {}
  for (const [id, m] of Object.entries(deviceModelsSeed)) {
    device_models[id] = {
      device_type: m.device_type,
      manufacturer: m.manufacturer || null,
      model: m.model || null,
      protocol: m.protocol,
      point_table: m.point_table,
      ...(m.read_mode ? { read_mode: m.read_mode } : {}),
      properties: {},
      connection_defaults: { ...m.connection_defaults },
    }
  }
  return {
    units: Object.fromEntries(Object.entries(unitsSeed).map(([k, v]) => [k, { ...v }])),
    device_types: Object.fromEntries(
      Object.entries(deviceTypesSeed).map(([k, v]) => [k, { ...v }]),
    ),
    device_models,
    point_tables,
    device_groups: [...deviceGroupsSeed],
    point_groups: [...pointGroupsSeed],
  }
}

// ---------------------------------------------------------------------------
// Devices：48 风机（24 modbus / 20 ads / 4 iec104）+ 8 PCS（modbus）
// ---------------------------------------------------------------------------
export interface MockDevice {
  device_id: string
  model: string
  device_group: string
  host: string
  port: number | null
  extensions: Record<string, unknown>
  enabled: boolean
}

export const devicesSeed: MockDevice[] = []
for (let i = 1; i <= 48; i++) {
  const id = `wtg-${String(i).padStart(3, '0')}`
  if (i <= 24) {
    devicesSeed.push({
      device_id: id,
      model: 'modbus_wtg',
      device_group: 'turbine_modbus',
      host: `192.168.100.${100 + i}`,
      port: null,
      extensions: {},
      enabled: true,
    })
  } else if (i <= 44) {
    devicesSeed.push({
      device_id: id,
      model: 'beckhoff_wtg',
      device_group: 'turbine_ads',
      host: `192.168.151.${i}`,
      port: null,
      extensions: { target_net_id: `192.168.151.${i}.1.1` },
      enabled: true,
    })
  } else {
    devicesSeed.push({
      device_id: id,
      model: 'iec104_wtg',
      device_group: 'turbine_iec104',
      host: `192.168.130.${i}`,
      port: null,
      extensions: {},
      // wtg-048 固定停用（少量 disabled 场景）
      enabled: i !== 48,
    })
  }
}
for (let i = 1; i <= 8; i++) {
  devicesSeed.push({
    device_id: `pcs-${String(i).padStart(2, '0')}`,
    model: 'pcs_modbus_a',
    device_group: 'storage_pcs',
    host: `192.168.60.${10 + i}`,
    port: null,
    extensions: {},
    // pcs-08 固定停用
    enabled: i !== 8,
  })
}

export function modelOf(device: MockDevice): MockDeviceModel | undefined {
  return deviceModelsSeed[device.model]
}

export function portOfDevice(device: MockDevice): number {
  return device.port ?? Number(modelOf(device)?.connection_defaults?.port ?? 0)
}

// ---------------------------------------------------------------------------
// Tasks：group task 与 single-device task 混合，RUNNING / STOPPED / 故障目标
// ---------------------------------------------------------------------------
export interface MockTask {
  task_id: string
  device: string | null
  device_group: string | null
  point_group: string
  interval: number | null
  sinks: string[]
  enabled: boolean
  running: boolean
}

export const tasksSeed: MockTask[] = [
  {
    task_id: 'turbine-modbus-all',
    device: null,
    device_group: 'turbine_modbus',
    point_group: 'all',
    interval: 1,
    sinks: ['file_archive', 'db_main'],
    enabled: true,
    running: true,
  },
  {
    task_id: 'turbine-ads-all',
    device: null,
    device_group: 'turbine_ads',
    point_group: 'all',
    interval: 1,
    sinks: ['file_archive'],
    enabled: true,
    running: true,
  },
  {
    task_id: 'turbine-iec104-all',
    device: null,
    device_group: 'turbine_iec104',
    point_group: 'all',
    interval: 5,
    sinks: ['file_archive'],
    enabled: true,
    running: false,
  },
  {
    task_id: 'pcs-fast',
    device: null,
    device_group: 'storage_pcs',
    point_group: 'fast',
    interval: 1,
    sinks: ['file_archive', 'kafka_main'],
    enabled: true,
    running: false,
  },
  {
    task_id: 'wtg-001-diag',
    device: 'wtg-001',
    device_group: null,
    point_group: 'status',
    interval: 5,
    sinks: ['file_archive'],
    enabled: false,
    running: false,
  },
  // 固定故障场景：目标 wtg-041 网络不可达，Start 必须失败并回滚
  {
    task_id: 'wtg-041-diag',
    device: 'wtg-041',
    device_group: null,
    point_group: 'status',
    interval: 5,
    sinks: ['file_archive'],
    enabled: true,
    running: false,
  },
]

// ---------------------------------------------------------------------------
// Sinks：file / kafka / db + 一个 disabled；healthy / failed / disabled 全覆盖
// ---------------------------------------------------------------------------
export interface MockSink {
  name: string
  type: 'file' | 'kafka' | 'db'
  enabled: boolean
  connection: Record<string, unknown>
  points: Record<string, unknown>[]
}

export const sinksSeed: MockSink[] = [
  {
    name: 'kafka_main',
    type: 'kafka',
    enabled: true,
    connection: {
      bootstrap_servers: 'kafka.windhub.local:9092',
      topic: 'wind-hub.raw',
      acks: 'all',
    },
    points: [{ point_group: 'all' }],
  },
  {
    name: 'db_main',
    type: 'db',
    enabled: true,
    connection: {
      dsn: 'postgresql://windhub:windhub@db.windhub.local:5432/windhub',
      table: 'points',
    },
    points: [{ point_group: 'all' }],
  },
  {
    name: 'file_archive',
    type: 'file',
    enabled: true,
    connection: { path: '/var/tmp/wind-hub/archive.jsonl', format: 'jsonl' },
    points: [{ point_group: 'fast' }, { point_group: 'status' }],
  },
  {
    name: 'debug_file',
    type: 'file',
    enabled: false,
    connection: { path: '/var/tmp/wind-hub/debug.jsonl', format: 'jsonl' },
    points: [],
  },
]

// ---------------------------------------------------------------------------
// Workers
// ---------------------------------------------------------------------------
export const workersSeed: WorkerDto[] = [
  {
    worker_id: 'collector-1',
    role: 'collector',
    endpoint: 'grpc://192.168.10.40:50051',
    state: 'online',
    capabilities: ['collect', 'route'],
    runtime_running: true,
    reported_id: 'collector-1',
    boot_id: 'boot-20261001-01',
    active_revision: 'r12',
    active_config_hash: 'sha256:mock-a1',
    last_seen_at: '2026-10-08T08:00:00Z',
    last_probe_at: '2026-10-08T08:00:00Z',
    last_error: null,
  },
  {
    worker_id: 'commander-1',
    role: 'commander',
    endpoint: 'grpc://192.168.10.41:50052',
    state: 'online',
    capabilities: ['schedule', 'dispatch'],
    runtime_running: true,
    reported_id: 'commander-1',
    boot_id: 'boot-20261001-02',
    active_revision: 'r12',
    active_config_hash: 'sha256:mock-a1',
    last_seen_at: '2026-10-08T08:00:00Z',
    last_probe_at: '2026-10-08T08:00:00Z',
    last_error: null,
  },
]

// ---------------------------------------------------------------------------
// 配置文件（7 个 YAML）：内容由结构化种子生成，apply/import 后 GET 可见更新。
// ---------------------------------------------------------------------------
export const CONFIG_FILE_NAMES = [
  'system.yaml',
  'units.yaml',
  'device_models.yaml',
  'points.yaml',
  'devices.yaml',
  'tasks.yaml',
  'reporting.yaml',
] as const

export type ConfigFileName = (typeof CONFIG_FILE_NAMES)[number]

function systemYaml(s: SettingsDto): string {
  return `site:
  site_id: ${s.site_id}
  name: "${s.site_name ?? ''}"

runtime:
  queue_maxsize: 1000
  backpressure_policy: drop_old
  shutdown_timeout: 30.0
  connect_timeout: 10.0
  read_timeout: 5.0
  write_timeout: 5.0

ads:
  local_ams_net_id: "${s.ads_local_ams_net_id ?? ''}"
  local_ip: "${s.ads_local_ip ?? ''}"
  username: "${s.ads_username}"
  password: "${s.ads_password}"

interfaces:
  api:
    enabled: ${s.api_enabled}
    host: "${s.api_host}"
    port: ${s.api_port}
`
}

function unitsYaml(): string {
  const body = Object.entries(unitsSeed)
    .map(([id, u]) => `  ${id}:\n    symbol: "${u.symbol}"\n    name: ${u.name}`)
    .join('\n')
  return `units:\n${body}\n`
}

function deviceModelsYaml(): string {
  const types = Object.entries(deviceTypesSeed)
    .map(([id, t]) => `  ${id}:\n    name: ${t.name}`)
    .join('\n')
  const models = Object.entries(deviceModelsSeed)
    .map(
      ([id, m]) =>
        `  ${id}:\n    device_type: ${m.device_type}\n    protocol: ${m.protocol}\n    point_table: ${m.point_table}\n    connection_defaults:\n` +
        Object.entries(m.connection_defaults)
          .map(([k, v]) => `      ${k}: ${typeof v === 'string' ? v : String(v)}`)
          .join('\n'),
    )
    .join('\n\n')
  return `device_types:\n${types}\n\ndevice_models:\n${models}\n`
}

function pointsYaml(): string {
  return Object.entries(pointTablesSeed)
    .map(([id, t]) => {
      const header = `  ${id}:\n    protocol: ${t.protocol}${t.extends ? `\n    extends: ${t.extends}` : ''}\n    points:`
      const rows = t.points
        .map(
          (p) =>
            `      - point_id: ${p.point_id}\n        data_type: ${p.data_type}\n        scale: ${p.scale}\n        unit: ${p.unit}`,
        )
        .join('\n')
      return `${header}\n${rows}`
    })
    .join('\n\n')
    .concat('\n')
}

export function devicesYaml(devices: MockDevice[]): string {
  return (
    'devices:\n' +
    devices
      .map(
        (d) =>
          `  - device_id: ${d.device_id}\n    model: ${d.model}\n    device_group: ${d.device_group}\n    host: "${d.host}"\n    enabled: ${d.enabled}`,
      )
      .join('\n') +
    '\n'
  )
}

export function tasksYaml(tasks: MockTask[]): string {
  return (
    'tasks:\n' +
    tasks
      .map(
        (t) =>
          `  - task_id: ${t.task_id}\n    device: ${t.device ?? 'null'}\n    device_group: ${t.device_group ?? 'null'}\n    point_group: ${t.point_group}\n    interval: ${t.interval ?? 'null'}\n    enabled: ${t.enabled}\n    sinks:${t.sinks.map((s) => `\n      - ${s}`).join('')}`,
      )
      .join('\n') +
    '\n'
  )
}

function reportingYaml(): string {
  return `reporting:\n  enabled: false\n  interval: 300\n  targets: []\n`
}

export function seedYamlFiles(
  settings: SettingsDto,
  devices: MockDevice[],
  tasks: MockTask[],
): Record<string, string> {
  return {
    'system.yaml': systemYaml(settings),
    'units.yaml': unitsYaml(),
    'device_models.yaml': deviceModelsYaml(),
    'points.yaml': pointsYaml(),
    'devices.yaml': devicesYaml(devices),
    'tasks.yaml': tasksYaml(tasks),
    'reporting.yaml': reportingYaml(),
  }
}
