// Config Store（Pinia）：只持有 Client/Edit State —— 配置编辑投影、脏标记、
// 显式保存，以及设备验证 / Sink 测试计数等客户端覆盖层。
// Server State 一律由 Vue Query 持有；useServerSnapshot 把查询结果同步进来。
// 持久化只经由 mutate() → markDirty → scheduleSave → save() 显式触发，
// 不存在 watch(store) 的隐式持久化。
import { markRaw } from 'vue'
import { defineStore } from 'pinia'
import { useDebounceFn } from '@vueuse/core'
import { ElMessage } from 'element-plus'
import { queryClient } from '../api/queryClient'
import { CONFIG_QUERY_KEYS, qk } from '../api/queryKeys'
import {
  buildAdminStatePayload,
  buildDefinitionsPayload,
  pointProjection,
  replaceAdminState,
  type DefinitionsDto,
  type SettingsDto,
} from '../api/config'
import type { DeviceDto } from '../api/devices'
import type { TaskDto } from '../api/tasks'
import {
  verifySink as apiVerifySink,
  writeTestSink as apiWriteTestSink,
  type SinkDto,
  type SinkTestResult,
} from '../api/sinks'
import type { OverviewDto } from '../api/monitoring'
import { ping, pointTableTest, protocolCheck } from '../api/diagnostics'
import { awaitOperation } from '../api/operations'
import { DEFAULT_POINT_GROUP_ID, DEFAULT_POINT_TABLE_ID } from '../domain/points'
import { emptyVerification } from '../domain/devices'
import { refreshTaskValidity } from '../domain/tasks'
import type {
  DeviceGroupDef,
  DeviceInst,
  DeviceModelDef,
  DeviceTypeDef,
  DeviceVerification,
  PointAddress,
  PointDef,
  PointGroupDef,
  PointTableDef,
  SinkDef,
  SinkVerificationCheck,
  TaskDef,
  UnitDef,
} from '../domain/types'

interface SystemInfo {
  siteId: string
  siteName: string
  collectorVersion: string
  adminVersion: string
  runtimeStatus: string
  configSet: string
  timezone: string
  logLevel: string
  tempDirectory: string
  dataDirectory: string
  reloadPolicy: string
  apiHost: string
  apiPort: number
  timeSync: string
  ads: { local_ip: string; local_ams_net_id: string; username: string; password: string }
}

interface ConfigStoreState {
  systemInfo: SystemInfo
  units: Record<string, UnitDef>
  deviceTypes: DeviceTypeDef[]
  deviceGroups: DeviceGroupDef[]
  deviceModels: DeviceModelDef[]
  pointTables: PointTableDef[]
  pointGroups: PointGroupDef[]
  points: Record<string, PointDef[]>
  devices: DeviceInst[]
  deviceVerification: Record<string, DeviceVerification>
  sinks: SinkDef[]
  tasks: TaskDef[]
  /** 编辑脏标记：只能经 markDirty/mutate 置位，save 成功或强制回退后清除。 */
  dirty: boolean
  saving: boolean
  hydrated: boolean
  /** 后端原始 point_tables：投影未变时透传，避免丢失 UI 不认识的键。 */
  rawPointTables: Record<string, Record<string, unknown>>
  baselinePointProjection: string
  _saveAgain: boolean
  _debouncedSave: (() => void) | null
  _syncedParts: string[]
  _definitionDeviceGroups: string[]
}

/** useServerSnapshot 推送的服务器快照（各部分可独立到达）。 */
export interface ServerSnapshot {
  settings?: SettingsDto
  definitions?: DefinitionsDto
  devices?: DeviceDto[]
  tasks?: TaskDto[]
  sinks?: SinkDto[]
  overview?: OverviewDto
}

function asString(value: unknown, fallback = '') {
  return typeof value === 'string' ? value : fallback
}
function asNumber(value: unknown, fallback = 0) {
  return typeof value === 'number' ? value : fallback
}
function asObject(value: unknown) {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}
function asArray(value: unknown) {
  return Array.isArray(value) ? value : []
}

function normalizePoint(raw: Record<string, unknown>): PointDef {
  return {
    point_id: asString(raw.point_id),
    variable_name: asString(raw.variable_name, asString(raw.point_id)),
    point_groups: asArray(raw.point_groups).map(String),
    address: { ...asObject(raw.address) } as PointAddress,
    data_type: asString(raw.data_type, 'float32'),
    scale: asNumber(raw.scale, 1),
    offset: asNumber(raw.offset, 0),
    unit: asString(raw.unit, 'none'),
    description: asString(raw.description),
  }
}

function formatNow(): string {
  return new Date().toISOString().replace('T', ' ').slice(0, 19)
}

function sinkSteps(result: SinkTestResult): SinkVerificationCheck[] {
  return (result.steps || []).map((step, index) => ({
    layer: (String(step.stage || 'target') === 'open'
      ? 'network'
      : 'target') as SinkVerificationCheck['layer'],
    name: String(step.stage || `step ${index + 1}`),
    state: (step.success === false ? 'failed' : 'passed') as SinkVerificationCheck['state'],
    target: 'backend',
    latency_ms: 0,
    detail: String(step.message || ''),
    error_code: step.success === false ? 'SINK_TEST_FAILED' : '',
  }))
}

export const useConfigStore = defineStore('config', {
  state: (): ConfigStoreState => ({
    systemInfo: {
      siteId: '',
      siteName: '',
      collectorVersion: 'server',
      adminVersion: 'v0.3.0',
      runtimeStatus: 'STARTING',
      configSet: '',
      timezone: '',
      logLevel: 'INFO',
      tempDirectory: '',
      dataDirectory: '',
      reloadPolicy: 'incremental',
      apiHost: '',
      apiPort: 8080,
      timeSync: '',
      ads: { local_ip: '', local_ams_net_id: '', username: 'Administrator', password: '' },
    },
    units: {},
    deviceTypes: [],
    deviceGroups: [],
    deviceModels: [],
    pointTables: [],
    pointGroups: [],
    points: {},
    devices: [],
    deviceVerification: {},
    sinks: [],
    tasks: [],
    dirty: false,
    saving: false,
    hydrated: false,
    rawPointTables: {},
    baselinePointProjection: '',
    _saveAgain: false,
    _debouncedSave: null,
    _syncedParts: [],
    _definitionDeviceGroups: [],
  }),

  actions: {
    /**
     * 服务器快照同步入口（唯一允许把 Server State 写入编辑投影的位置）。
     * - 非脏：整体重建对应部分（用户没有未保存编辑，直接跟随服务器）。
     * - 脏：仅补丁运行时字段（online / runtime / 健康度），绝不覆盖编辑。
     * force 用于保存失败后的强制回退。
     */
    syncFromServer(snapshot: ServerSnapshot, options: { force?: boolean } = {}) {
      const followServer = !this.dirty || options.force === true
      if (snapshot.settings) this._syncSettings(snapshot.settings)
      if (snapshot.overview) this._syncOverview(snapshot.overview)
      if (snapshot.definitions && followServer) this._syncDefinitions(snapshot.definitions)
      if (snapshot.devices) this._syncDevices(snapshot.devices, followServer)
      if (snapshot.tasks) this._syncTasks(snapshot.tasks, followServer)
      if (snapshot.sinks) this._syncSinks(snapshot.sinks, followServer)
      for (const key of Object.keys(snapshot)) {
        if (!this._syncedParts.includes(key)) this._syncedParts.push(key)
      }
      if (followServer) {
        refreshTaskValidity(this)
        if (snapshot.definitions) {
          this.baselinePointProjection = pointProjection(this)
        }
      }
      if (this._syncedParts.length >= 6) this.hydrated = true
    },

    _syncSettings(settings: SettingsDto) {
      this.systemInfo.siteId = settings.site_id
      this.systemInfo.siteName = settings.site_name || settings.site_id
      this.systemInfo.apiHost = settings.api_host
      this.systemInfo.apiPort = settings.api_port
      this.systemInfo.ads = {
        local_ip: settings.ads_local_ip || '',
        local_ams_net_id: settings.ads_local_ams_net_id || '',
        username: settings.ads_username,
        password: settings.ads_password,
      }
    },

    _syncOverview(overview: OverviewDto) {
      this.systemInfo.runtimeStatus = overview.runtime_running ? 'RUNNING' : 'STOPPED'
    },

    _syncDefinitions(defs: DefinitionsDto) {
      this.units = Object.fromEntries(
        Object.entries(defs.units).map(([id, value]) => [
          id,
          { symbol: asString(value.symbol), name: asString(value.name, id) },
        ]),
      )
      this.deviceTypes = Object.entries(defs.device_types).map(([id, value]) => ({
        id,
        name: asString(value.name, id),
      }))
      this.deviceModels = Object.entries(defs.device_models).map(([id, value]) => ({
        id,
        device_type: asString(value.device_type),
        manufacturer: asString(value.manufacturer),
        model: asString(value.model) || undefined,
        protocol: asString(value.protocol) as DeviceModelDef['protocol'],
        point_table: asString(value.point_table),
        read_mode: asString(value.read_mode),
        properties: { ...asObject(value.properties) },
        connection_defaults: { ...asObject(value.connection_defaults) },
      }))
      // markRaw：raw 透传基线只参与序列化（structuredClone），不能成为
      // 响应式代理（Proxy 不可克隆，会导致保存路径抛 DataCloneError）。
      // 入参可能已被 Vue Query 深度响应式化：JSON round-trip 深解包
      // （point_tables 来自 wire JSON，无不可序列化值）。
      this.rawPointTables = markRaw(
        JSON.parse(JSON.stringify(defs.point_tables)) as DefinitionsDto['point_tables'],
      )
      const tables: PointTableDef[] = [
        {
          id: DEFAULT_POINT_TABLE_ID,
          protocol: 'generic',
          extends: '',
          remove_points: [],
          system: true,
        },
      ]
      const points: Record<string, PointDef[]> = { [DEFAULT_POINT_TABLE_ID]: [] }
      for (const [id, value] of Object.entries(defs.point_tables)) {
        tables.push({
          id,
          protocol: (asString(value.protocol) || 'generic') as PointTableDef['protocol'],
          extends: asString(value.extends),
          remove_points: asArray(value.remove_points).map(String),
        })
        points[id] = asArray(value.points).map((item) => normalizePoint(asObject(item)))
      }
      this.pointTables = tables
      this.points = points
      this.pointGroups = [
        { id: DEFAULT_POINT_GROUP_ID, name: 'Default / Unassigned', system: true },
        ...defs.point_groups
          .filter((id) => id !== DEFAULT_POINT_GROUP_ID)
          .map((id) => ({ id, name: id })),
      ]
      this._definitionDeviceGroups = [...defs.device_groups]
      // definitions 可能晚于 devices 到达：到达后重推导一次 deviceGroups。
      if (this.devices.length) this._deriveDeviceGroups()
    },

    _deriveDeviceGroups() {
      const groupIds = [
        ...new Set([
          ...this._definitionDeviceGroups,
          ...this.devices.map((d) => d.device_group).filter(Boolean),
        ]),
      ]
      this.deviceGroups = groupIds.map((id) => {
        const device = this.devices.find((d) => d.device_group === id)
        const model = device ? this.deviceModels.find((m) => m.id === device.model) : undefined
        return { id, device_type: model?.device_type || '' }
      })
    },

    _syncDevices(rows: DeviceDto[], followServer: boolean) {
      if (!followServer) {
        // 脏状态仅补丁运行时字段。
        for (const row of rows) {
          const device = this.devices.find((d) => d.device_id === row.device_id)
          if (device) device.online = row.connected
        }
        return
      }
      this.devices = rows.map((row) => ({
        device_id: row.device_id,
        model: row.model || '',
        device_group: row.device_group || '',
        host: row.host,
        port: row.port_override ?? undefined,
        extensions: { ...(row.extension_overrides || {}) },
        enabled: row.enabled,
        online: row.connected,
      }))
      this._deriveDeviceGroups()
      const existing = this.deviceVerification
      this.deviceVerification = {}
      for (const device of this.devices) {
        this.deviceVerification[device.device_id] =
          existing[device.device_id] || emptyVerification()
      }
    },

    _syncTasks(rows: TaskDto[], followServer: boolean) {
      if (!followServer) {
        for (const row of rows) {
          const task = this.tasks.find((t) => t.task_id === row.task_id)
          if (task) task.runtime = row.runtime_state.toUpperCase()
        }
        return
      }
      this.tasks = rows.map((row) => ({
        task_id: row.task_id,
        device: row.device || '',
        device_group: row.device_group || '',
        point_group: row.point_group,
        interval: row.interval,
        sinks: [...row.targets],
        enabled: row.enabled,
        runtime: row.runtime_state.toUpperCase(),
        valid: true,
        invalid_reason: '',
        created_at: '',
        updated_at: '',
      }))
    },

    _syncSinks(rows: SinkDto[], followServer: boolean) {
      if (!followServer) {
        for (const row of rows) {
          const sink = this.sinks.find((s) => s.name === row.name)
          if (sink) {
            sink.runtime_state = !row.enabled ? 'disabled' : row.healthy ? 'healthy' : 'failed'
            sink.queue_depth = row.queue_depth
            if (!sink.error || row.message) sink.error = row.message || sink.error
          }
        }
        return
      }
      const previous = Object.fromEntries(this.sinks.map((s) => [s.name, s]))
      this.sinks = rows.map((row) => {
        const old = previous[row.name]
        return {
          name: row.name,
          type: row.type as SinkDef['type'],
          enabled: row.enabled,
          connection: { ...row.connection },
          points: (row.points || []).map((point) => ({ ...point })),
          runtime_state: (!row.enabled
            ? 'disabled'
            : row.healthy
              ? 'healthy'
              : 'failed') as SinkDef['runtime_state'],
          last_test_at: old?.last_test_at || '',
          last_write_at: old?.last_write_at || '',
          latency_ms: old?.latency_ms || 0,
          error: row.message || '',
          queue_depth: row.queue_depth,
          writes_total: old?.writes_total || 0,
          failures_total: old?.failures_total || 0,
          dropped_points: old?.dropped_points || 0,
          verification: old?.verification || {
            state: 'never',
            checked_at: '',
            passed: 0,
            total: 0,
            checks: [],
          },
        }
      })
    },

    /**
     * 全部编辑的唯一入口：执行变更 → 重算任务有效性 → 置脏 → 调度防抖保存。
     * 页面不得直接改 state 而不走 mutate（否则不会持久化）。
     */
    mutate(change?: () => void) {
      change?.()
      refreshTaskValidity(this)
      this.markDirty()
      this.scheduleSave()
    },

    markDirty() {
      this.dirty = true
    },

    scheduleSave() {
      if (!this._debouncedSave) {
        this._debouncedSave = useDebounceFn(() => {
          void this.save()
        }, 350)
      }
      this._debouncedSave()
    },

    /** 显式保存：PUT /admin-state；成功后失效配置类查询，失败回退服务器状态。 */
    async save() {
      if (!this.hydrated) return
      if (this.saving) {
        this._saveAgain = true
        return
      }
      this.saving = true
      try {
        const result = await replaceAdminState(
          buildAdminStatePayload(this, this.rawPointTables, this.baselinePointProjection),
        )
        if (!result.success) {
          throw new Error(result.errors.join('; ') || 'Configuration apply failed')
        }
        if (pointProjection(this) !== this.baselinePointProjection) {
          this.rawPointTables = markRaw(
            structuredClone(
              buildDefinitionsPayload(this, this.rawPointTables, this.baselinePointProjection)
                .point_tables,
            ),
          )
          this.baselinePointProjection = pointProjection(this)
        }
        this.dirty = false
        for (const key of CONFIG_QUERY_KEYS) {
          await queryClient.invalidateQueries({ queryKey: key })
        }
      } catch (error) {
        ElMessage.error(
          error instanceof Error ? error.message : 'Configuration synchronization failed',
        )
        this.dirty = false
        this.resyncFromCache()
        for (const key of CONFIG_QUERY_KEYS) {
          await queryClient.invalidateQueries({ queryKey: key })
        }
      } finally {
        this.saving = false
        if (this._saveAgain) {
          this._saveAgain = false
          void this.save()
        }
      }
    },

    /** 保存失败后：用 query 缓存中的服务器状态强制重建编辑投影。 */
    resyncFromCache() {
      const devices = queryClient.getQueryData<{ items: DeviceDto[] }>(qk.devices)
      const tasks = queryClient.getQueryData<{ items: TaskDto[] }>(qk.tasks)
      this.syncFromServer(
        {
          settings: queryClient.getQueryData<SettingsDto>(qk.settings),
          definitions: queryClient.getQueryData<DefinitionsDto>(qk.definitions),
          devices: devices?.items,
          tasks: tasks?.items,
          sinks: queryClient.getQueryData<SinkDto[]>(qk.sinks),
          overview: queryClient.getQueryData<OverviewDto>(qk.overview),
        },
        { force: true },
      )
    },

    /** 任务运行时展示补丁（非编辑，不置脏）。 */
    patchTaskRuntime(taskId: string, runtime: string) {
      const task = this.tasks.find((t) => t.task_id === taskId)
      if (task) task.runtime = runtime
    },

    // -- 设备验证（客户端覆盖层，多步编排） ---------------------------------

    async verifyDevice(d: DeviceInst): Promise<DeviceVerification> {
      const target =
        this.deviceVerification[d.device_id] ||
        (this.deviceVerification[d.device_id] = emptyVerification())
      Object.assign(target, {
        state: 'running',
        network: 'checking',
        protocol: 'checking',
        points: 'checking',
        errors: [],
      })
      const started = performance.now()
      try {
        const pingResult = await ping(d.host, 1)
        target.network = pingResult.reachable ? 'success' : 'failed'
        if (!pingResult.reachable) {
          target.protocol = 'failed'
          target.points = 'failed'
          target.state = 'failed'
          target.errors = [{ stage: 'network', target: d.host, message: 'Host unreachable' }]
          return target
        }
        const check = await protocolCheck(d.device_id)
        target.protocol = check.connected ? 'success' : 'failed'
        if (!check.connected) {
          target.points = 'failed'
          target.state = 'failed'
          target.errors = [
            { stage: 'protocol', target: d.device_id, message: 'Protocol connection unavailable' },
          ]
          return target
        }
        const op = await pointTableTest(d.device_id)
        const done = await awaitOperation(op.operation_id)
        const rows = Array.isArray(done.result?.points)
          ? (done.result.points as Array<Record<string, unknown>>)
          : []
        target.point_total = rows.length
        target.point_failed = rows.filter((row) => row.success === false).length
        target.point_success = target.point_total - target.point_failed
        target.points =
          done.state === 'success' ? 'success' : done.state === 'partial' ? 'partial' : 'failed'
        target.state =
          done.state === 'success' ? 'success' : done.state === 'partial' ? 'warning' : 'failed'
        target.errors = rows
          .filter((row) => row.success === false)
          .map((row) => ({
            stage: 'points' as const,
            target: String(row.point_id || d.device_id),
            message: String(row.error || 'Read failed'),
          }))
        return target
      } catch (error) {
        target.state = 'failed'
        target.network = target.network === 'checking' ? 'failed' : target.network
        target.protocol = target.protocol === 'checking' ? 'failed' : target.protocol
        target.points = target.points === 'checking' ? 'failed' : target.points
        target.errors.push({
          stage: 'protocol',
          target: d.device_id,
          message: error instanceof Error ? error.message : String(error),
        })
        return target
      } finally {
        target.verified_at = formatNow()
        target.latency_ms = Math.round(performance.now() - started)
      }
    },

    async verifyAllDevices(devices: DeviceInst[]) {
      let failed = 0
      let warning = 0
      for (const device of devices) {
        const result = await this.verifyDevice(device)
        if (result.state === 'failed') failed++
        if (result.state === 'warning') warning++
      }
      return { failed, warning }
    },

    // -- Sink 测试（客户端覆盖层） -------------------------------------------

    async verifySink(s: SinkDef): Promise<void> {
      const row = await apiVerifySink(s.name)
      s.last_test_at = formatNow()
      s.latency_ms = Math.round(row.latency_ms)
      s.error = row.message || ''
      s.verification = {
        state: row.success ? 'passed' : 'failed',
        checked_at: s.last_test_at,
        passed: (row.steps || []).filter((x) => x.success !== false).length,
        total: (row.steps || []).length,
        checks: sinkSteps(row),
      }
      s.runtime_state = !s.enabled ? 'disabled' : row.success ? 'healthy' : 'failed'
    },

    async writeTestSink(s: SinkDef) {
      const row = await apiWriteTestSink(s.name)
      if (row.success) {
        s.last_write_at = formatNow()
        s.writes_total++
      } else {
        s.failures_total++
      }
      s.latency_ms = Math.round(row.latency_ms)
      s.error = row.message || ''
      s.runtime_state = row.success ? 'healthy' : 'failed'
      await queryClient.invalidateQueries({ queryKey: ['logs'] })
      return {
        ok: row.success,
        title: row.success ? 'Write test passed' : 'Write test failed',
        detail: row.message || 'Backend test completed',
        latency: s.latency_ms,
      }
    },
  },
})
