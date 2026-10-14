// Mock 可变运行时状态：task 状态机、sink 计数器、config 三层
// （Working Copy / Applied State / Revision History）、异步 Operation、
// Quality tick。静态种子在 data.ts，故障场景在 scenarios.ts。
import type { components } from '../api/generated/schema'
import {
  devicesSeed,
  seedYamlFiles,
  settingsSeed,
  sinksSeed,
  tasksSeed,
  type MockDevice,
  type MockSink,
  type MockTask,
  type SettingsDto,
} from './data'
import { clearCommandOverrides } from './values'
import { seedLogs } from './logs'

export type OperationDto = components['schemas']['OperationResponse']

// Task 状态机：start → starting（readyAt 前）→ running；stop → stopping → stopped。
export const TASK_TRANSITION_MS = 8000

export interface TaskRuntime {
  phase: 'running' | 'starting' | 'stopping' | 'stopped'
  readyAt: number
}

export interface SinkRuntime {
  writes_total: number
  failures_total: number
  dropped_points: number
  queue_depth: number
  latency_ms: number
  error: string
  last_write_at: string
}

export interface OperationEntry {
  operation: OperationDto
  // 终态计划：poll 次数达到 total 后落定（pending/running → success/partial/failed）
  terminalState: string
  result: Record<string, unknown> | null
  error: { code: string; message: string } | null
}

export interface MockState {
  settings: SettingsDto
  devices: MockDevice[]
  tasks: MockTask[]
  taskRuntime: Map<string, TaskRuntime>
  sinks: MockSink[]
  sinkRuntime: Map<string, SinkRuntime>
  // Config 三层：workingTexts 是 GET 可见内容；appliedTexts 是生效态；
  // revisions / revisionSnapshots 是历史（restore 生成新 revision，不改旧的）。
  workingTexts: Record<string, string>
  appliedTexts: Record<string, string>
  revisions: components['schemas']['ConfigRevisionResponse'][]
  revisionSnapshots: Map<number, Record<string, string>>
  operations: Map<string, OperationEntry>
  operationCounter: number
  qualityCheckTick: number
}

function seedTaskRuntime(tasks: MockTask[]): Map<string, TaskRuntime> {
  return new Map(
    tasks.map((t) => [t.task_id, { phase: t.running ? 'running' : 'stopped', readyAt: 0 }]),
  )
}

function seedSinkRuntime(sinks: MockSink[]): Map<string, SinkRuntime> {
  const map = new Map<string, SinkRuntime>()
  for (const s of sinks) {
    map.set(s.name, {
      writes_total: s.name === 'file_archive' ? 18342 : 0,
      failures_total: s.name === 'db_main' ? 14 : 0,
      dropped_points: s.name === 'db_main' ? 6 : 0,
      queue_depth: s.name === 'db_main' ? 128 : s.name === 'file_archive' ? 2 : 0,
      latency_ms: s.name === 'file_archive' ? 4 : 0,
      error:
        s.name === 'db_main'
          ? 'SINK_CONNECTION_REFUSED'
          : s.name === 'kafka_main'
            ? 'SINK_BROKER_TIMEOUT'
            : '',
      last_write_at: s.name === 'file_archive' ? '2026-10-08T07:59:58Z' : '',
    })
  }
  return map
}

function seedState(): MockState {
  const devices = structuredClone(devicesSeed)
  const tasks = structuredClone(tasksSeed)
  const settings = structuredClone(settingsSeed)
  const yaml = seedYamlFiles(settings, devices, tasks)
  return {
    settings,
    devices,
    tasks,
    taskRuntime: seedTaskRuntime(tasks),
    sinks: structuredClone(sinksSeed),
    sinkRuntime: seedSinkRuntime(sinksSeed),
    workingTexts: { ...yaml },
    appliedTexts: { ...yaml },
    revisions: [
      {
        revision: 2,
        created_at: '2026-10-07T09:30:00Z',
        source: 'apply',
        comment: 'enable kafka_main sink',
      },
      {
        revision: 1,
        created_at: '2026-10-01T08:00:00Z',
        source: 'import',
        comment: 'initial template import',
      },
    ],
    revisionSnapshots: new Map([
      [2, { ...yaml }],
      [1, { ...yaml }],
    ]),
    operations: new Map(),
    operationCounter: 0,
    qualityCheckTick: 0,
  }
}

export const mockState: MockState = seedState()

/** 测试隔离：每个 spec 前还原种子（含点值 override 与日志）。 */
export function resetMockState(): void {
  const fresh = seedState()
  mockState.settings = fresh.settings
  mockState.devices = fresh.devices
  mockState.tasks = fresh.tasks
  mockState.taskRuntime = fresh.taskRuntime
  mockState.sinks = fresh.sinks
  mockState.sinkRuntime = fresh.sinkRuntime
  mockState.workingTexts = fresh.workingTexts
  mockState.appliedTexts = fresh.appliedTexts
  mockState.revisions = fresh.revisions
  mockState.revisionSnapshots = fresh.revisionSnapshots
  mockState.operations = fresh.operations
  mockState.operationCounter = fresh.operationCounter
  mockState.qualityCheckTick = fresh.qualityCheckTick
  clearCommandOverrides()
  seedLogs()
}

export function taskRuntimeOf(taskId: string): TaskRuntime {
  let runtime = mockState.taskRuntime.get(taskId)
  if (!runtime) {
    runtime = { phase: 'stopped', readyAt: 0 }
    mockState.taskRuntime.set(taskId, runtime)
  }
  return runtime
}

export function sinkRuntimeOf(name: string): SinkRuntime {
  let runtime = mockState.sinkRuntime.get(name)
  if (!runtime) {
    runtime = {
      writes_total: 0,
      failures_total: 0,
      dropped_points: 0,
      queue_depth: 0,
      latency_ms: 0,
      error: '',
      last_write_at: '',
    }
    mockState.sinkRuntime.set(name, runtime)
  }
  return runtime
}

/** Task 相位派生：starting/stopping 超过 readyAt 后落定（时间确定，无随机）。 */
export function taskPhase(taskId: string): 'running' | 'starting' | 'stopping' | 'stopped' {
  const runtime = taskRuntimeOf(taskId)
  if (
    (runtime.phase === 'starting' || runtime.phase === 'stopping') &&
    Date.now() >= runtime.readyAt
  ) {
    runtime.phase = runtime.phase === 'starting' ? 'running' : 'stopped'
    runtime.readyAt = 0
  }
  return runtime.phase
}

export function nextRevision(): number {
  return (mockState.revisions[0]?.revision ?? 0) + 1
}

export function registerOperation(
  kind: string,
  totalPolls: number,
  terminalState: string,
  result: Record<string, unknown> | null,
  error: { code: string; message: string } | null = null,
): OperationDto {
  mockState.operationCounter += 1
  const operation: OperationDto = {
    operation_id: `mock-op-${mockState.operationCounter}`,
    kind,
    state: 'running',
    progress: 0,
    completed: 0,
    total: totalPolls,
    created_at: new Date().toISOString(),
    started_at: new Date().toISOString(),
    finished_at: null,
    error: null,
    result: null,
  }
  mockState.operations.set(operation.operation_id, {
    operation,
    terminalState,
    result,
    error,
  })
  return operation
}

/** Operation 轮询：每次 completed+1，到达 total 后按预定终态落定。 */
export function pollOperation(operationId: string): OperationDto | undefined {
  const entry = mockState.operations.get(operationId)
  if (!entry) return undefined
  const op = entry.operation
  if (op.state === 'running' || op.state === 'pending') {
    op.completed += 1
    op.progress = op.total > 0 ? Math.min(1, op.completed / op.total) : 1
    if (op.completed >= op.total) {
      op.state = entry.terminalState
      op.progress = 1
      op.finished_at = new Date().toISOString()
      op.result = entry.result
      if (entry.error) op.error = { code: entry.error.code, message: entry.error.message }
    }
  }
  return op
}
