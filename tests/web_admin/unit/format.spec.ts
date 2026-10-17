// format.ts 单元测试：全站统一的时间格式与空值占位契约。
import { describe, expect, it } from 'vitest'

import {
  EMPTY,
  formatPercent,
  formatTimestamp,
  nowText,
  orEmpty,
} from '../../../src/web_admin/src/utils/format'

describe('formatTimestamp', () => {
  it('格式化为 YYYY-MM-DD HH:mm:ss 并零填充', () => {
    const d = new Date(2026, 0, 5, 9, 7, 3) // 2026-01-05 09:07:03 本地时区
    expect(formatTimestamp(d)).toBe('2026-01-05 09:07:03')
  })

  it('年末/月末边界不串位', () => {
    const d = new Date(2026, 11, 31, 23, 59, 59)
    expect(formatTimestamp(d)).toBe('2026-12-31 23:59:59')
  })
})

describe('nowText', () => {
  it('输出符合统一时间格式', () => {
    expect(nowText()).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
  })
})

describe('orEmpty', () => {
  it('null/undefined/空字符串统一显示为空值占位', () => {
    expect(orEmpty(null)).toBe(EMPTY)
    expect(orEmpty(undefined)).toBe(EMPTY)
    expect(orEmpty('')).toBe(EMPTY)
  })

  it('其他值原样返回（含 0 与 false 语义的字符串）', () => {
    expect(orEmpty(0)).toBe(0)
    expect(orEmpty('0')).toBe('0')
    expect(orEmpty('ok')).toBe('ok')
  })
})


describe('formatPercent（P3-08：Overview 资源百分比展示契约）', () => {
  it('统一保留一位小数（与 System Health 一致）', () => {
    expect(formatPercent(11.521181001283697)).toBe('11.5%')
    expect(formatPercent(0)).toBe('0.0%')
    expect(formatPercent(100)).toBe('100.0%')
    expect(formatPercent(3.06)).toBe('3.1%') // 四舍五入，不截断
  })

  it('null/undefined/NaN/Infinity 统一兜底为占位符', () => {
    expect(formatPercent(null)).toBe(EMPTY)
    expect(formatPercent(undefined)).toBe(EMPTY)
    expect(formatPercent(Number.NaN)).toBe(EMPTY)
    expect(formatPercent(Number.POSITIVE_INFINITY)).toBe(EMPTY)
    expect(formatPercent(Number.NEGATIVE_INFINITY)).toBe(EMPTY)
  })
})
