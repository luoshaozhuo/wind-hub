// Sinks capability：Sink 查询与连通性/写入测试。
import { call, client } from './client'
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

export function writeTestSink(name: string): Promise<SinkTestResult> {
  return call(client.POST('/api/v1/sinks/{name}/write-test', { params: { path: { name } } }))
}
