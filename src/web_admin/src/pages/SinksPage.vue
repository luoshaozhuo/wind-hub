<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useViewport } from '../composables/useViewport'
import { useConfigStore } from '../stores/config'
import type { SinkDef, SinkRuntimeState, SinkType } from '../domain/types'
import { nowText } from '../utils/format'
import { statusTagType } from '../utils/status'

const configStore = useConfigStore()

const { isMobile, isDesktop } = useViewport()
const drawerSize = computed(() =>
  isMobile.value ? '100%' : 'min(var(--app-drawer-width-md), 84vw)',
)
const search = ref('')
const typeFilter = ref<'All' | SinkType>('All')
const stateFilter = ref<'All' | SinkRuntimeState>('All')
const drawerOpen = ref(false)
const drawerTab = ref('Summary')
const creating = ref(false)
const selectedName = ref('')
const sinkSnapshot = ref('')
// Verify 与 Write Test 是两套独立操作状态；同一 Sink 上两者互斥，Verify All 与单 Sink 操作互斥，
// 避免交叉覆盖 runtime_state / verification。
const verifyingSink = ref('')
const writeTestingSink = ref('')
const verifyAllRunning = ref(false)
const sinkOperationActive = computed(
  () => verifyAllRunning.value || !!verifyingSink.value || !!writeTestingSink.value,
)
const batchVerification = ref<{
  checked_at: string
  checked: number
  passed: number
  failed: number
  warning: number
} | null>(null)
const testResult = ref<{ ok: boolean; title: string; detail: string; latency: number } | null>(null)

const selected = computed(() => configStore.sinks.find((s) => s.name === selectedName.value))
const rows = computed(() =>
  configStore.sinks.filter((s) => {
    const q = search.value.trim().toLowerCase()
    return (
      (!q || [s.name, s.type, endpointSummary(s)].some((x) => x.toLowerCase().includes(q))) &&
      (typeFilter.value === 'All' || s.type === typeFilter.value) &&
      (stateFilter.value === 'All' || s.runtime_state === stateFilter.value)
    )
  }),
)

const draft = reactive({
  name: '',
  type: 'file' as SinkType,
  enabled: true,
  bootstrap_servers: '',
  topic: '',
  key_field: '',
  compression_type: '',
  acks: 'all',
  kafka_batch_size: 16384,
  linger_ms: 0,
  dsn: '',
  table: 'points',
  db_batch_size: 1000,
  create_table: false,
  pool_min_size: 1,
  pool_max_size: 10,
  path: '',
  format: 'jsonl',
  max_size_mb: 100,
  max_age_hours: 24,
  compress: false,
  compress_level: 6,
  buffer_size: 100,
  flush_interval: 1,
  write_header: true,
  host: '0.0.0.0',
  server_port: 0,
  common_address: 1,
})
const sinkDraftState = computed(() => JSON.stringify(draft))
const sinkDirty = computed(() =>
  creating.value
    ? sinkDraftState.value !== sinkSnapshot.value
    : !!selected.value && sinkDraftState.value !== sinkSnapshot.value,
)

function stateLabel(s: SinkDef) {
  return !s.enabled
    ? 'Disabled'
    : s.runtime_state.charAt(0).toUpperCase() + s.runtime_state.slice(1)
}
function endpointSummary(s: SinkDef) {
  const p = s.connection
  if (s.type === 'kafka') return `${p.bootstrap_servers || '—'} · ${p.topic || '—'}`
  if (s.type === 'db') {
    const dsn = String(p.dsn || '')
    return `${dsn.replace(/:\/\/([^:]+):[^@]+@/, '://$1:***@') || '—'} · ${p.table || '—'}`
  }
  if (s.type === 'iec104' || s.type === 'modbus') return `${p.host || '0.0.0.0'}:${p.port || '—'}`
  return String(p.path || '—')
}
function taskRefs(name: string) {
  return configStore.tasks.filter((t) => t.sinks.includes(name))
}
function verifyLabel(s: SinkDef) {
  if (s.verification.state === 'never') return 'Never'
  return `${s.verification.state === 'passed' ? 'PASS' : 'FAIL'} · ${s.verification.passed}/${s.verification.total}`
}
function verifyType(s: SinkDef) {
  return statusTagType(s.verification.state)
}

function resetDraft(type: SinkType = 'file') {
  Object.assign(draft, {
    name: '',
    type,
    enabled: true,
    bootstrap_servers: '',
    topic: '',
    key_field: '',
    compression_type: '',
    acks: 'all',
    kafka_batch_size: 16384,
    linger_ms: 0,
    dsn: '',
    table: 'points',
    db_batch_size: 1000,
    create_table: false,
    pool_min_size: 1,
    pool_max_size: 10,
    path: '',
    format: 'jsonl',
    max_size_mb: 100,
    max_age_hours: 24,
    compress: false,
    compress_level: 6,
    buffer_size: 100,
    flush_interval: 1,
    write_header: true,
    host: '0.0.0.0',
    server_port: type === 'iec104' ? 2404 : type === 'modbus' ? 1502 : 0,
    common_address: 1,
  })
}
function loadDraft(s: SinkDef) {
  resetDraft(s.type)
  draft.name = s.name
  draft.enabled = s.enabled
  const p = s.connection
  if (s.type === 'kafka') {
    draft.bootstrap_servers = String(p.bootstrap_servers || '')
    draft.topic = String(p.topic || '')
    draft.key_field = String(p.key_field || '')
    draft.compression_type = String(p.compression_type || '')
    draft.acks = String(p.acks ?? 'all')
    draft.kafka_batch_size = Number(p.batch_size ?? 16384)
    draft.linger_ms = Number(p.linger_ms ?? 0)
  } else if (s.type === 'db') {
    draft.dsn = String(p.dsn || '')
    draft.table = String(p.table || 'points')
    draft.db_batch_size = Number(p.batch_size ?? 1000)
    draft.create_table = Boolean(p.create_table)
    draft.pool_min_size = Number(p.pool_min_size ?? 1)
    draft.pool_max_size = Number(p.pool_max_size ?? 10)
  } else if (s.type === 'iec104') {
    draft.host = String(p.host || '0.0.0.0')
    draft.server_port = Number(p.port ?? 2404)
    draft.common_address = Number(p.common_address ?? 1)
  } else if (s.type === 'modbus') {
    draft.host = String(p.host || '0.0.0.0')
    draft.server_port = Number(p.port ?? 1502)
  } else {
    draft.path = String(p.path || '')
    draft.format = String(p.format || 'jsonl')
    draft.max_size_mb = Number(p.max_size_mb ?? 100)
    draft.max_age_hours = Number(p.max_age_hours ?? 24)
    draft.compress = Boolean(p.compress)
    draft.compress_level = Number(p.compress_level ?? 6)
    draft.buffer_size = Number(p.buffer_size ?? 100)
    draft.flush_interval = Number(p.flush_interval ?? 1)
    draft.write_header = Boolean(p.write_header ?? true)
  }
  sinkSnapshot.value = JSON.stringify(draft)
}
function openSink(s: SinkDef) {
  selectedName.value = s.name
  creating.value = false
  drawerTab.value = 'Summary'
  testResult.value = null
  loadDraft(s)
  drawerOpen.value = true
}
function openAdd() {
  selectedName.value = ''
  creating.value = true
  drawerTab.value = 'Summary'
  testResult.value = null
  resetDraft('file')
  sinkSnapshot.value = JSON.stringify(draft)
  drawerOpen.value = true
}
function onTypeChange() {
  const name = draft.name,
    enabled = draft.enabled,
    type = draft.type
  resetDraft(type)
  draft.name = name
  draft.enabled = enabled
}
async function beforeSinkClose(done: () => void) {
  if (!sinkDirty.value) {
    done()
    return
  }
  try {
    await ElMessageBox.confirm('Discard unsaved Sink changes?', 'Unsaved Changes', {
      type: 'warning',
      confirmButtonText: 'Discard',
    })
    done()
  } catch {}
}
function connectionFromDraft(): Record<string, unknown> {
  if (draft.type === 'kafka')
    return {
      bootstrap_servers: draft.bootstrap_servers.trim(),
      topic: draft.topic.trim(),
      key_field: draft.key_field || undefined,
      compression_type: draft.compression_type || undefined,
      acks: draft.acks,
      batch_size: draft.kafka_batch_size,
      linger_ms: draft.linger_ms,
    }
  if (draft.type === 'db')
    return {
      dsn: draft.dsn.trim(),
      table: draft.table.trim(),
      batch_size: draft.db_batch_size,
      create_table: draft.create_table,
      pool_min_size: draft.pool_min_size,
      pool_max_size: draft.pool_max_size,
    }
  if (draft.type === 'iec104')
    return {
      host: draft.host.trim(),
      port: draft.server_port,
      common_address: draft.common_address,
    }
  if (draft.type === 'modbus') return { host: draft.host.trim(), port: draft.server_port }
  return {
    path: draft.path.trim(),
    format: draft.format,
    max_size_mb: draft.max_size_mb,
    max_age_hours: draft.max_age_hours,
    compress: draft.compress,
    compress_level: draft.compress_level,
    buffer_size: draft.buffer_size,
    flush_interval: draft.flush_interval,
    write_header: draft.write_header,
  }
}
function validateDraft() {
  if (!draft.name.trim()) return 'Sink name is required'
  if (creating.value && configStore.sinks.some((s) => s.name === draft.name.trim()))
    return 'Sink name already exists'
  if (draft.type === 'kafka' && (!draft.bootstrap_servers.trim() || !draft.topic.trim()))
    return 'Kafka bootstrap_servers and topic are required'
  if (draft.type === 'db' && (!draft.dsn.trim() || !draft.table.trim()))
    return 'PostgreSQL DSN and table are required'
  if (draft.type === 'db' && draft.pool_min_size > draft.pool_max_size)
    return 'Pool min size must be <= max size'
  if (draft.type === 'file' && !draft.path.trim()) return 'File path is required'
  if ((draft.type === 'iec104' || draft.type === 'modbus') && !draft.host.trim())
    return 'Listen host is required'
  if (
    (draft.type === 'iec104' || draft.type === 'modbus') &&
    (draft.server_port < 1 || draft.server_port > 65535)
  )
    return 'Listen port must be between 1 and 65535'
  return ''
}
async function saveSink() {
  const error = validateDraft()
  if (error) {
    ElMessage.error(error)
    return
  }
  const connection = connectionFromDraft()
  const wasCreating = creating.value
  if (creating.value) {
    const sink: SinkDef = {
      name: draft.name.trim(),
      type: draft.type,
      enabled: draft.enabled,
      connection,
      points: [],
      runtime_state: draft.enabled ? 'unknown' : 'disabled',
      last_test_at: '',
      last_write_at: '',
      latency_ms: 0,
      error: '',
      queue_depth: 0,
      writes_total: 0,
      failures_total: 0,
      dropped_points: 0,
      verification: { state: 'never', checked_at: '', passed: 0, total: 0, checks: [] },
    }
    configStore.mutate(() => {
      configStore.sinks.push(sink)
    })
    selectedName.value = sink.name
    creating.value = false
    loadDraft(sink)
  } else if (selected.value) {
    const refs = taskRefs(selected.value.name),
      running = refs.filter((t) => t.runtime === 'RUNNING')
    if (refs.length) {
      try {
        await ElMessageBox.confirm(
          '<b>Sink Change Impact</b><br><br>' +
            refs.length +
            ' Task(s) reference this Sink; ' +
            running.length +
            ' currently running.<br>The Sink instance will be reopened.',
          'Apply Sink Changes',
          { type: 'warning', confirmButtonText: 'Apply Changes', dangerouslyUseHTMLString: true },
        )
      } catch {
        return
      }
    }
    const target = selected.value
    configStore.mutate(() => {
      target.connection = connection
      target.enabled = draft.enabled
      target.runtime_state = draft.enabled ? 'unknown' : 'disabled'
      target.error = ''
    })
    loadDraft(target)
  }
  ElMessage.success(wasCreating ? 'Sink created ' : 'Sink configuration saved ')
}
// Verify / Write Test：检查链构建、成败判定、runtime_state/计数器更新与日志
// 全部在 backend service（§10）；页面只负责互斥守卫与结果展示。
async function verifySink(s: SinkDef) {
  if (sinkOperationActive.value) return
  verifyingSink.value = s.name
  try {
    await configStore.verifySink(s)
    if (s.verification.state === 'failed') ElMessage.error(`${s.name}: ${verifyLabel(s)}`)
    else ElMessage.success(`${s.name}: ${verifyLabel(s)}`)
  } finally {
    verifyingSink.value = ''
  }
}
async function verifyAll() {
  if (sinkOperationActive.value) return
  const targets = [...configStore.sinks]
  if (!targets.length) {
    ElMessage.warning('No Sinks configured')
    return
  }
  verifyAllRunning.value = true
  try {
    for (const s of targets) await configStore.verifySink(s)
    const failed = targets.filter((s) => s.verification.state === 'failed').length
    const warning = targets.filter((s) =>
      s.verification.checks.some((c) => c.state === 'warning'),
    ).length
    batchVerification.value = {
      checked_at: nowText(),
      checked: targets.length,
      passed: targets.length - failed,
      failed,
      warning,
    }
    if (failed) ElMessage.error(`Sink verification complete: ${failed} failed`)
    else if (warning) ElMessage.warning(`Sink verification complete: ${warning} warning`)
    else ElMessage.success('All Sink verifications passed')
  } finally {
    verifyAllRunning.value = false
  }
}
async function writeTest(s: SinkDef) {
  if (sinkOperationActive.value) return
  try {
    await ElMessageBox.confirm(
      'Write one synthetic PointValue to "' +
        s.name +
        '"? This test has a real side effect in the backend implementation.',
      'Write Test',
      { type: 'warning', confirmButtonText: 'Write Test' },
    )
  } catch {
    return
  }
  writeTestingSink.value = s.name
  try {
    testResult.value = await configStore.writeTestSink(s)
  } finally {
    writeTestingSink.value = ''
  }
}
async function toggleEnabled(s: SinkDef, enabled: boolean) {
  const refs = taskRefs(s.name)
  if (!enabled && refs.length) {
    try {
      await ElMessageBox.confirm(
        'Disabling "' + s.name + '" makes ' + refs.length + ' referencing Task(s) INVALID.',
        'Disable Sink',
        { type: 'warning', confirmButtonText: 'Disable' },
      )
    } catch {
      return
    }
  }
  configStore.mutate(() => {
    s.enabled = enabled
    s.runtime_state = enabled ? 'unknown' : 'disabled'
  })
}
async function deleteSink(s: SinkDef) {
  const refs = taskRefs(s.name)
  if (refs.length) {
    ElMessage.warning('Cannot delete: referenced by ' + refs.length + ' Task(s)')
    return
  }
  try {
    await ElMessageBox.confirm('Delete Sink "' + s.name + '"?', 'Delete Sink', {
      type: 'warning',
      confirmButtonText: 'Delete',
    })
  } catch {
    return
  }
  configStore.mutate(() => {
    configStore.sinks.splice(configStore.sinks.indexOf(s), 1)
  })
  drawerOpen.value = false
  ElMessage.success('Sink deleted ')
}
</script>

<template>
  <div class="sinks-page">
    <div class="head page-head">
      <div>
        <h1>Sinks</h1>
        <p>输出端配置、运行状态、连通性与写入测试</p>
      </div>
      <div class="head-actions">
        <el-button type="primary" @click="openAdd">+ Add Sink</el-button
        ><el-dropdown trigger="click"
          ><el-button :loading="verifyAllRunning">Actions</el-button
          ><template #dropdown
            ><el-dropdown-menu
              ><el-dropdown-item
                :disabled="sinkOperationActive || !configStore.sinks.length"
                title="Non-writing staged verification; Write Test is never included"
                @click="verifyAll"
                >Verify All Sinks</el-dropdown-item
              ></el-dropdown-menu
            ></template
          ></el-dropdown
        >
      </div>
    </div>

    <el-card shadow="never">
      <div class="sink-filter-row">
        <div class="sink-filters">
          <el-input v-model="search" clearable placeholder="Search name / endpoint..." /><el-select
            v-model="typeFilter"
            ><el-option label="All Types" value="All" /><el-option
              label="Kafka"
              value="kafka" /><el-option label="PostgreSQL" value="db" /><el-option
              label="File"
              value="file" /><el-option label="IEC104" value="iec104" /><el-option
              label="Modbus TCP"
              value="modbus" /></el-select
          ><el-select v-model="stateFilter"
            ><el-option label="All States" value="All" /><el-option
              label="Healthy"
              value="healthy" /><el-option label="Failed" value="failed" /><el-option
              label="Disabled"
              value="disabled" /><el-option label="Unknown" value="unknown"
          /></el-select>
        </div>
        <div v-if="batchVerification" class="batch-verify-summary">
          <span>Last Verification</span>
          <b
            >{{ batchVerification.checked }} checked · {{ batchVerification.passed }} passed ·
            {{ batchVerification.failed }} failed<template v-if="batchVerification.warning">
              · {{ batchVerification.warning }} warning</template
            ></b
          >
          <time>{{ batchVerification.checked_at }}</time>
        </div>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-table :data="rows" row-key="name">
        <el-table-column label="Sink" min-width="150"
          ><template #default="{ row }"
            ><el-button link @click="openSink(row)"
              ><b>{{ row.name }}</b></el-button
            ></template
          ></el-table-column
        >
        <el-table-column label="Type" width="110"
          ><template #default="{ row }">{{
            row.type === 'db' ? 'PostgreSQL' : row.type.toUpperCase()
          }}</template></el-table-column
        >
        <el-table-column v-if="isDesktop" label="Endpoint" min-width="220" show-overflow-tooltip
          ><template #default="{ row }">{{ endpointSummary(row) }}</template></el-table-column
        >
        <el-table-column label="Verification" width="125"
          ><template #default="{ row }"
            ><el-tag :type="verifyType(row)" size="small">{{ verifyLabel(row) }}</el-tag></template
          ></el-table-column
        >
        <el-table-column v-if="isDesktop" label="Last Verified" min-width="145"
          ><template #default="{ row }">{{
            row.verification.checked_at || 'Never'
          }}</template></el-table-column
        >
        <el-table-column label="State" width="105"
          ><template #default="{ row }"
            ><el-tag
              :type="statusTagType(row.enabled ? row.runtime_state : 'disabled')"
              size="small"
              >{{ stateLabel(row) }}</el-tag
            ></template
          ></el-table-column
        >
        <el-table-column v-if="isDesktop" label="Tasks" width="75" align="right"
          ><template #default="{ row }">{{ taskRefs(row.name).length }}</template></el-table-column
        >
        <el-table-column label="Enabled" width="95"
          ><template #default="{ row }"
            ><el-switch
              :model-value="row.enabled"
              @change="toggleEnabled(row, !!$event)" /></template
        ></el-table-column>
      </el-table>
    </el-card>

    <el-drawer
      v-model="drawerOpen"
      :title="creating ? 'Add Sink' : selected?.name || 'Sink'"
      direction="rtl"
      :size="drawerSize"
      append-to-body
      destroy-on-close
      :before-close="beforeSinkClose"
    >
      <template v-if="creating">
        <h3>Create Sink</h3>
        <p class="subtle">Choose a type and configure its required parameters.</p>
        <el-form label-position="top">
          <div class="sink-form-grid">
            <el-form-item label="Name"><el-input v-model="draft.name" /></el-form-item
            ><el-form-item label="Type"
              ><el-select v-model="draft.type" @change="onTypeChange" class="app-full-width"
                ><el-option label="Kafka" value="kafka" /><el-option
                  label="PostgreSQL"
                  value="db" /><el-option label="File" value="file" /><el-option
                  label="IEC104"
                  value="iec104" /><el-option
                  label="Modbus TCP"
                  value="modbus" /></el-select></el-form-item
            ><el-form-item label="Enabled"><el-switch v-model="draft.enabled" /></el-form-item>
          </div>
          <template v-if="draft.type === 'kafka'"
            ><div class="sink-form-grid">
              <el-form-item label="Bootstrap Servers"
                ><el-input
                  v-model="draft.bootstrap_servers"
                  placeholder="localhost:9092" /></el-form-item
              ><el-form-item label="Topic"><el-input v-model="draft.topic" /></el-form-item
              ><el-form-item label="Acks"><el-input v-model="draft.acks" /></el-form-item></div
          ></template>
          <template v-else-if="draft.type === 'db'"
            ><div class="sink-form-grid">
              <el-form-item label="DSN"
                ><el-input v-model="draft.dsn" type="password" show-password /></el-form-item
              ><el-form-item label="Table"><el-input v-model="draft.table" /></el-form-item
              ><el-form-item label="Batch Size"
                ><el-input-number
                  v-model="draft.db_batch_size"
                  :min="1"
                  class="app-full-width" /></el-form-item
              ><el-form-item label="Create Table"
                ><el-switch v-model="draft.create_table"
              /></el-form-item></div
          ></template>
          <template v-else-if="draft.type === 'iec104'"
            ><div class="sink-form-grid">
              <el-form-item label="Listen Host"><el-input v-model="draft.host" /></el-form-item
              ><el-form-item label="Port"
                ><el-input-number
                  v-model="draft.server_port"
                  :min="1"
                  :max="65535"
                  class="app-full-width" /></el-form-item
              ><el-form-item label="Common Address"
                ><el-input-number
                  v-model="draft.common_address"
                  :min="0"
                  :max="65535"
                  class="app-full-width"
              /></el-form-item></div
          ></template>
          <template v-else-if="draft.type === 'modbus'"
            ><div class="sink-form-grid">
              <el-form-item label="Listen Host"><el-input v-model="draft.host" /></el-form-item
              ><el-form-item label="Port"
                ><el-input-number
                  v-model="draft.server_port"
                  :min="1"
                  :max="65535"
                  class="app-full-width"
              /></el-form-item></div
          ></template>
          <template v-else
            ><div class="sink-form-grid">
              <el-form-item label="Path"
                ><el-input
                  v-model="draft.path"
                  placeholder="/var/tmp/wind-hub/archive.jsonl" /></el-form-item
              ><el-form-item label="Format"
                ><el-select v-model="draft.format" class="app-full-width"
                  ><el-option label="JSONL" value="jsonl" /><el-option
                    label="CSV"
                    value="csv" /></el-select></el-form-item
              ><el-form-item label="Max Size (MB)"
                ><el-input-number
                  v-model="draft.max_size_mb"
                  :min="0"
                  class="app-full-width" /></el-form-item
              ><el-form-item label="Flush Interval"
                ><el-input-number
                  v-model="draft.flush_interval"
                  :min="0"
                  :step="0.1"
                  class="app-full-width"
              /></el-form-item></div
          ></template>
        </el-form>
        <div class="editor-actions">
          <el-button type="primary" @click="saveSink">Create Sink</el-button>
        </div>
      </template>

      <el-tabs v-else v-model="drawerTab">
        <el-tab-pane label="Summary" name="Summary">
          <div class="sink-summary-grid" v-if="selected">
            <section class="sink-editor-card">
              <div class="section-head">
                <div>
                  <h3>Parameters</h3>
                  <p>Effective configuration is directly editable.</p>
                </div>
                <el-button type="primary" :disabled="!sinkDirty" @click="saveSink">Save</el-button>
              </div>
              <el-form label-position="top">
                <div class="sink-form-grid">
                  <el-form-item label="Name"
                    ><el-input v-model="draft.name" disabled /></el-form-item
                  ><el-form-item label="Type"
                    ><el-input
                      :model-value="draft.type === 'db' ? 'PostgreSQL' : draft.type.toUpperCase()"
                      disabled /></el-form-item
                  ><el-form-item label="Enabled"
                    ><el-switch v-model="draft.enabled"
                  /></el-form-item>
                </div>
                <template v-if="draft.type === 'kafka'"
                  ><div class="sink-form-grid">
                    <el-form-item label="Bootstrap Servers"
                      ><el-input v-model="draft.bootstrap_servers" /></el-form-item
                    ><el-form-item label="Topic"><el-input v-model="draft.topic" /></el-form-item
                    ><el-form-item label="Key Field"
                      ><el-input v-model="draft.key_field" /></el-form-item
                    ><el-form-item label="Compression"
                      ><el-input v-model="draft.compression_type" /></el-form-item
                    ><el-form-item label="Acks"><el-input v-model="draft.acks" /></el-form-item
                    ><el-form-item label="Batch Size"
                      ><el-input-number
                        v-model="draft.kafka_batch_size"
                        :min="1"
                        class="app-full-width" /></el-form-item
                    ><el-form-item label="Linger (ms)"
                      ><el-input-number v-model="draft.linger_ms" :min="0" class="app-full-width"
                    /></el-form-item></div
                ></template>
                <template v-else-if="draft.type === 'db'"
                  ><div class="sink-form-grid">
                    <el-form-item label="DSN"
                      ><el-input v-model="draft.dsn" type="password" show-password /></el-form-item
                    ><el-form-item label="Table"><el-input v-model="draft.table" /></el-form-item
                    ><el-form-item label="Batch Size"
                      ><el-input-number
                        v-model="draft.db_batch_size"
                        :min="1"
                        class="app-full-width" /></el-form-item
                    ><el-form-item label="Create Table"
                      ><el-switch v-model="draft.create_table" /></el-form-item
                    ><el-form-item label="Pool Min"
                      ><el-input-number
                        v-model="draft.pool_min_size"
                        :min="1"
                        class="app-full-width" /></el-form-item
                    ><el-form-item label="Pool Max"
                      ><el-input-number
                        v-model="draft.pool_max_size"
                        :min="1"
                        class="app-full-width"
                    /></el-form-item></div
                ></template>
                <template v-else-if="draft.type === 'iec104'"
                  ><div class="sink-form-grid">
                    <el-form-item label="Listen Host"
                      ><el-input v-model="draft.host" /></el-form-item
                    ><el-form-item label="Port"
                      ><el-input-number
                        v-model="draft.server_port"
                        :min="1"
                        :max="65535"
                        class="app-full-width" /></el-form-item
                    ><el-form-item label="Common Address"
                      ><el-input-number
                        v-model="draft.common_address"
                        :min="0"
                        :max="65535"
                        class="app-full-width"
                    /></el-form-item></div
                ></template>
                <template v-else-if="draft.type === 'modbus'"
                  ><div class="sink-form-grid">
                    <el-form-item label="Listen Host"
                      ><el-input v-model="draft.host" /></el-form-item
                    ><el-form-item label="Port"
                      ><el-input-number
                        v-model="draft.server_port"
                        :min="1"
                        :max="65535"
                        class="app-full-width"
                    /></el-form-item></div
                ></template>
                <template v-else
                  ><div class="sink-form-grid">
                    <el-form-item label="Path"><el-input v-model="draft.path" /></el-form-item
                    ><el-form-item label="Format"
                      ><el-select v-model="draft.format" class="app-full-width"
                        ><el-option label="JSONL" value="jsonl" /><el-option
                          label="CSV"
                          value="csv" /></el-select></el-form-item
                    ><el-form-item label="Max Size (MB)"
                      ><el-input-number
                        v-model="draft.max_size_mb"
                        :min="0"
                        class="app-full-width" /></el-form-item
                    ><el-form-item label="Max Age (h)"
                      ><el-input-number
                        v-model="draft.max_age_hours"
                        :min="0"
                        class="app-full-width" /></el-form-item
                    ><el-form-item label="Compress"
                      ><el-switch v-model="draft.compress" /></el-form-item
                    ><el-form-item label="Compression Level"
                      ><el-input-number
                        v-model="draft.compress_level"
                        :min="1"
                        :max="9"
                        class="app-full-width" /></el-form-item
                    ><el-form-item label="Buffer Size"
                      ><el-input-number
                        v-model="draft.buffer_size"
                        :min="1"
                        class="app-full-width" /></el-form-item
                    ><el-form-item label="Flush Interval"
                      ><el-input-number
                        v-model="draft.flush_interval"
                        :min="0"
                        :step="0.1"
                        class="app-full-width"
                    /></el-form-item></div
                ></template>
              </el-form>
            </section>
            <aside class="sink-runtime-card">
              <h3>Runtime</h3>
              <div class="runtime-list">
                <div>
                  <span>State</span><b>{{ stateLabel(selected) }}</b>
                </div>
                <div>
                  <span>Referenced Tasks</span><b>{{ taskRefs(selected.name).length }}</b>
                </div>
                <div>
                  <span>Queue Depth</span><b>{{ selected.queue_depth }}</b>
                </div>
                <div>
                  <span>Last Write</span><b>{{ selected.last_write_at || 'Never' }}</b>
                </div>
                <div>
                  <span>Writes</span><b>{{ selected.writes_total }}</b>
                </div>
                <div>
                  <span>Failures</span><b>{{ selected.failures_total }}</b>
                </div>
                <div>
                  <span>Dropped</span><b>{{ selected.dropped_points }}</b>
                </div>
              </div>
              <div class="danger-row">
                <el-button type="danger" plain @click="deleteSink(selected)">Delete Sink</el-button>
              </div>
            </aside>
          </div>
        </el-tab-pane>

        <el-tab-pane label="Test" name="Test" v-if="selected">
          <div class="section-head">
            <div>
              <h3>Connection Verification</h3>
              <p>
                No business data is written. The same staged checks are used by Verify All Sinks.
              </p>
            </div>
            <el-button
              type="primary"
              :loading="verifyingSink === selected.name"
              :disabled="verifyAllRunning || writeTestingSink === selected.name"
              @click="verifySink(selected)"
              >Verify</el-button
            >
          </div>
          <el-table
            :data="selected.verification.checks"
            size="small"
            empty-text="Not verified yet"
            table-layout="fixed"
          >
            <el-table-column label="Check" width="190">
              <template #default="{ row }">
                <div class="verify-check">
                  <span>{{ row.layer }}</span
                  ><b>{{ row.name }}</b>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="Result" width="100">
              <template #default="{ row }">
                <el-tag :type="statusTagType(row.state)" size="small">{{ row.state }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="Evidence" min-width="260">
              <template #default="{ row }">
                <div class="verify-evidence">
                  <b>{{ row.target || '—' }}</b>
                  <span>{{
                    row.error_code ? row.error_code + ' · ' + row.detail : row.detail
                  }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="Time" width="90" align="right">
              <template #default="{ row }">{{
                row.latency_ms ? row.latency_ms + ' ms' : '—'
              }}</template>
            </el-table-column>
          </el-table>
          <div class="test-meta">
            Last verified: {{ selected.verification.checked_at || 'Never' }} ·
            {{ verifyLabel(selected) }}
          </div>
          <el-divider content-position="left">Write Test</el-divider>
          <el-alert
            type="warning"
            :closable="false"
            title="Write Test sends one synthetic PointValue and therefore has a real side effect."
          />
          <el-button
            class="app-mt-3"
            :loading="writeTestingSink === selected.name"
            :disabled="verifyAllRunning || verifyingSink === selected.name"
            @click="writeTest(selected)"
            >Write Test</el-button
          >
          <el-result
            v-if="testResult"
            :icon="testResult.ok ? 'success' : 'error'"
            :title="testResult.title"
            :sub-title="testResult.detail"
          />
        </el-tab-pane>
      </el-tabs>
    </el-drawer>
  </div>
</template>

<style scoped>
.sink-filter-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--app-space-4);
}
.sink-filters {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 0.45fr) minmax(0, 0.45fr);
  gap: var(--app-space-3);
  flex: 1;
}
.batch-verify-summary {
  display: grid;
  gap: var(--app-space-1);
  text-align: right;
}
.batch-verify-summary span,
.batch-verify-summary time {
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.batch-verify-summary b {
  font-size: var(--app-font-body);
  font-weight: var(--app-font-weight-semibold);
}
.sink-summary-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.45fr) minmax(0, 0.55fr);
  gap: var(--app-space-4);
}
.sink-editor-card,
.sink-runtime-card {
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-card-radius);
  padding: var(--app-space-4);
  min-width: 0;
}
.section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--app-space-3);
  margin-bottom: var(--app-space-4);
}
.section-head h3,
.sink-runtime-card h3 {
  margin: 0;
}
.section-head p {
  margin: var(--app-space-1) 0 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.sink-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 var(--app-space-4);
}
.runtime-list {
  display: grid;
  gap: var(--app-space-2);
}
.runtime-list > div {
  display: flex;
  justify-content: space-between;
  gap: var(--app-space-3);
  padding: var(--app-space-2) 0;
  border-bottom: 1px solid var(--app-border-soft);
}
.runtime-list span {
  color: var(--app-text-muted);
}
.runtime-list b {
  text-align: right;
}
.danger-row {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--app-space-4);
}
.verify-check,
.verify-evidence {
  display: grid;
  gap: var(--app-space-1);
  min-width: 0;
}
.verify-check span {
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
  text-transform: capitalize;
}
.verify-check b {
  font-size: var(--app-font-body);
  font-weight: var(--app-font-weight-semibold);
}
.verify-evidence b {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--app-font-label);
  font-weight: var(--app-font-weight-medium);
}
.verify-evidence span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.test-meta {
  margin-top: var(--app-space-3);
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.editor-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--app-space-4);
}
@media (max-width: 1199px) {
  .sink-summary-grid {
    grid-template-columns: 1fr;
  }
  .sink-filter-row {
    align-items: stretch;
    flex-direction: column;
  }
  .batch-verify-summary {
    text-align: left;
  }
}
@media (max-width: 767px) {
  .sink-filters,
  .sink-form-grid {
    grid-template-columns: 1fr;
  }
  .section-head {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
