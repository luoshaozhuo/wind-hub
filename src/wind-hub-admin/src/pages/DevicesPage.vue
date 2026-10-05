<script setup lang="ts">
import * as echarts from 'echarts'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useEventListener, useIntervalFn } from '@vueuse/core'
import { useQuery } from '@tanstack/vue-query'
import { ElMessage, ElMessageBox } from 'element-plus'
import DeviceMetadataManager from '../components/DeviceMetadataManager.vue'
import ProtocolConnectionFields from '../components/devices/ProtocolConnectionFields.vue'
import { useViewport } from '../composables/useViewport'
import { protocolRead } from '../api/diagnostics'
import { fetchAllDeviceData, fetchDeviceTrend, sendDeviceCommand } from '../api/devices'
import { queryClient } from '../api/queryClient'
import { qk } from '../api/queryKeys'
import {
  amsNetIdFromHost,
  applyModelDefaults,
  connectionOverridesFromForm,
  defaultConnectionForm,
  formFromConnection,
  validateConnectionForm,
} from '../domain/deviceConnection'
import {
  deviceConnectionOverrides,
  effectiveConnection,
  emptyVerification,
  modelOf,
  tableOfDevice,
  unitSymbol,
} from '../domain/devices'
import { pointsOfTable } from '../domain/points'
import { refreshTaskValidity } from '../domain/tasks'
import { useConfigStore } from '../stores/config'
import type {
  DeviceInst,
  DeviceVerification,
  PointDef,
  Protocol,
  VerifyStepState,
} from '../domain/types'
import { baseAxisLabel, baseAxisLine, baseChartOption, baseSplitLine } from '../utils/chartTheme'
import { EMPTY, formatTimestamp } from '../utils/format'
import { statusTagType } from '../utils/status'

interface CommandOutcome {
  requested: number | boolean
  readback: string | number | boolean
  sentAt: string
  latency: number
  success: boolean
  error: string
  error_code: string
}

interface PointReadResult {
  state: 'success' | 'failed'
  timestamp: string
  latency_ms: number
  raw_data: string
  decoded_value: string
  engineering_value: string
  error_category: string
  error_code: string
  error_message: string
}

const configStore = useConfigStore()
const modelOfDevice = (d: DeviceInst) => modelOf(configStore, d)
const tableOfDeviceOf = (d: DeviceInst) => tableOfDevice(configStore, d)
const unitSymbolOf = (unit: string) => unitSymbol(configStore, unit)

interface DataRow {
  point_id: string
  variable_name: string
  description: string
  value: number | boolean | string
  unit: string
  groups: string[]
  updated_at: string
  data_type: string
  scale: number
  offset: number
  address: string
  updated: boolean
  read_state: 'success' | 'failed'
  error: string
  // 点在解析后点表中的下标：Trend/Command 与 Data 共用同一点值序列的定位键
  index: number
}

interface TrendSignal {
  id: string
  label: string
  unit: string
  pointIndex: number
}

const search = ref('')
const typeFilter = ref('All')
const modelFilter = ref('All')
const statusFilter = ref('All')
const drawer = ref(false)
const tab = ref('Config')
const selected = ref<DeviceInst | null>(null)
const deviceSnapshot = ref('')
const verifyAllRunning = ref(false)
const verifyingDeviceId = ref('')
const { isMobile } = useViewport()
const detailDrawerSize = computed(() => (isMobile.value ? '100%' : '72%'))

function verifyOf(d: DeviceInst): DeviceVerification {
  if (!configStore.deviceVerification[d.device_id])
    configStore.deviceVerification[d.device_id] = emptyVerification()
  return configStore.deviceVerification[d.device_id]
}

function typeName(typeId: string) {
  return configStore.deviceTypes.find((x) => x.id === typeId)?.name || typeId
}

function pointAddressText(p: PointDef) {
  const a = p.address
  if (a.symbol) return a.symbol
  if (a.index_group || a.index_offset)
    return `${a.index_group || EMPTY} / ${a.index_offset || EMPTY}`
  if (a.type || a.address !== undefined) return `${a.type || EMPTY} ${a.address ?? EMPTY}`
  if (a.ioa !== undefined) return `IOA ${a.ioa}`
  return EMPTY
}

const filteredDevices = computed(() =>
  configStore.devices.filter((d) => {
    const model = modelOf(configStore, d)
    const v = verifyOf(d)
    const q = search.value.trim().toLowerCase()
    const matchesSearch =
      !q ||
      [d.device_id, d.host, d.model, d.device_group, model?.protocol || ''].some((x) =>
        x.toLowerCase().includes(q),
      )

    const matchesType = typeFilter.value === 'All' || model?.device_type === typeFilter.value
    const matchesModel = modelFilter.value === 'All' || d.model === modelFilter.value
    const matchesStatus =
      statusFilter.value === 'All' ||
      (statusFilter.value === 'Healthy' && v.state === 'success') ||
      (statusFilter.value === 'Warning' && v.state === 'warning') ||
      (statusFilter.value === 'Fault' && v.state === 'failed') ||
      (statusFilter.value === 'Unverified' && v.state === 'idle')

    return matchesSearch && matchesType && matchesModel && matchesStatus
  }),
)

const devicePage = ref(1)
const devicePageSize = ref(24)
const pagedDevices = computed(() => {
  const start = (devicePage.value - 1) * devicePageSize.value
  return filteredDevices.value.slice(start, start + devicePageSize.value)
})
watch([search, typeFilter, modelFilter, statusFilter], () => {
  devicePage.value = 1
})

function groupKeyOf(d: DeviceInst) {
  const model = modelOf(configStore, d)
  return `${model?.device_type || 'unknown'}::${d.model}`
}

const groupedDevices = computed(() => {
  // 组统计基于 filteredDevices（过滤后的全量），分页只决定当前页显示哪些卡片，
  // 避免组头 healthy/warning/fault 计数被误读为全局统计。
  const stats = new Map<
    string,
    { total: number; healthy: number; warning: number; failed: number }
  >()
  for (const d of filteredDevices.value) {
    const key = groupKeyOf(d)
    if (!stats.has(key)) stats.set(key, { total: 0, healthy: 0, warning: 0, failed: 0 })
    const s = stats.get(key)!
    s.total += 1
    const state = verifyOf(d).state
    if (state === 'success') s.healthy += 1
    else if (state === 'warning') s.warning += 1
    else if (state === 'failed') s.failed += 1
  }

  const groups = new Map<string, DeviceInst[]>()
  for (const d of pagedDevices.value) {
    const key = groupKeyOf(d)
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(d)
  }

  return Array.from(groups.entries()).map(([key, devices]) => {
    const [typeId, modelId] = key.split('::')
    const model = configStore.deviceModels.find((m) => m.id === modelId)
    const s = stats.get(key) || { total: devices.length, healthy: 0, warning: 0, failed: 0 }
    return {
      key,
      typeId,
      modelId,
      model,
      devices,
      total: s.total,
      healthy: s.healthy,
      warning: s.warning,
      failed: s.failed,
    }
  })
})

function statusLabel(v: DeviceVerification) {
  if (v.state === 'running') return 'Verifying'
  if (v.state === 'success') return 'Healthy'
  if (v.state === 'warning') return 'Warning'
  if (v.state === 'failed') return 'Fault'
  return 'Unverified'
}

// DeviceVerifyState.running 表示“正在验证”（进行中），与 Task RUNNING（运行态）语义不同，
// 映射共享 helper 时显式翻译为 verifying，避免被当作运行态染成 success 绿。
function verifyTagType(v: DeviceVerification) {
  return statusTagType(v.state === 'running' ? 'verifying' : v.state)
}

function stepText(step: VerifyStepState) {
  if (step === 'checking') return 'Checking'
  if (step === 'success') return 'Passed'
  if (step === 'partial') return 'Partial'
  if (step === 'failed') return 'Failed'
  return 'Not tested'
}

function stepClass(step: VerifyStepState) {
  return `step-${step}`
}

function timestampAt(offsetMs = 0) {
  return formatTimestamp(new Date(Date.now() - offsetMs))
}

function protocolDescription(d: DeviceInst) {
  const model = modelOf(configStore, d)
  if (model?.protocol === 'ads') {
    const ams = `${d.host}.1.1`
    return `ADS · AMS ${ams} · Port 801`
  }
  if (model?.protocol === 'modbus') {
    return `Modbus TCP · ${d.host}:${d.port || 502}`
  }
  if (model?.protocol === 'iec104') {
    return `IEC 60870-5-104 · ${d.host}:${d.port || 2404}`
  }
  return model?.protocol || 'Unknown'
}

// Verification 操作互斥：同一时刻只允许一个 verification operation
// （Verify All 或某一台设备的 Verify），避免并发写同一 verification 状态。
const verifyOperationActive = computed(() => verifyAllRunning.value || !!verifyingDeviceId.value)

// 单台 Verify：流程推进与成败判定全部由后端 Diagnostics API 完成，
// 页面只负责互斥守卫与结果反馈。
async function verifyDevice(d: DeviceInst, quiet = false) {
  if (verifyOperationActive.value) return
  verifyingDeviceId.value = d.device_id
  const v = await configStore.verifyDevice(d)
  verifyingDeviceId.value = ''
  if (quiet) return
  if (v.state === 'success') ElMessage.success(`${d.device_id}: verification passed`)
  else if (v.state === 'warning')
    ElMessage.warning(`${d.device_id}: point verification partially failed`)
  else if (v.errors[0]?.stage === 'network')
    ElMessage.error(`${d.device_id}: network verification failed`)
  else if (v.errors[0]?.stage === 'protocol')
    ElMessage.error(`${d.device_id}: protocol verification failed`)
  else ElMessage.error(`${d.device_id}: verification failed`)
}

async function verifyAll() {
  if (verifyOperationActive.value) return

  const targets = [...filteredDevices.value]
  if (!targets.length) {
    ElMessage.warning('No devices match current filters')
    return
  }

  verifyAllRunning.value = true
  try {
    const { failed, warning } = await configStore.verifyAllDevices(targets)
    if (failed) ElMessage.error(`Verification complete: ${failed} failed, ${warning} warning`)
    else if (warning) ElMessage.warning(`Verification complete: ${warning} warning`)
    else ElMessage.success('Verification complete')
  } finally {
    verifyAllRunning.value = false
  }
}

// ---- Add Device ----
const addOpen = ref(false)
const addMode = ref<'single' | 'batch'>('single')
const batch = ref({
  model: 'beckhoff_wtg',
  group: 'turbine_ads',
  from: 1,
  to: 48,
  exclude: '',
  id_pattern: 'wtg-{num:03}',
  host_pattern: '192.168.151.{num}',
  netid_pattern: '192.168.151.{num}.1.1',
})
const batchModel = computed(() => configStore.deviceModels.find((m) => m.id === batch.value.model))

function renderBatchPattern(pattern: string, num: number): string {
  return pattern.replace(/\{num(?:\+(-?\d+))?(?::(\d+))?\}/g, (_all, delta, width) => {
    const value = num + Number(delta || 0)
    return width ? String(value).padStart(Number(width), '0') : String(value)
  })
}

function excludedBatchNumbers(): Set<number> {
  const result = new Set<number>()
  for (const token of batch.value.exclude
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean)) {
    const range = token.match(/^(\d+)\s*-\s*(\d+)$/)
    if (range) {
      const a = Number(range[1])
      const b = Number(range[2])
      for (let n = Math.min(a, b); n <= Math.max(a, b); n++) result.add(n)
    } else if (/^\d+$/.test(token)) result.add(Number(token))
  }
  return result
}

const batchPreview = computed(() => {
  const model = batchModel.value
  if (!model || batch.value.from > batch.value.to || batch.value.to - batch.value.from > 999)
    return []
  const excluded = excludedBatchNumbers()
  const generated: { num: number; id: string; host: string; netid: string; error: string }[] = []
  const seenIds = new Set<string>()
  const seenHosts = new Set<string>()
  const seenNetIds = new Set<string>()

  for (let num = batch.value.from; num <= batch.value.to; num++) {
    if (excluded.has(num)) continue
    const id = renderBatchPattern(batch.value.id_pattern, num)
    const host = renderBatchPattern(batch.value.host_pattern, num)
    const netid = model.protocol === 'ads' ? renderBatchPattern(batch.value.netid_pattern, num) : ''
    const errors: string[] = []
    if (!id || !host) errors.push('empty ID/host')
    if (configStore.devices.some((d) => d.device_id === id) || seenIds.has(id))
      errors.push('duplicate device ID')
    if (configStore.devices.some((d) => d.host === host) || seenHosts.has(host))
      errors.push('duplicate host')
    if (model.protocol === 'ads') {
      if (netid.split('.').length !== 6) errors.push('invalid AMS Net ID')
      if (
        configStore.devices.some((d) => d.extensions?.target_net_id === netid) ||
        seenNetIds.has(netid)
      )
        errors.push('duplicate AMS Net ID')
    }
    seenIds.add(id)
    seenHosts.add(host)
    if (netid) seenNetIds.add(netid)
    generated.push({ num, id, host, netid, error: errors.join('; ') })
  }
  return generated
})

function onBatchModelChange() {
  const model = batchModel.value
  if (!model) return
  if (
    !configStore.deviceGroups.some(
      (g) => g.id === batch.value.group && g.device_type === model.device_type,
    )
  ) {
    batch.value.group =
      configStore.deviceGroups.find((g) => g.device_type === model.device_type)?.id || ''
  }
}

function createBatchDevices() {
  const model = batchModel.value
  const rows = batchPreview.value
  if (!model || !rows.length) {
    ElMessage.error('No devices to create')
    return
  }
  if (rows.some((r) => r.error)) {
    ElMessage.error('Resolve all Batch Preview errors before creating devices')
    return
  }
  const group = configStore.deviceGroups.find((g) => g.id === batch.value.group)
  if (!group || group.device_type !== model.device_type) {
    ElMessage.error('Device Group must match the selected Model')
    return
  }

  configStore.mutate(() => {
    for (const row of rows) {
      const extensions: Record<string, unknown> = {}
      if (model.protocol === 'ads') extensions.target_net_id = row.netid
      configStore.devices.push({
        device_id: row.id,
        model: model.id,
        device_group: batch.value.group,
        host: row.host,
        port: undefined,
        extensions,
        enabled: true,
        online: false,
      })
      configStore.deviceVerification[row.id] = emptyVerification()
    }
  })
  addOpen.value = false
  ElMessage.success(rows.length + ' devices created from templates ')
}

const newDev = ref({
  id: '',
  type: 'turbine',
  group: 'turbine_modbus',
  model: 'modbus_wtg',
  host: '',
  enabled: true,
  connection: defaultConnectionForm('modbus'),
})

const newDevModel = computed(() =>
  configStore.deviceModels.find((m) => m.id === newDev.value.model),
)

function applyNewModelDefaults() {
  const model = newDevModel.value
  if (!model) return

  newDev.value.type = model.device_type
  applyModelDefaults(newDev.value.connection, model, newDev.value.host)

  const validGroup = configStore.deviceGroups.some(
    (g) => g.id === newDev.value.group && g.device_type === model.device_type,
  )
  if (!validGroup) {
    newDev.value.group =
      configStore.deviceGroups.find((g) => g.device_type === model.device_type)?.id || ''
  }
}

function onNewType() {
  const model = configStore.deviceModels.find((m) => m.device_type === newDev.value.type)
  if (model) {
    newDev.value.model = model.id
    applyNewModelDefaults()
  } else {
    newDev.value.model = ''
    newDev.value.group =
      configStore.deviceGroups.find((g) => g.device_type === newDev.value.type)?.id || ''
  }
}

function onNewModel() {
  applyNewModelDefaults()
}

function onNewHostChange() {
  if (newDevModel.value?.protocol === 'ads' && newDev.value.host.trim()) {
    newDev.value.connection.target_net_id = amsNetIdFromHost(newDev.value.host)
  }
}

function openAdd() {
  addMode.value = 'single'
  newDev.value = {
    id: '',
    type: 'turbine',
    group: 'turbine_modbus',
    model: 'modbus_wtg',
    host: '',
    enabled: true,
    connection: defaultConnectionForm('modbus'),
  }
  applyNewModelDefaults()
  addOpen.value = true
}

function addDevice() {
  const id = newDev.value.id.trim()
  const host = newDev.value.host.trim()
  const model = newDevModel.value

  if (!id || !host) {
    ElMessage.error('Device ID and Host are required')
    return
  }
  if (configStore.devices.some((d) => d.device_id === id)) {
    ElMessage.error(`Device ${id} already exists`)
    return
  }
  if (!model) {
    ElMessage.error('Select a valid model')
    return
  }
  if (
    !configStore.deviceGroups.some(
      (g) => g.id === newDev.value.group && g.device_type === model.device_type,
    )
  ) {
    ElMessage.error('Select a valid device group')
    return
  }

  const connError = validateConnectionForm(newDev.value.connection, model.protocol)
  if (connError) {
    ElMessage.error(connError)
    return
  }
  const { extensions, port } = connectionOverridesFromForm(newDev.value.connection, model)

  configStore.mutate(() => {
    configStore.devices.push({
      device_id: id,
      model: model.id,
      device_group: newDev.value.group,
      host,
      port,
      extensions,
      enabled: newDev.value.enabled,
      online: false,
    })
    configStore.deviceVerification[id] = emptyVerification()
  })
  addOpen.value = false
  ElMessage.success('Device created ')
}

// ---- Drawer / Config ----
const editForm = ref({
  device_id: '',
  device_type: '',
  model: '',
  manufacturer: '',
  hardware_model: '',
  protocol: 'modbus' as Protocol,
  point_table: '',
  read_mode: '',
  device_group: '',
  host: '',
  connection: defaultConnectionForm('modbus'),
})
const deviceFormState = computed(() => JSON.stringify(editForm.value))
const deviceDirty = computed(
  () => !!selected.value && deviceFormState.value !== deviceSnapshot.value,
)

function openDev(d: DeviceInst) {
  selected.value = d
  drawer.value = true
  tab.value = 'Config'
  loadEditForm()
}

async function beforeDeviceClose(done: () => void) {
  if (!deviceDirty.value) {
    done()
    return
  }
  try {
    await ElMessageBox.confirm('Discard unsaved Device changes?', 'Unsaved Changes', {
      type: 'warning',
      confirmButtonText: 'Discard',
    })
    done()
  } catch {
    /* keep drawer open */
  }
}

async function closeDeviceDrawer() {
  if (!deviceDirty.value) {
    drawer.value = false
    return
  }
  try {
    await ElMessageBox.confirm('Discard unsaved Device changes?', 'Unsaved Changes', {
      type: 'warning',
      confirmButtonText: 'Discard',
    })
    drawer.value = false
  } catch {
    /* keep drawer open */
  }
}

function loadEditForm() {
  if (!selected.value) return
  const model = modelOf(configStore, selected.value)
  const protocol = model?.protocol || 'modbus'
  editForm.value = {
    device_id: selected.value.device_id,
    device_type: model?.device_type || '',
    model: selected.value.model,
    manufacturer: model?.manufacturer || '',
    hardware_model: model?.model || '',
    protocol,
    point_table: model?.point_table || '',
    read_mode: model?.read_mode || '',
    device_group: selected.value.device_group,
    host: selected.value.host,
    connection: formFromConnection(protocol, mergedConnection(selected.value)),
  }
  deviceSnapshot.value = JSON.stringify(editForm.value)
}

function onEditModelChange() {
  const model = editSelectedModel.value
  if (!model) return

  editForm.value.device_type = model.device_type
  editForm.value.manufacturer = model.manufacturer || ''
  editForm.value.hardware_model = model.model || ''
  editForm.value.protocol = model.protocol
  editForm.value.point_table = model.point_table
  editForm.value.read_mode = model.read_mode || ''

  const validGroup = configStore.deviceGroups.some(
    (g) => g.id === editForm.value.device_group && g.device_type === model.device_type,
  )
  if (!validGroup) {
    editForm.value.device_group =
      configStore.deviceGroups.find((g) => g.device_type === model.device_type)?.id || ''
  }

  applyModelDefaults(editForm.value.connection, model, editForm.value.host)
}

function affectedTasksForDevice(d: DeviceInst, nextGroup = d.device_group) {
  const groups = new Set([d.device_group, nextGroup].filter(Boolean))
  return configStore.tasks.filter(
    (t) => (t.device && t.device === d.device_id) || (t.device_group && groups.has(t.device_group)),
  )
}

async function changeDeviceEnabled(d: DeviceInst, enabled: boolean) {
  const affected = affectedTasksForDevice(d)
  const running = affected.filter((t) => t.runtime === 'RUNNING')
  if (!enabled && running.length) {
    try {
      await ElMessageBox.confirm(
        'Disabling "' +
          d.device_id +
          '" affects ' +
          running.length +
          ' running Task definition(s). The device instances will be stopped; other group members remain unaffected.',
        'Disable Device',
        { type: 'warning', confirmButtonText: 'Disable' },
      )
    } catch {
      return
    }
  }
  configStore.mutate(() => {
    d.enabled = enabled
    if (!enabled) {
      for (const t of running) {
        if (t.device === d.device_id) t.runtime = 'STOPPED'
      }
    }
  })
}

async function saveConfig() {
  if (!selected.value) return

  const device = selected.value
  const oldDeviceId = device.device_id
  const newDeviceId = editForm.value.device_id.trim()
  if (!newDeviceId) {
    ElMessage.error('Device ID is required')
    return
  }
  if (newDeviceId !== oldDeviceId) {
    ElMessage.warning('Device ID is stable after creation')
    return
  }

  const model = editSelectedModel.value
  if (!model) {
    ElMessage.error('Select a valid model')
    return
  }
  const group = configStore.deviceGroups.find((g) => g.id === editForm.value.device_group)
  if (!group || group.device_type !== model.device_type) {
    ElMessage.error('Device Group must match the selected Model device type')
    return
  }
  if (!editForm.value.host.trim()) {
    ElMessage.error('Host is required')
    return
  }

  const { extensions, port } = connectionOverridesFromForm(editForm.value.connection, model)

  const modelChanged = device.model !== model.id
  const groupChanged = device.device_group !== editForm.value.device_group
  const endpointChanged =
    device.host !== editForm.value.host.trim() ||
    (device.port || undefined) !== port ||
    JSON.stringify(device.extensions || {}) !== JSON.stringify(extensions)

  const affectedTasks = affectedTasksForDevice(device, editForm.value.device_group)
  const running = affectedTasks.filter((t) => t.runtime === 'RUNNING')
  if ((modelChanged || groupChanged || endpointChanged) && affectedTasks.length) {
    try {
      await ElMessageBox.confirm(
        '<b>Device Configuration Change Impact</b><br><br>' +
          affectedTasks.length +
          ' Task definition(s) affected; ' +
          running.length +
          ' currently running.<br>' +
          (modelChanged ? 'Device Model / Point Table binding will change.<br>' : '') +
          (groupChanged ? 'Device Group task membership will be recalculated.<br>' : '') +
          (endpointChanged ? 'The device connection will be rebuilt.<br>' : '') +
          '<br>Only affected acquisition instances will be stopped and restored if still valid.',
        'Apply Device Changes',
        { type: 'warning', confirmButtonText: 'Apply Changes', dangerouslyUseHTMLString: true },
      )
    } catch {
      return
    }
  }

  const runningIds = new Set(running.map((t) => t.task_id))
  configStore.mutate(() => {
    for (const t of running) t.runtime = 'STOPPED'

    Object.assign(device, {
      device_group: editForm.value.device_group,
      model: model.id,
      host: editForm.value.host.trim(),
      port,
      extensions,
    })

    refreshTaskValidity(configStore)
    for (const t of affectedTasks) {
      if (runningIds.has(t.task_id) && t.valid !== false && t.enabled) t.runtime = 'RUNNING'
    }
  })

  loadEditForm()
  ElMessage.success('Device config updated ')
}

async function del() {
  if (!selected.value) return
  const target = selected.value
  const id = target.device_id
  const directTasks = configStore.tasks.filter((t) => t.device === id)
  const groupTasks = configStore.tasks.filter((t) => t.device_group === target.device_group)
  const running = affectedTasksForDevice(target).filter((t) => t.runtime === 'RUNNING')

  try {
    await ElMessageBox.confirm(
      '<b>Delete Device "' +
        id +
        '"?</b><br><br>' +
        running.length +
        ' running Task definition(s) have instances affected.<br>' +
        directTasks.length +
        ' direct Task definition(s) will be kept and become INVALID.<br>' +
        groupTasks.length +
        ' group Task definition(s) will remain and continue with their other devices.<br><br>' +
        'No Task definition will be deleted automatically.',
      'Delete Device',
      { type: 'warning', confirmButtonText: 'Delete', dangerouslyUseHTMLString: true },
    )
  } catch {
    return
  }

  configStore.mutate(() => {
    for (const t of directTasks) t.runtime = 'STOPPED'
    configStore.devices = configStore.devices.filter((x) => x !== target)
    delete configStore.deviceVerification[id]
  })
  drawer.value = false
  ElMessage.success('Device deleted; referenced task definitions were preserved ')
}

const deleteAllOpen = ref(false)
const deleteAllConfirm = ref('')

function openDeleteAll() {
  if (!configStore.devices.length) {
    ElMessage.info('No devices to delete')
    return
  }
  deleteAllConfirm.value = ''
  deleteAllOpen.value = true
}

function deleteAllDevices() {
  if (deleteAllConfirm.value !== 'DELETE ALL') return
  const count = configStore.devices.length
  configStore.mutate(() => {
    for (const t of configStore.tasks) t.runtime = 'STOPPED'
    configStore.devices = []
    for (const key of Object.keys(configStore.deviceVerification))
      delete configStore.deviceVerification[key]
  })
  selected.value = null
  drawer.value = false
  deleteAllOpen.value = false
  ElMessage.success(count + ' devices deleted; Task Definitions preserved and revalidated ')
}

const selectedModel = computed(() =>
  selected.value ? modelOf(configStore, selected.value) : undefined,
)
const editSelectedModel = computed(() =>
  configStore.deviceModels.find((m) => m.id === editForm.value.model),
)

function mergedConnection(d: DeviceInst) {
  return effectiveConnection(configStore, d)
}

function overrideKeys(d: DeviceInst): string[] {
  return [
    ...(d.port !== undefined ? ['port'] : []),
    ...Object.keys(deviceConnectionOverrides(configStore, d)).filter(
      (key) => key !== 'target_net_id',
    ),
  ]
}

function connValue(d: DeviceInst, key: string, fallback: unknown = '') {
  const value = mergedConnection(d)[key]
  return value === undefined || value === null || value === '' ? fallback : value
}

const selectedVerify = computed(() =>
  selected.value ? verifyOf(selected.value) : emptyVerification(),
)

// ---- Read Test / Data ----
const dataSearch = ref('')
const dataGroup = ref('All')
const dataRefreshInterval = ref(1000)
const dataAutoRefresh = ref(false)
const dataLastRefreshAt = ref('')

const resolvedPoints = computed(() =>
  selected.value ? pointsOfTable(configStore, tableOfDevice(configStore, selected.value)) : [],
)

// Data 当前值来自后端 LatestPointStore（Vue Query 托管）：
// 与 Trend 尾点、Diagnostics Read、Read Test 同源；Command 成功后
// invalidate deviceData 查询触发联动刷新（§6）。
const deviceDataQuery = useQuery({
  queryKey: computed(() => qk.deviceData(selected.value?.device_id || '')),
  queryFn: () => fetchAllDeviceData(selected.value!.device_id),
  enabled: computed(() => !!selected.value && drawer.value && tab.value === 'Data'),
  refetchInterval: computed(() => (dataAutoRefresh.value ? dataRefreshInterval.value : false)),
})
const dataRefreshing = computed(() => deviceDataQuery.isFetching.value)
watch(
  () => deviceDataQuery.dataUpdatedAt.value,
  (ts) => {
    if (ts) dataLastRefreshAt.value = timestampAt()
  },
)

interface PointValue {
  value: number | boolean | string
  read_state: 'success' | 'failed'
  error: string
}
const latestPointValues = computed(() => {
  const map = new Map<string, { value: number | boolean | string; quality: string | null }>()
  for (const row of deviceDataQuery.data.value || []) {
    map.set(row.point_id, {
      value: (row.value ?? EMPTY) as number | boolean | string,
      quality: row.quality ?? null,
    })
  }
  return map
})
function readPointValue(pointId: string): PointValue {
  const cached = latestPointValues.value.get(pointId)
  if (!cached) return { value: EMPTY, read_state: 'failed', error: 'No sample' }
  if (cached.quality === 'bad')
    return { value: cached.value, read_state: 'failed', error: 'BAD quality' }
  return { value: cached.value, read_state: 'success', error: '' }
}

const dataRows = computed<DataRow[]>(() => {
  const d = selected.value
  if (!d) return []
  return resolvedPoints.value.map((p, i) => {
    const pv = readPointValue(p.point_id)
    const failed = pv.read_state === 'failed'
    return {
      point_id: p.point_id,
      variable_name: p.variable_name,
      description: p.description,
      value: failed ? EMPTY : pv.value,
      unit: unitSymbol(configStore, p.unit),
      groups: p.point_groups,
      updated_at: failed ? EMPTY : timestampAt((i % 7) * 120),
      data_type: p.data_type,
      scale: p.scale,
      offset: p.offset,
      address: pointAddressText(p),
      updated: !failed,
      read_state: pv.read_state,
      error: pv.error,
      index: i,
    }
  })
})

const visibleData = computed(() =>
  dataRows.value.filter((r) => {
    const q = dataSearch.value.trim().toLowerCase()
    return (
      (dataGroup.value === 'All' || r.groups.includes(dataGroup.value)) &&
      (!q || [r.point_id, r.variable_name, r.description].some((x) => x.toLowerCase().includes(q)))
    )
  }),
)
const dataSuccessCount = computed(
  () => dataRows.value.filter((r) => r.read_state === 'success').length,
)
const dataFailureCount = computed(() => dataRows.value.length - dataSuccessCount.value)

// 大点表下只渲染当前页，避免 Auto Refresh 高频重建数百个卡片 DOM。
const dataPage = ref(1)
const dataPageSize = ref(50)
const pagedData = computed(() => {
  const start = (dataPage.value - 1) * dataPageSize.value
  return visibleData.value.slice(start, start + dataPageSize.value)
})
watch([dataSearch, dataGroup], () => {
  dataPage.value = 1
})
watch(visibleData, (rows) => {
  const maxPage = Math.max(1, Math.ceil(rows.length / dataPageSize.value))
  if (dataPage.value > maxPage) dataPage.value = maxPage
})

async function refreshData() {
  if (!selected.value || dataRefreshing.value) return
  await deviceDataQuery.refetch()
}

// ---- Read Test ----
const readPointId = ref('')
const readLoading = ref(false)
const readResult = ref<PointReadResult | null>(null)
const readPoint = computed(() => resolvedPoints.value.find((p) => p.point_id === readPointId.value))

function resetReadTest() {
  readResult.value = null
}
watch(
  resolvedPoints,
  (rows) => {
    if (!rows.some((p) => p.point_id === readPointId.value))
      readPointId.value = rows[0]?.point_id || ''
  },
  { immediate: true },
)
watch(readPointId, resetReadTest)

// 单点诊断读：成败与错误码由 backend service 按场景注册表决定（§5）
async function readSelectedPoint() {
  if (!selected.value || !readPoint.value || readLoading.value) return
  readLoading.value = true
  readResult.value = null
  const started = performance.now()
  try {
    const row = await protocolRead(selected.value.device_id, readPoint.value.point_id)
    const value = String(row.value ?? EMPTY)
    readResult.value = {
      state: row.quality === 'bad' ? 'failed' : 'success',
      timestamp: row.timestamp,
      latency_ms: Math.round(performance.now() - started),
      raw_data: 'Not exposed by protocol adapter',
      decoded_value: value,
      engineering_value: value,
      error_category: row.quality === 'bad' ? 'quality' : '',
      error_code: row.quality === 'bad' ? 'BAD_QUALITY' : '',
      error_message: '',
    }
  } catch (error) {
    readResult.value = {
      state: 'failed',
      timestamp: new Date().toISOString(),
      latency_ms: Math.round(performance.now() - started),
      raw_data: EMPTY,
      decoded_value: EMPTY,
      engineering_value: EMPTY,
      error_category: 'protocol',
      error_code: 'READ_FAILED',
      error_message: error instanceof Error ? error.message : String(error),
    }
  } finally {
    readLoading.value = false
  }
}

// ---- Trend ----
const trendChartEl = ref<HTMLElement | null>(null)
let trendChart: echarts.ECharts | null = null
let trendResizeObserver: ResizeObserver | null = null
// Trend 为按需拉取的图表数据：页面级缓存，随 fetchDeviceTrend 结果覆盖。
const trendSeriesCache = new Map<string, Array<[Date, number]>>()
const trendLegendSelected = ref<Record<string, boolean>>({})
const trendPickerOpen = ref(false)
const trendSearch = ref('')
const trendRange = ref('1 min')
const trendAutoRefresh = ref(false)
const trendLastRefreshAt = ref('')
const trendSignals = ref<TrendSignal[]>([])
const trendRecording = ref(false)

function trendRangeMs() {
  if (trendRange.value === '5 min') return 5 * 60_000
  if (trendRange.value === '15 min') return 15 * 60_000
  if (trendRange.value === '1 h') return 60 * 60_000
  return 60_000
}

// Trend 序列与 Data 当前值同源（§7）：尾点即 pointValueAt(当前时刻)，
// Command 写回后趋势在同一序列上追加新值。
function makeTrendRawData(signal: TrendSignal): Array<[Date, number]> {
  if (!selected.value) return []
  return trendSeriesCache.get(signal.id) || []
}

function downsampleForChart(raw: [Date, number][], maxPoints = 240) {
  if (raw.length <= maxPoints) return raw
  const step = Math.ceil(raw.length / maxPoints)
  return raw.filter((_, i) => i % step === 0 || i === raw.length - 1)
}

function csvCell(value: string | number) {
  const text = String(value)
  return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text
}

async function recordTrendRawData() {
  if (!selected.value || !trendSignals.value.length) {
    ElMessage.warning('Select at least one trend signal first')
    return
  }
  trendRecording.value = true
  try {
    const rows: string[] = ['timestamp,device_id,point_id,variable_name,value,unit']
    trendSignals.value.forEach((signal) => {
      for (const [ts, value] of makeTrendRawData(signal)) {
        rows.push(
          [ts.toISOString(), selected.value!.device_id, signal.id, signal.label, value, signal.unit]
            .map(csvCell)
            .join(','),
        )
      }
    })
    const blob = new Blob([rows.join('\n')], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${selected.value.device_id}_waveform_${trendRange.value.replace(/\s+/g, '_')}.csv`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('Raw waveform data exported ')
  } finally {
    trendRecording.value = false
  }
}

const trendCandidates = computed(() =>
  dataRows.value.filter((r) => {
    const q = trendSearch.value.trim().toLowerCase()
    return (
      !q || [r.point_id, r.variable_name, r.description].some((x) => x.toLowerCase().includes(q))
    )
  }),
)

function toggleTrendSelection(r: DataRow) {
  const existing = trendSignals.value.find((x) => x.id === r.point_id)
  if (existing) {
    trendSignals.value = trendSignals.value.filter((x) => x.id !== r.point_id)
    delete trendLegendSelected.value[existing.label]
  } else {
    const signal = {
      id: r.point_id,
      label: r.variable_name || r.point_id,
      unit: r.unit,
      pointIndex: r.index,
    }
    trendSignals.value.push(signal)
    trendLegendSelected.value[signal.label] = true
  }
  void renderTrend()
}

function seedTrendSignals() {
  if (trendSignals.value.length || !dataRows.value.length) return
  trendSignals.value = dataRows.value.slice(0, 4).map((r) => ({
    id: r.point_id,
    label: r.variable_name || r.point_id,
    unit: r.unit,
    pointIndex: r.index,
  }))
  for (const s of trendSignals.value) trendLegendSelected.value[s.label] = true
}

function makeTrendData(signal: TrendSignal) {
  return downsampleForChart(makeTrendRawData(signal))
}

async function renderTrend() {
  if (selected.value && trendSignals.value.length) {
    const rows = await fetchDeviceTrend(
      selected.value.device_id,
      trendSignals.value.map((signal) => signal.id),
      Math.max(1, Math.ceil(trendRangeMs() / 1000)),
    )
    for (const row of rows) {
      trendSeriesCache.set(
        row.point_id,
        row.samples
          .filter((sample) => typeof sample.value === 'number')
          .map((sample) => [new Date(sample.timestamp), Number(sample.value)]),
      )
    }
  }
  trendLastRefreshAt.value = timestampAt()
  nextTick(() => {
    if (!trendChartEl.value) return
    if (!trendChart) {
      trendChart = echarts.init(trendChartEl.value)
      trendResizeObserver = new ResizeObserver(() => trendChart?.resize())
      trendResizeObserver.observe(trendChartEl.value)
      trendChart.on('legendselectchanged', (params: any) => {
        trendLegendSelected.value = { ...(params.selected || {}) }
      })
    }

    const series = trendSignals.value.map((s) => ({
      name: s.label,
      type: 'line',
      showSymbol: false,
      smooth: true,
      data: makeTrendData(s),
    }))

    trendChart.setOption(
      {
        ...baseChartOption(),
        legend: {
          type: 'scroll',
          top: 10,
          left: 18,
          right: 18,
          selected: trendLegendSelected.value,
        },
        grid: { top: 58, left: 56, right: 24, bottom: 42 },
        xAxis: {
          type: 'time',
          boundaryGap: false,
          axisLabel: baseAxisLabel(),
          axisLine: baseAxisLine(),
        },
        yAxis: {
          type: 'value',
          scale: true,
          axisLabel: baseAxisLabel(),
          splitLine: baseSplitLine(),
        },
        series,
      },
      true,
    )
    trendChart.resize()
  })
}

const trendRefreshTimer = useIntervalFn(() => void renderTrend(), 1000, { immediate: false })
function syncTrendRefreshTimer() {
  if (drawer.value && tab.value === 'ControlTrend' && trendAutoRefresh.value) {
    trendRefreshTimer.resume()
  } else {
    trendRefreshTimer.pause()
  }
}
function onResize() {
  trendChart?.resize()
}

watch(tab, (value) => {
  if (value === 'Data') void refreshData()
  if (value === 'ControlTrend') {
    seedTrendSignals()
    void renderTrend()
  }
  syncTrendRefreshTimer()
})
watch([trendRange, trendAutoRefresh], () => {
  if (tab.value === 'ControlTrend') void renderTrend()
  syncTrendRefreshTimer()
})
watch(
  () => selected.value?.device_id,
  () => {
    trendSignals.value = []
    trendLegendSelected.value = {}
    trendSeriesCache.clear()
    readResult.value = null
    dataLastRefreshAt.value = ''
    dataPage.value = 1
    commandResult.value = null
    seedTrendSignals()
    if (tab.value === 'ControlTrend') void renderTrend()
  },
)

function onDeviceDrawerClosed() {
  trendRefreshTimer.pause()
}

useEventListener(window, 'resize', onResize)
onBeforeUnmount(() => {
  trendRefreshTimer.pause()
  trendResizeObserver?.disconnect()
  trendResizeObserver = null
  trendChart?.dispose()
  trendChart = null
})

// ---- Control ----
const cmdPoint = ref('')
const cmdValue = ref(0)
const cmdBool = ref(false)
const sending = ref(false)
const commandResult = ref<CommandOutcome | null>(null)

const controlCandidates = computed(() => dataRows.value.filter((r) => r.groups.includes('control')))
const currentControlRow = computed(() =>
  controlCandidates.value.find((r) => r.point_id === cmdPoint.value),
)
const cmdIsBool = computed(() => currentControlRow.value?.data_type === 'bool')

function formatCommandValue(v: string | number | boolean) {
  return typeof v === 'boolean' ? (v ? 'true' : 'false') : v
}

watch(
  controlCandidates,
  (rows) => {
    if (!cmdPoint.value || !rows.some((r) => r.point_id === cmdPoint.value)) {
      cmdPoint.value = rows[0]?.point_id || ''
      cmdValue.value = Number(rows[0]?.value) || 0
      cmdBool.value = rows[0]?.value === true
    }
  },
  { immediate: true },
)

watch(cmdPoint, (id) => {
  const row = controlCandidates.value.find((r) => r.point_id === id)
  if (row) {
    cmdValue.value = Number(row.value) || 0
    cmdBool.value = row.value === true
  }
  commandResult.value = null
})

async function sendCommand() {
  if (sending.value) return
  if (!selected.value || !currentControlRow.value) {
    ElMessage.warning('Select a command point first')
    return
  }
  const device = selected.value
  const row = currentControlRow.value
  const target: number | boolean = cmdIsBool.value ? cmdBool.value : Number(cmdValue.value)
  const targetText = `${formatCommandValue(target)}${row.unit ? ' ' + row.unit : ''}`

  // 设备写操作必须显式确认：设备、点、当前值、目标值全部展示后再执行。
  try {
    await ElMessageBox.confirm(
      `<b>Confirm device write</b><br><br>` +
        `Device: <b>${device.device_id}</b><br>` +
        `Point: <b>${row.point_id}</b>${row.variable_name ? ` · ${row.variable_name}` : ''}<br>` +
        `Current Value: <b>${formatCommandValue(row.value)}${row.unit ? ' ' + row.unit : ''}</b><br>` +
        `Target Value: <b>${targetText}</b>`,
      'Send Command',
      {
        type: 'warning',
        confirmButtonText: 'Send',
        cancelButtonText: 'Cancel',
        dangerouslyUseHTMLString: true,
      },
    )
  } catch {
    return
  }

  sending.value = true
  commandResult.value = null
  try {
    // 成败判定、readback、Data/Trend 联动与日志全部在 backend service（§6）
    const pointIndex = resolvedPoints.value.findIndex((p) => p.point_id === row.point_id)
    const pointDef = pointIndex >= 0 ? resolvedPoints.value[pointIndex] : undefined
    if (!pointDef) {
      ElMessage.error('Command point no longer exists in the resolved point table')
      return
    }
    const outcome = await sendDeviceCommand(device.device_id, pointDef.point_id, target)
    commandResult.value = {
      requested: target,
      readback: (outcome.readback ?? EMPTY) as string | number | boolean,
      sentAt: outcome.sent_at,
      latency: Math.round(outcome.latency_ms),
      success: outcome.success,
      error: outcome.error || outcome.readback_error || '',
      error_code: outcome.success ? '' : 'COMMAND_FAILED',
    }
    // 成败判定、readback、Data/Trend 联动与日志全部在 backend service（§6）；
    // 写操作后无论成败都刷新即时值与日志（与旧 sendDeviceCommand 一致）。
    await queryClient.invalidateQueries({ queryKey: qk.deviceData(device.device_id) })
    await queryClient.invalidateQueries({ queryKey: ['logs'] })
    if (commandResult.value.success) {
      ElMessage.success('Command completed ')
      if (tab.value === 'ControlTrend') void renderTrend()
    } else {
      ElMessage.error('Command failed ')
    }
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <div class="devices-page">
    <div class="head page-head">
      <div>
        <h1>Devices</h1>
        <p>设备资产、通信状态、诊断、实时数据与控制</p>
      </div>
      <div class="head-actions">
        <el-button type="primary" @click="openAdd">+ Add Device</el-button>
        <el-dropdown trigger="click">
          <el-button :loading="verifyAllRunning">Actions</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item
                :disabled="verifyOperationActive || !filteredDevices.length"
                @click="verifyAll"
              >
                Verify All
              </el-dropdown-item>
              <DeviceMetadataManager dropdown-item />
              <el-dropdown-item
                divided
                :disabled="!configStore.devices.length"
                @click="openDeleteAll"
              >
                Delete All Devices
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <el-card shadow="never" class="filter-card">
      <div class="filter-grid">
        <el-input
          v-model="search"
          clearable
          placeholder="Search device ID / IP / model / protocol..."
        />
        <el-select v-model="typeFilter" class="device-filter">
          <el-option label="All Types" value="All" />
          <el-option
            v-for="t in configStore.deviceTypes"
            :key="t.id"
            :label="t.name"
            :value="t.id"
          />
        </el-select>
        <el-select v-model="modelFilter" class="device-filter">
          <el-option label="All Models" value="All" />
          <el-option
            v-for="m in configStore.deviceModels"
            :key="m.id"
            :label="m.id"
            :value="m.id"
          />
        </el-select>
        <el-select v-model="statusFilter" class="device-filter">
          <el-option label="All Status" value="All" />
          <el-option label="Healthy" value="Healthy" />
          <el-option label="Warning" value="Warning" />
          <el-option label="Fault" value="Fault" />
          <el-option label="Unverified" value="Unverified" />
        </el-select>
      </div>
    </el-card>

    <el-empty
      v-if="!groupedDevices.length"
      description="No devices match current filters."
      :image-size="72"
    />

    <section v-for="group in groupedDevices" :key="group.key" class="model-group">
      <div class="group-head">
        <div>
          <div class="group-title">
            <h2>{{ typeName(group.typeId) }}</h2>
            <span>/</span>
            <b>{{ group.modelId }}</b>
          </div>
          <div class="group-meta">
            <span>{{ group.model?.protocol?.toUpperCase() }}</span>
            <span>Point Table: {{ group.model?.point_table }}</span>
            <span>{{ group.total }} devices</span>
          </div>
        </div>
        <div class="group-summary">
          <span class="ok">{{ group.healthy }} healthy</span>
          <span class="warn">{{ group.warning }} warning</span>
          <span class="bad">{{ group.failed }} fault</span>
        </div>
      </div>

      <div class="device-grid">
        <el-card
          v-for="d in group.devices"
          :key="d.device_id"
          shadow="never"
          class="device-card device-card-compact"
          :class="{ 'device-disabled': !d.enabled }"
        >
          <div class="device-card-top">
            <el-button link class="device-id-link" @click="openDev(d)">{{ d.device_id }}</el-button>
            <div class="device-enabled">
              <span>Enabled</span>
              <el-switch
                :model-value="d.enabled"
                size="small"
                @change="changeDeviceEnabled(d, !!$event)"
              />
            </div>
          </div>

          <div class="device-fields">
            <div>
              <span>Protocol</span>
              <b>{{ modelOfDevice(d)?.protocol?.toUpperCase() || EMPTY }}</b>
            </div>
            <div>
              <span>IP</span>
              <b>{{ d.host }}</b>
            </div>
            <div>
              <span>Port</span>
              <b>{{ d.port || modelOfDevice(d)?.connection_defaults?.port || EMPTY }}</b>
            </div>
          </div>

          <div class="status-pills">
            <el-tag
              size="small"
              :type="statusTagType(verifyOf(d).network)"
              :effect="verifyOf(d).network === 'checking' ? 'dark' : 'light'"
              >Network</el-tag
            >
            <el-tag
              size="small"
              :type="statusTagType(verifyOf(d).protocol)"
              :effect="verifyOf(d).protocol === 'checking' ? 'dark' : 'light'"
              >Protocol</el-tag
            >
            <el-tag
              size="small"
              :type="statusTagType(verifyOf(d).points)"
              :effect="verifyOf(d).points === 'checking' ? 'dark' : 'light'"
              >Points</el-tag
            >
          </div>
        </el-card>
      </div>
    </section>

    <div v-if="filteredDevices.length > devicePageSize" class="pagination">
      <el-pagination
        v-model:current-page="devicePage"
        v-model:page-size="devicePageSize"
        :page-sizes="[12, 24, 48]"
        :total="filteredDevices.length"
        :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"
      />
    </div>

    <!-- Add Device -->
    <el-drawer
      v-model="addOpen"
      title="Add Device"
      direction="rtl"
      :size="isMobile ? '100%' : addMode === 'batch' ? 'min(980px, 86vw)' : 'min(760px, 78vw)'"
      append-to-body
      destroy-on-close
    >
      <el-tabs v-model="addMode">
        <el-tab-pane label="Single" name="single">
          <el-alert
            type="info"
            :closable="false"
            title="Model connection defaults are inherited. Only values that differ from the Model are stored as Device overrides."
          />
          <el-form label-position="top" class="add-device-form">
            <div class="form-grid add-device-grid">
              <el-form-item label="Device ID"
                ><el-input v-model="newDev.id" placeholder="wtg-001"
              /></el-form-item>
              <el-form-item label="Type"
                ><el-select v-model="newDev.type" @change="onNewType" class="app-full-width"
                  ><el-option
                    v-for="t in configStore.deviceTypes"
                    :key="t.id"
                    :label="t.name"
                    :value="t.id" /></el-select
              ></el-form-item>
              <el-form-item label="Model"
                ><el-select v-model="newDev.model" @change="onNewModel" class="app-full-width"
                  ><el-option
                    v-for="m in configStore.deviceModels.filter(
                      (m) => m.device_type === newDev.type,
                    )"
                    :key="m.id"
                    :label="m.id"
                    :value="m.id" /></el-select
              ></el-form-item>
              <el-form-item label="Group"
                ><el-select v-model="newDev.group" class="app-full-width"
                  ><el-option
                    v-for="g in configStore.deviceGroups.filter(
                      (g) => g.device_type === newDev.type,
                    )"
                    :key="g.id"
                    :label="g.id"
                    :value="g.id" /></el-select
              ></el-form-item>
              <el-form-item label="Protocol"
                ><el-input :model-value="newDevModel?.protocol?.toUpperCase() || ''" disabled
              /></el-form-item>
              <el-form-item label="Host / Remote IP"
                ><el-input
                  v-model="newDev.host"
                  placeholder="192.168.151.1"
                  @change="onNewHostChange"
              /></el-form-item>
              <el-form-item label="Port"
                ><el-input-number
                  v-model="newDev.connection.port"
                  :min="1"
                  :max="65535"
                  class="app-full-width"
              /></el-form-item>
              <el-form-item label="Enabled"><el-switch v-model="newDev.enabled" /></el-form-item>
            </div>

            <div v-if="newDevModel" class="form-grid add-device-grid">
              <ProtocolConnectionFields
                v-model="newDev.connection"
                :protocol="newDevModel.protocol"
                show-target-net-id
              />
            </div>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="Batch" name="batch">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="Templates: {num}, {num:03}, {num+100}, {num+100:03}. Exclude examples: 5,17,30-32."
          />
          <el-form label-position="top">
            <div class="form-grid add-device-grid">
              <el-form-item label="Model"
                ><el-select
                  v-model="batch.model"
                  @change="onBatchModelChange"
                  class="app-full-width"
                  ><el-option
                    v-for="m in configStore.deviceModels"
                    :key="m.id"
                    :label="m.id + ' · ' + m.protocol.toUpperCase()"
                    :value="m.id" /></el-select
              ></el-form-item>
              <el-form-item label="Group"
                ><el-select v-model="batch.group" class="app-full-width"
                  ><el-option
                    v-for="g in configStore.deviceGroups.filter(
                      (g) => g.device_type === batchModel?.device_type,
                    )"
                    :key="g.id"
                    :label="g.id"
                    :value="g.id" /></el-select
              ></el-form-item>
              <el-form-item label="From"
                ><el-input-number v-model="batch.from" :min="0" :max="9999" class="app-full-width"
              /></el-form-item>
              <el-form-item label="To"
                ><el-input-number v-model="batch.to" :min="0" :max="9999" class="app-full-width"
              /></el-form-item>
              <el-form-item label="Exclude"
                ><el-input v-model="batch.exclude" placeholder="5,17,30-32"
              /></el-form-item>
              <el-form-item label="Device ID Pattern"
                ><el-input v-model="batch.id_pattern" placeholder="wtg-{num:03}"
              /></el-form-item>
              <el-form-item label="Host Pattern"
                ><el-input v-model="batch.host_pattern" placeholder="192.168.151.{num}"
              /></el-form-item>
              <el-form-item v-if="batchModel?.protocol === 'ads'" label="Target AMS Net ID Pattern"
                ><el-input v-model="batch.netid_pattern" placeholder="192.168.151.{num}.1.1"
              /></el-form-item>
            </div>
          </el-form>
          <el-divider content-position="left"
            >Preview · {{ batchPreview.length }} Devices</el-divider
          >
          <el-table :data="batchPreview" max-height="340" size="small">
            <el-table-column prop="num" label="#" width="64" />
            <el-table-column prop="id" label="Device ID" min-width="140" />
            <el-table-column prop="host" label="Host" min-width="150" />
            <el-table-column
              v-if="batchModel?.protocol === 'ads'"
              prop="netid"
              label="Target AMS Net ID"
              min-width="180"
            />
            <el-table-column label="Validation" min-width="170"
              ><template #default="s"
                ><el-tag :type="s.row.error ? 'danger' : 'success'" size="small">{{
                  s.row.error || 'OK'
                }}</el-tag></template
              ></el-table-column
            >
          </el-table>
        </el-tab-pane>
      </el-tabs>

      <template #footer>
        <el-button @click="addOpen = false">Cancel</el-button>
        <el-button v-if="addMode === 'single'" type="primary" @click="addDevice"
          >Add Device</el-button
        >
        <el-button
          v-else
          type="primary"
          :disabled="!batchPreview.length || batchPreview.some((r) => !!r.error)"
          @click="createBatchDevices"
          >Create {{ batchPreview.length }} Devices</el-button
        >
      </template>
    </el-drawer>

    <el-dialog
      v-model="deleteAllOpen"
      title="Delete All Devices"
      width="var(--app-dialog-width-sm)"
    >
      <el-alert
        type="error"
        :closable="false"
        show-icon
        title="All Devices will be removed. Metadata and Task Definitions are preserved."
      />
      <el-descriptions :column="1" border size="small" class="delete-summary">
        <el-descriptions-item label="Devices">{{
          configStore.devices.length
        }}</el-descriptions-item>
        <el-descriptions-item label="Task Definitions"
          >{{ configStore.tasks.length }} preserved</el-descriptions-item
        >
        <el-descriptions-item label="Running Tasks"
          >{{ configStore.tasks.filter((t) => t.runtime === 'RUNNING').length }} will
          stop</el-descriptions-item
        >
        <el-descriptions-item label="After Delete"
          >Tasks without target devices become INVALID</el-descriptions-item
        >
      </el-descriptions>
      <el-form label-position="top">
        <el-form-item label='Type "DELETE ALL" to confirm'
          ><el-input v-model="deleteAllConfirm" autocomplete="off"
        /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deleteAllOpen = false">Cancel</el-button>
        <el-button
          type="danger"
          :disabled="deleteAllConfirm !== 'DELETE ALL'"
          @click="deleteAllDevices"
          >Delete All Devices</el-button
        >
      </template>
    </el-dialog>

    <!-- Device Drawer -->
    <el-drawer
      v-model="drawer"
      :size="detailDrawerSize"
      :with-header="false"
      class="device-drawer"
      :before-close="beforeDeviceClose"
      @closed="onDeviceDrawerClosed"
    >
      <template v-if="selected">
        <div class="drawer-head">
          <div>
            <div class="drawer-title-row">
              <h2>{{ selected.device_id }}</h2>
              <el-tag :type="verifyTagType(selectedVerify)">
                {{ statusLabel(selectedVerify) }}
              </el-tag>
            </div>
            <p>
              {{ selected.model }} · {{ selectedModel?.protocol?.toUpperCase() }} ·
              {{ selected.host
              }}<template v-if="connValue(selected, 'port')"
                >:{{ connValue(selected, 'port') }}</template
              >
            </p>
          </div>
          <el-button text @click="closeDeviceDrawer">Close</el-button>
        </div>

        <el-tabs v-model="tab" class="drawer-tabs">
          <!-- CONFIG -->
          <el-tab-pane label="Config" name="Config">
            <div class="config-layout">
              <section class="panel-card">
                <div class="panel-head">
                  <div>
                    <h3>Device Configuration</h3>
                    <p>
                      {{ overrideKeys(selected).length }} connection override(s) · Model defaults
                      are inherited
                    </p>
                  </div>
                  <div>
                    <el-button type="primary" :disabled="!deviceDirty" @click="saveConfig"
                      >Save</el-button
                    >
                  </div>
                </div>

                <el-form label-position="top" class="device-config-form">
                  <el-divider content-position="left">Device Identity</el-divider>
                  <div class="form-grid config-edit-grid">
                    <el-form-item label="Device ID"
                      ><el-input v-model="editForm.device_id" disabled
                    /></el-form-item>
                    <el-form-item label="Model">
                      <el-select
                        v-model="editForm.model"
                        @change="onEditModelChange"
                        class="app-full-width"
                      >
                        <el-option
                          v-for="m in configStore.deviceModels"
                          :key="m.id"
                          :label="m.id"
                          :value="m.id"
                        />
                      </el-select>
                    </el-form-item>
                    <el-form-item label="Group">
                      <el-select v-model="editForm.device_group" class="app-full-width">
                        <el-option
                          v-for="g in configStore.deviceGroups.filter(
                            (g) => g.device_type === editForm.device_type,
                          )"
                          :key="g.id"
                          :label="g.id"
                          :value="g.id"
                        />
                      </el-select>
                    </el-form-item>
                    <el-form-item label="Host / Remote IP"
                      ><el-input v-model="editForm.host"
                    /></el-form-item>
                    <el-form-item v-if="editForm.protocol === 'ads'" label="Target AMS Net ID"
                      ><el-input v-model="editForm.connection.target_net_id"
                    /></el-form-item>
                  </div>

                  <el-divider content-position="left">Connection Overrides</el-divider>
                  <div class="form-grid config-edit-grid">
                    <el-form-item label="Port"
                      ><el-input-number
                        v-model="editForm.connection.port"
                        :min="1"
                        :max="65535"
                        class="app-full-width"
                    /></el-form-item>
                    <ProtocolConnectionFields
                      v-model="editForm.connection"
                      :protocol="editForm.protocol"
                      :modbus-modes="['tcp', 'rtu']"
                    />
                  </div>

                  <el-alert
                    type="info"
                    :closable="false"
                    title="Protocol, Point Table, Read Mode, manufacturer and hardware model are Model properties. Change them in Manage Metadata."
                  />
                </el-form>

                <div class="danger-row">
                  <el-button type="danger" plain @click="del">Delete Device</el-button>
                </div>
              </section>

              <section class="panel-card">
                <div class="panel-head">
                  <div>
                    <h3>Connectivity</h3>
                    <p>网络、协议和点表读取验证</p>
                  </div>
                  <el-button
                    type="primary"
                    :loading="verifyingDeviceId === selected.device_id"
                    :disabled="verifyOperationActive && verifyingDeviceId !== selected.device_id"
                    @click="verifyDevice(selected)"
                  >
                    Verify Device
                  </el-button>
                </div>

                <div class="connectivity-list connectivity-result-list">
                  <div>
                    <span>Network</span>
                    <b :class="stepClass(selectedVerify.network)">{{
                      stepText(selectedVerify.network)
                    }}</b>
                    <small
                      >{{ selected.host
                      }}<template v-if="selectedVerify.latency_ms">
                        · {{ selectedVerify.latency_ms }} ms</template
                      ></small
                    >
                  </div>
                  <div>
                    <span>Protocol</span>
                    <b :class="stepClass(selectedVerify.protocol)">{{
                      stepText(selectedVerify.protocol)
                    }}</b>
                    <small>{{ protocolDescription(selected) }}</small>
                  </div>
                  <div>
                    <span>Point Read</span>
                    <b :class="stepClass(selectedVerify.points)">{{
                      stepText(selectedVerify.points)
                    }}</b>
                    <small>
                      <template v-if="selectedVerify.point_total"
                        >{{ selectedVerify.point_success }} /
                        {{ selectedVerify.point_total }} passed</template
                      >
                      <template v-else>Point Table: {{ tableOfDeviceOf(selected) }}</template>
                    </small>
                  </div>
                </div>
              </section>
            </div>

            <section
              v-if="selectedVerify.errors.length"
              class="panel-card verification-panel error-information"
            >
              <div class="panel-head">
                <div>
                  <h3>Error Information</h3>
                  <p>最近一次验证发现的错误</p>
                </div>
                <span v-if="selectedVerify.verified_at" class="subtle">{{
                  selectedVerify.verified_at
                }}</span>
              </div>
              <div class="error-list">
                <div
                  v-for="(e, i) in selectedVerify.errors"
                  :key="`${e.stage}-${e.target}-${i}`"
                  class="error-item"
                >
                  <div class="error-item-head">
                    <b>{{ e.target }}</b
                    ><span>{{ e.stage }}</span>
                  </div>
                  <p>{{ e.message }}</p>
                </div>
              </div>
            </section>
          </el-tab-pane>

          <!-- READ TEST -->
          <el-tab-pane label="Read Test" name="ReadTest">
            <div class="read-test-layout">
              <section class="panel-card">
                <div class="panel-head">
                  <div>
                    <h3>Point Read Test</h3>
                    <p>只测试当前设备 Point Table 中已经配置的 Point。</p>
                  </div>
                </div>
                <el-form label-position="top">
                  <el-form-item label="Point">
                    <el-select v-model="readPointId" filterable class="app-full-width">
                      <el-option
                        v-for="p in resolvedPoints"
                        :key="p.point_id"
                        :label="`${p.point_id} · ${p.variable_name || pointAddressText(p)}`"
                        :value="p.point_id"
                      />
                    </el-select>
                  </el-form-item>
                </el-form>

                <el-descriptions v-if="readPoint" :column="isMobile ? 1 : 2" border>
                  <el-descriptions-item label="Point ID">{{
                    readPoint.point_id
                  }}</el-descriptions-item>
                  <el-descriptions-item label="Variable">{{
                    readPoint.variable_name || '—'
                  }}</el-descriptions-item>
                  <el-descriptions-item label="Address">{{
                    pointAddressText(readPoint)
                  }}</el-descriptions-item>
                  <el-descriptions-item label="Data Type">{{
                    readPoint.data_type
                  }}</el-descriptions-item>
                  <el-descriptions-item label="Scale / Offset"
                    >{{ readPoint.scale }} / {{ readPoint.offset }}</el-descriptions-item
                  >
                  <el-descriptions-item label="Unit">{{
                    unitSymbolOf(readPoint.unit) || '—'
                  }}</el-descriptions-item>
                  <el-descriptions-item label="Groups" :span="2">{{
                    readPoint.point_groups.join(', ') || '—'
                  }}</el-descriptions-item>
                  <el-descriptions-item label="Description" :span="2">{{
                    readPoint.description || '—'
                  }}</el-descriptions-item>
                </el-descriptions>

                <div class="read-test-actions">
                  <el-button
                    type="primary"
                    :loading="readLoading"
                    :disabled="!readPoint"
                    @click="readSelectedPoint"
                    >Read Once</el-button
                  >
                </div>
              </section>

              <section class="panel-card">
                <div class="panel-head">
                  <div>
                    <h3>Read Result</h3>
                    <p>保留 Raw Data、解码结果和协议错误。</p>
                  </div>
                </div>
                <el-empty
                  v-if="!readResult"
                  description="No read executed in this session"
                  :image-size="64"
                />
                <template v-else>
                  <el-alert
                    :type="readResult.state === 'success' ? 'success' : 'error'"
                    :closable="false"
                    :title="readResult.state === 'success' ? 'Read succeeded' : 'Read failed'"
                  />
                  <el-descriptions :column="1" border class="read-result-details">
                    <el-descriptions-item label="Timestamp">{{
                      readResult.timestamp
                    }}</el-descriptions-item>
                    <el-descriptions-item label="Latency"
                      >{{ readResult.latency_ms }} ms</el-descriptions-item
                    >
                    <el-descriptions-item label="Raw Data"
                      ><code>{{ readResult.raw_data }}</code></el-descriptions-item
                    >
                    <el-descriptions-item label="Decoded">{{
                      readResult.decoded_value
                    }}</el-descriptions-item>
                    <el-descriptions-item label="Engineering">{{
                      readResult.engineering_value
                    }}</el-descriptions-item>
                    <template v-if="readResult.state === 'failed'">
                      <el-descriptions-item label="Category">{{
                        readResult.error_category
                      }}</el-descriptions-item>
                      <el-descriptions-item label="Error Code">{{
                        readResult.error_code
                      }}</el-descriptions-item>
                      <el-descriptions-item label="Message">{{
                        readResult.error_message
                      }}</el-descriptions-item>
                    </template>
                  </el-descriptions>
                </template>
              </section>
            </div>
          </el-tab-pane>

          <!-- DATA -->
          <el-tab-pane label="Data" name="Data">
            <div class="data-toolbar">
              <div>
                <h3>{{ dataRows.length }} points</h3>
                <p>
                  {{ dataSuccessCount }} / {{ dataRows.length }} read
                  <template v-if="dataFailureCount"> · {{ dataFailureCount }} failed</template>
                  <template v-if="dataLastRefreshAt">
                    · Last refreshed {{ dataLastRefreshAt }}</template
                  >
                </p>
              </div>
              <div class="data-tools data-refresh-tools">
                <el-input v-model="dataSearch" clearable placeholder="Search variable..." />
                <el-select v-model="dataGroup">
                  <el-option label="All Groups" value="All" />
                  <el-option
                    v-for="g in configStore.pointGroups"
                    :key="g.id"
                    :label="g.name"
                    :value="g.id"
                  />
                </el-select>
                <el-select v-model="dataRefreshInterval" class="data-refresh-interval">
                  <el-option :value="500" label="500 ms" />
                  <el-option :value="1000" label="1 s" />
                  <el-option :value="2000" label="2 s" />
                  <el-option :value="5000" label="5 s" />
                  <el-option :value="10000" label="10 s" />
                </el-select>
                <div class="auto-refresh-toggle">
                  <span>Auto Refresh</span><el-switch v-model="dataAutoRefresh" />
                </div>
                <el-button :disabled="dataRefreshing" @click="refreshData">Refresh</el-button>
              </div>
            </div>

            <div class="compact-data-grid">
              <article
                v-for="r in pagedData"
                :key="r.point_id"
                class="compact-data-item"
                :class="{ 'data-read-failed': r.read_state === 'failed' }"
              >
                <div class="compact-data-top">
                  <b :title="r.variable_name || r.point_id">{{ r.variable_name || r.point_id }}</b>
                  <div class="compact-data-value">
                    <strong>{{ r.value }}</strong
                    ><span>{{ r.unit }}</span>
                  </div>
                </div>
                <time v-if="r.read_state === 'success'">{{ r.updated_at }}</time>
                <small v-else class="data-error">{{ r.error }}</small>
              </article>
            </div>

            <div v-if="visibleData.length > dataPageSize" class="pagination">
              <el-pagination
                v-model:current-page="dataPage"
                v-model:page-size="dataPageSize"
                :page-sizes="[50, 100, 200]"
                :total="visibleData.length"
                :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"
              />
            </div>
          </el-tab-pane>

          <!-- CONTROL & TREND -->
          <el-tab-pane label="Control & Trend" name="ControlTrend">
            <div class="control-trend-layout">
              <div class="control-column">
                <section class="panel-card">
                  <div class="panel-head">
                    <div>
                      <h3>Command</h3>
                      <p>选择控制 Point，确认定义后写入目标值并执行回读。</p>
                    </div>
                  </div>

                  <el-form label-position="top">
                    <el-form-item label="Command Point">
                      <el-select
                        v-model="cmdPoint"
                        filterable
                        class="app-full-width"
                        :disabled="sending"
                      >
                        <el-option
                          v-for="r in controlCandidates"
                          :key="r.point_id"
                          :label="`${r.point_id} · ${r.variable_name}`"
                          :value="r.point_id"
                        />
                      </el-select>
                    </el-form-item>

                    <el-descriptions
                      v-if="currentControlRow"
                      :column="1"
                      border
                      class="control-definition"
                    >
                      <el-descriptions-item label="Point ID">{{
                        currentControlRow.point_id
                      }}</el-descriptions-item>
                      <el-descriptions-item label="Variable / Address"
                        >{{ currentControlRow.variable_name || '—' }} ·
                        {{ currentControlRow.address }}</el-descriptions-item
                      >
                      <el-descriptions-item label="Data Type">{{
                        currentControlRow.data_type
                      }}</el-descriptions-item>
                      <el-descriptions-item label="Scale / Offset"
                        >{{ currentControlRow.scale }} /
                        {{ currentControlRow.offset }}</el-descriptions-item
                      >
                      <el-descriptions-item label="Unit">{{
                        currentControlRow.unit || '—'
                      }}</el-descriptions-item>
                      <el-descriptions-item label="Current"
                        >{{ currentControlRow.value }}
                        {{ currentControlRow.unit }}</el-descriptions-item
                      >
                      <el-descriptions-item label="Updated">{{
                        currentControlRow.updated_at
                      }}</el-descriptions-item>
                    </el-descriptions>

                    <el-form-item label="Target Value" class="control-target-field">
                      <el-switch
                        v-if="cmdIsBool"
                        v-model="cmdBool"
                        :disabled="sending"
                        active-text="true"
                        inactive-text="false"
                      />
                      <el-input-number
                        v-else
                        v-model="cmdValue"
                        :step="1"
                        controls-position="right"
                        class="app-full-width"
                        :disabled="sending"
                      />
                    </el-form-item>
                    <el-button
                      type="primary"
                      :loading="sending"
                      :disabled="!currentControlRow || sending"
                      @click="sendCommand"
                      class="app-full-width"
                      >Send Command</el-button
                    >
                  </el-form>
                </section>

                <section class="panel-card command-result-panel">
                  <div class="panel-head">
                    <div>
                      <h3>Command Result</h3>
                      <p>写入结果与回读值</p>
                    </div>
                  </div>
                  <div v-if="!commandResult" class="empty-state compact">
                    No command executed in this session.
                  </div>
                  <div v-else class="command-result">
                    <div>
                      <span>Requested</span><b>{{ formatCommandValue(commandResult.requested) }}</b>
                    </div>
                    <div>
                      <span>Sent</span><b>{{ commandResult.sentAt }}</b>
                    </div>
                    <div>
                      <span>Write</span
                      ><b :class="commandResult.success ? 'step-success' : 'app-text-fault'">{{
                        commandResult.success ? '✓ Success' : '✕ Failed'
                      }}</b>
                    </div>
                    <div v-if="commandResult.error">
                      <span>Error</span><b class="app-text-fault">{{ commandResult.error }}</b>
                    </div>
                    <div>
                      <span>Read back</span><b>{{ formatCommandValue(commandResult.readback) }}</b>
                    </div>
                    <div
                      v-if="commandResult.success && typeof commandResult.requested === 'number'"
                    >
                      <span>Difference</span
                      ><b>{{
                        (Number(commandResult.readback) - commandResult.requested).toFixed(2)
                      }}</b>
                    </div>
                    <div>
                      <span>Latency</span><b>{{ commandResult.latency }} ms</b>
                    </div>
                  </div>
                </section>
              </div>

              <section class="panel-card trend-column">
                <div class="trend-header">
                  <div class="trend-summary">
                    <b>{{ trendSignals.length }} signals</b>
                    <span>Window {{ trendRange }}</span>
                    <span v-if="trendLastRefreshAt">Updated {{ trendLastRefreshAt }}</span>
                  </div>
                  <div class="trend-primary-actions">
                    <el-button @click="trendPickerOpen = true">Select Signals</el-button>
                    <div class="record-action">
                      <el-button
                        :loading="trendRecording"
                        :disabled="!trendSignals.length"
                        @click="recordTrendRawData"
                        >Record Raw Data</el-button
                      >
                      <el-tooltip
                        content="Chart may be downsampled; recording exports all raw samples in the selected time window."
                        placement="bottom"
                      >
                        <button type="button" class="help-dot" aria-label="Raw recording help">
                          ?
                        </button>
                      </el-tooltip>
                    </div>
                  </div>
                </div>

                <div class="trend-view-bar">
                  <div class="trend-window-control">
                    <span>Time Window</span>
                    <el-segmented
                      v-model="trendRange"
                      :options="['1 min', '5 min', '15 min', '1 h']"
                    />
                  </div>
                  <div class="trend-update-control">
                    <div class="auto-refresh-toggle">
                      <span>Auto Update · 1 s</span><el-switch v-model="trendAutoRefresh" />
                    </div>
                    <el-button @click="renderTrend">Refresh</el-button>
                  </div>
                </div>

                <div v-if="!trendSignals.length" class="trend-empty">
                  No trend signals selected. Use “Select Signals” to add variables.
                </div>
                <div ref="trendChartEl" class="trend-chart control-trend-chart"></div>
              </section>
            </div>
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-drawer>

    <!-- Trend picker -->
    <el-dialog
      v-model="trendPickerOpen"
      title="Select Trend Signals"
      width="var(--app-dialog-width-md)"
    >
      <el-input
        v-model="trendSearch"
        clearable
        placeholder="Search point / variable / description..."
        class="trend-search"
      />

      <div class="trend-picker-list">
        <div v-for="r in trendCandidates" :key="r.point_id" class="trend-picker-row">
          <div>
            <b>{{ r.point_id }}</b>
            <span>{{ r.variable_name }} · {{ r.groups.join(', ') }}</span>
          </div>
          <div class="trend-picker-value">
            <strong>{{ r.value }} {{ r.unit }}</strong>
            <el-button
              size="small"
              class="trend-select-button"
              :type="trendSignals.some((x) => x.id === r.point_id) ? 'success' : 'default'"
              @click="toggleTrendSelection(r)"
            >
              {{ trendSignals.some((x) => x.id === r.point_id) ? 'Selected' : 'Select' }}
            </el-button>
          </div>
        </div>
      </div>

      <template #footer>
        <span class="subtle">{{ trendSignals.length }} selected</span>
        <el-button type="primary" @click="trendPickerOpen = false">Done</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.connectivity-result-list > div {
  display: grid;
  grid-template-columns: minmax(max-content, 0.45fr) minmax(max-content, 0.45fr) minmax(0, 1fr);
  align-items: center;
  gap: var(--app-space-2);
}
.connectivity-result-list b {
  font-size: var(--app-font-body);
  font-weight: var(--app-font-weight-semibold);
}
.connectivity-result-list small {
  min-width: 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-label);
  font-weight: var(--app-font-weight-regular);
  overflow-wrap: anywhere;
}
.read-test-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: var(--app-space-4);
}
.read-test-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--app-space-3);
}
.read-result-details {
  margin-top: var(--app-space-3);
}
.read-result-details code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: var(--app-font-label);
}
.data-refresh-tools {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  flex-wrap: wrap;
}
.auto-refresh-toggle {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  white-space: nowrap;
  color: var(--app-text-secondary);
  font-size: var(--app-font-label);
}
.trend-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--app-space-4);
  padding: var(--app-space-2) 0 var(--app-space-3);
}
.trend-summary {
  display: flex;
  align-items: baseline;
  gap: var(--app-space-3);
  min-width: 0;
}
.trend-summary b {
  font-size: var(--app-font-panel-title);
  font-weight: var(--app-font-weight-semibold);
}
.trend-summary span {
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
  white-space: nowrap;
}
.trend-primary-actions,
.trend-view-bar,
.trend-window-control,
.trend-update-control,
.record-action {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}
.trend-view-bar {
  justify-content: space-between;
  padding: var(--app-space-2) 0 var(--app-space-3);
  border-top: 1px solid var(--app-border-soft);
}
.trend-window-control > span {
  color: var(--app-text-secondary);
  font-size: var(--app-font-label);
  white-space: nowrap;
}
.help-dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: var(--app-help-icon-size);
  height: var(--app-help-icon-size);
  border: 1px solid var(--app-border-soft);
  border-radius: 50%;
  background: transparent;
  padding: 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
  cursor: help;
}
.data-read-failed {
  border-color: var(--app-status-fault);
}
.data-error {
  color: var(--app-status-fault);
  font-size: var(--app-font-caption);
}
.control-definition {
  margin-bottom: var(--app-space-4);
}
.control-target-field {
  margin-top: var(--app-space-4);
}
@media (max-width: 1199px) {
  .read-test-layout {
    grid-template-columns: 1fr;
  }
  .trend-header,
  .trend-view-bar {
    align-items: flex-start;
    flex-direction: column;
  }
  .trend-primary-actions,
  .trend-update-control {
    width: 100%;
  }
  .trend-view-bar {
    gap: var(--app-space-3);
  }
}
@media (max-width: 767px) {
  .connectivity-result-list > div {
    grid-template-columns: 1fr;
    gap: var(--app-space-1);
  }
  .data-refresh-tools {
    align-items: stretch;
  }
  .data-refresh-tools > * {
    max-width: 100%;
  }
  .trend-summary,
  .trend-primary-actions,
  .trend-window-control,
  .trend-update-control {
    flex-wrap: wrap;
  }
}

.delete-summary {
  margin: var(--app-space-4) 0;
}
.data-refresh-interval {
  width: var(--app-control-width-short);
}

.control-trend-layout {
  display: grid;
  grid-template-columns: minmax(var(--app-layout-pane-min-width), 0.72fr) minmax(0, 1.28fr);
  gap: var(--app-space-4);
  align-items: start;
}
.control-column {
  display: grid;
  gap: var(--app-space-4);
  min-width: 0;
}
.trend-column {
  min-width: 0;
}
.command-result-panel {
  min-height: 0;
}
.control-trend-chart {
  height: var(--app-chart-height-xl);
}
@media (max-width: 1199px) {
  .control-trend-layout {
    grid-template-columns: minmax(var(--app-layout-pane-min-width), 0.8fr) minmax(0, 1.2fr);
  }
}
@media (max-width: 767px) {
  .control-trend-layout {
    grid-template-columns: 1fr;
  }
  .control-trend-chart {
    height: var(--app-chart-height-lg);
  }
}
</style>
