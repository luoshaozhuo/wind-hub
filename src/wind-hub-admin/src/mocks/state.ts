// Mock 最小可变状态：写操作（task start/stop、admin-state、settings、config
// apply）改变 state，后续 GET 可观察到变化；异步 Operation 支持轮询状态流转。
// 不模拟真实 PLC / Kafka / DB，只维护 HTTP 层可见的一致性。
import type { components } from '../api/generated/schema'
import {
  configContentsSeed,
  configHistorySeed,
  devicesSeed,
  settingsSeed,
  sinksSeed,
  taskInstancesSeed,
  tasksSeed,
  type ConfigRevisionDto,
  type DeviceDto,
  type SettingsDto,
  type SinkDto,
  type TaskDto,
  type TaskInstanceDto,
} from './data'

export type OperationDto = components['schemas']['OperationResponse']
export type AdminStateRequestDto = components['schemas']['AdminStateRequest']

interface MockState {
  settings: SettingsDto
  devices: DeviceDto[]
  tasks: TaskDto[]
  taskInstances: Record<string, TaskInstanceDto[]>
  sinks: SinkDto[]
  configContents: Record<string, string>
  configHistory: ConfigRevisionDto[]
  operations: Map<string, OperationDto>
  operationCounter: number
}

function seedState(): MockState {
  return {
    settings: structuredClone(settingsSeed),
    devices: structuredClone(devicesSeed),
    tasks: structuredClone(tasksSeed),
    taskInstances: structuredClone(taskInstancesSeed),
    sinks: structuredClone(sinksSeed),
    configContents: structuredClone(configContentsSeed),
    configHistory: structuredClone(configHistorySeed),
    operations: new Map(),
    operationCounter: 0,
  }
}

export const mockState: MockState = seedState()

/** 测试隔离：每个 spec 开始前还原种子数据。 */
export function resetMockState(): void {
  const fresh = seedState()
  mockState.settings = fresh.settings
  mockState.devices = fresh.devices
  mockState.tasks = fresh.tasks
  mockState.taskInstances = fresh.taskInstances
  mockState.sinks = fresh.sinks
  mockState.configContents = fresh.configContents
  mockState.configHistory = fresh.configHistory
  mockState.operations = fresh.operations
  mockState.operationCounter = fresh.operationCounter
}

export function setTaskRuntime(taskId: string, running: boolean): TaskDto | undefined {
  const task = mockState.tasks.find((t) => t.task_id === taskId)
  if (!task) return undefined
  task.runtime_state = running ? 'running' : 'stopped'
  task.running_instances = running ? 1 : 0
  task.stopped_instances = running ? 0 : 1
  task.instance_count = running ? 1 : 0
  task.placement_state = running ? 'placed' : 'unplaced'
  task.assigned_worker_id = running ? 'collector-1' : null
  const target = task.device ?? task.targets[0] ?? taskId
  mockState.taskInstances[taskId] = running
    ? [
        {
          instance_id: `${taskId}#0`,
          task_id: taskId,
          device_id: target,
          point_group: task.point_group,
          interval: task.interval,
          state: 'running',
          assigned_worker_id: 'collector-1',
          targets: task.targets,
        },
      ]
    : []
  return task
}

/** 应用 admin-state 全量替换（与 PUT /api/v1/admin-state 语义一致）。 */
export function applyAdminState(body: AdminStateRequestDto): void {
  for (const item of body.devices) {
    const device = mockState.devices.find((d) => d.device_id === item.device_id)
    if (device) {
      device.enabled = item.enabled
      device.host = item.host
      if (item.port != null) device.port = item.port
      device.device_group = item.device_group ?? null
    }
  }
  for (const item of body.tasks) {
    const task = mockState.tasks.find((t) => t.task_id === item.task_id)
    if (task) {
      task.enabled = item.enabled
      task.interval = item.interval ?? null
      task.point_group = item.point_group
    }
  }
  for (const item of body.sinks) {
    const sink = mockState.sinks.find((s) => s.name === item.name)
    if (sink) {
      sink.enabled = item.enabled
      sink.connection = { ...item.connection }
      sink.healthy = item.enabled ? sink.healthy : false
    }
  }
}

/** 注册异步 Operation：首轮 running，第二次轮询起 success（确定性流转）。 */
export function createOperation(
  kind: string,
  total: number,
  result?: Record<string, unknown>,
): OperationDto {
  mockState.operationCounter += 1
  const operation: OperationDto = {
    operation_id: `mock-op-${mockState.operationCounter}`,
    kind,
    state: 'running',
    progress: 0,
    completed: 0,
    total,
    created_at: new Date().toISOString(),
    started_at: new Date().toISOString(),
    finished_at: null,
    error: null,
    result: result ?? null,
  }
  mockState.operations.set(operation.operation_id, operation)
  return operation
}

export function pollOperation(operationId: string): OperationDto | undefined {
  const operation = mockState.operations.get(operationId)
  if (!operation) return undefined
  if (operation.state === 'running') {
    operation.completed += 1
    operation.progress = operation.total > 0 ? operation.completed / operation.total : 1
    if (operation.completed >= operation.total) {
      operation.state = 'success'
      operation.progress = 1
      operation.finished_at = new Date().toISOString()
    }
  }
  return operation
}
