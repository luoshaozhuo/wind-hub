// 高保真 Mock 能力矩阵测试：对应 1f3cb794 旧 Mock 行为基线逐条验证，
// 全部经真实 api/*.ts → MSW → mock service 路径（与页面同源）。
import { describe, expect, it, beforeAll, afterAll, afterEach, vi } from 'vitest'
import { server } from '../../src/mocks/node'
import { resetMockState } from '../../src/mocks/state'
import {
  fetchDevice,
  fetchDevices,
  fetchAllDeviceData,
  fetchDeviceTrend,
  sendDeviceCommand,
} from '../../src/api/devices'
import { fetchTask, startTask, fetchTaskInstances } from '../../src/api/tasks'
import { fetchSink, verifySink, writeTestSink } from '../../src/api/sinks'
import { protocolRead, pointTableTest, ping, protocolCheck } from '../../src/api/diagnostics'
import { awaitOperation, fetchOperation } from '../../src/api/operations'
import {
  applyConfig,
  fetchConfigFile,
  fetchConfigHistory,
  importConfig,
  restoreConfig,
} from '../../src/api/config'
import {
  fetchOverview,
  fetchQuality,
  fetchLogs,
  fetchSystemHealth,
  runQualityCheck,
} from '../../src/api/monitoring'
import { ApiError } from '../../src/api/client'

describe('高保真 Mock 能力矩阵', () => {
  beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
  afterEach(() => {
    server.resetHandlers()
    resetMockState()
    vi.useRealTimers()
  })
  afterAll(() => server.close())

  it('1. 设备规模 ≥50 且三种协议齐备', async () => {
    const page = await fetchDevices(200)
    expect(page.items.length).toBeGreaterThanOrEqual(50)
    const protocols = new Set(page.items.map((d) => d.protocol))
    expect(protocols).toEqual(new Set(['ads', 'modbus', 'iec104']))
    const groups = new Set(page.items.map((d) => d.device_group))
    expect(groups.size).toBeGreaterThanOrEqual(4)
  })

  it('2. 固定故障场景在 Devices 可观察', async () => {
    expect((await fetchDevice('wtg-041')).connected).toBe(false)
    expect((await fetchDevice('wtg-041')).last_error).toContain('unreachable')
    expect((await fetchDevice('wtg-043')).connected).toBe(false)
    expect((await fetchDevice('wtg-043')).last_error).toContain('ADS')
    expect((await fetchDevice('wtg-048')).enabled).toBe(false)
    expect((await fetchDevice('wtg-001')).connected).toBe(true)
  })

  it('3. 点值确定性且随时间变化（无随机）', async () => {
    const a = await fetchAllDeviceData('wtg-001')
    const b = await fetchAllDeviceData('wtg-001')
    const sampleA = a.find((p) => p.point_id === 'wtg_mb_001')!
    const sampleB = b.find((p) => p.point_id === 'wtg_mb_001')!
    expect(sampleA.value).toEqual(sampleB.value) // 同一秒稳定
    vi.setSystemTime(Date.now() + 7000)
    const c = await fetchAllDeviceData('wtg-001')
    const sampleC = c.find((p) => p.point_id === 'wtg_mb_001')!
    expect(sampleC.value).not.toEqual(sampleA.value) // 随时间变化
  })

  it('4. Trend 尾点 == Data 当前值 == Diagnostics Read', async () => {
    const data = await fetchAllDeviceData('wtg-002')
    const point = data.find((p) => p.data_type !== 'bool')!
    const trend = await fetchDeviceTrend('wtg-002', [point.point_id], 600)
    const tail = trend[0].samples[trend[0].samples.length - 1]
    const read = await protocolRead('wtg-002', point.point_id)
    expect(tail.value as number).toBeCloseTo(point.value as number, 5)
    expect(read.value as number).toBeCloseTo(point.value as number, 5)
  })

  it('5. Command 成功后 Data / Read / Trend 全部联动到新值', async () => {
    const before = await fetchAllDeviceData('wtg-005')
    const point = before.find((p) => p.data_type !== 'bool')!
    const result = await sendDeviceCommand('wtg-005', point.point_id, 88.5)
    expect(result.success).toBe(true)
    expect(result.readback).toBe(88.5)
    const after = await fetchAllDeviceData('wtg-005')
    const changed = after.find((p) => p.point_id === point.point_id)!
    expect(changed.value as number).toBeCloseTo(88.5, 0)
    const read = await protocolRead('wtg-005', point.point_id)
    expect(read.value as number).toBeCloseTo(88.5, 0)
    // Logs 追加
    const logs = await fetchLogs({ page: 1, pageSize: 10, keyword: 'command applied' })
    expect(logs.items.length).toBeGreaterThan(0)
  })

  it('6. Command 固定失败场景带稳定错误码', async () => {
    const rejected = await sendDeviceCommand('wtg-044', 'wtg_ads_001', 1)
    expect(rejected.success).toBe(false)
    expect(rejected.error).toContain('COMMAND_REJECTED')
    const timeout = await sendDeviceCommand('wtg-041', 'wtg_ads_001', 1)
    expect(timeout.success).toBe(false)
    expect(timeout.error).toContain('COMMAND_TIMEOUT')
    const session = await sendDeviceCommand('wtg-043', 'wtg_ads_001', 1)
    expect(session.success).toBe(false)
    expect(session.error).toContain('ADS_SESSION_UNAVAILABLE')
  })

  it('7. Verify 中间态经 Operation polling 可观察（running → success）', async () => {
    const operation = await pointTableTest('wtg-001')
    const mid = await fetchOperation(operation.operation_id)
    expect(['pending', 'running']).toContain(mid.state)
    const done = await awaitOperation(operation.operation_id)
    expect(done.state).toBe('success')
  })

  it('8. Verify 点部分失败 → partial；网络失败 → failed', async () => {
    const partial = await awaitOperation((await pointTableTest('wtg-026')).operation_id)
    expect(partial.state).toBe('partial')
    const failedRows = (partial.result?.points as Array<{ success: boolean }>).filter(
      (r) => !r.success,
    )
    expect(failedRows.length).toBeGreaterThan(0)

    const failedOp = await pointTableTest('wtg-041')
    let failed = await fetchOperation(failedOp.operation_id)
    while (!['success', 'partial', 'failed', 'cancelled'].includes(failed.state)) {
      failed = await fetchOperation(failedOp.operation_id)
    }
    expect(failed.state).toBe('failed')
    expect(failed.error?.code).toBe('HOST_UNREACHABLE')
  })

  it('9. 网络失败阻断协议检查', async () => {
    expect((await ping('192.168.151.41')).reachable).toBe(false)
    expect((await protocolCheck('wtg-041')).connected).toBe(false)
    expect((await protocolCheck('wtg-043')).connected).toBe(false) // 协议会话失败
    expect((await protocolCheck('wtg-001')).connected).toBe(true)
  })

  it('10. Task start：STOPPED → STARTING → RUNNING', async () => {
    const started = await startTask('pcs-fast')
    expect(started.runtime_state).toBe('starting')
    vi.setSystemTime(Date.now() + 10_000)
    expect((await fetchTask('pcs-fast')).runtime_state).toBe('running')
  })

  it('11. Task 故障目标启动失败：409 + 回滚 STOPPED + 稳定错误码', async () => {
    await expect(startTask('wtg-041-diag')).rejects.toMatchObject({ status: 409 })
    try {
      await startTask('wtg-041-diag')
      expect.unreachable()
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError)
      expect((error as ApiError).code).toBe('TASK_DEVICE_UNAVAILABLE')
    }
    expect((await fetchTask('wtg-041-diag')).runtime_state).toBe('stopped')
    const logs = await fetchLogs({ page: 1, pageSize: 10, keyword: 'start failed' })
    expect(logs.items.length).toBeGreaterThan(0)
  })

  it('12. 运行中 Task 实例状态由设备场景派生', async () => {
    const instances = await fetchTaskInstances('turbine-ads-all')
    const byDevice = Object.fromEntries(instances.map((i) => [i.device_id, i.state]))
    expect(byDevice['wtg-041']).toBe('failed')
    expect(byDevice['wtg-043']).toBe('failed')
    expect(byDevice['wtg-026']).toBe('warning')
    expect(byDevice['wtg-025']).toBe('running')
  })

  it('13. Sink verify 分阶段：file 全过、db 固定失败并阻断后续', async () => {
    const ok = await verifySink('file_archive')
    expect(ok.success).toBe(true)
    expect(ok.steps!.every((s) => s.state === 'passed')).toBe(true)

    const failed = await verifySink('db_main')
    expect(failed.success).toBe(false)
    const states = failed.steps!.map((s) => s.state)
    expect(states).toContain('failed')
    expect(states).toContain('skipped')
    expect(failed.message).toContain('SINK_CONNECTION_REFUSED')

    const kafka = await verifySink('kafka_main')
    expect(kafka.success).toBe(false)
    const kafkaStates = kafka.steps!.map((s) => s.state)
    expect(kafkaStates).toContain('warning') // ICMP advisory
    expect(kafka.message).toContain('SINK_BROKER_TIMEOUT')
  })

  it('14. Sink write test 失败影响 Quality 与 Logs', async () => {
    const before = await fetchQuality('1h')
    const result = await writeTestSink('db_main')
    expect(result.success).toBe(false)
    const after = await fetchQuality('1h')
    const metricOf = (q: typeof before) =>
      q.data_metrics.find((m) => m.key === 'dropped_points')!.value
    expect(metricOf(after)).toBeGreaterThan(metricOf(before))
    const logs = await fetchLogs({ page: 1, pageSize: 10, keyword: 'write test failed' })
    expect(logs.items.length).toBeGreaterThan(0)
    const sink = await fetchSink('db_main')
    expect(sink.healthy).toBe(false)
  })

  it('15. Logs：seed ≥100 且操作追加最新在前', async () => {
    const logs = await fetchLogs({ page: 1, pageSize: 500 })
    expect(logs.page.total).toBeGreaterThanOrEqual(100)
    await sendDeviceCommand('wtg-006', 'wtg_mb_001', 5)
    const after = await fetchLogs({ page: 1, pageSize: 5 })
    expect(after.items[0].message).toContain('command applied')
  })

  it('16. Quality 随场景派生且 Auto Check 有 tick 变化', async () => {
    const q1 = await fetchQuality('1h')
    const continuity = q1.dimensions.find((d) => d.key === 'continuity')!
    expect(continuity.status).not.toBe('ok') // wtg-041/043 失败
    expect(q1.issues.some((i) => i.object === 'wtg-041')).toBe(true)
    expect(q1.issues.some((i) => i.object === 'db_main')).toBe(true)
    const q2 = await runQualityCheck('1h')
    expect(q2.window).toBe('1h')
    const q24 = await fetchQuality('24h')
    const expectedOf = (q: typeof q1) =>
      q.data_metrics.find((m) => m.key === 'expected_points')!.value
    expect(expectedOf(q24)).toBeGreaterThan(expectedOf(q1))
  })

  it('17. Health 序列非静态且 Overview 与各子系统一致', async () => {
    const h1 = await fetchSystemHealth('1h')
    expect(new Set(h1.series.cpu_host_pct).size).toBeGreaterThan(5)
    const overview = await fetchOverview()
    expect(overview.device_count).toBe(56)
    expect(overview.devices_connected).toBe(52)
    expect(overview.task_count).toBe(6)
    expect(overview.sink_count).toBe(4)
    expect(overview.sinks_healthy).toBe(1) // 仅 file_archive 健康
  })

  it('18. Config：apply revision+1 / import same → No Changes / restore 新 revision', async () => {
    const historyBefore = await fetchConfigHistory()
    const applied = await applyConfig(
      'reporting.yaml',
      'reporting:\n  enabled: true\n',
      'enable reporting',
    )
    expect(applied.success).toBe(true)
    expect(applied.revision).toBe(historyBefore[0].revision + 1)
    expect((await fetchConfigFile('reporting.yaml')).content).toContain('enabled: true')

    // 相同内容 import → success 但 revision 为 null（No Changes）
    const same = await importConfig('reporting.yaml', 'reporting:\n  enabled: true\n')
    expect(same.success).toBe(true)
    expect(same.revision).toBeNull()

    // 空 import → 422
    await expect(importConfig('reporting.yaml', '  \n')).rejects.toMatchObject({ status: 422 })

    // restore 生成新 revision，旧 revision 不变
    const head = (await fetchConfigHistory())[0]
    const restored = await restoreConfig(head.revision)
    expect(restored.success).toBe(true)
    expect(restored.revision).toBe(head.revision + 1)
    const historyAfter = await fetchConfigHistory()
    expect(historyAfter.find((r) => r.revision === head.revision)).toBeDefined()
  })
})
