// useDeviceVerification 测试：Verify All 与单台 Verify 的全局互斥、
// store 编排委托与空目标守卫。
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useDeviceVerification } from '../../src/composables/useDeviceVerification'
import { emptyVerification } from '../../src/domain/devices'
import { useConfigStore } from '../../src/stores/config'
import type { DeviceInst } from '../../src/domain/types'

vi.mock('element-plus', async (importOriginal) => {
  const mod = await importOriginal<typeof import('element-plus')>()
  return {
    ...mod,
    ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
  }
})

function device(id: string): DeviceInst {
  return {
    device_id: id,
    model: 'm1',
    device_group: 'g1',
    host: '10.0.0.1',
    extensions: {},
    enabled: true,
    online: false,
  }
}

describe('useDeviceVerification', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('verifyOf 惰性初始化空验证状态', () => {
    const store = useConfigStore()
    const { verifyOf } = useDeviceVerification()
    const v = verifyOf(device('wtg-001'))
    expect(v.state).toBe('idle')
    expect(store.deviceVerification['wtg-001']).toBe(v)
  })

  it('单台 Verify 委托 configStore.verifyDevice', async () => {
    const store = useConfigStore()
    const spy = vi
      .spyOn(store, 'verifyDevice')
      .mockResolvedValue({ ...emptyVerification(), state: 'success' })
    const { verifyDevice } = useDeviceVerification()

    await verifyDevice(device('wtg-001'))

    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy.mock.calls[0][0].device_id).toBe('wtg-001')
  })

  it('互斥：一台 Verify 进行中时另一台与 Verify All 直接跳过', async () => {
    const store = useConfigStore()
    let release!: () => void
    const spy = vi.spyOn(store, 'verifyDevice').mockImplementation(
      () =>
        new Promise((resolve) => {
          release = () => resolve({ ...emptyVerification(), state: 'success' })
        }),
    )
    const allSpy = vi
      .spyOn(store, 'verifyAllDevices')
      .mockResolvedValue({ failed: 0, warning: 0 } as never)

    const v1 = useDeviceVerification()
    const v2 = useDeviceVerification()
    const inFlight = v1.verifyDevice(device('wtg-001'), true)
    expect(v1.verifyOperationActive.value).toBe(true)

    // 两个调用方共享同一把互斥锁（模块级单例）。
    await v2.verifyDevice(device('wtg-002'), true)
    await v2.verifyAll([device('wtg-003')])
    expect(spy).toHaveBeenCalledTimes(1)
    expect(allSpy).not.toHaveBeenCalled()

    release()
    await inFlight
    expect(v1.verifyOperationActive.value).toBe(false)
  })

  it('Verify All 委托 store.verifyAllDevices；空目标不调用', async () => {
    const store = useConfigStore()
    const allSpy = vi
      .spyOn(store, 'verifyAllDevices')
      .mockResolvedValue({ failed: 0, warning: 0 } as never)
    const { verifyAll, verifyAllRunning } = useDeviceVerification()

    await verifyAll([])
    expect(allSpy).not.toHaveBeenCalled()

    const targets = [device('wtg-001'), device('wtg-002')]
    await verifyAll(targets)
    expect(allSpy).toHaveBeenCalledWith(targets)
    expect(verifyAllRunning.value).toBe(false)
  })
})
