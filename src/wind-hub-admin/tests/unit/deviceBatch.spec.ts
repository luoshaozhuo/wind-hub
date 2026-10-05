// 批量创建设备领域纯函数测试：编号模板渲染、排除区间解析、
// 预览生成的空值/冲突校验与非法区间。
import { describe, expect, it } from 'vitest'

import {
  buildBatchPreview,
  excludedBatchNumbers,
  renderBatchPattern,
  type BatchTemplate,
} from '../../src/domain/deviceBatch'
import type { DeviceInst, DeviceModelDef } from '../../src/domain/types'

function model(protocol: 'ads' | 'modbus' | 'iec104'): DeviceModelDef {
  return {
    id: 'm1',
    device_type: 'turbine',
    manufacturer: 'generic',
    protocol,
    point_table: 't1',
    read_mode: '',
    properties: {},
    connection_defaults: {},
  }
}

function template(overrides: Partial<BatchTemplate> = {}): BatchTemplate {
  return {
    model: 'm1',
    group: 'g1',
    from: 1,
    to: 3,
    exclude: '',
    id_pattern: 'wtg-{num:03}',
    host_pattern: '192.168.151.{num}',
    netid_pattern: '192.168.151.{num}.1.1',
    ...overrides,
  }
}

function device(overrides: Partial<DeviceInst> = {}): DeviceInst {
  return {
    device_id: 'existing',
    model: 'm1',
    device_group: 'g1',
    host: '10.0.0.1',
    extensions: {},
    enabled: true,
    online: false,
    ...overrides,
  }
}

describe('renderBatchPattern', () => {
  it('支持 {num} 占位', () => {
    expect(renderBatchPattern('wtg-{num}', 7)).toBe('wtg-7')
  })

  it('支持 {num:03} 零填充', () => {
    expect(renderBatchPattern('wtg-{num:03}', 7)).toBe('wtg-007')
  })

  it('支持 {num+100} 偏移与组合零填充', () => {
    expect(renderBatchPattern('{num+100}', 1)).toBe('101')
    expect(renderBatchPattern('{num+100:03}', 1)).toBe('101')
    expect(renderBatchPattern('192.168.151.{num}.1.1', 12)).toBe('192.168.151.12.1.1')
  })

  it('非模板文本原样保留', () => {
    expect(renderBatchPattern('static', 5)).toBe('static')
  })
})

describe('excludedBatchNumbers', () => {
  it('解析单号与区间', () => {
    expect([...excludedBatchNumbers('5,17,30-32')].sort((a, b) => a - b)).toEqual([
      5, 17, 30, 31, 32,
    ])
  })

  it('容忍空白与反向区间，忽略非法 token', () => {
    expect([...excludedBatchNumbers(' 3 , 8-6 ,abc')].sort((a, b) => a - b)).toEqual([3, 6, 7, 8])
    expect(excludedBatchNumbers('').size).toBe(0)
  })
})

describe('buildBatchPreview', () => {
  it('生成排除后的编号序列行', () => {
    const rows = buildBatchPreview(template({ exclude: '2' }), model('modbus'), [])
    expect(rows.map((r) => r.num)).toEqual([1, 3])
    expect(rows[0]).toMatchObject({ id: 'wtg-001', host: '192.168.151.1', error: '' })
    // 非 ADS 模型不生成 netid。
    expect(rows[0].netid).toBe('')
  })

  it('非法区间与超大批量返回空', () => {
    expect(buildBatchPreview(template({ from: 5, to: 1 }), model('modbus'), [])).toEqual([])
    expect(buildBatchPreview(template({ from: 1, to: 1001 }), model('modbus'), [])).toEqual([])
    expect(buildBatchPreview(template(), undefined, [])).toEqual([])
  })

  it('标记空 ID/host 与既有设备冲突', () => {
    const rows = buildBatchPreview(template({ id_pattern: '', to: 1 }), model('modbus'), [
      device({ host: '192.168.151.1' }),
    ])
    expect(rows[0].error).toContain('empty ID/host')
    expect(rows[0].error).toContain('duplicate host')

    const dupId = buildBatchPreview(template({ to: 1 }), model('modbus'), [
      device({ device_id: 'wtg-001' }),
    ])
    expect(dupId[0].error).toContain('duplicate device ID')
  })

  it('标记批内生成的重复 ID', () => {
    const rows = buildBatchPreview(
      template({ id_pattern: 'wtg-fixed', from: 1, to: 2 }),
      model('modbus'),
      [],
    )
    expect(rows[0].error).toBe('')
    expect(rows[1].error).toContain('duplicate device ID')
  })

  it('ADS 模型校验 AMS Net ID 形态与冲突', () => {
    const rows = buildBatchPreview(template(), model('ads'), [])
    expect(rows[0].netid).toBe('192.168.151.1.1.1')
    expect(rows.every((r) => r.error === '')).toBe(true)

    const invalid = buildBatchPreview(template({ netid_pattern: '{num}' }), model('ads'), [])
    expect(invalid[0].error).toContain('invalid AMS Net ID')

    const dup = buildBatchPreview(template({ to: 1 }), model('ads'), [
      device({ extensions: { target_net_id: '192.168.151.1.1.1' } }),
    ])
    expect(dup[0].error).toContain('duplicate AMS Net ID')
  })
})
