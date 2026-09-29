// 全站统一 mock store —— 各页面共享同一份数据。
import { reactive } from 'vue'
import type {
  DeviceGroupDef, DeviceInst, DeviceModelDef, DeviceTypeDef,
  DeviceVerification, PointAddress, PointDef, PointGroupDef,
  PointTableDef, PointOrigin, SinkDef, TaskDef, UnitDef,
} from './types'

const systemInfo = {
  siteId: 'wind_farm_a',
  siteName: '示例风场',
  collectorVersion: 'v0.3.0',
  adminVersion: 'v0.3.0',
  runtimeStatus: 'RUNNING',
  configSet: 'template',
}

const units: Record<string, UnitDef> = {
  none: { symbol: '', name: 'Dimensionless' },
  percent: { symbol: '%', name: 'Percent' },
  volt: { symbol: 'V', name: 'Volt' },
  kilovolt: { symbol: 'kV', name: 'Kilovolt' },
  ampere: { symbol: 'A', name: 'Ampere' },
  watt: { symbol: 'W', name: 'Watt' },
  kilowatt: { symbol: 'kW', name: 'Kilowatt' },
  megawatt: { symbol: 'MW', name: 'Megawatt' },
  var: { symbol: 'var', name: 'Reactive power' },
  kilovar: { symbol: 'kVar', name: 'Kilovar' },
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

const deviceTypes: DeviceTypeDef[] = [
  { id: 'turbine', name: '风力发电机组' },
  { id: 'pcs', name: '储能变流器' },
  { id: 'bms', name: '电池管理系统' },
]

const deviceGroups: DeviceGroupDef[] = [
  { id: 'turbine_modbus', device_type: 'turbine' },
  { id: 'turbine_ads', device_type: 'turbine' },
  { id: 'storage_pcs', device_type: 'pcs' },
]

const deviceModels: DeviceModelDef[] = [
  {
    id: 'beckhoff_wtg',
    device_type: 'turbine',
    manufacturer: 'Beckhoff',
    model: 'TwinCAT 2 WTG',
    protocol: 'ads',
    point_table: 'beckhoff_wtg_v1',
    read_mode: 'sum',
    properties: {},
    connection_defaults: {
      port: 48898,
      twincat_version: '2',
      timeout: 3.0,
      target_port: 801,
      reconnect_max_retries: 5,
      reconnect_backoff_max: 30.0,
    },
  },
  {
    id: 'modbus_wtg',
    device_type: 'turbine',
    manufacturer: '',
    model: 'Modbus WTG',
    protocol: 'modbus',
    point_table: 'modbus_wtg_v1',
    read_mode: '',
    properties: {},
    connection_defaults: {
      port: 502,
      unit_id: 1,
      mode: 'tcp',
      timeout: 3.0,
      word_order: 'little_endian',
      reconnect_max_retries: 5,
      reconnect_backoff_max: 30.0,
    },
  },
  {
    id: 'pcs_modbus_a',
    device_type: 'pcs',
    manufacturer: '',
    model: 'PCS Modbus A',
    protocol: 'modbus',
    point_table: 'pcs_modbus_v1',
    read_mode: '',
    properties: {},
    connection_defaults: {
      port: 502,
      unit_id: 1,
      mode: 'tcp',
      timeout: 3.0,
      word_order: 'little_endian',
      reconnect_max_retries: 5,
      reconnect_backoff_max: 30.0,
    },
  },
]

export const DEFAULT_POINT_TABLE_BY_PROTOCOL = {
  ads: 'default_ads',
  modbus: 'default_modbus',
  iec104: 'default_iec104',
} as const

export const DEFAULT_POINT_GROUP_ID = 'default'

const pointTables: PointTableDef[] = [
  { id: 'default_ads', protocol: 'ads', extends: '', remove_points: [], system: true },
  { id: 'default_modbus', protocol: 'modbus', extends: '', remove_points: [], system: true },
  { id: 'default_iec104', protocol: 'iec104', extends: '', remove_points: [], system: true },
  { id: 'beckhoff_base_v1', protocol: 'ads', extends: '', remove_points: [] },
  { id: 'beckhoff_wtg_v1', protocol: 'ads', extends: 'beckhoff_base_v1', remove_points: [] },
  { id: 'modbus_wtg_v1', protocol: 'modbus', extends: '', remove_points: [] },
  { id: 'pcs_modbus_v1', protocol: 'modbus', extends: '', remove_points: [] },
]

const pointGroups: PointGroupDef[] = [
  { id: DEFAULT_POINT_GROUP_ID, name: 'Default / Unassigned', system: true },
  { id: 'all', name: 'All points' },
  { id: 'fast', name: 'Fast telemetry' },
  { id: 'status', name: 'Status' },
  { id: 'control', name: 'Control / command' },
  { id: 'electrical', name: 'Electrical' },
  { id: 'mechanical', name: 'Mechanical' },
  { id: 'monitor', name: 'Monitor' },
]

const pgroupIds = pointGroups.filter(g => !g.system).map(g => g.id)
const unitIds = Object.keys(units).filter(u => u !== 'none')
const adDTypes = ['float32', 'float32', 'int16', 'int32', 'bool', 'uint16']

function adsPoints(prefix: string, count: number): PointDef[] {
  return Array.from({ length: count }, (_, i) => {
    const name = `${prefix}_${String(i + 1).padStart(3, '0')}`
    const groups = [...new Set([
      pgroupIds[i % pgroupIds.length],
      ...(i % 7 === 0 ? ['all'] : []),
      ...(i % 11 === 0 ? ['control'] : []),
    ])]
    return {
      point_id: name,
      variable_name: name,
      point_groups: groups,
      // 真实 Beckhoff 配置允许 symbol 与 index 寻址共存（读写以 symbol 优先），
      // mock 让每个点都带全两种地址，编辑时各字段均有原值
      address: {
        symbol: `MAIN.${name}`,
        index_group: '0x4020',
        index_offset: `0x${((i + 1) * 8).toString(16).toUpperCase()}`,
      },
      data_type: adDTypes[i % adDTypes.length],
      scale: i % 6 === 0 ? 0.1 : 1,
      offset: 0,
      unit: unitIds[i % unitIds.length],
      description: `${name.replace(/_/g, ' ')} measurement`,
    }
  })
}

function modbusPoints(prefix: string, count: number): PointDef[] {
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

const points: Record<string, PointDef[]> = {
  default_ads: [],
  default_modbus: [],
  default_iec104: [],
  beckhoff_base_v1: adsPoints('base', 12),
  beckhoff_wtg_v1: adsPoints('wtg_ads', 40),
  modbus_wtg_v1: modbusPoints('wtg_mb', 32),
  pcs_modbus_v1: modbusPoints('pcs_mb', 24),
}

const devices: DeviceInst[] = []
for (let i = 1; i <= 48; i++) {
  const modbus = i < 25
  devices.push({
    device_id: `wtg-${String(i).padStart(3, '0')}`,
    model: modbus ? 'modbus_wtg' : 'beckhoff_wtg',
    device_group: modbus ? 'turbine_modbus' : 'turbine_ads',
    host: modbus ? `192.168.100.${100 + i}` : `192.168.151.${i}`,
    port: modbus ? 502 : 48898,
    extensions: modbus ? {} : { target_net_id: `192.168.151.${i}.1.1` },
    enabled: true,
    online: i !== 41,
  })
}
for (let i = 1; i <= 8; i++) {
  devices.push({
    device_id: `pcs-${String(i).padStart(2, '0')}`,
    model: 'pcs_modbus_a',
    device_group: 'storage_pcs',
    host: `192.168.60.${10 + i}`,
    port: 502,
    extensions: {},
    enabled: true,
    online: true,
  })
}

function emptyVerification(): DeviceVerification {
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

const deviceVerification: Record<string, DeviceVerification> = {}
for (const d of devices) deviceVerification[d.device_id] = emptyVerification()

const sinks: SinkDef[] = [
  { name: 'kafka_main', type: 'kafka', enabled: false },
  { name: 'db_main', type: 'db', enabled: false },
  { name: 'file_archive', type: 'file', enabled: true },
]

const tasks: TaskDef[] = [
  { task_id: 'turbine-modbus-all', device: '', device_group: 'turbine_modbus', point_group: 'all', interval: 1, sinks: ['file_archive'], enabled: true, runtime: 'RUNNING', valid: true, invalid_reason: '' },
  { task_id: 'turbine-ads-all', device: '', device_group: 'turbine_ads', point_group: 'all', interval: 1, sinks: ['file_archive'], enabled: true, runtime: 'RUNNING', valid: true, invalid_reason: '' },
  { task_id: 'pcs-fast', device: '', device_group: 'storage_pcs', point_group: 'fast', interval: 1, sinks: ['file_archive'], enabled: true, runtime: 'STOPPED', valid: true, invalid_reason: '' },
  { task_id: 'wtg-001-diag', device: 'wtg-001', device_group: '', point_group: 'status', interval: 5, sinks: ['file_archive'], enabled: false, runtime: 'STOPPED', valid: true, invalid_reason: '' },
]

export const store = reactive({
  systemInfo,
  units,
  deviceTypes,
  deviceGroups,
  deviceModels,
  pointTables,
  pointGroups,
  points,
  devices,
  deviceVerification,
  sinks,
  tasks,
})

export function modelOf(d: DeviceInst): DeviceModelDef | undefined {
  return store.deviceModels.find(m => m.id === d.model)
}

export function protocolOfDevice(d: DeviceInst): string {
  return modelOf(d)?.protocol || ''
}

export function tableOfDevice(d: DeviceInst): string {
  return modelOf(d)?.point_table || ''
}

function clonePoint(p: PointDef): PointDef {
  return { ...p, address: { ...p.address }, point_groups: [...p.point_groups] }
}

export function pointsOfTable(tableId: string, stack: string[] = []): PointDef[] {
  if (stack.includes(tableId)) return []
  const table = store.pointTables.find(t => t.id === tableId)
  if (!table) return []

  const resolved = new Map<string, PointDef>()
  if (table.extends) {
    for (const p of pointsOfTable(table.extends, [...stack, tableId])) {
      resolved.set(p.point_id, clonePoint(p))
    }
  }
  for (const id of table.remove_points || []) resolved.delete(id)
  for (const p of store.points[tableId] || []) resolved.set(p.point_id, clonePoint(p))
  return [...resolved.values()]
}

export function pointOrigin(tableId: string, pointId: string): PointOrigin {
  const table = store.pointTables.find(t => t.id === tableId)
  const local = (store.points[tableId] || []).some(p => p.point_id === pointId)
  const inherited = !!table?.extends && pointsOfTable(table.extends).some(p => p.point_id === pointId)
  if (local && inherited) return 'override'
  if (local) return 'local'
  return 'inherited'
}

export function descendantTableIds(tableId: string): string[] {
  const result: string[] = []
  const visit = (id: string) => {
    for (const child of store.pointTables.filter(t => t.extends === id)) {
      if (!result.includes(child.id)) {
        result.push(child.id)
        visit(child.id)
      }
    }
  }
  visit(tableId)
  return result
}

export function affectedByPointTables(tableIds: string[]) {
  const tables = [...new Set(tableIds)]
  const modelIds = store.deviceModels.filter(m => tables.includes(m.point_table)).map(m => m.id)
  const devices = store.devices.filter(d => modelIds.includes(d.model))
  const deviceIds = new Set(devices.map(d => d.device_id))
  const groups = new Set(devices.map(d => d.device_group))
  const tasks = store.tasks.filter(t =>
    (t.device && deviceIds.has(t.device)) ||
    (t.device_group && groups.has(t.device_group))
  )
  return { tables, modelIds, devices, tasks }
}

export function tableProtocol(tableId: string): string {
  return store.pointTables.find(t => t.id === tableId)?.protocol || ''
}

export function unitSymbol(unitId: string): string {
  return store.units[unitId]?.symbol ?? unitId
}

export function addressText(protocol: string, p: PointDef): string {
  const a = p.address
  if (protocol === 'ads') {
    if (a.symbol && a.index_group) return `${a.symbol} (${a.index_group} / ${a.index_offset})`
    if (a.symbol) return a.symbol
    return `${a.index_group ?? ''} / ${a.index_offset ?? ''}`
  }
  if (protocol === 'modbus') return `${a.type ?? ''} ${a.address ?? ''}`
  return `ioa ${a.ioa ?? ''}${a.type ? ` · ${a.type}` : ''}`
}

export function validateAddress(protocol: string, a: PointAddress): string {
  if (protocol === 'ads') {
    const hasSymbol = !!(a.symbol && a.symbol.trim())
    const hasGroup = !!(a.index_group && a.index_group.trim())
    const hasOffset = !!(a.index_offset && a.index_offset.trim())
    if (hasGroup !== hasOffset) return "'index_group' and 'index_offset' must be configured together"
    if (!hasSymbol && !hasGroup) return "ADS point must define 'symbol' or 'index_group' + 'index_offset'"
    return ''
  }
  if (protocol === 'modbus') {
    if (!a.type) return 'Modbus point must define register type (holding/input/coil/discrete_input)'
    if (a.address === undefined || a.address === null || a.address < 0) return 'Modbus point must define a 0-based address'
    return ''
  }
  if (a.ioa === undefined || a.ioa === null || a.ioa < 0 || a.ioa > 0xFFFFFF) {
    return 'IEC104 ioa must be an integer in [0, 0xFFFFFF]'
  }
  return ''
}


export function defaultPointTableFor(protocol: string): string {
  return DEFAULT_POINT_TABLE_BY_PROTOCOL[protocol as keyof typeof DEFAULT_POINT_TABLE_BY_PROTOCOL] || ''
}

export function isDefaultPointTable(tableId: string): boolean {
  return Object.values(DEFAULT_POINT_TABLE_BY_PROTOCOL).includes(tableId as typeof DEFAULT_POINT_TABLE_BY_PROTOCOL[keyof typeof DEFAULT_POINT_TABLE_BY_PROTOCOL])
}

export function isDefaultPointGroup(groupId: string): boolean {
  return groupId === DEFAULT_POINT_GROUP_ID
}

export function devicesForTask(t: TaskDef): DeviceInst[] {
  if (t.device) return store.devices.filter(d => d.device_id === t.device && d.enabled)
  if (t.device_group) return store.devices.filter(d => d.device_group === t.device_group && d.enabled)
  return []
}

export function taskInvalidReason(t: TaskDef): string {
  if (!store.pointGroups.some(g => g.id === t.point_group)) return 'Point Group does not exist'
  if (isDefaultPointGroup(t.point_group)) return 'Default Point Group is a placeholder and cannot be collected'

  const targets = devicesForTask(t)
  if (!targets.length) return 'Task target resolves to no devices'

  for (const d of targets) {
    const model = modelOf(d)
    if (!model) return `Device ${d.device_id} has no valid model`
    if (isDefaultPointTable(model.point_table)) return `Device ${d.device_id} is assigned to a default Point Table`
    if (!store.pointTables.some(pt => pt.id === model.point_table)) return `Device ${d.device_id} references a missing Point Table`
    if (!pointsOfTable(model.point_table).some(p => p.point_groups.includes(t.point_group))) {
      return `Device ${d.device_id} Point Table has no points in group '${t.point_group}'`
    }
  }
  return ''
}

export function refreshTaskValidity(): void {
  for (const t of store.tasks) {
    const reason = taskInvalidReason(t)
    t.valid = !reason
    t.invalid_reason = reason
    if (reason && t.runtime === 'RUNNING') t.runtime = 'STOPPED'
  }
}
