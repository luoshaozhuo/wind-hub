// 设备即时数据领域纯函数测试：点地址展示文本、LatestPointStore 快照
// 读值语义（No sample / BAD quality）与 Data 行构建。
import { describe, expect, it } from 'vitest'

import {
  buildDataRows,
  pointAddressText,
  pointValueFromSnapshot,
} from '../../src/domain/deviceData'
import type { PointDef } from '../../src/domain/types'
import { EMPTY } from '../../src/utils/format'

function point(overrides: Partial<PointDef> = {}): PointDef {
  return {
    point_id: 'p1',
    variable_name: 'Power',
    point_groups: ['measurement'],
    address: { ioa: 1 },
    data_type: 'float32',
    scale: 1,
    offset: 0,
    unit: 'mw',
    description: 'desc',
    ...overrides,
  } as PointDef
}

describe('pointAddressText', () => {
  it('ADS symbol 优先', () => {
    expect(pointAddressText(point({ address: { symbol: 'MAIN.nPower' } as never }))).toBe(
      'MAIN.nPower',
    )
  })

  it('ADS index group/offset', () => {
    expect(
      pointAddressText(point({ address: { index_group: '0xF030', index_offset: '0x1' } as never })),
    ).toBe('0xF030 / 0x1')
  })

  it('Modbus type + address', () => {
    expect(pointAddressText(point({ address: { type: 'holding', address: 100 } as never }))).toBe(
      'holding 100',
    )
    // address 为 0 时仍展示（不落入 EMPTY）。
    expect(pointAddressText(point({ address: { type: 'coil', address: 0 } as never }))).toBe(
      'coil 0',
    )
  })

  it('IEC104 IOA 与空地址', () => {
    expect(pointAddressText(point({ address: { ioa: 42 } as never }))).toBe('IOA 42')
    expect(pointAddressText(point({ address: {} as never }))).toBe(EMPTY)
  })
})

describe('pointValueFromSnapshot', () => {
  it('快照缺失 → No sample 失败', () => {
    const pv = pointValueFromSnapshot(new Map(), 'p1')
    expect(pv).toEqual({ value: EMPTY, read_state: 'failed', error: 'No sample' })
  })

  it('bad quality → 保留值但视为失败', () => {
    const pv = pointValueFromSnapshot(new Map([['p1', { value: 12.5, quality: 'bad' }]]), 'p1')
    expect(pv).toEqual({ value: 12.5, read_state: 'failed', error: 'BAD quality' })
  })

  it('good/无 quality → 成功', () => {
    expect(
      pointValueFromSnapshot(new Map([['p1', { value: true, quality: 'good' }]]), 'p1'),
    ).toEqual({ value: true, read_state: 'success', error: '' })
    expect(pointValueFromSnapshot(new Map([['p1', { value: 0, quality: null }]]), 'p1')).toEqual({
      value: 0,
      read_state: 'success',
      error: '',
    })
  })
})

describe('buildDataRows', () => {
  it('成功行取值、单位与时间戳；失败行展示 EMPTY 与错误', () => {
    const points = [point(), point({ point_id: 'p2', variable_name: 'Wind' })]
    const rows = buildDataRows(
      points,
      (id) =>
        id === 'p1'
          ? { value: 3.5, read_state: 'success', error: '' }
          : { value: EMPTY, read_state: 'failed', error: 'No sample' },
      (unit) => (unit === 'mw' ? 'MW' : unit),
      (i) => `t${i}`,
    )

    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({
      point_id: 'p1',
      value: 3.5,
      unit: 'MW',
      updated_at: 't0',
      read_state: 'success',
      updated: true,
      index: 0,
    })
    expect(rows[1]).toMatchObject({
      point_id: 'p2',
      value: EMPTY,
      updated_at: EMPTY,
      read_state: 'failed',
      error: 'No sample',
      updated: false,
      index: 1,
    })
    // index 与点表顺序一致（Trend/Command 共用定位键）。
    expect(rows.map((r) => r.index)).toEqual([0, 1])
  })
})
