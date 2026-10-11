// 设备即时数据行领域纯函数：点地址展示文本、点值读取语义与 Data 行构建。
// Server State（点值来源）由 Vue Query 持有，这里只做纯转换。
import { EMPTY } from '../utils/format'
import type { PointDef } from './types'

export interface PointValue {
  value: number | boolean | string
  read_state: 'success' | 'failed'
  error: string
}

export interface DataRow {
  point_id: string
  variable_name: string
  description: string
  value: number | boolean | string
  unit: string
  groups: string[]
  updated_at: string
  data_type: string
  scale: number
  offset: number
  address: string
  updated: boolean
  read_state: 'success' | 'failed'
  error: string
  // 点在解析后点表中的下标：Trend/Command 与 Data 共用同一点值序列的定位键
  index: number
}

/** 点地址展示文本：按已配置字段自动选择 ADS / Modbus / IEC104 形态。 */
export function pointAddressText(p: PointDef): string {
  const a = p.address
  if (a.symbol) return a.symbol
  if (a.index_group || a.index_offset)
    return `${a.index_group || EMPTY} / ${a.index_offset || EMPTY}`
  if (a.type || a.address !== undefined) return `${a.type || EMPTY} ${a.address ?? EMPTY}`
  if (a.ioa !== undefined) return `IOA ${a.ioa}`
  return EMPTY
}

/** 从 LatestPointStore 快照读取单点值：bad quality 视为读失败。 */
export function pointValueFromSnapshot(
  snapshot: Map<string, { value: number | boolean | string; quality: string | null }>,
  pointId: string,
): PointValue {
  const cached = snapshot.get(pointId)
  if (!cached) return { value: EMPTY, read_state: 'failed', error: 'No sample' }
  if (cached.quality === 'bad')
    return { value: cached.value, read_state: 'failed', error: 'BAD quality' }
  return { value: cached.value, read_state: 'success', error: '' }
}

/**
 * 构建 Data 展示行。timestampOf 由调用方注入（展示层时间戳），
 * unitSymbolOf 解析单位符号。
 */
export function buildDataRows(
  points: PointDef[],
  readValue: (pointId: string) => PointValue,
  unitSymbolOf: (unit: string) => string,
  timestampOf: (index: number) => string,
): DataRow[] {
  return points.map((p, i) => {
    const pv = readValue(p.point_id)
    const failed = pv.read_state === 'failed'
    return {
      point_id: p.point_id,
      variable_name: p.variable_name,
      description: p.description,
      value: failed ? EMPTY : pv.value,
      unit: unitSymbolOf(p.unit),
      groups: p.point_groups,
      updated_at: failed ? EMPTY : timestampOf(i),
      data_type: p.data_type,
      scale: p.scale,
      offset: p.offset,
      address: pointAddressText(p),
      updated: !failed,
      read_state: pv.read_state,
      error: pv.error,
      index: i,
    }
  })
}
