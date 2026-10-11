// Devices capability：设备配置/运行时查询、即时点值、趋势、命令下发。
import { call, client } from './client'
import type { components } from './generated/schema'

export type DeviceDto = components['schemas']['DeviceResponse']
export type DevicePageDto = components['schemas']['DevicePageResponse']
export type DeviceDataItem = components['schemas']['DeviceDataItemResponse']
export type DeviceDataPage = components['schemas']['DeviceDataPageResponse']
export type TrendSeries = components['schemas']['TrendSeriesResponse']
export type CommandResult = components['schemas']['DeviceCommandResponse']

export function fetchDevices(pageSize = 200): Promise<DevicePageDto> {
  return call(
    client.GET('/api/v1/devices', { params: { query: { page: 1, page_size: pageSize } } }),
  )
}

export function fetchDevice(deviceId: string): Promise<DeviceDto> {
  return call(
    client.GET('/api/v1/devices/{device_id}', { params: { path: { device_id: deviceId } } }),
  )
}

/** 与旧实现一致：按 200/页拉齐设备全部即时点值。 */
export async function fetchAllDeviceData(deviceId: string): Promise<DeviceDataItem[]> {
  const items: DeviceDataItem[] = []
  let pageNumber = 1
  let total = Number.POSITIVE_INFINITY
  while (items.length < total) {
    const page = await call(
      client.GET('/api/v1/devices/{device_id}/data', {
        params: { path: { device_id: deviceId }, query: { page: pageNumber, page_size: 200 } },
      }),
    )
    total = page.page.total
    items.push(...page.items)
    if (!page.items.length) break
    pageNumber += 1
  }
  return items
}

export function fetchDeviceTrend(
  deviceId: string,
  pointIds: string[],
  windowSeconds: number,
): Promise<TrendSeries[]> {
  return call(
    client.GET('/api/v1/devices/{device_id}/trend', {
      params: {
        path: { device_id: deviceId },
        query: { point_id: pointIds, window_seconds: windowSeconds, limit_per_point: 3600 },
      },
    }),
  )
}

export function sendDeviceCommand(
  deviceId: string,
  pointId: string,
  value: number | boolean,
): Promise<CommandResult> {
  return call(
    client.POST('/api/v1/devices/{device_id}/commands', {
      params: { path: { device_id: deviceId } },
      // timeout 与后端 DeviceCommandRequest 默认值一致（5s，范围 0–30）；
      // 生成契约把带 default 的字段标为必填，这里显式传默认值保持既有行为。
      body: { point_id: pointId, value, timeout: 5 },
    }),
  )
}
