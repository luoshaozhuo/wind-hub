// Config capability：配置文件 / 校验 / 应用 / 历史 / 备份 / 设置 / 定义 / admin-state。
// 同时承载 Edit Model → Wire DTO（AdminStateRequest）的序列化，序列化是唯一
// 允许把 UI 模型翻译回 wire 形态的位置。
import { call, client, downloadBlob } from './client'
import type { components } from './generated/schema'
import type {
  DeviceInst,
  DeviceModelDef,
  DeviceTypeDef,
  PointDef,
  PointTableDef,
  SinkDef,
  TaskDef,
  UnitDef,
} from '../domain/types'

export type ConfigFileDto = components['schemas']['ConfigFileResponse']
export type ConfigContentDto = components['schemas']['ConfigContentResponse']
export type ConfigReviewDto = components['schemas']['ConfigReviewResponse']
export type ConfigApplyDto = components['schemas']['ConfigApplyResponse']
export type ConfigRevisionDto = components['schemas']['ConfigRevisionResponse']
export type SettingsDto = components['schemas']['SettingsResponse']
export type DefinitionsDto = components['schemas']['DefinitionsResponse']
export type AdminStateRequestDto = components['schemas']['AdminStateRequest']
export type AdminDefinitionsRequestDto = components['schemas']['AdminDefinitionsStateRequest']

export function fetchConfigFiles(): Promise<ConfigFileDto[]> {
  return call(client.GET('/api/v1/config/files'))
}

export function fetchConfigFile(name: string): Promise<ConfigContentDto> {
  return call(client.GET('/api/v1/config/files/{name}', { params: { path: { name } } }))
}

export function validateConfig(name: string, content: string): Promise<ConfigReviewDto> {
  return call(client.POST('/api/v1/config/validate', { body: { name, content, comment: '' } }))
}

export function applyConfig(name: string, content: string, comment = ''): Promise<ConfigApplyDto> {
  return call(client.POST('/api/v1/config/apply', { body: { name, content, comment } }))
}

export function importConfig(name: string, content: string, comment = ''): Promise<ConfigApplyDto> {
  return call(client.POST('/api/v1/config/import', { body: { name, content, comment } }))
}

export function restoreConfig(revision: number): Promise<ConfigApplyDto> {
  return call(
    client.POST('/api/v1/config/history/{revision}/restore', {
      params: { path: { revision } },
    }),
  )
}

export function fetchConfigHistory(): Promise<ConfigRevisionDto[]> {
  return call(client.GET('/api/v1/config/history'))
}

export function downloadConfigBackup(): Promise<Blob> {
  return downloadBlob('/api/v1/config/backup')
}

export function fetchSettings(): Promise<SettingsDto> {
  return call(client.GET('/api/v1/settings'))
}

export function updateSettings(
  body: components['schemas']['SettingsRequest'],
): Promise<ConfigApplyDto> {
  return call(client.PUT('/api/v1/settings', { body }))
}

export function fetchDefinitions(): Promise<DefinitionsDto> {
  return call(client.GET('/api/v1/definitions'))
}

export function replaceAdminState(body: AdminStateRequestDto): Promise<ConfigApplyDto> {
  return call(client.PUT('/api/v1/admin-state', { body }))
}

// -- Edit Model → Wire DTO 序列化 -------------------------------------------

export interface DefinitionsEditState {
  units: Record<string, UnitDef>
  deviceTypes: DeviceTypeDef[]
  deviceModels: DeviceModelDef[]
  pointTables: PointTableDef[]
  points: Record<string, PointDef[]>
}

export interface AdminStateEditState extends DefinitionsEditState {
  devices: DeviceInst[]
  tasks: TaskDef[]
  sinks: SinkDef[]
}

/** 非 system 点表（含点）的稳定投影，用于脏检测与 raw 透传基线。 */
export function pointProjection(state: DefinitionsEditState): string {
  return JSON.stringify(
    state.pointTables
      .filter((t) => !t.system)
      .map((t) => ({ ...t, points: state.points[t.id] || [] })),
  )
}

/**
 * 序列化 definitions。点表投影未变时透传后端原始 point_tables（保留未知键），
 * 发生变化时按 UI 模型重建。
 */
export function buildDefinitionsPayload(
  state: DefinitionsEditState,
  rawPointTables: Record<string, Record<string, unknown>>,
  baselineProjection: string,
): AdminDefinitionsRequestDto {
  const units = Object.fromEntries(
    Object.entries(state.units).map(([id, value]) => [id, { ...value }]),
  )
  const device_types = Object.fromEntries(
    state.deviceTypes.map((item) => [item.id, { name: item.name || null }]),
  )
  const device_models = Object.fromEntries(
    state.deviceModels.map((item) => {
      const value: Record<string, unknown> = {
        device_type: item.device_type,
        manufacturer: item.manufacturer || null,
        model: item.model || null,
        protocol: item.protocol,
        point_table: item.point_table,
        properties: { ...item.properties },
        connection_defaults: { ...item.connection_defaults },
      }
      if (item.read_mode) value.read_mode = item.read_mode
      return [item.id, value]
    }),
  )
  let point_tables: Record<string, Record<string, unknown>>
  if (pointProjection(state) === baselineProjection) {
    point_tables = structuredClone(rawPointTables)
  } else {
    point_tables = Object.fromEntries(
      state.pointTables
        .filter((t) => !t.system)
        .map((table) => {
          const value: Record<string, unknown> = {
            remove_points: [...(table.remove_points || [])],
            points: (state.points[table.id] || []).map((point) => ({
              point_id: point.point_id,
              variable_name: point.variable_name || undefined,
              point_groups: [...point.point_groups],
              address: { ...point.address },
              data_type: point.data_type,
              scale: point.scale,
              offset: point.offset,
              unit: point.unit,
              description: point.description || undefined,
            })),
          }
          if (table.protocol !== 'generic') value.protocol = table.protocol
          if (table.extends) value.extends = table.extends
          return [table.id, value]
        }),
    )
  }
  return { units, device_types, device_models, point_tables }
}

export function buildAdminStatePayload(
  state: AdminStateEditState,
  rawPointTables: Record<string, Record<string, unknown>>,
  baselineProjection: string,
): AdminStateRequestDto {
  return {
    devices: state.devices.map((d) => ({
      device_id: d.device_id,
      model: d.model,
      device_group: d.device_group || null,
      host: d.host,
      port: d.port ?? null,
      extensions: { ...(d.extensions || {}) },
      enabled: d.enabled,
    })),
    tasks: state.tasks.map((t) => ({
      task_id: t.task_id,
      device: t.device || null,
      device_group: t.device_group || null,
      point_group: t.point_group,
      interval: t.interval,
      sinks: [...t.sinks],
      enabled: t.enabled,
    })),
    sinks: state.sinks.map((s) => ({
      name: s.name,
      type: s.type,
      enabled: s.enabled,
      connection: { ...s.connection },
      points: s.points.map((point) => ({ ...point })),
    })),
    definitions: buildDefinitionsPayload(state, rawPointTables, baselineProjection),
  }
}
