// 点表/点组领域纯函数：不依赖 store/网络，输入显式传入，可单测。
import type { PointAddress, PointDef, PointTableDef } from './types'

export const DEFAULT_POINT_TABLE_ID = 'default'
export const DEFAULT_POINT_GROUP_ID = 'default'

export interface PointsSnapshot {
  pointTables: PointTableDef[]
  points: Record<string, PointDef[]>
}

function clonePoint(p: PointDef): PointDef {
  return { ...p, address: { ...p.address }, point_groups: [...p.point_groups] }
}

/** 解析点表继承链（extends + remove_points + 本地覆盖），含环保护。 */
export function pointsOfTable(
  snapshot: PointsSnapshot,
  tableId: string,
  stack: string[] = [],
): PointDef[] {
  if (stack.includes(tableId)) return []
  const table = snapshot.pointTables.find((t) => t.id === tableId)
  if (!table) return []

  const resolved = new Map<string, PointDef>()
  if (table.extends) {
    for (const p of pointsOfTable(snapshot, table.extends, [...stack, tableId])) {
      resolved.set(p.point_id, clonePoint(p))
    }
  }
  for (const id of table.remove_points || []) resolved.delete(id)
  for (const p of snapshot.points[tableId] || []) resolved.set(p.point_id, clonePoint(p))
  return [...resolved.values()]
}

export function pointOrigin(
  snapshot: PointsSnapshot,
  tableId: string,
  pointId: string,
): 'inherited' | 'override' | 'local' {
  const table = snapshot.pointTables.find((t) => t.id === tableId)
  const local = (snapshot.points[tableId] || []).some((p) => p.point_id === pointId)
  const inherited =
    !!table?.extends && pointsOfTable(snapshot, table.extends).some((p) => p.point_id === pointId)
  if (local && inherited) return 'override'
  if (local) return 'local'
  return 'inherited'
}

/** 所有（递归）继承自 tableId 的点表 id。 */
export function descendantTableIds(snapshot: PointsSnapshot, tableId: string): string[] {
  const result: string[] = []
  const visit = (id: string) => {
    for (const child of snapshot.pointTables.filter((t) => t.extends === id)) {
      if (!result.includes(child.id)) {
        result.push(child.id)
        visit(child.id)
      }
    }
  }
  visit(tableId)
  return result
}

export function tableProtocol(snapshot: PointsSnapshot, tableId: string): string {
  return snapshot.pointTables.find((t) => t.id === tableId)?.protocol || ''
}

export function isDefaultPointTable(tableId: string): boolean {
  return tableId === DEFAULT_POINT_TABLE_ID
}

export function isDefaultPointGroup(groupId: string): boolean {
  return groupId === DEFAULT_POINT_GROUP_ID
}

export function defaultPointTableFor(_protocol: string): string {
  return DEFAULT_POINT_TABLE_ID
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
    if (hasGroup !== hasOffset)
      return "'index_group' and 'index_offset' must be configured together"
    if (!hasSymbol && !hasGroup)
      return "ADS point must define 'symbol' or 'index_group' + 'index_offset'"
    return ''
  }
  if (protocol === 'modbus') {
    if (!a.type) return 'Modbus point must define register type (holding/input/coil/discrete_input)'
    if (a.address === undefined || a.address === null || a.address < 0)
      return 'Modbus point must define a 0-based address'
    return ''
  }
  if (a.ioa === undefined || a.ioa === null || a.ioa < 0 || a.ioa > 0xffffff) {
    return 'IEC104 ioa must be an integer in [0, 0xFFFFFF]'
  }
  return ''
}
