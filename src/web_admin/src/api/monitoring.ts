// Monitoring capability：总览 / Workers / 数据质量 / 日志 / 系统健康。
import { call, client } from './client'
import type { components } from './generated/schema'

export type OverviewDto = components['schemas']['OverviewResponse']
export type WorkerDto = components['schemas']['WorkerResponse']
export type QualityDto = components['schemas']['QualityResponse']
export type LogPageDto = components['schemas']['LogPageResponse']
export type SystemHealthDto = components['schemas']['SystemHealthResponse']

export type QualityWindowParam = '1h' | '24h' | '7d'
export type HealthRangeParam = '1h' | '24h' | '7d' | '30d'

export function fetchOverview(): Promise<OverviewDto> {
  return call(client.GET('/api/v1/overview'))
}

export function fetchWorkers(): Promise<WorkerDto[]> {
  return call(client.GET('/api/v1/workers'))
}

export function fetchWorker(workerId: string): Promise<WorkerDto> {
  return call(
    client.GET('/api/v1/workers/{worker_id}', { params: { path: { worker_id: workerId } } }),
  )
}

export function fetchQuality(window: QualityWindowParam): Promise<QualityDto> {
  return call(client.GET('/api/v1/quality', { params: { query: { window } } }))
}

/** 立即刷新监控快照并按窗口重算（POST 语义，调用方负责写入 query 缓存）。 */
export function runQualityCheck(window: QualityWindowParam): Promise<QualityDto> {
  return call(client.POST('/api/v1/quality/check', { params: { query: { window } } }))
}

export interface LogQueryParams {
  page: number
  pageSize: number
  level?: string
  source?: string
  keyword?: string
}

export function fetchLogs(params: LogQueryParams): Promise<LogPageDto> {
  return call(
    client.GET('/api/v1/logs', {
      params: {
        query: {
          page: params.page,
          page_size: params.pageSize,
          level: params.level && params.level !== 'All' ? params.level : undefined,
          source: params.source && params.source !== 'All' ? params.source : undefined,
          keyword: params.keyword || undefined,
        },
      },
    }),
  )
}

export function fetchLogSources(): Promise<string[]> {
  return call(client.GET('/api/v1/logs/sources'))
}

export function fetchSystemHealth(range: HealthRangeParam): Promise<SystemHealthDto> {
  return call(client.GET('/api/v1/system-health', { params: { query: { range } } })).then(
    // openapi-fetch 的 Readable 工具类型会把元组展宽为数组；wire 数据不变，
    // 按生成契约收窄 load_average。
    (data) => ({ ...data, load_average: data.load_average as SystemHealthDto['load_average'] }),
  )
}
