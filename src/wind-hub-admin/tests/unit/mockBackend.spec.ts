// Mock 模式契约测试：复用 src/mocks/node.ts（与 dev:mock 同一组 handlers），
// 直接经 api/*.ts 真实调用 —— Mock/Real 共用同一 api 层在此得到验证。
import { describe, expect, it, beforeAll, afterAll, afterEach } from 'vitest'
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
    expect(settings.site_id).toBe('mock-site')
    expect(Object.keys(definitions.device_models)).toContain('ads-cx9020')
    expect(devices.items).toHaveLength(3)
    expect(tasks.items).toHaveLength(2)
    expect(sinks).toHaveLength(3)
    expect(overview.device_count).toBe(3)
    expect(overview.devices_connected).toBe(1)
  })

  it('常用运行页面查询可用', async () => {
    expect((await fetchQuality('1h')).window).toBe('1h')
    expect((await runQualityCheck('24h')).window).toBe('24h')
    expect((await fetchLogs({ page: 1, pageSize: 50 })).items.length).toBeGreaterThan(0)
    expect(await fetchLogSources()).toContain('collector')
    expect((await fetchSystemHealth('1h')).cpu_count).toBe(8)
    expect(await fetchWorkers()).toHaveLength(1)
  })

  it('task start/stop 改变 runtime_state，后续 GET 可观察', async () => {
    expect((await fetchTask('task-wtg-slow')).runtime_state).toBe('stopped')
    const started = await startTask('task-wtg-slow')
    expect(started.runtime_state).toBe('running')
    expect((await fetchTask('task-wtg-slow')).runtime_state).toBe('running')
    const stopped = await stopTask('task-wtg-slow')
    expect(stopped.runtime_state).toBe('stopped')
    expect((await fetchTask('task-wtg-slow')).runtime_state).toBe('stopped')
  })

  it('operation polling 能完成状态流转', async () => {
    const operation = await pointTableTest('inv-ads-01')
    expect(operation.operation_id).toMatch(/^mock-op-/)
    const done = await awaitOperation(operation.operation_id)
    expect(done.state).toBe('success')
    expect(done.completed).toBe(done.total)
  })
})
