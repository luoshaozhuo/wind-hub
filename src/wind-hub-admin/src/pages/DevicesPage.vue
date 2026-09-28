<script setup lang="ts">
import * as echarts from 'echarts'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  modelOf,
  pointsOfTable,
  store,
  tableOfDevice,
  unitSymbol,
} from '../mock/data'
import type { DeviceInst, DeviceVerification, PointDef, VerifyStepState } from '../mock/types'

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
}

interface TrendSignal {
  id: string
  label: string
  unit: string
}

const search = ref('')
const typeFilter = ref('All')
const modelFilter = ref('All')
const statusFilter = ref('All')
const drawer = ref(false)
const tab = ref('Config')
const selected = ref<DeviceInst | null>(null)
const verifyAllRunning = ref(false)
const verifyingDeviceId = ref('')

function emptyVerify(): DeviceVerification {
  return {
    state: 'idle',
    network: 'unknown',
    protocol: 'unknown',
    points: 'unknown',
    point_total: 0,
    point_success: 0,
    point_failed: 0,
    verified_at: '',
    latency_ms: 0,
    errors: [],
  }
}

function verifyOf(d: DeviceInst): DeviceVerification {
  if (!store.deviceVerification[d.device_id]) store.deviceVerification[d.device_id] = emptyVerify()
  return store.deviceVerification[d.device_id]
}

function typeName(typeId: string) {
  return store.deviceTypes.find(x => x.id === typeId)?.name || typeId
}

function pointAddressText(p: PointDef) {
  const a = p.address
  if (a.symbol) return a.symbol
  if (a.index_group || a.index_offset) return `${a.index_group || '-'} / ${a.index_offset || '-'}`
  if (a.type || a.address !== undefined) return `${a.type || '-'} ${a.address ?? '-'}`
  if (a.ioa !== undefined) return `IOA ${a.ioa}`
  return '-'
}

const filteredDevices = computed(() => store.devices.filter(d => {
  const model = modelOf(d)
  const v = verifyOf(d)
  const q = search.value.trim().toLowerCase()
  const matchesSearch = !q || [
    d.device_id,
    d.host,
    d.model,
    d.device_group,
    model?.protocol || '',
  ].some(x => x.toLowerCase().includes(q))

  const matchesType = typeFilter.value === 'All' || model?.device_type === typeFilter.value
  const matchesModel = modelFilter.value === 'All' || d.model === modelFilter.value
  const matchesStatus =
    statusFilter.value === 'All' ||
    (statusFilter.value === 'Healthy' && v.state === 'success') ||
    (statusFilter.value === 'Warning' && v.state === 'warning') ||
    (statusFilter.value === 'Fault' && v.state === 'failed') ||
    (statusFilter.value === 'Unverified' && v.state === 'idle')

  return matchesSearch && matchesType && matchesModel && matchesStatus
}))

const groupedDevices = computed(() => {
  const groups = new Map<string, DeviceInst[]>()
  for (const d of filteredDevices.value) {
    const model = modelOf(d)
    const key = `${model?.device_type || 'unknown'}::${d.model}`
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(d)
  }

  return Array.from(groups.entries()).map(([key, devices]) => {
    const [typeId, modelId] = key.split('::')
    const model = store.deviceModels.find(m => m.id === modelId)
    const healthy = devices.filter(d => verifyOf(d).state === 'success').length
    const warning = devices.filter(d => verifyOf(d).state === 'warning').length
    const failed = devices.filter(d => verifyOf(d).state === 'failed').length
    return { key, typeId, modelId, model, devices, healthy, warning, failed }
  })
})

function statusLabel(v: DeviceVerification) {
  if (v.state === 'running') return 'Verifying'
  if (v.state === 'success') return 'Healthy'
  if (v.state === 'warning') return 'Warning'
  if (v.state === 'failed') return 'Fault'
  return 'Unverified'
}

function statusType(v: DeviceVerification): '' | 'success' | 'warning' | 'danger' | 'info' {
  if (v.state === 'success') return 'success'
  if (v.state === 'warning') return 'warning'
  if (v.state === 'failed') return 'danger'
  if (v.state === 'running') return ''
  return 'info'
}

function stepText(step: VerifyStepState) {
  if (step === 'checking') return 'Checking'
  if (step === 'success') return 'Passed'
  if (step === 'partial') return 'Partial'
  if (step === 'failed') return 'Failed'
  return 'Not tested'
}

function stepIcon(step: VerifyStepState) {
  if (step === 'success') return '✓'
  if (step === 'partial') return '△'
  if (step === 'failed') return '✕'
  if (step === 'checking') return '…'
  return '—'
}

function stepClass(step: VerifyStepState) {
  return `step-${step}`
}

function sleep(ms: number) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function timestampAt(offsetMs = 0) {
  const d = new Date(Date.now() - offsetMs)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function extraConnectionInfo(d: DeviceInst) {
  const protocol = modelOf(d)?.protocol
  if (protocol === 'ads') return `AMS Net ID ${d.host}.1.1`
  return ''
}

function cardStepClass(step: VerifyStepState) {
  if (step === 'success') return 'state-pill state-ok'
  if (step === 'failed' || step === 'partial') return 'state-pill state-bad'
  return 'state-pill state-idle'
}

function protocolDescription(d: DeviceInst) {
  const model = modelOf(d)
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

async function verifyDevice(d: DeviceInst, quiet = false) {
  const v = verifyOf(d)
  verifyingDeviceId.value = d.device_id
  Object.assign(v, emptyVerify(), { state: 'running' as DeviceVerification['state'] })

  v.network = 'checking'
  await sleep(260)

  const networkFail = !d.enabled || d.device_id === 'wtg-041'
  if (networkFail) {
    v.network = 'failed'
    v.state = 'failed'
    v.verified_at = timestampAt()
    v.errors.push({
      stage: 'network',
      target: d.host,
      message: d.enabled ? 'Host unreachable' : 'Device is disabled',
    })
    verifyingDeviceId.value = ''
    if (!quiet) ElMessage.error(`${d.device_id}: network verification failed`)
    return
  }

  v.network = 'success'
  v.latency_ms = 2 + (d.device_id.length % 7)

  v.protocol = 'checking'
  await sleep(300)

  const protocolFail = d.device_id === 'wtg-043'
  if (protocolFail) {
    v.protocol = 'failed'
    v.state = 'failed'
    v.verified_at = timestampAt()
    v.errors.push({
      stage: 'protocol',
      target: protocolDescription(d),
      message: 'Protocol connection failed',
    })
    verifyingDeviceId.value = ''
    if (!quiet) ElMessage.error(`${d.device_id}: protocol verification failed`)
    return
  }

  v.protocol = 'success'
  v.points = 'checking'
  await sleep(420)

  const points = pointsOfTable(tableOfDevice(d))
  v.point_total = points.length

  const failedPoints = d.device_id === 'wtg-026'
    ? points.slice(0, 1)
    : d.device_id === 'wtg-044'
      ? points.slice(0, Math.min(3, points.length))
      : []

  v.point_failed = failedPoints.length
  v.point_success = Math.max(0, v.point_total - v.point_failed)

  for (const p of failedPoints) {
    v.errors.push({
      stage: 'points',
      target: p.variable_name || p.point_id,
      message: modelOf(d)?.protocol === 'ads'
        ? 'ADS symbol not found / read failed'
        : 'Read request returned invalid response',
    })
  }

  if (v.point_failed === 0) {
    v.points = 'success'
    v.state = 'success'
  } else if (v.point_success > 0) {
    v.points = 'partial'
    v.state = 'warning'
  } else {
    v.points = 'failed'
    v.state = 'failed'
  }

  v.verified_at = timestampAt()
  verifyingDeviceId.value = ''
  if (!quiet) {
    if (v.state === 'success') ElMessage.success(`${d.device_id}: verification passed`)
    else if (v.state === 'warning') ElMessage.warning(`${d.device_id}: point verification partially failed`)
    else ElMessage.error(`${d.device_id}: verification failed`)
  }
}

async function verifyAll() {
  verifyAllRunning.value = true
  try {
    for (const d of filteredDevices.value) {
      await verifyDevice(d, true)
    }
    const failed = filteredDevices.value.filter(d => verifyOf(d).state === 'failed').length
    const warning = filteredDevices.value.filter(d => verifyOf(d).state === 'warning').length
    if (failed) ElMessage.error(`Verification complete: ${failed} failed, ${warning} warning`)
    else if (warning) ElMessage.warning(`Verification complete: ${warning} warning`)
    else ElMessage.success('Verification complete')
  } finally {
    verifyAllRunning.value = false
  }
}

// ---- Add Device ----
const addOpen = ref(false)
const newDev = ref({
  id: '',
  type: 'turbine',
  group: 'turbine_modbus',
  model: 'modbus_wtg',
  host: '',
  port: 502,
  enabled: true,
  target_net_id: '',
  target_port: 801,
  twincat_version: '2',
  timeout: 3,
  unit_id: 1,
  mode: 'tcp',
  word_order: 'little_endian',
  iec_common_address: 1,
})

const newDevModel = computed(() => store.deviceModels.find(m => m.id === newDev.value.model))

function applyNewModelDefaults() {
  const model = newDevModel.value
  if (!model) return

  const defaults = model.connection_defaults || {}
  newDev.value.type = model.device_type
  newDev.value.port = Number(defaults.port || (
    model.protocol === 'ads' ? 48898 :
    model.protocol === 'iec104' ? 2404 : 502
  ))
  newDev.value.timeout = Number(defaults.timeout || 3)

  if (model.protocol === 'ads') {
    newDev.value.target_port = Number(defaults.target_port || defaults.ams_port || 801)
    newDev.value.twincat_version = String(defaults.twincat_version || '2')
    if (newDev.value.host) newDev.value.target_net_id = `${newDev.value.host}.1.1`
  } else if (model.protocol === 'modbus') {
    newDev.value.unit_id = Number(defaults.unit_id || 1)
    newDev.value.mode = String(defaults.mode || 'tcp')
    newDev.value.word_order = String(defaults.word_order || 'little_endian')
  } else if (model.protocol === 'iec104') {
    newDev.value.iec_common_address = Number(defaults.common_address || 1)
  }

  const validGroup = store.deviceGroups.some(
    g => g.id === newDev.value.group && g.device_type === model.device_type,
  )
  if (!validGroup) {
    newDev.value.group =
      store.deviceGroups.find(g => g.device_type === model.device_type)?.id || ''
  }
}

function onNewType() {
  const model = store.deviceModels.find(m => m.device_type === newDev.value.type)
  if (model) {
    newDev.value.model = model.id
    applyNewModelDefaults()
  } else {
    newDev.value.model = ''
    newDev.value.group =
      store.deviceGroups.find(g => g.device_type === newDev.value.type)?.id || ''
  }
}

function onNewModel() {
  applyNewModelDefaults()
}

function onNewHostChange() {
  if (newDevModel.value?.protocol === 'ads' && newDev.value.host.trim()) {
    newDev.value.target_net_id = `${newDev.value.host.trim()}.1.1`
  }
}

function openAdd() {
  newDev.value = {
    id: '',
    type: 'turbine',
    group: 'turbine_modbus',
    model: 'modbus_wtg',
    host: '',
    port: 502,
    enabled: true,
    target_net_id: '',
    target_port: 801,
    twincat_version: '2',
    timeout: 3,
    unit_id: 1,
    mode: 'tcp',
    word_order: 'little_endian',
    iec_common_address: 1,
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
  if (store.devices.some(d => d.device_id === id)) {
    ElMessage.error(`Device ${id} already exists`)
    return
  }
  if (!model) {
    ElMessage.error('Select a valid model')
    return
  }
  if (!store.deviceGroups.some(
    g => g.id === newDev.value.group && g.device_type === model.device_type,
  )) {
    ElMessage.error('Select a valid device group')
    return
  }

  const extensions: Record<string, unknown> = {}

  if (model.protocol === 'ads') {
    if (!newDev.value.target_net_id.trim()) {
      ElMessage.error('Target AMS Net ID is required for ADS')
      return
    }
    extensions.target_net_id = newDev.value.target_net_id.trim()
    extensions.target_port = newDev.value.target_port
    extensions.twincat_version = newDev.value.twincat_version
    extensions.timeout = newDev.value.timeout
  } else if (model.protocol === 'modbus') {
    extensions.unit_id = newDev.value.unit_id
    extensions.mode = newDev.value.mode
    extensions.timeout = newDev.value.timeout
    extensions.word_order = newDev.value.word_order
  } else if (model.protocol === 'iec104') {
    extensions.common_address = newDev.value.iec_common_address
    extensions.timeout = newDev.value.timeout
  }

  store.devices.push({
    device_id: id,
    model: model.id,
    device_group: newDev.value.group,
    host,
    port: newDev.value.port || undefined,
    extensions,
    enabled: newDev.value.enabled,
    online: false,
  })
  store.deviceVerification[id] = emptyVerify()

  addOpen.value = false
  ElMessage.success('Device created (mock)')
}

// ---- Drawer / Config ----
const editForm = ref({
  device_id: '',
  device_type: '',
  model: '',
  manufacturer: '',
  hardware_model: '',
  protocol: 'modbus',
  point_table: '',
  read_mode: '',
  device_group: '',
  host: '',
  port: 0,
  target_net_id: '',
  target_port: 801,
  twincat_version: '2',
  timeout: 3,
  unit_id: 1,
  mode: 'tcp',
  word_order: 'little_endian',
})

function openDev(d: DeviceInst) {
  selected.value = d
  drawer.value = true
  tab.value = 'Config'
  loadEditForm()
}

function loadEditForm() {
  if (!selected.value) return
  const conn = mergedConnection(selected.value)
  const model = modelOf(selected.value)
  editForm.value = {
    device_id: selected.value.device_id,
    device_type: model?.device_type || '',
    model: selected.value.model,
    manufacturer: model?.manufacturer || '',
    hardware_model: model?.model || '',
    protocol: model?.protocol || 'modbus',
    point_table: model?.point_table || '',
    read_mode: model?.read_mode || '',
    device_group: selected.value.device_group,
    host: selected.value.host,
    port: Number(conn.port || selected.value.port || 0),
    target_net_id: String(conn.target_net_id || ''),
    target_port: Number(conn.target_port || conn.ams_port || 801),
    twincat_version: String(conn.twincat_version || '2'),
    timeout: Number(conn.timeout || 3),
    unit_id: Number(conn.unit_id || 1),
    mode: String(conn.mode || 'tcp'),
    word_order: String(conn.word_order || 'little_endian'),
  }
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

  const validGroup = store.deviceGroups.some(
    g => g.id === editForm.value.device_group && g.device_type === model.device_type,
  )
  if (!validGroup) {
    editForm.value.device_group =
      store.deviceGroups.find(g => g.device_type === model.device_type)?.id || ''
  }

  const defaults = model.connection_defaults || {}
  editForm.value.port = Number(defaults.port || editForm.value.port || 0)

  if (model.protocol === 'ads') {
    editForm.value.target_port = Number(defaults.target_port || defaults.ams_port || 801)
    editForm.value.twincat_version = String(defaults.twincat_version || '2')
    editForm.value.timeout = Number(defaults.timeout || 3)
    if (!editForm.value.target_net_id && editForm.value.host) {
      editForm.value.target_net_id = `${editForm.value.host}.1.1`
    }
  } else if (model.protocol === 'modbus') {
    editForm.value.unit_id = Number(defaults.unit_id || 1)
    editForm.value.mode = String(defaults.mode || 'tcp')
    editForm.value.timeout = Number(defaults.timeout || 3)
    editForm.value.word_order = String(defaults.word_order || 'little_endian')
  }
}

function onEditTypeChange() {
  if (!store.deviceModels.some(
    m => m.id === editForm.value.model && m.device_type === editForm.value.device_type,
  )) {
    const next = store.deviceModels.find(m => m.device_type === editForm.value.device_type)
    if (next) {
      editForm.value.model = next.id
      onEditModelChange()
    }
  }

  if (!store.deviceGroups.some(
    g => g.id === editForm.value.device_group && g.device_type === editForm.value.device_type,
  )) {
    editForm.value.device_group =
      store.deviceGroups.find(g => g.device_type === editForm.value.device_type)?.id || ''
  }
}

function onEditProtocolChange() {
  if (!store.pointTables.some(
    t => t.id === editForm.value.point_table && t.protocol === editForm.value.protocol,
  )) {
    editForm.value.point_table =
      store.pointTables.find(t => t.protocol === editForm.value.protocol)?.id || ''
  }
  if (editForm.value.protocol === 'ads' && !editForm.value.read_mode) {
    editForm.value.read_mode = 'sum'
  }
}

function saveConfig() {
  if (!selected.value) return

  const oldDeviceId = selected.value.device_id
  const newDeviceId = editForm.value.device_id.trim()
  if (!newDeviceId) {
    ElMessage.error('Device ID is required')
    return
  }
  if (
    newDeviceId !== oldDeviceId &&
    store.devices.some(d => d !== selected.value && d.device_id === newDeviceId)
  ) {
    ElMessage.error(`Device ID ${newDeviceId} already exists`)
    return
  }

  const model = editSelectedModel.value
  if (!model) {
    ElMessage.error('Select a valid model')
    return
  }

  model.device_type = editForm.value.device_type
  model.manufacturer = editForm.value.manufacturer.trim()
  model.model = editForm.value.hardware_model.trim()
  model.protocol = editForm.value.protocol as typeof model.protocol
  model.point_table = editForm.value.point_table
  model.read_mode = editForm.value.protocol === 'ads' ? editForm.value.read_mode : ''

  const extensions: Record<string, unknown> = {}
  if (editForm.value.protocol === 'ads') {
    extensions.target_net_id = editForm.value.target_net_id.trim()
    extensions.target_port = editForm.value.target_port
    extensions.twincat_version = editForm.value.twincat_version
    extensions.timeout = editForm.value.timeout
  } else if (editForm.value.protocol === 'modbus') {
    extensions.unit_id = editForm.value.unit_id
    extensions.mode = editForm.value.mode
    extensions.timeout = editForm.value.timeout
    extensions.word_order = editForm.value.word_order
  }

  Object.assign(selected.value, {
    device_id: newDeviceId,
    device_group: editForm.value.device_group,
    model: editForm.value.model,
    host: editForm.value.host.trim(),
    port: editForm.value.port || undefined,
    extensions,
  })

  if (newDeviceId !== oldDeviceId) {
    const verification = store.deviceVerification[oldDeviceId]
    delete store.deviceVerification[oldDeviceId]
    if (verification) store.deviceVerification[newDeviceId] = verification
    for (const task of store.tasks) {
      if (task.device === oldDeviceId) task.device = newDeviceId
    }
  }

  loadEditForm()
  ElMessage.success('Device config updated (mock)')
}

async function del() {
  if (!selected.value) return
  const id = selected.value.device_id
  const direct = store.tasks.filter(t => t.device === id).length
  await ElMessageBox.confirm(
    `删除 ${id}？将删除直接引用该设备的 ${direct} 个任务定义；Group 任务保留。`,
    'Delete Device',
    {
      type: 'warning',
      confirmButtonText: 'Delete',
    },
  )
  store.tasks = store.tasks.filter(t => t.device !== id)
  store.devices = store.devices.filter(x => x !== selected.value)
  delete store.deviceVerification[id]
  drawer.value = false
  ElMessage.success('Device deleted (mock)')
}

const selectedModel = computed(() => selected.value ? modelOf(selected.value) : undefined)
const editSelectedModel = computed(() => store.deviceModels.find(m => m.id === editForm.value.model))

function mergedConnection(d: DeviceInst) {
  const model = modelOf(d)
  return {
    ...(model?.connection_defaults || {}),
    ...(d.extensions || {}),
    port: d.port ?? model?.connection_defaults?.port,
  } as Record<string, unknown>
}

function connValue(d: DeviceInst, key: string, fallback: unknown = '') {
  const value = mergedConnection(d)[key]
  return value === undefined || value === null || value === '' ? fallback : value
}

function boolValue(value: unknown, fallback = false) {
  return value === undefined || value === null ? fallback : Boolean(value)
}

const selectedVerify = computed(() => selected.value ? verifyOf(selected.value) : emptyVerify())

// ---- Data ----
const dataSearch = ref('')
const dataGroup = ref('All')

const dataRows = computed<DataRow[]>(() => {
  if (!selected.value) return []
  return pointsOfTable(tableOfDevice(selected.value)).map((p, i) => {
    const raw = ((i * 31 + 17) % 1300) / 10
    return {
      point_id: p.point_id,
      variable_name: p.variable_name,
      description: p.description,
      value: p.data_type === 'bool' ? i % 2 === 0 : raw,
      unit: unitSymbol(p.unit),
      groups: p.point_groups,
      updated_at: timestampAt(80 + (i % 17) * 1100),
      data_type: p.data_type,
      scale: p.scale,
      offset: p.offset,
      address: pointAddressText(p),
      updated: i % 3 === 0,
    }
  })
})

const visibleData = computed(() => dataRows.value.filter(r => {
  const q = dataSearch.value.trim().toLowerCase()
  return (dataGroup.value === 'All' || r.groups.includes(dataGroup.value)) &&
    (!q || [r.point_id, r.variable_name, r.description].some(x => x.toLowerCase().includes(q)))
}))

// ---- Trend ----
const trendChartEl = ref<HTMLElement | null>(null)
let trendChart: echarts.ECharts | null = null
const trendLegendSelected = ref<Record<string, boolean>>({})
const trendPickerOpen = ref(false)
const trendSearch = ref('')
const trendRange = ref('Real-time')
const trendSignals = ref<TrendSignal[]>([])

const trendCandidates = computed(() => dataRows.value.filter(r => {
  const q = trendSearch.value.trim().toLowerCase()
  return !q || [r.point_id, r.variable_name, r.description].some(x => x.toLowerCase().includes(q))
}))

function toggleTrendSelection(r: DataRow) {
  const existing = trendSignals.value.find(x => x.id === r.point_id)
  if (existing) {
    trendSignals.value = trendSignals.value.filter(x => x.id !== r.point_id)
    delete trendLegendSelected.value[existing.label]
  } else {
    const signal = {
      id: r.point_id,
      label: r.variable_name || r.point_id,
      unit: r.unit,
    }
    trendSignals.value.push(signal)
    trendLegendSelected.value[signal.label] = true
  }
  renderTrend()
}

function seedTrendSignals() {
  if (trendSignals.value.length || !dataRows.value.length) return
  trendSignals.value = dataRows.value.slice(0, 4).map(r => ({
    id: r.point_id,
    label: r.variable_name || r.point_id,
    unit: r.unit,
  }))
  for (const s of trendSignals.value) trendLegendSelected.value[s.label] = true
}

function makeTrendData(index: number) {
  const now = Date.now()
  return Array.from({ length: 80 }, (_, i) => {
    const x = new Date(now - (79 - i) * 1000)
    const y = 30 + index * 25 + Math.sin(i / 7 + index) * (5 + index * 2) + (i % 9) * 0.2
    return [x, Number(y.toFixed(2))]
  })
}

function renderTrend() {
  nextTick(() => {
    if (!trendChartEl.value) return
    if (!trendChart) {
      trendChart = echarts.init(trendChartEl.value)
      trendChart.on('legendselectchanged', (params: any) => {
        trendLegendSelected.value = { ...(params.selected || {}) }
      })
    }

    const series = trendSignals.value.map((s, index) => ({
      name: s.label,
      type: 'line',
      showSymbol: false,
      smooth: true,
      data: makeTrendData(index),
    }))

    trendChart.setOption({
      animation: false,
      tooltip: { trigger: 'axis' },
      legend: {
        type: 'scroll',
        top: 10,
        left: 18,
        right: 18,
        selected: trendLegendSelected.value,
      },
      grid: { top: 58, left: 56, right: 24, bottom: 42 },
      xAxis: { type: 'time', boundaryGap: false },
      yAxis: { type: 'value', scale: true },
      series,
    }, true)
    trendChart.resize()
  })
}

function onResize() {
  trendChart?.resize()
}

watch(tab, value => {
  if (value === 'Trend') {
    seedTrendSignals()
    renderTrend()
  }
})

watch(() => selected.value?.device_id, () => {
  trendSignals.value = []
  trendLegendSelected.value = {}
  seedTrendSignals()
  renderTrend()
})

window.addEventListener('resize', onResize)
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  trendChart?.dispose()
  trendChart = null
})

// ---- Control ----
const cmdPoint = ref('')
const cmdValue = ref(0)
const sending = ref(false)
const commandResult = ref<{
  requested: number
  readback: number
  sentAt: string
  latency: number
  success: boolean
} | null>(null)

const controlCandidates = computed(() => dataRows.value.filter(r => r.groups.includes('control')))
const currentControlRow = computed(() => controlCandidates.value.find(r => r.point_id === cmdPoint.value))

watch(controlCandidates, rows => {
  if (!cmdPoint.value && rows.length) {
    cmdPoint.value = rows[0].point_id
    cmdValue.value = Number(rows[0].value) || 0
  }
}, { immediate: true })

watch(cmdPoint, id => {
  const row = controlCandidates.value.find(r => r.point_id === id)
  if (row) cmdValue.value = Number(row.value) || 0
  commandResult.value = null
})

async function sendCommand() {
  if (!selected.value || !cmdPoint.value) {
    ElMessage.warning('Select a command point first')
    return
  }
  sending.value = true
  commandResult.value = null
  try {
    await sleep(420)
    const requested = Number(cmdValue.value)
    commandResult.value = {
      requested,
      readback: Number((requested - 0.6).toFixed(2)),
      sentAt: timestampAt(),
      latency: 160 + (selected.value.device_id.length % 6) * 7,
      success: true,
    }
    ElMessage.success('Command completed (mock)')
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
        <el-button
          :loading="verifyAllRunning"
          :disabled="verifyAllRunning"
          @click="verifyAll"
        >
          Verify All
        </el-button>
        <el-button type="primary" @click="openAdd">+ Add Device</el-button>
      </div>
    </div>

    <el-card shadow="never" class="filter-card">
      <div class="filter-grid">
        <el-input
          v-model="search"
          clearable
          placeholder="Search device ID / IP / model / protocol..."
        />
        <el-select v-model="typeFilter">
          <el-option label="All Types" value="All" />
          <el-option
            v-for="t in store.deviceTypes"
            :key="t.id"
            :label="t.name"
            :value="t.id"
          />
        </el-select>
        <el-select v-model="modelFilter">
          <el-option label="All Models" value="All" />
          <el-option
            v-for="m in store.deviceModels"
            :key="m.id"
            :label="m.id"
            :value="m.id"
          />
        </el-select>
        <el-select v-model="statusFilter">
          <el-option label="All Status" value="All" />
          <el-option label="Healthy" value="Healthy" />
          <el-option label="Warning" value="Warning" />
          <el-option label="Fault" value="Fault" />
          <el-option label="Unverified" value="Unverified" />
        </el-select>
      </div>
    </el-card>

    <div v-if="!groupedDevices.length" class="empty-state">
      No devices match current filters.
    </div>

    <section
      v-for="group in groupedDevices"
      :key="group.key"
      class="model-group"
    >
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
            <span>{{ group.devices.length }} devices</span>
          </div>
        </div>
        <div class="group-summary">
          <span class="ok">{{ group.healthy }} healthy</span>
          <span class="warn">{{ group.warning }} warning</span>
          <span class="bad">{{ group.failed }} fault</span>
        </div>
      </div>

      <div class="device-grid">
        <article
          v-for="d in group.devices"
          :key="d.device_id"
          class="device-card device-card-compact"
          :class="{ 'device-disabled': !d.enabled }"
        >
          <div class="device-card-top">
            <button class="device-id-link" @click="openDev(d)">
              {{ d.device_id }}
            </button>
            <div class="device-enabled">
              <span>Enabled</span>
              <el-switch v-model="d.enabled" size="small" />
            </div>
          </div>

          <div class="device-fields">
            <div>
              <span>Protocol</span>
              <b>{{ modelOf(d)?.protocol?.toUpperCase() || '-' }}</b>
            </div>
            <div>
              <span>IP</span>
              <b>{{ d.host }}</b>
            </div>
            <div>
              <span>Port</span>
              <b>{{ d.port || modelOf(d)?.connection_defaults?.port || '-' }}</b>
            </div>
          </div>

          <div class="status-pills">
            <span :class="['status-pill', stepClass(verifyOf(d).network)]">
              <i></i>Network
            </span>
            <span :class="['status-pill', stepClass(verifyOf(d).protocol)]">
              <i></i>Protocol
            </span>
            <span :class="['status-pill', stepClass(verifyOf(d).points)]">
              <i></i>Points
            </span>
          </div>
        </article>
      </div>
    </section>

    <!-- Add Device -->
    <el-dialog
      v-model="addOpen"
      title="Add Device"
      width="780px"
      class="add-device-dialog"
    >
      <el-form label-position="top" class="add-device-form">
        <div class="config-section">
          <div class="config-section-title">
            <b>Device</b>
            <span>devices.yaml</span>
          </div>

          <div class="form-grid add-device-grid">
            <el-form-item label="Device ID">
              <el-input v-model="newDev.id" placeholder="wtg-001" />
            </el-form-item>

            <el-form-item label="Type">
              <el-select v-model="newDev.type" style="width:100%" @change="onNewType">
                <el-option
                  v-for="t in store.deviceTypes"
                  :key="t.id"
                  :label="t.name"
                  :value="t.id"
                />
              </el-select>
            </el-form-item>

            <el-form-item label="Model">
              <el-select v-model="newDev.model" style="width:100%" @change="onNewModel">
                <el-option
                  v-for="m in store.deviceModels.filter(m => m.device_type === newDev.type)"
                  :key="m.id"
                  :label="m.id"
                  :value="m.id"
                />
              </el-select>
            </el-form-item>

            <el-form-item label="Group">
              <el-select v-model="newDev.group" style="width:100%">
                <el-option
                  v-for="g in store.deviceGroups.filter(g => g.device_type === newDev.type)"
                  :key="g.id"
                  :label="g.id"
                  :value="g.id"
                />
              </el-select>
            </el-form-item>

            <el-form-item label="Protocol">
              <el-input :model-value="newDevModel?.protocol?.toUpperCase() || '-'" disabled />
            </el-form-item>

            <el-form-item label="Point Table">
              <el-input :model-value="newDevModel?.point_table || '-'" disabled />
            </el-form-item>

            <el-form-item
              v-if="newDevModel?.protocol === 'ads'"
              label="Read Mode"
            >
              <el-input :model-value="newDevModel?.read_mode || 'sum'" disabled />
            </el-form-item>

            <el-form-item label="Host">
              <el-input
                v-model="newDev.host"
                placeholder="192.168.1.100"
                @change="onNewHostChange"
              />
            </el-form-item>

            <el-form-item label="Port">
              <el-input-number
                v-model="newDev.port"
                :min="1"
                :max="65535"
                controls-position="right"
                style="width:100%"
              />
            </el-form-item>

            <el-form-item label="Enabled">
              <div class="add-device-enabled">
                <el-switch v-model="newDev.enabled" />
                <span>{{ newDev.enabled ? 'Enabled' : 'Disabled' }}</span>
              </div>
            </el-form-item>
          </div>
        </div>

        <div v-if="newDevModel?.protocol === 'ads'" class="config-section">
          <div class="config-section-title">
            <b>ADS</b>
            <span>endpoint.extensions</span>
          </div>

          <div class="form-grid add-device-grid">
            <el-form-item label="Target AMS Net ID">
              <el-input
                v-model="newDev.target_net_id"
                placeholder="192.168.151.25.1.1"
              />
            </el-form-item>

            <el-form-item label="ADS Port">
              <el-input-number
                v-model="newDev.target_port"
                :min="1"
                :max="65535"
                controls-position="right"
                style="width:100%"
              />
            </el-form-item>

            <el-form-item label="TwinCAT Version">
              <el-select v-model="newDev.twincat_version" style="width:100%">
                <el-option label="TwinCAT 2" value="2" />
                <el-option label="TwinCAT 3" value="3" />
              </el-select>
            </el-form-item>

            <el-form-item label="Timeout (s)">
              <el-input-number
                v-model="newDev.timeout"
                :min="0.1"
                :step="0.5"
                controls-position="right"
                style="width:100%"
              />
            </el-form-item>
          </div>
        </div>

        <div v-else-if="newDevModel?.protocol === 'modbus'" class="config-section">
          <div class="config-section-title">
            <b>Modbus</b>
            <span>endpoint.extensions</span>
          </div>

          <div class="form-grid add-device-grid">
            <el-form-item label="Unit ID">
              <el-input-number
                v-model="newDev.unit_id"
                :min="0"
                :max="255"
                controls-position="right"
                style="width:100%"
              />
            </el-form-item>

            <el-form-item label="Mode">
              <el-select v-model="newDev.mode" style="width:100%">
                <el-option label="TCP" value="tcp" />
                <el-option label="RTU" value="rtu" />
              </el-select>
            </el-form-item>

            <el-form-item label="Timeout (s)">
              <el-input-number
                v-model="newDev.timeout"
                :min="0.1"
                :step="0.5"
                controls-position="right"
                style="width:100%"
              />
            </el-form-item>

            <el-form-item label="Word Order">
              <el-select v-model="newDev.word_order" style="width:100%">
                <el-option label="Little endian" value="little_endian" />
                <el-option label="Big endian" value="big_endian" />
              </el-select>
            </el-form-item>
          </div>
        </div>

        <div v-else-if="newDevModel?.protocol === 'iec104'" class="config-section">
          <div class="config-section-title">
            <b>IEC 104</b>
            <span>endpoint.extensions</span>
          </div>

          <div class="form-grid add-device-grid">
            <el-form-item label="Common Address">
              <el-input-number
                v-model="newDev.iec_common_address"
                :min="0"
                :max="65535"
                controls-position="right"
                style="width:100%"
              />
            </el-form-item>

            <el-form-item label="Timeout (s)">
              <el-input-number
                v-model="newDev.timeout"
                :min="0.1"
                :step="0.5"
                controls-position="right"
                style="width:100%"
              />
            </el-form-item>
          </div>
        </div>
      </el-form>

      <template #footer>
        <el-button @click="addOpen = false">Cancel</el-button>
        <el-button type="primary" @click="addDevice">Add Device</el-button>
      </template>
    </el-dialog>

    <!-- Device Drawer -->
    <el-drawer
      v-model="drawer"
      size="72%"
      :with-header="false"
      class="device-drawer"
    >
      <template v-if="selected">
        <div class="drawer-head">
          <div>
            <div class="drawer-title-row">
              <h2>{{ selected.device_id }}</h2>
              <el-tag :type="statusType(selectedVerify)">
                {{ statusLabel(selectedVerify) }}
              </el-tag>
            </div>
            <p>
              {{ selected.model }} ·
              {{ selectedModel?.protocol?.toUpperCase() }} ·
              {{ selected.host }}<template v-if="selected.port">:{{ selected.port }}</template>
            </p>
          </div>
          <el-button text @click="drawer = false">Close</el-button>
        </div>

        <el-tabs v-model="tab" class="drawer-tabs">
          <!-- CONFIG -->
          <el-tab-pane label="Config" name="Config">
            <div class="config-layout">
              <section class="panel-card">
                <div class="panel-head">
                  <div>
                    <h3>Basic Information</h3>
                    <p>设备实例、型号绑定与协议配置</p>
                  </div>
                  <el-button type="primary" @click="saveConfig">Update Config</el-button>
                </div>

                <el-form label-position="top" class="device-config-form">
                  <div class="config-section">
                    <div class="form-grid config-edit-grid">
                      <el-form-item label="Device ID">
                        <el-input v-model="editForm.device_id" />
                      </el-form-item>

                      <el-form-item label="Type">
                        <el-select v-model="editForm.device_type" style="width:100%" @change="onEditTypeChange">
                          <el-option v-for="t in store.deviceTypes" :key="t.id" :label="t.name" :value="t.id" />
                        </el-select>
                      </el-form-item>

                      <el-form-item label="Model ID">
                        <el-select v-model="editForm.model" style="width:100%" @change="onEditModelChange">
                          <el-option
                            v-for="m in store.deviceModels.filter(m => m.device_type === editForm.device_type)"
                            :key="m.id"
                            :label="m.id"
                            :value="m.id"
                          />
                        </el-select>
                      </el-form-item>

                      <el-form-item label="Manufacturer">
                        <el-input v-model="editForm.manufacturer" />
                      </el-form-item>

                      <el-form-item label="Hardware Model">
                        <el-input v-model="editForm.hardware_model" />
                      </el-form-item>

                      <el-form-item label="Group">
                        <el-select v-model="editForm.device_group" style="width:100%">
                          <el-option
                            v-for="g in store.deviceGroups.filter(g => g.device_type === editForm.device_type)"
                            :key="g.id"
                            :label="g.id"
                            :value="g.id"
                          />
                        </el-select>
                      </el-form-item>

                      <el-form-item label="Protocol">
                        <el-select v-model="editForm.protocol" style="width:100%" @change="onEditProtocolChange">
                          <el-option label="ADS" value="ads" />
                          <el-option label="Modbus" value="modbus" />
                          <el-option label="IEC 104" value="iec104" />
                        </el-select>
                      </el-form-item>

                      <el-form-item label="Point Table">
                        <el-select v-model="editForm.point_table" style="width:100%">
                          <el-option
                            v-for="t in store.pointTables.filter(t => t.protocol === editForm.protocol)"
                            :key="t.id"
                            :label="t.id"
                            :value="t.id"
                          />
                        </el-select>
                      </el-form-item>

                      <el-form-item label="Host">
                        <el-input v-model="editForm.host" />
                      </el-form-item>

                      <el-form-item label="Port">
                        <el-input-number v-model="editForm.port" :min="1" :max="65535" controls-position="right" style="width:100%" />
                      </el-form-item>

                      <el-form-item v-if="editForm.protocol === 'ads'" label="Read Mode">
                        <el-select v-model="editForm.read_mode" style="width:100%">
                          <el-option label="sum" value="sum" />
                          <el-option label="sequential" value="sequential" />
                        </el-select>
                      </el-form-item>
                    </div>
                  </div>

                  <div v-if="editForm.protocol === 'ads'" class="config-section">
                    <div class="config-section-title">
                      <b>ADS</b>
                      <span>endpoint.extensions</span>
                    </div>
                    <div class="form-grid config-edit-grid">
                      <el-form-item label="Target AMS Net ID">
                        <el-input v-model="editForm.target_net_id" />
                      </el-form-item>
                      <el-form-item label="ADS Port">
                        <el-input-number v-model="editForm.target_port" :min="1" :max="65535" controls-position="right" style="width:100%" />
                      </el-form-item>
                      <el-form-item label="TwinCAT Version">
                        <el-select v-model="editForm.twincat_version" style="width:100%">
                          <el-option label="TwinCAT 2" value="2" />
                          <el-option label="TwinCAT 3" value="3" />
                        </el-select>
                      </el-form-item>
                      <el-form-item label="Timeout (s)">
                        <el-input-number v-model="editForm.timeout" :min="0.1" :step="0.5" controls-position="right" style="width:100%" />
                      </el-form-item>
                    </div>
                  </div>

                  <div v-else-if="editForm.protocol === 'modbus'" class="config-section">
                    <div class="config-section-title">
                      <b>Modbus</b>
                      <span>endpoint.extensions</span>
                    </div>
                    <div class="form-grid config-edit-grid">
                      <el-form-item label="Unit ID">
                        <el-input-number v-model="editForm.unit_id" :min="0" :max="255" controls-position="right" style="width:100%" />
                      </el-form-item>
                      <el-form-item label="Mode">
                        <el-select v-model="editForm.mode" style="width:100%">
                          <el-option label="TCP" value="tcp" />
                          <el-option label="RTU" value="rtu" />
                        </el-select>
                      </el-form-item>
                      <el-form-item label="Timeout (s)">
                        <el-input-number v-model="editForm.timeout" :min="0.1" :step="0.5" controls-position="right" style="width:100%" />
                      </el-form-item>
                      <el-form-item label="Word Order">
                        <el-select v-model="editForm.word_order" style="width:100%">
                          <el-option label="Little endian" value="little_endian" />
                          <el-option label="Big endian" value="big_endian" />
                        </el-select>
                      </el-form-item>
                    </div>
                  </div>
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
                    @click="verifyDevice(selected)"
                  >
                    Verify Device
                  </el-button>
                </div>

                <div class="connectivity-list">
                  <div>
                    <span>Network</span>
                    <b :class="stepClass(selectedVerify.network)">
                      {{ stepIcon(selectedVerify.network) }}
                      {{ selected.host }} · {{ stepText(selectedVerify.network) }}
                    </b>
                  </div>
                  <div>
                    <span>Protocol</span>
                    <b :class="stepClass(selectedVerify.protocol)">
                      {{ stepIcon(selectedVerify.protocol) }}
                      {{ protocolDescription(selected) }}
                    </b>
                  </div>
                  <div>
                    <span>Point Table</span>
                    <b :class="stepClass(selectedVerify.points)">
                      {{ stepIcon(selectedVerify.points) }}
                      <template v-if="selectedVerify.point_total">
                        {{ selectedVerify.point_success }} / {{ selectedVerify.point_total }} passed
                      </template>
                      <template v-else>
                        {{ stepText(selectedVerify.points) }}
                      </template>
                    </b>
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
                <span v-if="selectedVerify.verified_at" class="subtle">
                  {{ selectedVerify.verified_at }}
                </span>
              </div>

              <div class="error-list">
                <div
                  v-for="(e, i) in selectedVerify.errors"
                  :key="`${e.stage}-${e.target}-${i}`"
                  class="error-item"
                >
                  <div class="error-item-head">
                    <b>{{ e.target }}</b>
                    <span>{{ e.stage }}</span>
                  </div>
                  <p>{{ e.message }}</p>
                </div>
              </div>
            </section>
          </el-tab-pane>

          <!-- DATA -->
          <el-tab-pane label="Data" name="Data">
            <div class="data-toolbar">
              <div>
                <h3>{{ dataRows.length }} points</h3>
                <p>变量实时值与最后更新时间</p>
              </div>
              <div class="data-tools">
                <el-input
                  v-model="dataSearch"
                  clearable
                  placeholder="Search variable..."
                />
                <el-select v-model="dataGroup">
                  <el-option label="All Groups" value="All" />
                  <el-option
                    v-for="g in store.pointGroups"
                    :key="g.id"
                    :label="g.name"
                    :value="g.id"
                  />
                </el-select>
              </div>
            </div>

            <div class="compact-data-grid">
              <article
                v-for="r in visibleData"
                :key="r.point_id"
                class="compact-data-item"
              >
                <div class="compact-data-top">
                  <b :title="r.variable_name || r.point_id">
                    {{ r.variable_name || r.point_id }}
                  </b>
                  <div class="compact-data-value">
                    <strong>{{ r.value }}</strong>
                    <span>{{ r.unit }}</span>
                  </div>
                </div>
                <time>{{ r.updated_at }}</time>
              </article>
            </div>
          </el-tab-pane>

          <!-- TREND -->
          <el-tab-pane label="Trend" name="Trend">
            <div class="trend-toolbar">
              <div>
                <h3>Trend</h3>
                <p>{{ trendSignals.length }} selected · 点击图例显示/隐藏曲线</p>
              </div>
              <div class="trend-actions">
                <el-button @click="trendPickerOpen = true">Select Signals</el-button>
                <el-segmented
                  v-model="trendRange"
                  :options="['Real-time', '5 min', '15 min', '1 h']"
                />
              </div>
            </div>

            <div
              v-if="!trendSignals.length"
              class="trend-empty"
            >
              No trend signals selected. Use “Select Signals” to add variables.
            </div>

            <div ref="trendChartEl" class="trend-chart"></div>
          </el-tab-pane>

          <!-- CONTROL -->
          <el-tab-pane label="Control" name="Control">
            <div class="control-layout">
              <section class="panel-card">
                <div class="panel-head">
                  <div>
                    <h3>Command</h3>
                    <p>选择命令点，写入目标值并执行回读</p>
                  </div>
                </div>

                <el-form label-position="top">
                  <el-form-item label="Command Point">
                    <el-select v-model="cmdPoint" filterable>
                      <el-option
                        v-for="r in controlCandidates"
                        :key="r.point_id"
                        :label="`${r.point_id} · ${r.variable_name}`"
                        :value="r.point_id"
                      />
                    </el-select>
                  </el-form-item>

                  <div class="control-current">
                    <span>Current</span>
                    <strong>
                      {{ currentControlRow?.value ?? '-' }}
                      {{ currentControlRow?.unit }}
                    </strong>
                    <small>{{ currentControlRow?.updated_at || '' }}</small>
                  </div>

                  <el-form-item label="Target Value">
                    <el-input-number
                      v-model="cmdValue"
                      :step="1"
                      controls-position="right"
                      style="width: 100%"
                    />
                  </el-form-item>

                  <el-button
                    type="primary"
                    :loading="sending"
                    style="width: 100%"
                    @click="sendCommand"
                  >
                    Send Command
                  </el-button>
                </el-form>
              </section>

              <section class="panel-card">
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
                  <div><span>Requested</span><b>{{ commandResult.requested }}</b></div>
                  <div><span>Sent</span><b>{{ commandResult.sentAt }}</b></div>
                  <div><span>Write</span><b class="step-success">✓ Success</b></div>
                  <div><span>Read back</span><b>{{ commandResult.readback }}</b></div>
                  <div>
                    <span>Difference</span>
                    <b>{{ (commandResult.readback - commandResult.requested).toFixed(2) }}</b>
                  </div>
                  <div><span>Latency</span><b>{{ commandResult.latency }} ms</b></div>
                </div>
              </section>
            </div>
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-drawer>

    <!-- Trend picker -->
    <el-dialog v-model="trendPickerOpen" title="Select Trend Signals" width="760px">
      <el-input
        v-model="trendSearch"
        clearable
        placeholder="Search point / variable / description..."
        class="trend-search"
      />

      <div class="trend-picker-list">
        <div
          v-for="r in trendCandidates"
          :key="r.point_id"
          class="trend-picker-row"
        >
          <div>
            <b>{{ r.point_id }}</b>
            <span>{{ r.variable_name }} · {{ r.groups.join(', ') }}</span>
          </div>
          <div class="trend-picker-value">
            <strong>{{ r.value }} {{ r.unit }}</strong>
            <el-button
              size="small"
              class="trend-select-button"
              :type="trendSignals.some(x => x.id === r.point_id) ? 'success' : 'default'"
              @click="toggleTrendSelection(r)"
            >
              {{ trendSignals.some(x => x.id === r.point_id) ? 'Selected' : 'Select' }}
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
