// 全站统一 mock store —— 各页面共享同一份数据，保证
// Device Instance → Device Model → Device Type → Protocol → Point Table
// 与 Task → Device / Device Group 的引用关系一致。
import { reactive } from 'vue'
import type {
  DeviceGroupDef, DeviceInst, DeviceModelDef, DeviceTypeDef,
  PointAddress, PointDef, PointGroupDef, PointTableDef, SinkDef, TaskDef, UnitDef,
} from './types'

// units.yaml（template 子集，key 为 unit ID）
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
  { id: 'beckhoff_wtg', device_type: 'turbine', manufacturer: 'Beckhoff', protocol: 'ads', point_table: 'beckhoff_wtg_v1', read_mode: 'sum' },
  { id: 'modbus_wtg', device_type: 'turbine', manufacturer: '', protocol: 'modbus', point_table: 'modbus_wtg_v1', read_mode: '' },
  { id: 'pcs_modbus_a', device_type: 'pcs', manufacturer: '', protocol: 'modbus', point_table: 'pcs_modbus_v1', read_mode: '' },
]

const pointTables: PointTableDef[] = [
  { id: 'beckhoff_base_v1', protocol: 'ads', extends: '' },
  { id: 'beckhoff_wtg_v1', protocol: 'ads', extends: 'beckhoff_base_v1' },
  { id: 'modbus_wtg_v1', protocol: 'modbus', extends: '' },
  { id: 'pcs_modbus_v1', protocol: 'modbus', extends: '' },
]

const pointGroups: PointGroupDef[] = [
  { id: 'all', name: 'All points' },
  { id: 'fast', name: 'Fast telemetry' },
  { id: 'status', name: 'Status' },
  { id: 'control', name: 'Control / command' },
  { id: 'electrical', name: 'Electrical' },
  { id: 'mechanical', name: 'Mechanical' },
  { id: 'monitor', name: 'Monitor' },
]

const pgroupIds = pointGroups.map(g => g.id)
const unitIds = Object.keys(units).filter(u => u !== 'none')
const adDTypes = ['float32', 'float32', 'int16', 'int32', 'bool', 'uint16']

// ADS 点表（Symbol 寻址为主；混入少量 index_group/index_offset 兼容寻址点）
function adsPoints(prefix: string, count: number): PointDef[] {
  return Array.from({ length: count }, (_, i) => {
    const name = `${prefix}_${String(i + 1).padStart(3, '0')}`
    const legacy = i % 19 === 18 // 少量兼容寻址点：index_group + index_offset 成对
    const groups = [...new Set([pgroupIds[i % pgroupIds.length], ...(i % 7 === 0 ? ['all'] : []), ...(i % 11 === 0 ? ['control'] : [])])]
    return {
      point_id: name,
      variable_name: name,
      point_groups: groups,
      address: legacy
        ? { index_group: '0x4020', index_offset: `0x${((i + 1) * 8).toString(16).toUpperCase()}` }
        : { symbol: `MAIN.${name}` },
      data_type: adDTypes[i % adDTypes.length],
      scale: i % 6 === 0 ? 0.1 : 1,
      offset: 0,
      unit: unitIds[i % unitIds.length],
      description: `${name.replace(/_/g, ' ')} measurement`,
    }
  })
}

// Modbus 点表（type + 0-based address）
function modbusPoints(prefix: string, count: number): PointDef[] {
  const rtypes = ['input', 'holding', 'holding', 'input']
  return Array.from({ length: count }, (_, i) => {
    const name = `${prefix}_${String(i + 1).padStart(3, '0')}`
    return {
      point_id: name,
      variable_name: name,
      point_groups: [pgroupIds[i % pgroupIds.length], ...(i % 7 === 0 ? ['all'] : []), ...(i % 11 === 0 ? ['control'] : [])],
      address: { type: rtypes[i % rtypes.length], address: 100 + i * 2 },
      data_type: ['float32', 'int32', 'int16', 'uint16', 'bool'][i % 5],
      scale: i % 6 === 0 ? 0.01 : 1,
      offset: 0,
      unit: unitIds[i % unitIds.length],
      description: `${name.replace(/_/g, ' ')} measurement`,
    }
  })
}

// 点表数据：原型阶段各表保存继承展开后的完整点集（extends 仅作元信息展示）
const points: Record<string, PointDef[]> = {
  beckhoff_base_v1: adsPoints('base', 12),
  beckhoff_wtg_v1: adsPoints('wtg_ads', 40),
  modbus_wtg_v1: modbusPoints('wtg_mb', 32),
  pcs_modbus_v1: modbusPoints('pcs_mb', 24),
}

// 设备实例：wtg-001..024 → modbus_wtg / turbine_modbus；
// wtg-025..048 → beckhoff_wtg / turbine_ads；pcs-01..08 → pcs_modbus_a / storage_pcs
const devices: DeviceInst[] = []
for (let i = 1; i <= 48; i++) {
  const modbus = i < 25
  devices.push({
    device_id: `wtg-${String(i).padStart(3, '0')}`,
    model: modbus ? 'modbus_wtg' : 'beckhoff_wtg',
    device_group: modbus ? 'turbine_modbus' : 'turbine_ads',
    host: modbus ? `192.168.100.${100 + i}` : `192.168.151.${i}`,
    port: modbus ? 502 : 48898,
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
    enabled: true,
    online: true,
  })
}

const sinks: SinkDef[] = [
  { name: 'kafka_main', type: 'kafka', enabled: false },
  { name: 'db_main', type: 'db', enabled: false },
  { name: 'file_archive', type: 'file', enabled: true },
]

const tasks: TaskDef[] = [
  { task_id: 'turbine-modbus-all', device: '', device_group: 'turbine_modbus', point_group: 'all', interval: 1, sinks: ['file_archive'], enabled: true, runtime: 'RUNNING' },
  { task_id: 'turbine-ads-all', device: '', device_group: 'turbine_ads', point_group: 'all', interval: 1, sinks: ['file_archive'], enabled: true, runtime: 'RUNNING' },
  { task_id: 'pcs-fast', device: '', device_group: 'storage_pcs', point_group: 'fast', interval: 1, sinks: ['file_archive'], enabled: true, runtime: 'STOPPED' },
  { task_id: 'wtg-001-diag', device: 'wtg-001', device_group: '', point_group: 'status', interval: 5, sinks: ['file_archive'], enabled: false, runtime: 'STOPPED' },
]

export const store = reactive({
  units,
  deviceTypes,
  deviceGroups,
  deviceModels,
  pointTables,
  pointGroups,
  points,
  devices,
  sinks,
  tasks,
})

// ---- 引用关系访问 helper ----

export function modelOf(d: DeviceInst): DeviceModelDef | undefined {
  return store.deviceModels.find(m => m.id === d.model)
}

export function protocolOfDevice(d: DeviceInst): string {
  return modelOf(d)?.protocol || ''
}

export function tableOfDevice(d: DeviceInst): string {
  return modelOf(d)?.point_table || ''
}

export function pointsOfTable(tableId: string): PointDef[] {
  return store.points[tableId] || []
}

export function tableProtocol(tableId: string): string {
  return store.pointTables.find(t => t.id === tableId)?.protocol || ''
}

export function unitSymbol(unitId: string): string {
  return store.units[unitId]?.symbol ?? unitId
}

// 点地址的展示形式（数据本身保持协议字段结构，仅展示层拼接）
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

// loader.py 的地址校验规则（原型侧复刻，用于 Add/Edit Point 表单校验）
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
  if (a.ioa === undefined || a.ioa === null || a.ioa < 0 || a.ioa > 0xFFFFFF) return 'IEC104 ioa must be an integer in [0, 0xFFFFFF]'
  return ''
}
