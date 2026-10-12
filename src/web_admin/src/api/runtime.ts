/** 与最新 Collector / Commander REST 网关直接对应的运行时 API。 */

const base = (import.meta.env.VITE_API_BASE || '').replace(/\/api\/v1\/?$/, '').replace(/\/$/, '')

export interface RuntimeTask {
  task_id: string
  point_group: string
  enabled: boolean
  runtime_state: string
  instance_count: string | number
}

export interface RuntimeInstance {
  instance_id: string
  task_id: string
  device_id: string
  point_group: string
  state: string
}

export interface RuntimeSink {
  name: string
  healthy: boolean
  message: string
  queue_depth: string | number
}

export interface LatestPointValue {
  device_id: string
  point_id: string
  value?: Record<string, unknown>
  quality: string
  timestamp?: string
  timestamp_source: string
  source: string
}

export interface RuntimeDevice {
  device_id: string
  protocol: string
  connected: boolean
  last_seen?: string
  consecutive_failures: string | number
  last_error: string
}

/** 命令写入结果三态：UNKNOWN 表示命令可能已执行，不得盲目重试。 */
export type CommandOutcome =
  | 'COMMAND_OUTCOME_CONFIRMED_SUCCESS'
  | 'COMMAND_OUTCOME_CONFIRMED_FAILURE'
  | 'COMMAND_OUTCOME_UNKNOWN'

export interface CommandResult {
  command_id: string
  outcome: CommandOutcome
  error: string
  finished_at?: string
}

export class RuntimeApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message)
    this.name = 'RuntimeApiError'
  }
}

const REQUEST_TIMEOUT_MS = 15_000

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  // 每次请求带硬超时：网关故障时 fetch 不会无限挂起。
  const response = await fetch(base + path, {
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    ...options,
  })
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null)
    const detail =
      body && typeof body === 'object' && 'detail' in body
        ? (body as { detail?: unknown }).detail
        : null
    const error =
      detail && typeof detail === 'object' ? (detail as { code?: string; message?: string }) : {}
    throw new RuntimeApiError(
      response.status,
      error.code || 'HTTP_ERROR',
      error.message || response.statusText || 'Request failed',
    )
  }
  return (await response.json()) as T
}

function action<T>(path: string, apiKey: string): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'X-API-Key': apiKey },
  })
}

export const runtimeApi = {
  tasks: async (): Promise<RuntimeTask[]> =>
    (await request<{ items: RuntimeTask[] }>('/api/v1/collector/tasks')).items,
  instances: async (): Promise<RuntimeInstance[]> =>
    (await request<{ items: RuntimeInstance[] }>('/api/v1/collector/instances')).items,
  startTask: (id: string, apiKey: string): Promise<RuntimeTask> =>
    action('/api/v1/collector/tasks/' + encodeURIComponent(id) + '/start', apiKey),
  stopTask: (id: string, apiKey: string): Promise<RuntimeTask> =>
    action('/api/v1/collector/tasks/' + encodeURIComponent(id) + '/stop', apiKey),
  startInstance: (id: string, apiKey: string): Promise<RuntimeInstance> =>
    action('/api/v1/collector/instances/' + encodeURIComponent(id) + '/start', apiKey),
  stopInstance: (id: string, apiKey: string): Promise<RuntimeInstance> =>
    action('/api/v1/collector/instances/' + encodeURIComponent(id) + '/stop', apiKey),
  sinks: async (): Promise<RuntimeSink[]> =>
    (await request<{ items: RuntimeSink[] }>('/api/v1/collector/sinks')).items,
  verifySink: (name: string): Promise<{ success: boolean; message: string }> =>
    request('/api/v1/collector/sinks/' + encodeURIComponent(name) + '/verify'),
  testSink: (name: string, apiKey: string): Promise<{ success: boolean; message: string }> =>
    action('/api/v1/collector/sinks/' + encodeURIComponent(name) + '/test', apiKey),
  devices: async (): Promise<RuntimeDevice[]> =>
    (await request<{ items: RuntimeDevice[] }>('/api/v1/collector/devices')).items,
  verifyDevice: (
    deviceId: string,
  ): Promise<{ ok: boolean; stages: Array<{ name: string; ok: boolean; message: string }> }> =>
    request('/api/v1/commander/devices/' + encodeURIComponent(deviceId) + '/verify'),
  writePoint: (
    input: {
      command_id: string
      device_id: string
      point_id: string
      value: boolean | number | string
      timeout: number
    },
    apiKey: string,
  ): Promise<CommandResult> =>
    request('/api/v1/commander/commands', {
      method: 'POST',
      headers: { 'X-API-Key': apiKey, 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }),
  latestValues: async (deviceId?: string): Promise<LatestPointValue[]> => {
    const query = deviceId ? '?device_id=' + encodeURIComponent(deviceId) : ''
    return (
      await request<{ values: LatestPointValue[] }>('/api/v1/collector/latest-values' + query)
    ).values
  },
}
