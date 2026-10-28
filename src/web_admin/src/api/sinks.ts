// Sinks capability：Sink 查询与连通性/写入测试。
import { API_BASE, call, client, normalizeApiError } from './client'
import type { components } from './generated/schema'

export type SinkDto = components['schemas']['SinkResponse']
export type SinkTestResult = components['schemas']['SinkTestResponse']

export function fetchSinks(): Promise<SinkDto[]> {
  return call(client.GET('/api/v1/sinks'))
}

export function fetchSink(name: string): Promise<SinkDto> {
  return call(client.GET('/api/v1/sinks/{name}', { params: { path: { name } } }))
}

export function verifySink(name: string): Promise<SinkTestResult> {
  return call(client.POST('/api/v1/sinks/{name}/verify', { params: { path: { name } } }))
}


export type SinkOperation = 'start' | 'stop' | 'restart' | 'clear'

export async function manageSink(name: string, operation: SinkOperation): Promise<void> {
  const endpoint = `${API_BASE}/api/v1/sinks/${encodeURIComponent(name)}/${operation}`
  let response: Response
  try {
    response = await fetch(endpoint, { method: 'POST' })
  } catch (error) {
    throw normalizeApiError(error)
  }
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => ({}))
    throw normalizeApiError(payload, response.status)
  }
}
