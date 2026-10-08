/** 查询可用性与业务设备状态分离；失败缓存不等同于实时数据。 */
export type DataSourceState = 'valid' | 'pending' | 'stale' | 'unavailable' | 'invalid'

export interface QueryAvailability {
  isPending: boolean
  isError: boolean
  hasData: boolean
  isInvalid?: boolean
}

export function dataSourceState(query: QueryAvailability): DataSourceState {
  if (query.isError) return query.hasData ? 'stale' : 'unavailable'
  if (query.isPending || !query.hasData) return 'pending'
  if (query.isInvalid) return 'invalid'
  return 'valid'
}

export const dataSourceLabels: Record<DataSourceState, string> = {
  valid: '数据有效',
  pending: '等待数据',
  stale: '数据中断',
  unavailable: '数据不可用',
  invalid: '数据无效',
}

export function unavailableValue(state: DataSourceState, value: string | number): string {
  return state === 'valid' ? String(value) : '—'
}
