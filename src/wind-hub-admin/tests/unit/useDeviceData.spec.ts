// useDeviceData 集成测试：Vue Query 托管的即时数据查询按 active 启停、
// Data 行构建与手动/自动 refresh（无手写 polling）。
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, defineComponent, h, ref, type Ref } from 'vue'

import { fetchAllDeviceData } from '../../src/api/devices'
import { useDeviceData, type DeviceDataFeature } from '../../src/composables/useDeviceData'
import { useConfigStore } from '../../src/stores/config'
import type { DeviceInst, PointDef } from '../../src/domain/types'

vi.mock('../../src/api/devices', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../src/api/devices')>()
  return { ...mod, fetchAllDeviceData: vi.fn() }
})

const fetchMock = vi.mocked(fetchAllDeviceData)

const point = {
  point_id: 'p1',
  variable_name: 'Power',
  point_groups: ['measurement'],
  address: { ioa: 1 },
  data_type: 'float32',
  scale: 1,
  offset: 0,
  unit: 'mw',
  description: '',
} as PointDef

const device: DeviceInst = {
  device_id: 'wtg-001',
  model: 'modbus_wtg',
  device_group: 'g1',
  host: '10.0.0.1',
  extensions: {},
  enabled: true,
  online: true,
}

function seedStore() {
  const store = useConfigStore()
  store.units = { mw: { symbol: 'MW', name: 'Megawatt' } } as never
  store.deviceModels = [
    {
      id: 'modbus_wtg',
      device_type: 'turbine',
      manufacturer: 'generic',
      protocol: 'modbus',
      point_table: 't1',
      read_mode: '',
      properties: {},
      connection_defaults: {},
    },
  ]
  store.pointTables = [{ id: 't1', protocol: 'modbus', extends: '', remove_points: [] }]
  store.points = { t1: [point] }
}

function mountFeature(active: Ref<boolean>) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  })
  let feature!: DeviceDataFeature
  const wrapper = mount(
    defineComponent({
      setup() {
        feature = useDeviceData(
          ref(device),
          computed(() => active.value),
        )
        return () => h('div')
      },
    }),
    { global: { plugins: [[VueQueryPlugin, { queryClient }]] } },
  )
  return { feature, wrapper, queryClient }
}

describe('useDeviceData', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    seedStore()
    fetchMock.mockReset()
    fetchMock.mockResolvedValue([
      { point_id: 'p1', value: 3.5, quality: 'good', timestamp: '2026-01-01 00:00:00' },
    ] as never)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('inactive 时不拉取；active 后经 Vue Query 拉取并构建 Data 行', async () => {
    const active = ref(false)
    const { feature, wrapper } = mountFeature(active)
    await flushPromises()
    expect(fetchMock).not.toHaveBeenCalled()
    // 点表解析与快照无关：inactive 时行为 No sample。
    expect(feature.dataRows.value).toHaveLength(1)
    expect(feature.dataRows.value[0].read_state).toBe('failed')

    active.value = true
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('wtg-001')
    expect(feature.dataRows.value[0]).toMatchObject({
      point_id: 'p1',
      value: 3.5,
      unit: 'MW',
      read_state: 'success',
    })
    expect(feature.lastRefreshAt.value).not.toBe('')
    wrapper.unmount()
  })

  it('refresh() 触发 refetch 并更新点值', async () => {
    const active = ref(true)
    const { feature, wrapper } = mountFeature(active)
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    await flushPromises()

    fetchMock.mockResolvedValue([
      { point_id: 'p1', value: 9.25, quality: 'good', timestamp: '2026-01-01 00:00:01' },
    ] as never)
    await feature.refresh()
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(feature.dataRows.value[0].value).toBe(9.25)
    wrapper.unmount()
  })
})
