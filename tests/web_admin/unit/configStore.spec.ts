// Config Store 显式持久化测试：编辑只能经 mutate() → markDirty → scheduleSave
// → save() 落盘（PUT /admin-state）；绕过 mutate 的直接 state 修改不得触发保存。
// 快照同步的脏语义：脏时仅补丁运行时字段，保存失败强制回退服务器状态。
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { queryClient } from '../../src/api/queryClient'
import { CONFIG_QUERY_KEYS, qk } from '../../src/api/queryKeys'
import { replaceAdminState, type ConfigApplyDto } from '../../src/api/config'
import type { DefinitionsDto } from '../../src/api/config'
import type { DeviceDto } from '../../src/api/devices'
import type { OverviewDto } from '../../src/api/monitoring'
import type { SettingsDto } from '../../src/api/config'
import type { SinkDto } from '../../src/api/sinks'
import type { TaskDto } from '../../src/api/tasks'
import { useConfigStore } from '../../src/stores/config'

vi.mock('../../src/api/config', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../src/api/config')>()
  return { ...mod, replaceAdminState: vi.fn() }
})

const replaceAdminStateMock = vi.mocked(replaceAdminState)

function settingsDto(): SettingsDto {
  return {
    site_id: 'site-1',
    site_name: 'Site 1',
    api_host: '127.0.0.1',
    api_port: 8080,
    ads_local_ip: '',
    ads_local_ams_net_id: '',
    ads_username: 'Administrator',
    ads_password: '',
  } as unknown as SettingsDto
}

function definitionsDto(): DefinitionsDto {
  return {
    units: { mw: { symbol: 'MW', name: 'Megawatt' } },
    device_types: { iec104: { name: 'IEC 104' } },
    device_models: {
      'iec104-rtu': {
        device_type: 'iec104',
        manufacturer: 'generic',
        protocol: 'iec104',
        point_table: 'iec104-table',
        properties: {},
        connection_defaults: {},
      },
    },
    point_tables: {
      'iec104-table': {
        protocol: 'iec104',
        points: [
          {
            point_id: 'p1',
            variable_name: 'P1',
            point_groups: ['default'],
            address: { ioa: 1 },
            data_type: 'float32',
            scale: 1,
            offset: 0,
            unit: 'mw',
            description: '',
          },
        ],
      },
    },
    point_groups: ['default'],
    device_groups: ['g1'],
  } as unknown as DefinitionsDto
}

function deviceDto(overrides: Partial<DeviceDto> = {}): DeviceDto {
  return {
    device_id: 'wtg-001',
    model: 'iec104-rtu',
    device_group: 'g1',
    host: '10.0.0.1',
    port_override: null,
    extension_overrides: {},
    enabled: true,
    connected: true,
    ...overrides,
  } as unknown as DeviceDto
}

function taskDto(): TaskDto {
  return {
    task_id: 't1',
    device: 'wtg-001',
    device_group: '',
    point_group: 'default',
    interval: 5,
    targets: ['kafka-main'],
    enabled: true,
    runtime_state: 'running',
  } as unknown as TaskDto
}

function sinkDto(): SinkDto {
  return {
    name: 'kafka-main',
    type: 'kafka',
    enabled: true,
    connection: {},
    points: [],
    healthy: true,
    message: '',
    queue_depth: 0,
  } as unknown as SinkDto
}

function overviewDto(): OverviewDto {
  return { runtime_running: true } as unknown as OverviewDto
}

/** 六部分快照全量到达 → hydrated=true（save 的前置条件）。 */
function hydrate(store: ReturnType<typeof useConfigStore>) {
  store.syncFromServer({
    settings: settingsDto(),
    definitions: definitionsDto(),
    devices: [deviceDto()],
    tasks: [taskDto()],
    sinks: [sinkDto()],
    overview: overviewDto(),
  })
}

describe('config store 显式持久化', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setActivePinia(createPinia())
    queryClient.clear()
    replaceAdminStateMock.mockReset()
    replaceAdminStateMock.mockResolvedValue({
      success: true,
      errors: [],
    } as unknown as ConfigApplyDto)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('mutate 执行变更、置脏，防抖后触发一次 PUT /admin-state', async () => {
    const store = useConfigStore()
    hydrate(store)
    expect(store.dirty).toBe(false)

    store.mutate(() => {
      store.devices[0].host = '10.0.0.99'
    })
    expect(store.devices[0].host).toBe('10.0.0.99')
    expect(store.dirty).toBe(true)
    // 防抖窗口内不保存。
    expect(replaceAdminStateMock).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(400)
    expect(replaceAdminStateMock).toHaveBeenCalledTimes(1)
    const payload = replaceAdminStateMock.mock.calls[0][0]
    expect(payload.devices[0].host).toBe('10.0.0.99')
    // 保存成功后清除脏标记。
    expect(store.dirty).toBe(false)
  })

  it('连续 mutate 合并为一次防抖保存', async () => {
    const store = useConfigStore()
    hydrate(store)

    store.mutate(() => {
      store.devices[0].host = '10.0.0.2'
    })
    await vi.advanceTimersByTimeAsync(100)
    store.mutate(() => {
      store.devices[0].host = '10.0.0.3'
    })
    await vi.advanceTimersByTimeAsync(400)

    expect(replaceAdminStateMock).toHaveBeenCalledTimes(1)
    expect(replaceAdminStateMock.mock.calls[0][0].devices[0].host).toBe('10.0.0.3')
  })

  it('绕过 mutate 直接改 state 不触发保存（无隐式持久化）', async () => {
    const store = useConfigStore()
    hydrate(store)

    store.devices[0].host = '10.0.0.77'
    await vi.advanceTimersByTimeAsync(1000)

    expect(replaceAdminStateMock).not.toHaveBeenCalled()
  })

  it('未 hydrated 时 save 直接返回（半同步状态不落盘）', async () => {
    const store = useConfigStore()
    store.mutate(() => {
      store.devices.push({
        device_id: 'x',
        model: '',
        device_group: '',
        host: '1.1.1.1',
        extensions: {},
        enabled: true,
        online: false,
      })
    })
    await vi.advanceTimersByTimeAsync(1000)
    expect(replaceAdminStateMock).not.toHaveBeenCalled()
  })

  it('保存成功后统一失效配置类查询', async () => {
    const store = useConfigStore()
    hydrate(store)
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    store.mutate(() => {
      store.devices[0].host = '10.0.0.5'
    })
    await vi.advanceTimersByTimeAsync(400)

    expect(replaceAdminStateMock).toHaveBeenCalledTimes(1)
    for (const key of CONFIG_QUERY_KEYS) {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: key })
    }
    invalidateSpy.mockRestore()
  })

  it('保存失败：清除脏标记并从 query 缓存强制回退服务器状态', async () => {
    const store = useConfigStore()
    hydrate(store)
    // 缓存中的服务器状态（回退来源）。
    queryClient.setQueryData(qk.devices, { items: [deviceDto()] })
    queryClient.setQueryData(qk.tasks, { items: [taskDto()] })
    replaceAdminStateMock.mockResolvedValue({
      success: false,
      errors: ['apply failed'],
    } as unknown as ConfigApplyDto)

    store.mutate(() => {
      store.devices[0].host = '10.0.0.66'
    })
    await vi.advanceTimersByTimeAsync(400)

    expect(replaceAdminStateMock).toHaveBeenCalledTimes(1)
    expect(store.dirty).toBe(false)
    // 编辑被回退为缓存中的服务器值。
    expect(store.devices[0].host).toBe('10.0.0.1')
  })
})

describe('config store 快照同步脏语义', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    queryClient.clear()
    replaceAdminStateMock.mockReset()
  })

  it('非脏：快照整体重建编辑投影', () => {
    const store = useConfigStore()
    hydrate(store)

    store.syncFromServer({ devices: [deviceDto({ host: '192.168.1.1', connected: false })] })

    expect(store.devices[0].host).toBe('192.168.1.1')
    expect(store.devices[0].online).toBe(false)
  })

  it('脏：仅补丁运行时字段，绝不覆盖未保存编辑', () => {
    const store = useConfigStore()
    hydrate(store)
    store.mutate(() => {
      store.devices[0].host = '10.9.9.9'
    })

    store.syncFromServer({ devices: [deviceDto({ host: '192.168.1.1', connected: false })] })
    store.syncFromServer({ tasks: [{ ...taskDto(), runtime_state: 'stopped' }] })

    // 编辑保留；运行时字段跟随服务器。
    expect(store.devices[0].host).toBe('10.9.9.9')
    expect(store.devices[0].online).toBe(false)
    expect(store.tasks[0].runtime).toBe('STOPPED')
  })

  it('patchTaskRuntime 更新运行时不置脏', () => {
    const store = useConfigStore()
    hydrate(store)

    store.patchTaskRuntime('t1', 'FAILED')

    expect(store.tasks[0].runtime).toBe('FAILED')
    expect(store.dirty).toBe(false)
  })
})
