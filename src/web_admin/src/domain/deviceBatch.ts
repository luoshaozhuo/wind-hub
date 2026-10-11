// 批量创建设备领域纯函数：编号模板渲染、排除区间解析、预览行生成与冲突校验。
import type { DeviceInst, DeviceModelDef } from './types'

export interface BatchTemplate {
  model: string
  group: string
  from: number
  to: number
  exclude: string
  id_pattern: string
  host_pattern: string
  netid_pattern: string
}

export interface BatchRow {
  num: number
  id: string
  host: string
  netid: string
  error: string
}

/** 渲染 {num} / {num:03} / {num+100} / {num+100:03} 模板占位。 */
export function renderBatchPattern(pattern: string, num: number): string {
  return pattern.replace(/\{num(?:\+(-?\d+))?(?::(\d+))?\}/g, (_all, delta, width) => {
    const value = num + Number(delta || 0)
    return width ? String(value).padStart(Number(width), '0') : String(value)
  })
}

/** 解析排除清单："5,17,30-32" → 编号集合。 */
export function excludedBatchNumbers(exclude: string): Set<number> {
  const result = new Set<number>()
  for (const token of exclude
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean)) {
    const range = token.match(/^(\d+)\s*-\s*(\d+)$/)
    if (range) {
      const a = Number(range[1])
      const b = Number(range[2])
      for (let n = Math.min(a, b); n <= Math.max(a, b); n++) result.add(n)
    } else if (/^\d+$/.test(token)) result.add(Number(token))
  }
  return result
}

/** 生成批量预览行（含空值与 ID/host/AMS Net ID 冲突校验）；非法区间返回空。 */
export function buildBatchPreview(
  template: BatchTemplate,
  model: DeviceModelDef | undefined,
  existingDevices: DeviceInst[],
): BatchRow[] {
  if (!model || template.from > template.to || template.to - template.from > 999) return []
  const excluded = excludedBatchNumbers(template.exclude)
  const rows: BatchRow[] = []
  const seenIds = new Set<string>()
  const seenHosts = new Set<string>()
  const seenNetIds = new Set<string>()

  for (let num = template.from; num <= template.to; num++) {
    if (excluded.has(num)) continue
    const id = renderBatchPattern(template.id_pattern, num)
    const host = renderBatchPattern(template.host_pattern, num)
    const netid = model.protocol === 'ads' ? renderBatchPattern(template.netid_pattern, num) : ''
    const errors: string[] = []
    if (!id || !host) errors.push('empty ID/host')
    if (existingDevices.some((d) => d.device_id === id) || seenIds.has(id))
      errors.push('duplicate device ID')
    if (existingDevices.some((d) => d.host === host) || seenHosts.has(host))
      errors.push('duplicate host')
    if (model.protocol === 'ads') {
      if (netid.split('.').length !== 6) errors.push('invalid AMS Net ID')
      if (
        existingDevices.some((d) => d.extensions?.target_net_id === netid) ||
        seenNetIds.has(netid)
      )
        errors.push('duplicate AMS Net ID')
    }
    seenIds.add(id)
    seenHosts.add(host)
    if (netid) seenNetIds.add(netid)
    rows.push({ num, id, host, netid, error: errors.join('; ') })
  }
  return rows
}
