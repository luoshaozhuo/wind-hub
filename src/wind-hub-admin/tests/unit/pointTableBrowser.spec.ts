// usePointTableBrowser 测试：Point Table 选择切换解析行与协议、
// 搜索过滤与分页回收。
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { nextTick, ref } from 'vue'

import { usePointTableBrowser } from '../../src/composables/usePointTableBrowser'
import { useConfigStore } from '../../src/stores/config'
import type { PointDef } from '../../src/domain/types'

function point(id: string, overrides: Partial<PointDef> = {}): PointDef {
  return {
    point_id: id,
    variable_name: id.toUpperCase(),
    point_groups: ['measurement'],
    address: { type: 'holding', address: 1 },
    data_type: 'float32',
    scale: 1,
    offset: 0,
    unit: 'mw',
    description: '',
    ...overrides,
  } as PointDef
}

function seedStore() {
  const store = useConfigStore()
  store.units = { mw: { symbol: 'MW', name: 'Megawatt' } } as never
  store.pointTables = [
    { id: 't1', protocol: 'modbus', extends: '', remove_points: [] },
    { id: 't2', protocol: 'iec104', extends: '', remove_points: [] },
  ]
  store.points = {
    t1: [point('p1'), point('p2', { variable_name: 'WindSpeed' })],
    t2: [point('q1', { address: { ioa: 7 } })],
  }
}

describe('usePointTableBrowser', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    seedStore()
  })

  it('table select 切换解析行与协议/地址标签', () => {
    const pointTable = ref('t1')
    const browser = usePointTableBrowser(pointTable)

    expect(browser.rows.value.map((p) => p.point_id)).toEqual(['p1', 'p2'])
    expect(browser.protocol.value).toBe('modbus')
    expect(browser.addrLabel.value).toBe('Type / Address')

    pointTable.value = 't2'
    expect(browser.rows.value.map((p) => p.point_id)).toEqual(['q1'])
    expect(browser.protocol.value).toBe('iec104')
    expect(browser.addrLabel.value).toBe('IOA')
    expect(browser.addressOf(browser.rows.value[0])).toBe('ioa 7')
  })

  it('搜索命中 point_id / variable_name / 地址 / 组', () => {
    const pointTable = ref('t1')
    const browser = usePointTableBrowser(pointTable)

    browser.pointSearch.value = 'wind'
    expect(browser.filteredRows.value.map((p) => p.point_id)).toEqual(['p2'])

    browser.pointSearch.value = 'holding'
    expect(browser.filteredRows.value).toHaveLength(2)

    browser.pointSearch.value = 'measurement'
    expect(browser.filteredRows.value).toHaveLength(2)

    browser.pointSearch.value = 'nothing-matches'
    expect(browser.filteredRows.value).toHaveLength(0)
  })

  it('切表或搜索时页码重置；过滤结果收缩时页码回收', async () => {
    const pointTable = ref('t1')
    const browser = usePointTableBrowser(pointTable)
    browser.pointPageSize.value = 1
    browser.pointPage.value = 2

    browser.pointSearch.value = 'p1'
    await nextTick()
    expect(browser.pointPage.value).toBe(1)

    browser.pointSearch.value = ''
    browser.pointPage.value = 2
    pointTable.value = 't2'
    await nextTick()
    expect(browser.pointPage.value).toBe(1)
    expect(browser.pagedRows.value).toHaveLength(1)
  })
})
