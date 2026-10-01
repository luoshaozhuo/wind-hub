import * as mock from '../mock/service'
import type {
  DeviceInst,
  DeviceVerification,
  PointDef,
  SinkDef,
  TaskDef,
} from '../mock/types'

const USE_MOCK = import.meta.env.VITE_USE_MOCK_BACKEND === 'true'
const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api/v1').replace(/\/$/, '')

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API_BASE + path, {
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
    ...init,
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(body?.error?.message || `HTTP ${response.status}`)
  }
  return body as T
}

export const LATENCY = mock.LATENCY
export const sleep = mock.sleep
export const emptyVerification = mock.emptyVerification
export const readPointValue = mock.readPointValue
export const pointTrendSeries = mock.pointTrendSeries
export const pointRawData = mock.pointRawData
export const sinkCheckPlan = mock.sinkCheckPlan
export const subnetHostResult = mock.subnetHostResult
export const configApplyImpact = mock.configApplyImpact
export const applySystemYamlToState = mock.applySystemYamlToState
export const applyDevicesYamlToState = mock.applyDevicesYamlToState
export const logConfigApplied = mock.logConfigApplied

export type PointReadResult = mock.PointReadResult
export type CommandOutcome = mock.CommandOutcome
export type DiagnosticReadRow = mock.DiagnosticReadRow
export type ConfigValidation = mock.ConfigValidation
export type PointTestOutcome = mock.PointTestOutcome
export type TaskStartResult = mock.TaskStartResult
export type WriteTestOutcome = mock.WriteTestOutcome

export async function verifyDevice(
  device: DeviceInst,
): Promise<DeviceVerification> {
  if (USE_MOCK) return mock.verifyDevice(device)
  return request<DeviceVerification>(
    `/verify/devices/${encodeURIComponent(device.device_id)}`,
    { method: 'POST' },
  )
}

export async function verifyAllDevices(devices: DeviceInst[]) {
  if (USE_MOCK) return mock.verifyAllDevices(devices)
  return request<{ failed: number; warning: number }>(
    '/verify/devices',
    {
      method: 'POST',
      body: JSON.stringify(devices.map(device => device.device_id)),
    },
  )
}

export async function readDevicePoint(
  device: DeviceInst,
  point: PointDef,
  index: number,
): Promise<PointReadResult> {
  if (USE_MOCK) return mock.readDevicePoint(device, point, index)
  try {
    const row = await request<any>(
      '/diagnostics/protocol/read',
      {
        method: 'POST',
        body: JSON.stringify({
          device_id: device.device_id,
          point_id: point.point_id,
        }),
      },
    )
    return {
      ok: true,
      error: '',
      errorCode: '',
      latency: Number(
        String(row.Latency || '0').replace(/[^0-9.]/g, ''),
      ) || 0,
      value: row.Value,
      raw: '—',
    } as PointReadResult
  } catch (error) {
    return {
      ok: false,
      error: String(error),
      errorCode: 'PROTOCOL_ERROR',
      latency: 0,
    } as PointReadResult
  }
}

export async function sendDeviceCommand(
  device: DeviceInst,
  point: PointDef,
  index: number,
  value: number | boolean,
): Promise<CommandOutcome> {
  if (USE_MOCK) {
    return mock.sendDeviceCommand(device, point, index, value)
  }
  const row = await request<any>(
    `/devices/${encodeURIComponent(device.device_id)}/commands`,
    {
      method: 'POST',
      body: JSON.stringify({
        point_id: point.point_id,
        value,
      }),
    },
  )
  return {
    requested: row.requested,
    readback: row.readback,
    sentAt: row.sent_at,
    latency: Math.round(row.latency_ms),
    success: row.success,
    error: row.error || row.readback_error || '',
  }
}

export async function startTask(
  task: TaskDef,
): Promise<TaskStartResult> {
  if (USE_MOCK) return mock.startTask(task)
  try {
    await request(
      `/tasks/${encodeURIComponent(task.task_id)}/start`,
      { method: 'POST' },
    )
    task.runtime = 'RUNNING'
    return { ok: true }
  } catch (error) {
    return { ok: false, error: String(error) }
  }
}

export async function stopTask(task: TaskDef): Promise<void> {
  if (USE_MOCK) return mock.stopTask(task)
  await request(
    `/tasks/${encodeURIComponent(task.task_id)}/stop`,
    { method: 'POST' },
  )
  task.runtime = 'STOPPED'
}

export const taskInstanceState = mock.taskInstanceState

export async function verifySink(sink: SinkDef): Promise<void> {
  if (USE_MOCK) return mock.verifySink(sink)
  const result = await request<any>(
    `/sinks/${encodeURIComponent(sink.name)}/verify`,
    { method: 'POST' },
  )
  sink.verification = result
  sink.runtime_state = result.state === 'passed'
    ? 'healthy'
    : 'failed'
  sink.last_test_at = result.checked_at
}

export async function writeTestSink(
  sink: SinkDef,
): Promise<WriteTestOutcome> {
  if (USE_MOCK) return mock.writeTestSink(sink)
  return request(
    `/sinks/${encodeURIComponent(sink.name)}/write-test`,
    { method: 'POST' },
  )
}

export async function pingHost(host: string) {
  if (USE_MOCK) return mock.pingHost(host)
  return request<Record<string, string>>(
    '/diagnostics/ping',
    {
      method: 'POST',
      body: JSON.stringify({ host }),
    },
  )
}

export async function probePorts(
  host: string,
  ports: number[],
) {
  if (USE_MOCK) return mock.probePorts(host, ports)
  return request<Array<Record<string, string | number>>>(
    '/diagnostics/tcp',
    {
      method: 'POST',
      body: JSON.stringify({ host, ports }),
    },
  )
}

export async function runProtocolRead(
  devices: DeviceInst[],
  point: PointDef,
  addressText: string,
): Promise<DiagnosticReadRow[]> {
  if (USE_MOCK) {
    return mock.runProtocolRead(devices, point, addressText)
  }
  return Promise.all(
    devices.map(async device => {
      const row = await request<any>(
        '/diagnostics/protocol/read',
        {
          method: 'POST',
          body: JSON.stringify({
            device_id: device.device_id,
            point_id: point.point_id,
          }),
        },
      )
      return {
        ...row,
        Host: device.host,
        Address: addressText,
        Raw: '—',
        Unit: '—',
        Error: '—',
      } as DiagnosticReadRow
    }),
  )
}

export async function runProtocolWrite(
  device: DeviceInst,
  point: PointDef,
  valueText: string,
  addressText: string,
): Promise<Record<string, string>> {
  if (USE_MOCK) {
    return mock.runProtocolWrite(
      device,
      point,
      valueText,
      addressText,
    )
  }
  const value = valueText === 'true'
    ? true
    : valueText === 'false'
      ? false
      : Number(valueText)
  const row = await request<any>(
    '/diagnostics/protocol/write',
    {
      method: 'POST',
      body: JSON.stringify({
        device_id: device.device_id,
        point_id: point.point_id,
        value,
      }),
    },
  )
  return {
    ...row,
    Address: addressText,
  }
}

export const manualProtocolReadRow = mock.manualProtocolReadRow
export const manualProtocolWriteRow = mock.manualProtocolWriteRow

export function validateConfig(
  file: string,
  text: string,
): ConfigValidation {
  if (USE_MOCK) return mock.validateConfig(file, text)
  if (!text.trim()) {
    return {
      ok: false,
      errors: [`${file}: document is empty`],
    }
  }
  return { ok: true, errors: [] }
}

export async function validateConfigRemote(
  file: string,
  text: string,
): Promise<ConfigValidation> {
  return request(
    '/config/validate',
    {
      method: 'POST',
      body: JSON.stringify({ file, text }),
    },
  )
}

export async function applyConfigRemote(
  file: string,
  text: string,
) {
  return request(
    '/config/apply',
    {
      method: 'POST',
      body: JSON.stringify({ file, text }),
    },
  )
}

export async function loadSettings() {
  return request<any>('/settings')
}

export async function saveSettings(payload: any) {
  return request(
    '/settings',
    {
      method: 'PUT',
      body: JSON.stringify(payload),
    },
  )
}

export async function loadQuality(window = '24 h') {
  return request<any>(
    `/quality?window=${encodeURIComponent(window)}`,
  )
}

export async function runQualityCheck() {
  if (USE_MOCK) return mock.runQualityCheck()
  return request('/quality/check', { method: 'POST' })
}

export async function loadSystemHealth(window = '24 h') {
  return request<any>(
    `/system/health?window=${encodeURIComponent(window)}`,
  )
}

export async function loadLogs(
  params: Record<string, string | number> = {},
) {
  const query = new URLSearchParams(
    Object.entries(params).map(
      ([key, value]) => [key, String(value)],
    ),
  )
  return request<any>(`/logs?${query}`)
}

export async function testPointRead(
  device: DeviceInst,
  protocol: string,
  requestText: string,
): Promise<PointTestOutcome> {
  if (USE_MOCK) {
    return mock.testPointRead(
      device,
      protocol,
      requestText,
    )
  }
  const pointId = requestText.split(/[\s,]+/)[0] || requestText
  try {
    const row = await request<any>(
      '/diagnostics/protocol/read',
      {
        method: 'POST',
        body: JSON.stringify({
          device_id: device.device_id,
          point_id: pointId,
        }),
      },
    )
    return {
      ok: true,
      error: '',
      errorCode: '',
      latency: Number(
        String(row.Latency || '0').replace(/[^0-9.]/g, ''),
      ) || 0,
    }
  } catch (error) {
    return {
      ok: false,
      error: String(error),
      errorCode: 'PROTOCOL_ERROR',
      latency: 0,
    }
  }
}


export async function loadConfigHistory(){
  return request<Array<{revision:number;created_at:string;message:string}>>(
    '/config/history',
  )
}

export async function restoreConfigRevision(revision:number){
  return request(
    `/config/history/${revision}/restore`,
    {method:'POST'},
  )
}

export async function downloadConfigBackup():Promise<Blob>{
  const response=await fetch(API_BASE+'/config/backup')
  if(!response.ok){
    const body=await response.json().catch(()=>({}))
    throw new Error(body?.error?.message||`HTTP ${response.status}`)
  }
  return response.blob()
}

export async function startSubnetScan(cidr:string,ports:number[]){
  return request<{operation_id:string}>(
    '/diagnostics/subnet-scan',
    {method:'POST',body:JSON.stringify({cidr,ports})},
  )
}

export async function getOperation(operationId:string){
  return request<any>(
    `/operations/${encodeURIComponent(operationId)}`,
  )
}

export async function loadDeviceData(
  deviceId:string,
  params:{page?:number;page_size?:number;search?:string;point_group?:string}={},
){
  const query=new URLSearchParams()
  for(const [key,value] of Object.entries(params)){
    if(value!==undefined&&value!=='')query.set(key,String(value))
  }
  return request<any>(
    `/devices/${encodeURIComponent(deviceId)}/data?${query}`,
  )
}

export async function loadDeviceTrend(
  deviceId:string,
  pointIds:string[],
  windowSeconds=600,
){
  const query=new URLSearchParams()
  pointIds.forEach(id=>query.append('point_id',id))
  query.set('window_seconds',String(windowSeconds))
  query.set('limit_per_point','600')
  return request<any[]>(
    `/devices/${encodeURIComponent(deviceId)}/trend?${query}`,
  )
}
