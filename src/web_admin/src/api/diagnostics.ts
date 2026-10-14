// Diagnostics capability：ping / 端口探测 / 子网扫描 / 协议检查与读写 / 点表测试。
import { call, client } from './client'
import type { components } from './generated/schema'

export type PingResult = components['schemas']['PingResponse']
export type PortProbe = components['schemas']['PortProbeResponse']
export type ProtocolCheckResult = components['schemas']['ProtocolCheckResponse']
export type ProtocolReadResult = components['schemas']['ProtocolReadResponse']
export type CommandWriteResult = components['schemas']['DeviceCommandResponse']
export type OperationDto = components['schemas']['OperationResponse']

export function ping(host: string, timeout = 1): Promise<PingResult> {
  return call(client.POST('/api/v1/diagnostics/ping', { body: { host, timeout } }))
}

export function probePorts(host: string, ports: number[], timeout = 1): Promise<PortProbe[]> {
  return call(client.POST('/api/v1/diagnostics/ports', { body: { host, ports, timeout } }))
}

export function scanSubnet(network: string, ports: number[], timeout = 0.5): Promise<OperationDto> {
  return call(client.POST('/api/v1/diagnostics/subnet-scan', { body: { network, ports, timeout } }))
}

export function protocolCheck(deviceId: string): Promise<ProtocolCheckResult> {
  return call(client.POST('/api/v1/diagnostics/protocol/check', { body: { device_id: deviceId } }))
}

export function protocolRead(deviceId: string, pointId: string): Promise<ProtocolReadResult> {
  return call(
    client.POST('/api/v1/diagnostics/protocol/read', {
      body: { device_id: deviceId, point_id: pointId },
    }),
  )
}

export function protocolWrite(
  deviceId: string,
  pointId: string,
  value: number | boolean,
): Promise<CommandWriteResult> {
  return call(
    client.POST('/api/v1/diagnostics/protocol/write', {
      body: { device_id: deviceId, point_id: pointId, value },
    }),
  )
}

/** 点表测试为异步 Operation，返回 operation_id 供 awaitOperation 跟踪。 */
export function pointTableTest(deviceId: string): Promise<OperationDto> {
  return call(client.POST('/api/v1/diagnostics/point-table', { body: { device_id: deviceId } }))
}
