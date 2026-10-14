// Mock 模式契约测试：复用 src/mocks/node.ts（与 dev:mock 同一组 handlers），
// 直接经 api/*.ts 真实调用 —— Mock/Real 共用同一 api 层在此得到验证。
import { describe, expect, it, beforeAll, afterAll, afterEach, vi } from 'vitest'
import { server } from '../../src/mocks/node'
import { resetMockState } from '../../src/mocks/state'
import { fetchSettings, fetchDefinitions } from '../../src/api/config'
import { fetchDevices } from '../../src/api/devices'
import { fetchTasks, startTask, stopTask, fetchTask } from '../../src/api/tasks'
import { fetchSinks } from '../../src/api/sinks'
import {
  fetchOverview,
  fetchQuality,
  runQualityCheck,
  fetchLogs,
  fetchLogSources,
  fetchSystemHealth,
  fetchWorkers,
} from '../../src/api/monitoring'
import { pointTableTest } from '../../src/api/diagnostics'
import { awaitOperation } from '../../src/api/operations'

describe('MSW mock 后端契约', () => {
  beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
  afterEach(() => {
    server.resetHandlers()
    resetMockState()
    vi.useRealTimers()
  })
  afterAll(() => server.close())

  it('App 启动 6 个核心 GET 全部成功', async () => {
    const [settings, definitions, devices, tasks, sinks, overview] = await Promise.all([
      fetchSettings(),
      fetchDefinitions(),
      fetchDevices(),
      fetchTasks(),
      fetchSinks(),
      fetchOverview(),
    ])
    expect(settings.site_id).toBe('wind_farm_a')
    expect(Object.keys(definitions.device_models)).toEqual(
      expect.arrayContaining(['beckhoff_wtg', 'modbus_wtg', 'iec104_wtg', 'pcs_modbus_a']),
    )
    expect(devices.items).toHaveLength(56)
    expect(tasks.items).toHaveLength(6)
    expect(sinks).toHaveLength(4)
    expect(overview.device_count).toBe(56)
    // 56 台中：2 台 disabled、wtg-041 网络不可达、wtg-043 协议失败 → 52 connected
    expect(overview.devices_connected).toBe(52)
  })

  it('常用运行页面查询可用', async () => {
    expect((await fetchQuality('1h')).window).toBe('1h')
    expect((await runQualityCheck('24h')).window).toBe('24h')
    expect((await fetchLogs({ page: 1, pageSize: 50 })).items.length).toBeGreaterThan(0)
    expect(await fetchLogSources()).toContain('ads')
    expect((await fetchSystemHealth('1h')).cpu_count).toBe(8)
    expect(await fetchWorkers()).toHaveLength(2)
  })

  it('task start/stop 改变 runtime_state，后续 GET 可观察', async () => {
    expect((await fetchTask('pcs-fast')).runtime_state).toBe('stopped')
    const started = await startTask('pcs-fast')
    expect(started.runtime_state).toBe('starting')
    // 时间推进过 TASK_TRANSITION_MS 后落定 RUNNING
    vi.setSystemTime(Date.now() + 10_000)
    expect((await fetchTask('pcs-fast')).runtime_state).toBe('running')

    const stopped = await stopTask('pcs-fast')
    expect(stopped.runtime_state).toBe('stopping')
    vi.setSystemTime(Date.now() + 10_000)
    expect((await fetchTask('pcs-fast')).runtime_state).toBe('stopped')
  })

  it('operation polling 能完成状态流转', async () => {
    const operation = await pointTableTest('wtg-001')
    expect(operation.operation_id).toMatch(/^mock-op-/)
    const done = await awaitOperation(operation.operation_id)
    expect(done.state).toBe('success')
    expect(done.completed).toBe(done.total)
  })
})
