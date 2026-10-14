// status.ts 单元测试：业务状态 → Element Plus tag type 的语义映射契约。
import { describe, expect, it } from 'vitest'

import { statusTagType } from '../../src/utils/status'

describe('statusTagType', () => {
  it('健康/运行中映射为 success', () => {
    for (const s of ['success', 'healthy', 'normal', 'recovered', 'passed', 'ok', 'running']) {
      expect(statusTagType(s)).toBe('success')
    }
  })

  it('退化/部分成功映射为 warning', () => {
    for (const s of ['warning', 'degraded', 'partial']) {
      expect(statusTagType(s)).toBe('warning')
    }
  })

  it('故障/失效映射为 danger', () => {
    for (const s of ['fault', 'failed', 'interrupted', 'active', 'error', 'invalid']) {
      expect(statusTagType(s)).toBe('danger')
    }
  })

  it('中性/未启用映射为 info', () => {
    for (const s of ['unknown', 'disabled', 'stopped', 'idle', 'never', 'unverified', 'skipped']) {
      expect(statusTagType(s)).toBe('info')
    }
  })

  it('进行中的状态返回 undefined（落回默认 primary 样式）', () => {
    for (const s of ['checking', 'testing', 'starting', 'stopping', 'verifying']) {
      expect(statusTagType(s)).toBeUndefined()
    }
  })

  it('空值返回 undefined', () => {
    expect(statusTagType(null)).toBeUndefined()
    expect(statusTagType(undefined)).toBeUndefined()
    expect(statusTagType('')).toBeUndefined()
  })

  it('未收录状态收敛为 info，大小写与首尾空白不敏感', () => {
    expect(statusTagType('some-future-state')).toBe('info')
    expect(statusTagType(' RUNNING ')).toBe('success')
    expect(statusTagType('Failed')).toBe('danger')
  })
})
