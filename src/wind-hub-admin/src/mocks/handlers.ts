// MSW handlers：在 HTTP 边界拦截现有 /api/v1/... 请求。与 real 模式共用
// api/*.ts、generated schema、composables、stores、pages —— 不存在第二套 client。
import { http, HttpResponse } from 'msw'
import type { components } from '../api/generated/schema'
import {
  deviceDataSeed,
  definitionsSeed,
  logsSeed,
  logSourcesSeed,
  qualitySeed,
  systemHealthSeed,
  workersSeed,
  MOCK_NOW,
  type DefinitionsDto,
} from './data'
import { applyAdminState, createOperation, mockState, pollOperation, setTaskRuntime } from './state'

type ConfigApplyDto = components['schemas']['ConfigApplyResponse']
type ConfigReviewDto = components['schemas']['ConfigReviewResponse']
type CommandResult = components['schemas']['DeviceCommandResponse']
type PingResult = components['schemas']['PingResponse']
type PortProbe = components['schemas']['PortProbeResponse']
type ProtocolCheckResult = components['schemas']['ProtocolCheckResponse']
type ProtocolReadResult = components['schemas']['ProtocolReadResponse']
type SinkTestResult = components['schemas']['SinkTestResponse']
type TrendSeries = components['schemas']['TrendSeriesResponse']

const API = '*/api/v1' // 通配 host：浏览器同源与 Node 测试（VITE_API_BASE 绝对 URL）都匹配

function pageMeta(total: number, page = 1, pageSize = 200) {
  return { page, page_size: pageSize, total }
}

function applyOk(revision: number | null = null): ConfigApplyDto {
  return { success: true, errors: [], revision, rollback_performed: false }
}

function notFound(message: string) {
  return HttpResponse.json({ error: { code: 'NOT_FOUND', message } }, { status: 404 })
}

async function readJson<T>(request: Request): Promise<T> {
  return (await request.json()) as T
}

function nextRevision(): number {
  return (mockState.configHistory[0]?.revision ?? 0) + 1
}

function pushHistory(source: string, comment: string): number {
  const revision = nextRevision()
  mockState.configHistory.unshift({
    revision,
    created_at: new Date().toISOString(),
    source,
    comment,
  })
  return revision
}

export const handlers = [
  // -- 启动依赖：settings / definitions / devices / tasks / sinks / overview --
  http.get(`${API}/settings`, () => HttpResponse.json(mockState.settings)),
  http.put(`${API}/settings`, async ({ request }) => {
    const body = await readJson<components['schemas']['SettingsRequest']>(request)
    mockState.settings = { ...mockState.settings, ...body }
    return HttpResponse.json(applyOk(pushHistory('settings', 'update settings')))
  }),
  http.get(`${API}/definitions`, (): HttpResponse<DefinitionsDto> =>
    HttpResponse.json(structuredClone(definitionsSeed)),
  ),
  http.put(`${API}/admin-state`, async ({ request }) => {
    const body = await readJson<components['schemas']['AdminStateRequest']>(request)
    applyAdminState(body)
    return HttpResponse.json(applyOk(pushHistory('admin-state', 'replace admin state')))
  }),

  http.get(`${API}/devices`, () =>
    HttpResponse.json({ items: mockState.devices, page: pageMeta(mockState.devices.length) }),
  ),
  http.get(`${API}/devices/:deviceId`, ({ params }) => {
    const device = mockState.devices.find((d) => d.device_id === params.deviceId)
    return device ? HttpResponse.json(device) : notFound(`device ${params.deviceId} not found`)
  }),
  http.get(`${API}/devices/:deviceId/data`, ({ params }) => {
    const items = deviceDataSeed[String(params.deviceId)] ?? []
    return HttpResponse.json({ items, page: pageMeta(items.length) })
  }),
  http.get(`${API}/devices/:deviceId/trend`, ({ request }) => {
    const url = new URL(request.url)
    const pointIds = url.searchParams.getAll('point_id')
    const series: TrendSeries[] = pointIds.map((pointId) => ({
      point_id: pointId,
      variable_name: pointId,
      unit: 'mw',
      unit_symbol: 'MW',
      samples: [
        { timestamp: '2026-10-08T07:30:00Z', value: 1.1, quality: 'good', source: 'mock' },
        { timestamp: MOCK_NOW, value: 1.25, quality: 'good', source: 'mock' },
      ],
    }))
    return HttpResponse.json(series)
  }),
  http.post(`${API}/devices/:deviceId/commands`, async ({ params, request }) => {
    const body = await readJson<{ point_id: string; value: unknown }>(request)
    const device = mockState.devices.find((d) => d.device_id === params.deviceId)
    if (!device) return notFound(`device ${params.deviceId} not found`)
    const result: CommandResult = {
      command_id: `mock-cmd-${device.device_id}`,
      success: device.connected,
      error: device.connected ? null : 'device offline',
      requested: body.value,
      readback: device.connected ? body.value : null,
      readback_error: device.connected ? null : 'device offline',
      readback_quality: device.connected ? 'good' : 'bad',
      readback_timestamp: MOCK_NOW,
      latency_ms: 15,
      sent_at: MOCK_NOW,
      finished_at: MOCK_NOW,
    }
    return HttpResponse.json(result)
  }),

  http.get(`${API}/tasks`, () =>
    HttpResponse.json({ items: mockState.tasks, page: pageMeta(mockState.tasks.length) }),
  ),
  http.get(`${API}/tasks/:taskId`, ({ params }) => {
    const task = mockState.tasks.find((t) => t.task_id === params.taskId)
    return task ? HttpResponse.json(task) : notFound(`task ${params.taskId} not found`)
  }),
  http.get(`${API}/tasks/:taskId/instances`, ({ params }) =>
    HttpResponse.json(mockState.taskInstances[String(params.taskId)] ?? []),
  ),
  http.post(`${API}/tasks/:taskId/start`, ({ params }) => {
    const task = setTaskRuntime(String(params.taskId), true)
    return task ? HttpResponse.json(task) : notFound(`task ${params.taskId} not found`)
  }),
  http.post(`${API}/tasks/:taskId/stop`, ({ params }) => {
    const task = setTaskRuntime(String(params.taskId), false)
    return task ? HttpResponse.json(task) : notFound(`task ${params.taskId} not found`)
  }),

  http.get(`${API}/sinks`, () => HttpResponse.json(mockState.sinks)),
  http.get(`${API}/sinks/:name`, ({ params }) => {
    const sink = mockState.sinks.find((s) => s.name === params.name)
    return sink ? HttpResponse.json(sink) : notFound(`sink ${params.name} not found`)
  }),
  http.post(`${API}/sinks/:name/verify`, ({ params }) => {
    const sink = mockState.sinks.find((s) => s.name === params.name)
    if (!sink) return notFound(`sink ${params.name} not found`)
    const result: SinkTestResult = {
      success: sink.enabled,
      latency_ms: 9,
      message: sink.enabled ? 'connection ok (mock)' : 'sink disabled',
      steps: [{ step: 'connect', success: sink.enabled }],
    }
    return HttpResponse.json(result)
  }),
  http.post(`${API}/sinks/:name/write-test`, ({ params }) => {
    const sink = mockState.sinks.find((s) => s.name === params.name)
    if (!sink) return notFound(`sink ${params.name} not found`)
    const result: SinkTestResult = {
      success: sink.enabled,
      latency_ms: 14,
      message: sink.enabled ? 'write test ok (mock)' : 'sink disabled',
      steps: [
        { step: 'connect', success: sink.enabled },
        { step: 'write', success: sink.enabled },
      ],
    }
    return HttpResponse.json(result)
  }),

  // -- Monitoring：overview / workers / quality / logs / system-health --
  http.get(`${API}/overview`, () => {
    const connected = mockState.devices.filter((d) => d.connected).length
    const enabledSinks = mockState.sinks.filter((s) => s.enabled)
    const running = mockState.tasks.filter((t) => t.runtime_state === 'running')
    return HttpResponse.json({
      device_count: mockState.devices.length,
      devices_connected: connected,
      devices_offline: mockState.devices.length - connected,
      task_count: mockState.tasks.length,
      task_instances: mockState.tasks.reduce((n, t) => n + t.instance_count, 0),
      task_instances_running: running.reduce((n, t) => n + t.running_instances, 0),
      task_instances_failed: 0,
      sink_count: mockState.sinks.length,
      sinks_healthy: enabledSinks.filter((s) => s.healthy).length,
      points_collected: 3600,
      points_dropped: 4,
      points_routed: 3596,
      runtime_running: true,
      runtime_state: 'running',
      site_id: mockState.settings.site_id,
      site_name: mockState.settings.site_name,
      workers_unavailable: [],
    })
  }),
  http.get(`${API}/workers`, () => HttpResponse.json(workersSeed)),
  http.get(`${API}/workers/:workerId`, ({ params }) => {
    const worker = workersSeed.find((w) => w.worker_id === params.workerId)
    return worker ? HttpResponse.json(worker) : notFound(`worker ${params.workerId} not found`)
  }),
  http.get(`${API}/quality`, ({ request }) => {
    const window = new URL(request.url).searchParams.get('window') ?? '1h'
    return HttpResponse.json({ ...qualitySeed, window })
  }),
  http.post(`${API}/quality/check`, ({ request }) => {
    const window = new URL(request.url).searchParams.get('window') ?? '1h'
    return HttpResponse.json({ ...qualitySeed, window, sampled_to: new Date().toISOString() })
  }),
  http.get(`${API}/logs`, ({ request }) => {
    const url = new URL(request.url)
    const level = url.searchParams.get('level')
    const source = url.searchParams.get('source')
    const keyword = url.searchParams.get('keyword')
    const page = Number(url.searchParams.get('page') ?? 1)
    const pageSize = Number(url.searchParams.get('page_size') ?? 50)
    let items = logsSeed
    if (level) items = items.filter((l) => l.level === level)
    if (source) items = items.filter((l) => l.source === source)
    if (keyword) items = items.filter((l) => l.message.includes(keyword))
    return HttpResponse.json({ items, page: pageMeta(items.length, page, pageSize) })
  }),
  http.get(`${API}/logs/sources`, () => HttpResponse.json(logSourcesSeed)),
  http.get(`${API}/system-health`, ({ request }) => {
    const range = new URL(request.url).searchParams.get('range') ?? '1h'
    return HttpResponse.json({ ...systemHealthSeed, range })
  }),

  // -- Diagnostics & Operations --
  http.post(`${API}/diagnostics/ping`, async ({ request }) => {
    const body = await readJson<{ host: string }>(request)
    const result: PingResult = { host: body.host, reachable: true, latency_ms: 3 }
    return HttpResponse.json(result)
  }),
  http.post(`${API}/diagnostics/ports`, async ({ request }) => {
    const body = await readJson<{ ports: number[] }>(request)
    const result: PortProbe[] = body.ports.map((port) => ({
      port,
      state: 'open',
      latency_ms: 4,
    }))
    return HttpResponse.json(result)
  }),
  http.post(`${API}/diagnostics/subnet-scan`, () =>
    HttpResponse.json(createOperation('subnet-scan', 2, { hosts: ['192.168.10.11'] })),
  ),
  http.post(`${API}/diagnostics/protocol/check`, async ({ request }) => {
    const body = await readJson<{ device_id: string }>(request)
    const device = mockState.devices.find((d) => d.device_id === body.device_id)
    if (!device) return notFound(`device ${body.device_id} not found`)
    const result: ProtocolCheckResult = {
      device_id: device.device_id,
      connected: device.connected,
    }
    return HttpResponse.json(result)
  }),
  http.post(`${API}/diagnostics/protocol/read`, async ({ request }) => {
    const body = await readJson<{ device_id: string; point_id: string }>(request)
    const item = (deviceDataSeed[body.device_id] ?? []).find((p) => p.point_id === body.point_id)
    const result: ProtocolReadResult = {
      device_id: body.device_id,
      point_id: body.point_id,
      value: item?.value ?? 0,
      quality: item?.quality ?? 'good',
      source: 'mock',
      timestamp: MOCK_NOW,
    }
    return HttpResponse.json(result)
  }),
  http.post(`${API}/diagnostics/protocol/write`, async ({ request }) => {
    const body = await readJson<{ device_id: string; point_id: string; value: unknown }>(request)
    const result: CommandResult = {
      command_id: `mock-diag-${body.device_id}`,
      success: true,
      error: null,
      requested: body.value,
      readback: body.value,
      readback_error: null,
      readback_quality: 'good',
      readback_timestamp: MOCK_NOW,
      latency_ms: 11,
      sent_at: MOCK_NOW,
      finished_at: MOCK_NOW,
    }
    return HttpResponse.json(result)
  }),
  http.post(`${API}/diagnostics/point-table`, async ({ request }) => {
    const body = await readJson<{ device_id: string }>(request)
    return HttpResponse.json(
      createOperation('point-table-test', 2, { device_id: body.device_id, passed: true }),
    )
  }),
  http.get(`${API}/operations/:operationId`, ({ params }) => {
    const operation = pollOperation(String(params.operationId))
    return operation
      ? HttpResponse.json(operation)
      : notFound(`operation ${params.operationId} not found`)
  }),

  // -- Config capability：files / validate / apply / import / history / backup --
  http.get(`${API}/config/files`, () =>
    HttpResponse.json(
      Object.entries(mockState.configContents).map(([name]) => ({
        name,
        exists: true,
        optional: false,
      })),
    ),
  ),
  http.get(`${API}/config/files/:name`, ({ params }) => {
    const name = String(params.name)
    const content = mockState.configContents[name]
    return content !== undefined
      ? HttpResponse.json({ name, content })
      : notFound(`config file ${name} not found`)
  }),
  http.post(`${API}/config/validate`, async ({ request }) => {
    const body = await readJson<{ name: string; content: string }>(request)
    const result: ConfigReviewDto = {
      name: body.name,
      valid: true,
      changed: mockState.configContents[body.name] !== body.content,
      errors: [],
      diff: {},
    }
    return HttpResponse.json(result)
  }),
  http.post(`${API}/config/apply`, async ({ request }) => {
    const body = await readJson<{ name: string; content: string; comment?: string }>(request)
    mockState.configContents[body.name] = body.content
    return HttpResponse.json(applyOk(pushHistory('apply', body.comment || 'apply config')))
  }),
  http.post(`${API}/config/import`, async ({ request }) => {
    const body = await readJson<{ name: string; content: string; comment?: string }>(request)
    mockState.configContents[body.name] = body.content
    return HttpResponse.json(applyOk(pushHistory('import', body.comment || 'import config')))
  }),
  http.get(`${API}/config/history`, () => HttpResponse.json(mockState.configHistory)),
  http.post(`${API}/config/history/:revision/restore`, ({ params }) =>
    HttpResponse.json(
      applyOk(pushHistory('restore', `restore revision ${String(params.revision)}`)),
    ),
  ),
  http.get(
    `${API}/config/backup`,
    () =>
      new HttpResponse('mock backup archive', {
        headers: { 'Content-Type': 'application/octet-stream' },
      }),
  ),
]
