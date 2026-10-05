// 任务领域纯函数：目标解析、可采集性校验、实例状态推导。
import { isDefaultPointGroup, isDefaultPointTable, pointsOfTable } from './points'
import { modelOf } from './devices'
import type {
  DeviceInst,
  DeviceModelDef,
  DeviceVerification,
  PointDef,
  PointGroupDef,
  PointTableDef,
  SinkDef,
  TaskDef,
} from './types'

export interface TasksSnapshot {
  devices: DeviceInst[]
  deviceModels: DeviceModelDef[]
  pointTables: PointTableDef[]
  pointGroups: PointGroupDef[]
  points: Record<string, PointDef[]>
  sinks: SinkDef[]
  tasks: TaskDef[]
}

export function devicesForTask(snapshot: TasksSnapshot, t: TaskDef): DeviceInst[] {
  if (t.device) return snapshot.devices.filter((d) => d.device_id === t.device && d.enabled)
  if (t.device_group)
    return snapshot.devices.filter((d) => d.device_group === t.device_group && d.enabled)
  return []
}

/** 返回空串表示任务可采集，否则为不可采集原因（用于禁用 Start）。 */
export function taskInvalidReason(snapshot: TasksSnapshot, t: TaskDef): string {
  if (!t.sinks.length) return 'Task has no Sink target'
  for (const sinkName of t.sinks) {
    const sink = snapshot.sinks.find((s) => s.name === sinkName)
    if (!sink) return `Sink '${sinkName}' does not exist`
    if (!sink.enabled) return `Sink '${sinkName}' is disabled`
  }
  if (!snapshot.pointGroups.some((g) => g.id === t.point_group)) return 'Point Group does not exist'
  if (isDefaultPointGroup(t.point_group))
    return 'Default Point Group is a placeholder and cannot be collected'

  const targets = devicesForTask(snapshot, t)
  if (!targets.length) return 'Task target resolves to no devices'

  for (const d of targets) {
    const model = modelOf(snapshot, d)
    if (!model) return `Device ${d.device_id} has no valid model`
    if (isDefaultPointTable(model.point_table))
      return `Device ${d.device_id} is assigned to a default Point Table`
    if (!snapshot.pointTables.some((pt) => pt.id === model.point_table))
      return `Device ${d.device_id} references a missing Point Table`
    if (
      !pointsOfTable(snapshot, model.point_table).some((p) =>
        p.point_groups.includes(t.point_group),
      )
    ) {
      return `Device ${d.device_id} Point Table has no points in group '${t.point_group}'`
    }
  }
  return ''
}

/** 重算全部任务 valid/invalid_reason；无效运行中任务的展示态降为 STOPPED（展示语义与旧实现一致）。 */
export function refreshTaskValidity(snapshot: TasksSnapshot): void {
  for (const t of snapshot.tasks) {
    const reason = taskInvalidReason(snapshot, t)
    t.valid = !reason
    t.invalid_reason = reason
    if (reason && t.runtime === 'RUNNING') t.runtime = 'STOPPED'
  }
}

/** 受一组 Point Table 影响的 model / device / task 集合（PointsPage 影响面确认用）。 */
export function affectedByPointTables(snapshot: TasksSnapshot, tableIds: string[]) {
  const tables = [...new Set(tableIds)]
  const modelIds = snapshot.deviceModels
    .filter((m) => tables.includes(m.point_table))
    .map((m) => m.id)
  const devices = snapshot.devices.filter((d) => modelIds.includes(d.model))
  const deviceIds = new Set(devices.map((d) => d.device_id))
  const groups = new Set(devices.map((d) => d.device_group))
  const tasks = snapshot.tasks.filter(
    (t) => (t.device && deviceIds.has(t.device)) || (t.device_group && groups.has(t.device_group)),
  )
  return { tables, modelIds, devices, tasks }
}

export function taskInstanceState(
  t: TaskDef,
  d: DeviceInst,
  verification: DeviceVerification | undefined,
): 'RUNNING' | 'WARNING' | 'FAILED' | 'STOPPED' {
  if (t.runtime !== 'RUNNING') return 'STOPPED'
  if (!d.online) return 'FAILED'
  if (verification?.state === 'warning') return 'WARNING'
  if (verification?.state === 'failed') return 'FAILED'
  return 'RUNNING'
}
