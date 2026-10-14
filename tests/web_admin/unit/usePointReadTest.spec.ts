// usePointReadTest 测试：Test Read 的设备守卫、地址校验、
// raw-address 回落匹配与错误码；候选解码纯函数。
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { computed } from 'vue'

import { protocolRead } from '../../src/api/diagnostics'
import { usePointReadTest } from '../../src/composables/usePointReadTest'
import { decodeCandidates, emptyTestCandidates } from '../../src/domain/pointReadTest'
import { useConfigStore } from '../../src/stores/config'
import type { DeviceInst, PointDef } from '../../src/domain/types'

vi.mock('../../src/api/diagnostics', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../src/api/diagnostics')>()
  return { ...mod, protocolRead: vi.fn() }
})

const readMock = vi.mocked(protocolRead)

const adsPoint = {
  point_id: 'p1',
  variable_name: 'Power',
  point_groups: ['pg1'],
  address: { symbol: 'MAIN.power' },
  data_type: 'float32',
  scale: 1,
  offset: 0,
  unit: 'mw',
  description: '',
} as PointDef

const device: DeviceInst = {
  device_id: 'wtg-001',
  model: 'm1',
  device_group: 'g1',
  host: '10.0.0.1',
  extensions: {},
  enabled: true,
  online: true,
}

function seedStore(withDevice = true) {
  const store = useConfigStore()
  store.deviceModels = [
    {
      id: 'm1',
      device_type: 'turbine',
      manufacturer: 'generic',
      protocol: 'ads',
      point_table: 't1',
      read_mode: '',
      properties: {},
      connection_defaults: {},
    },
  ]
  store.pointTables = [{ id: 't1', protocol: 'ads', extends: '', remove_points: [] }]
  store.points = { t1: [adsPoint] }
  store.devices = withDevice ? [device] : []
}

function makeTest(requestText: string) {
  return usePointReadTest(
    computed(() => 'ads' as const),
    computed(() => ({ symbol: 'MAIN.power' })),
    computed(() => requestText),
  )
}

describe('usePointReadTest', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    readMock.mockReset()
  })

  it('未选设备时拒绝执行', async () => {
    seedStore(false)
    const test = makeTest('Symbol · MAIN.power')
    test.reset()
    await test.run()
    expect(test.testError.value).toBe('Select a device first')
    expect(readMock).not.toHaveBeenCalled()
  })

  it('reset 优先选择在线且启用的设备', () => {
    seedStore()
    const store = useConfigStore()
    store.devices.push({ ...device, device_id: 'wtg-002', online: false })
    const test = makeTest('Symbol · MAIN.power')
    test.reset()
    expect(test.testDeviceId.value).toBe('wtg-001')
  })

  it('raw-address 不可用且匹配不到已配置点 → RAW_READ_UNAVAILABLE', async () => {
    seedStore()
    const test = makeTest('Index · 0x4020 / 0x1')
    test.reset()
    await test.run()
    expect(test.testError.value).toContain('RAW_READ_UNAVAILABLE')
    expect(readMock).not.toHaveBeenCalled()
  })

  it('匹配已配置点后执行协议读；bad quality 报 BAD_QUALITY', async () => {
    seedStore()
    readMock.mockResolvedValue({ quality: 'bad', value: null, timestamp: 't' } as never)
    const test = makeTest('Symbol · MAIN.power')
    test.reset()
    await test.run()

    expect(readMock).toHaveBeenCalledWith('wtg-001', 'p1')
    expect(test.testError.value).toContain('BAD_QUALITY')
    expect(test.testLoading.value).toBe(false)
  })

  it('协议读成功但无 raw bytes：不报错、候选保持占位', async () => {
    seedStore()
    readMock.mockResolvedValue({ quality: 'good', value: 1.5, timestamp: 't' } as never)
    const test = makeTest('Symbol · MAIN.power')
    test.reset()
    await test.run()

    expect(test.testError.value).toBe('')
    expect(test.testCandidates.value).toEqual(emptyTestCandidates())
  })

  it('协议读异常 → READ_FAILED', async () => {
    seedStore()
    readMock.mockRejectedValue(new Error('connection refused'))
    const test = makeTest('Symbol · MAIN.power')
    test.reset()
    await test.run()

    expect(test.testError.value).toContain('READ_FAILED')
    expect(test.testError.value).toContain('connection refused')
  })

  it('地址非法时短路为校验错误', async () => {
    seedStore()
    const test = usePointReadTest(
      computed(() => 'ads' as const),
      computed(() => ({})),
      computed(() => 'Index · — / —'),
    )
    test.reset()
    await test.run()
    expect(test.testError.value).not.toBe('')
    expect(readMock).not.toHaveBeenCalled()
  })
})

describe('decodeCandidates', () => {
  it('按小端解码多种候选类型', () => {
    // 0x3FC00000 = float32 1.5；bool 取首字节。
    const bytes = new Uint8Array([0, 0, 0xc0, 0x3f, 0, 0, 0, 0])
    const rows = decodeCandidates(bytes)
    expect(rows.find((r) => r.type === 'float32')?.value).toBe('1.5')
    expect(rows.find((r) => r.type === 'bool')?.value).toBe('false')
    expect(rows.find((r) => r.type === 'int16')?.value).toBe('0')
    expect(rows).toHaveLength(emptyTestCandidates().length)
  })
})
