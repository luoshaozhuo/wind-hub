// MSW handlers（薄层）：parse path/query/body → 调用 mocks/service → HTTP 包络。
// 状态机与成败判定全部在 service/state/scenarios；这里不写业务规则。
import { http, HttpResponse } from 'msw'
import type { components } from '../api/generated/schema'
import { buildDefinitions, workersSeed } from './data'
import { mockState } from './state'
import {
  LATENCY,
  applyConfigText,
  configBackupText,
  createPointTableOperation,
  deviceDataItems,
  deviceDto,
  deviceTrend,
  errorBody,
  findDevice,
  importConfigText,
  pingHost,
  pollOperation,
  probePorts,
  protocolCheckDevice,
  protocolReadPoint,
  replaceAdminState,
  restoreConfigRevision,
  sinkDto,
  sinkHealthy,
  sleep,
  startTaskById,
  stopTaskById,
  subnetScanResults,
  targetsOfTask,
  taskDto,
  taskInstances,
  updateSettings,
  validateConfigText,
  verifySinkByName,
  writeDevicePoint,
  writeTestSinkByName,
} from './service'
import { qualityForWindow, runQualityCheck } from './quality'
import { systemHealthForRange } from './health'
import { LOG_SOURCES, queryLogs } from './logs'
import { deviceConnected, deviceRuntimeState } from './scenarios'
import { registerOperation, taskPhase, sinkRuntimeOf } from './state'
import { epochSecNow } from './values'

const API = '*/api/v1' // 通配 host：浏览器同源与 Node 测试（VITE_API_BASE 绝对 URL）都匹配

function pageMeta(total: number, page = 1, pageSize = 200) {
  return { page, page_size: pageSize, total }
}

function notFound(message: string) {
  return HttpResponse.json(errorBody('NOT_FOUND', message), { status: 404 })
}

function conflict(code: string, message: string) {
  return HttpResponse.json(errorBody(code, message), { status: 409 })
}

async function readJson<T>(request: Request): Promise<T> {
  return (await request.json()) as T
}

function isErr(v: unknown): v is { error: { code: string; message: string } } {
  // OperationDto 带可空 error 字段，只有带 code 的对象才算失败结果
  if (typeof v !== 'object' || v === null || !('error' in v)) return false
  const error = (v as { error: unknown }).error
  return typeof error === 'object' && error !== null && 'code' in error
}

function errStatus(code: string): number {
  return code === 'NOT_FOUND' ? 404 : 400
}

export const handlers = [
  // -- 启动依赖：settings / definitions / devices / tasks / sinks / overview --
  http.get(`${API}/settings`, () => HttpResponse.json(mockState.settings)),
  http.put(`${API}/settings`, async ({ request }) => {
    const body = await readJson<components['schemas']['SettingsRequest']>(request)
    await sleep(LATENCY.configApply)
    return HttpResponse.json(updateSettings(body))
  }),
  http.get(`${API}/definitions`, () => HttpResponse.json(buildDefinitions())),
  http.put(`${API}/admin-state`, async ({ request }) => {
    const body = await readJson<components['schemas']['AdminStateRequest']>(request)
    await sleep(LATENCY.configApply)
    return HttpResponse.json(replaceAdminState(body))
  }),

  http.get(`${API}/devices`, ({ request }) => {
    const url = new URL(request.url)
    const page = Number(url.searchParams.get('page') ?? 1)
    const pageSize = Number(url.searchParams.get('page_size') ?? 200)
    const keyword = url.searchParams.get('keyword')
    const group = url.searchParams.get('device_group')
    let items = mockState.devices
    if (keyword) items = items.filter((d) => d.device_id.includes(keyword))
    if (group) items = items.filter((d) => d.device_group === group)
    const paged = items.slice((page - 1) * pageSize, page * pageSize)
    return HttpResponse.json({
      items: paged.map(deviceDto),
      page: pageMeta(items.length, page, pageSize),
    })
  }),
  http.get(`${API}/devices/:deviceId`, ({ params }) => {
    const device = findDevice(String(params.deviceId))
    return device
      ? HttpResponse.json(deviceDto(device))
      : notFound(`device ${params.deviceId} not found`)
  }),
  http.get(`${API}/devices/:deviceId/data`, ({ params, request }) => {
    const device = findDevice(String(params.deviceId))
    if (!device) return notFound(`device ${params.deviceId} not found`)
    const url = new URL(request.url)
    const page = Number(url.searchParams.get('page') ?? 1)
    const pageSize = Number(url.searchParams.get('page_size') ?? 200)
    const all = deviceDataItems(device)
    const items = all.slice((page - 1) * pageSize, page * pageSize)
    return HttpResponse.json({ items, page: pageMeta(all.length, page, pageSize) })
  }),
  http.get(`${API}/devices/:deviceId/trend`, ({ params, request }) => {
    const device = findDevice(String(params.deviceId))
    if (!device) return notFound(`device ${params.deviceId} not found`)
    const url = new URL(request.url)
    const pointIds = url.searchParams.getAll('point_id')
    const windowSeconds = Number(url.searchParams.get('window_seconds') ?? 3600)
    const limit = Number(url.searchParams.get('limit_per_point') ?? 120)
    return HttpResponse.json(deviceTrend(device, pointIds, windowSeconds, limit))
  }),
  http.post(`${API}/devices/:deviceId/commands`, async ({ params, request }) => {
    const device = findDevice(String(params.deviceId))
    if (!device) return notFound(`device ${params.deviceId} not found`)
    const body = await readJson<{ point_id: string; value: number | boolean }>(request)
    await sleep(LATENCY.command)
    return HttpResponse.json(writeDevicePoint(device.device_id, body.point_id, body.value))
  }),

  http.get(`${API}/tasks`, () =>
    HttpResponse.json({
      items: mockState.tasks.map(taskDto),
      page: pageMeta(mockState.tasks.length),
    }),
  ),
  http.get(`${API}/tasks/:taskId`, ({ params }) => {
    const task = mockState.tasks.find((t) => t.task_id === params.taskId)
    return task ? HttpResponse.json(taskDto(task)) : notFound(`task ${params.taskId} not found`)
  }),
  http.get(`${API}/tasks/:taskId/instances`, ({ params }) => {
    const task = mockState.tasks.find((t) => t.task_id === params.taskId)
    return task
      ? HttpResponse.json(taskInstances(task))
      : notFound(`task ${params.taskId} not found`)
  }),
  http.post(`${API}/tasks/:taskId/start`, async ({ params }) => {
    await sleep(LATENCY.taskTransition)
    const outcome = startTaskById(String(params.taskId))
    return outcome.ok ? HttpResponse.json(outcome.task) : conflict(outcome.code, outcome.message)
  }),
  http.post(`${API}/tasks/:taskId/stop`, async ({ params }) => {
    await sleep(LATENCY.taskTransition)
    const outcome = stopTaskById(String(params.taskId))
    return outcome.ok ? HttpResponse.json(outcome.task) : conflict(outcome.code, outcome.message)
  }),

  http.get(`${API}/sinks`, () => HttpResponse.json(mockState.sinks.map(sinkDto))),
  http.get(`${API}/sinks/:name`, ({ params }) => {
    const sink = mockState.sinks.find((s) => s.name === params.name)
    return sink ? HttpResponse.json(sinkDto(sink)) : notFound(`sink ${params.name} not found`)
  }),
  http.post(`${API}/sinks/:name/verify`, async ({ params }) => {
    const result = await verifySinkByName(String(params.name))
    return isErr(result)
      ? HttpResponse.json(errorBody(result.error.code, result.error.message), {
          status: errStatus(result.error.code),
        })
      : HttpResponse.json(result)
  }),
  http.post(`${API}/sinks/:name/write-test`, async ({ params }) => {
    const result = await writeTestSinkByName(String(params.name))
    return isErr(result)
      ? HttpResponse.json(errorBody(result.error.code, result.error.message), {
          status: errStatus(result.error.code),
        })
      : HttpResponse.json(result)
  }),

  // -- Monitoring：overview / workers / quality / logs / system-health --
  http.get(`${API}/overview`, () => {
    const connected = mockState.devices.filter((d) =>
      deviceConnected(d.device_id, d.enabled),
    ).length
    const running = mockState.tasks.filter((t) => taskPhase(t.task_id) === 'running')
    const instances = running.flatMap((t) => targetsOfTask(t))
    const failed = instances.filter((d) => deviceRuntimeOf(d) === 'failed').length
    const now = epochSecNow()
    const dropped = mockState.sinks.reduce((n, s) => n + sinkRuntimeOf(s.name).dropped_points, 0)
    const collected = instances.length * 60 + (now % 60)
    return HttpResponse.json({
      device_count: mockState.devices.length,
      devices_connected: connected,
      devices_offline: mockState.devices.length - connected,
      task_count: mockState.tasks.length,
      task_instances: instances.length,
      task_instances_running: instances.length - failed,
      task_instances_failed: failed,
      sink_count: mockState.sinks.length,
      sinks_healthy: mockState.sinks.filter(sinkHealthy).length,
      points_collected: collected,
      points_dropped: dropped,
      points_routed: Math.max(0, collected - dropped),
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
    return HttpResponse.json(qualityForWindow(window))
  }),
  http.post(`${API}/quality/check`, async ({ request }) => {
    const window = new URL(request.url).searchParams.get('window') ?? '1h'
    await sleep(LATENCY.qualityCheck)
    return HttpResponse.json(runQualityCheck(window))
  }),
  http.get(`${API}/logs`, ({ request }) => {
    const url = new URL(request.url)
    const page = Number(url.searchParams.get('page') ?? 1)
    const pageSize = Number(url.searchParams.get('page_size') ?? 50)
    const all = queryLogs({
      level: url.searchParams.get('level') ?? undefined,
      source: url.searchParams.get('source') ?? undefined,
      keyword: url.searchParams.get('keyword') ?? undefined,
    })
    const items = all.slice((page - 1) * pageSize, page * pageSize)
    return HttpResponse.json({ items, page: pageMeta(all.length, page, pageSize) })
  }),
  http.get(`${API}/logs/sources`, () => HttpResponse.json(LOG_SOURCES)),
  http.get(`${API}/system-health`, ({ request }) => {
    const range = new URL(request.url).searchParams.get('range') ?? '1h'
    return HttpResponse.json(systemHealthForRange(range))
  }),

  // -- Diagnostics & Operations --
  http.post(`${API}/diagnostics/ping`, async ({ request }) => {
    const body = await readJson<{ host: string }>(request)
    await sleep(LATENCY.verifyStep)
    return HttpResponse.json(pingHost(body.host))
  }),
  http.post(`${API}/diagnostics/ports`, async ({ request }) => {
    const body = await readJson<{ host: string; ports: number[] }>(request)
    await sleep(LATENCY.verifyStep)
    return HttpResponse.json(probePorts(body.host, body.ports))
  }),
  http.post(`${API}/diagnostics/subnet-scan`, async ({ request }) => {
    const body = await readJson<{ network: string }>(request)
    await sleep(LATENCY.scanBatch)
    return HttpResponse.json(
      registerOperation('subnet-scan', 2, 'success', { hosts: subnetScanResults(body.network) }),
    )
  }),
  http.post(`${API}/diagnostics/protocol/check`, async ({ request }) => {
    const body = await readJson<{ device_id: string }>(request)
    await sleep(LATENCY.verifyStep)
    const result = protocolCheckDevice(body.device_id)
    return isErr(result)
      ? HttpResponse.json(errorBody(result.error.code, result.error.message), {
          status: errStatus(result.error.code),
        })
      : HttpResponse.json({ device_id: body.device_id, connected: result.connected })
  }),
  http.post(`${API}/diagnostics/protocol/read`, async ({ request }) => {
    const body = await readJson<{ device_id: string; point_id: string }>(request)
    await sleep(LATENCY.diagnostic)
    const result = protocolReadPoint(body.device_id, body.point_id)
    return isErr(result)
      ? HttpResponse.json(errorBody(result.error.code, result.error.message), {
          status: errStatus(result.error.code),
        })
      : HttpResponse.json(result)
  }),
  http.post(`${API}/diagnostics/protocol/write`, async ({ request }) => {
    const body = await readJson<{ device_id: string; point_id: string; value: number | boolean }>(
      request,
    )
    await sleep(LATENCY.diagnostic)
    if (!findDevice(body.device_id)) return notFound(`device ${body.device_id} not found`)
    return HttpResponse.json(writeDevicePoint(body.device_id, body.point_id, body.value))
  }),
  http.post(`${API}/diagnostics/point-table`, async ({ request }) => {
    const body = await readJson<{ device_id: string }>(request)
    await sleep(LATENCY.verifyBulk)
    const result = createPointTableOperation(body.device_id)
    return isErr(result)
      ? HttpResponse.json(errorBody(result.error.code, result.error.message), {
          status: errStatus(result.error.code),
        })
      : HttpResponse.json(result)
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
      Object.keys(mockState.workingTexts).map((name) => ({
        name,
        exists: true,
        optional: name === 'reporting.yaml',
      })),
    ),
  ),
  http.get(`${API}/config/files/:name`, ({ params }) => {
    const name = String(params.name)
    const content = mockState.workingTexts[name]
    return content !== undefined
      ? HttpResponse.json({ name, content })
      : notFound(`config file ${name} not found`)
  }),
  http.post(`${API}/config/validate`, async ({ request }) => {
    const body = await readJson<{ name: string; content: string }>(request)
    await sleep(LATENCY.uiLocal)
    return HttpResponse.json(validateConfigText(body.name, body.content))
  }),
  http.post(`${API}/config/apply`, async ({ request }) => {
    const body = await readJson<{ name: string; content: string; comment?: string }>(request)
    await sleep(LATENCY.configApply)
    return HttpResponse.json(applyConfigText(body.name, body.content, body.comment ?? '', 'apply'))
  }),
  http.post(`${API}/config/import`, async ({ request }) => {
    const body = await readJson<{ name: string; content: string; comment?: string }>(request)
    await sleep(LATENCY.configApply)
    const outcome = importConfigText(body.name, body.content, body.comment ?? '')
    return outcome.ok
      ? HttpResponse.json(outcome.body)
      : HttpResponse.json(errorBody(outcome.code, outcome.message), { status: outcome.status })
  }),
  http.get(`${API}/config/history`, () => HttpResponse.json(mockState.revisions)),
  http.post(`${API}/config/history/:revision/restore`, async ({ params }) => {
    await sleep(LATENCY.configApply)
    const result = restoreConfigRevision(Number(params.revision))
    return isErr(result)
      ? HttpResponse.json(errorBody(result.error.code, result.error.message), {
          status: errStatus(result.error.code),
        })
      : HttpResponse.json(result)
  }),
  http.get(
    `${API}/config/backup`,
    () =>
      new HttpResponse(configBackupText(), {
        headers: { 'Content-Type': 'application/octet-stream' },
      }),
  ),
]

// 实例级派生（仅 overview 计数使用）
function deviceRuntimeOf(d: { device_id: string; enabled: boolean }): string {
  const runtime = deviceRuntimeState(d.device_id, d.enabled)
  if (runtime.network !== 'ok' || runtime.protocolError) return 'failed'
  return 'running'
}
