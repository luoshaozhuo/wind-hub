<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useConfigStore } from '../stores/config'
import type { SinkDef, SinkType } from '../domain/types'
import { statusTagType } from '../utils/status'

const configStore = useConfigStore()
const filter = ref('')
const drawerOpen = ref(false)
const creating = ref(false)
const busy = ref(false)
const selectedName = ref('')
const selected = computed(() => configStore.sinks.find((sink) => sink.name === selectedName.value))
const sinks = computed(() =>
  configStore.sinks.filter((sink) =>
    [sink.name, sink.type].some((text) => text.toLowerCase().includes(filter.value.toLowerCase())),
  ),
)

const draft = reactive({
  name: '',
  type: 'file' as SinkType,
  enabled: true,
  path: '',
  maxSizeMb: 100,
  retentionDays: 30,
  host: 'localhost',
  port: 6379,
  database: 0,
  password: '',
  keyPrefix: 'wind-hub',
  })
const snapshot = ref('')
const changed = computed(() => JSON.stringify(draft) !== snapshot.value)

function defaults(type: SinkType) {
  return type === 'file'
    ? { path: '', max_size_mb: 100, retention_days: 30 }
    : type === 'redis'
      ? { host: 'localhost', port: 6379, database: 0, key_prefix: 'wind-hub' }
      : { host: '0.0.0.0', port: 502 }
}

function reset(type: SinkType = 'file') {
  const settings = defaults(type)
  Object.assign(draft, {
    name: '',
    type,
    enabled: true,
    path: '',
    maxSizeMb: 100,
    retentionDays: 30,
    host: String('host' in settings ? settings.host : 'localhost'),
    port: Number('port' in settings ? settings.port : 6379),
    database: 0,
    password: '',
    keyPrefix: 'wind-hub',
      })
  snapshot.value = JSON.stringify(draft)
}

function load(sink: SinkDef) {
  reset(sink.type)
  const connection = sink.connection
  Object.assign(draft, {
    name: sink.name,
    enabled: sink.enabled,
    path: String(connection.path ?? ''),
    maxSizeMb: Number(connection.max_size_mb ?? 100),
    retentionDays: Number(connection.retention_days ?? 30),
    host: String(connection.host ?? 'localhost'),
    port: Number(connection.port ?? (sink.type === 'redis' ? 6379 : 502)),
    database: Number(connection.database ?? 0),
    password: '',
    keyPrefix: String(connection.key_prefix ?? 'wind-hub'),
  })
  snapshot.value = JSON.stringify(draft)
}

function openCreate() {
  creating.value = true
  selectedName.value = ''
  reset()
  drawerOpen.value = true
}

function openEdit(sink: SinkDef) {
  creating.value = false
  selectedName.value = sink.name
  load(sink)
  drawerOpen.value = true
}

function changeType() {
  const name = draft.name
  reset(draft.type)
  draft.name = name
}

function connection(): Record<string, unknown> {
  if (draft.type === 'file') {
    return {
      path: draft.path.trim(),
      max_size_mb: draft.maxSizeMb,
      retention_days: draft.retentionDays,
    }
  }
  if (draft.type === 'redis') {
    const old = !creating.value ? selected.value?.connection : undefined
    return {
      host: draft.host.trim(),
      port: draft.port,
      database: draft.database,
      key_prefix: draft.keyPrefix.trim(),
      ...(!creating.value && !draft.password ? { password: old?.password } : {}),
      ...(draft.password ? { password: draft.password } : {}),
    }
  }
  return { host: draft.host.trim(), port: draft.port }
}

function validate() {
  if (!draft.name.trim()) return 'Sink ID 不能为空'
  if (creating.value && configStore.sinks.some((s) => s.name === draft.name.trim()))
    return 'Sink ID 已存在'
  if (draft.type === 'file' && (!draft.path.trim() || draft.maxSizeMb <= 0 || draft.retentionDays < 1))
    return '文件归档目录、容量和保留天数必须有效'
  if (draft.type !== 'file' && (!draft.host.trim() || draft.port < 1 || draft.port > 65535))
    return '主机和端口必须有效'
  if (draft.type === 'redis' && (draft.database < 0 || !draft.keyPrefix.trim()))
    return 'Redis 数据库和键前缀必须有效'
  return ''
}

function save() {
  const issue = validate()
  if (issue) {
    ElMessage.error(issue)
    return
  }
  const values = connection()
  if (creating.value) {
    const sink: SinkDef = {
      name: draft.name.trim(),
      type: draft.type,
      enabled: draft.enabled,
      connection: values,
      runtime_state: 'unknown',
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
    configStore.mutate(() => configStore.sinks.push(sink))
  } else if (selected.value) {
    const sink = selected.value
    configStore.mutate(() => {
      sink.enabled = draft.enabled
      sink.connection = values
    })
  }
  drawerOpen.value = false
  ElMessage.success('已提交 Sink 配置修改')
}

async function verify(sink: SinkDef) {
  if (busy.value) return
  busy.value = true
  try {
    await configStore.verifySink(sink)
  } finally {
    busy.value = false
  }
}

async function testWrite(sink: SinkDef) {
  if (busy.value) return
  busy.value = true
  try {
    await configStore.writeTestSink(sink)
  } finally {
    busy.value = false
  }
}

async function remove(sink: SinkDef) {
  if (configStore.tasks.some((task) => task.sinks.includes(sink.name))) {
    ElMessage.error('仍有 Task 引用该 Sink，不能删除')
    return
  }
  try {
    await ElMessageBox.confirm(`删除 Sink ${sink.name}？`, '确认删除', { type: 'warning' })
  } catch {
    return
  }
  configStore.mutate(() =>
    configStore.sinks.splice(
      configStore.sinks.findIndex((item) => item.name === sink.name),
      1,
    ),
  )
  drawerOpen.value = false
}
</script>

<template>
  <div class="app-page">
    <el-row justify="space-between" align="middle" :gutter="16">
      <el-col :xs="24" :sm="12">
        <h1>Sinks</h1>
        <p>配置文件、Redis 和 Modbus 输出目标；状态由 Collector 返回。</p>
      </el-col>
      <el-col :xs="24" :sm="12">
        <el-row justify="end" :gutter="8">
          <el-col :xs="15" :sm="14"
            ><el-input v-model="filter" clearable placeholder="搜索 Sink"
          /></el-col>
          <el-col :xs="9" :sm="10"
            ><el-button type="primary" @click="openCreate">新增 Sink</el-button></el-col
          >
        </el-row>
      </el-col>
    </el-row>

    <el-card shadow="never">
      <el-table :data="sinks" row-key="name">
        <el-table-column prop="name" label="Sink" min-width="140">
          <template #default="{ row }">
            <el-button link @click="openEdit(row)">{{ row.name }}</el-button>
          </template>
        </el-table-column>
        <el-table-column prop="type" label="类型" width="100" />
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.enabled ? row.runtime_state : 'disabled')">
              {{ row.enabled ? row.runtime_state : 'disabled' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="queue_depth" label="队列深度" width="110" />
        <el-table-column label="操作" min-width="210">
          <template #default="{ row }">
            <el-space wrap>
              <el-button link :disabled="busy" @click="verify(row)">验证</el-button>
              <el-button link :disabled="busy" @click="testWrite(row)">测试写入</el-button>
              <el-button link type="danger" :disabled="busy" @click="remove(row)">删除</el-button>
            </el-space>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer
      v-model="drawerOpen"
      :title="creating ? '新增 Sink' : '编辑 Sink'"
      size="min(480px, 95vw)"
    >
      <el-form label-position="top">
        <el-form-item label="Sink ID">
          <el-input v-model="draft.name" :disabled="!creating" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="draft.type" :disabled="!creating" @change="changeType">
            <el-option label="文件 CSV" value="file" />
            <el-option label="Redis" value="redis" />
            <el-option label="Modbus TCP" value="modbus" />
          </el-select>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="draft.enabled" />
        </el-form-item>
        <template v-if="draft.type === 'file'">
          <el-form-item label="目录或 .csv 文件路径"
            ><el-input v-model="draft.path"
          /></el-form-item>
          <el-form-item label="最大容量 (MB)">
            <el-input-number v-model="draft.maxSizeMb" :min="0.1" />
          </el-form-item>
          <el-form-item label="最多保留文件数">
            <el-input-number v-model="draft.retentionDays" :min="1" :step="1" />
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item label="Host"><el-input v-model="draft.host" /></el-form-item>
          <el-form-item label="Port">
            <el-input-number v-model="draft.port" :min="1" :max="65535" />
          </el-form-item>
          <template v-if="draft.type === 'redis'">
            <el-form-item label="Database">
              <el-input-number v-model="draft.database" :min="0" />
            </el-form-item>
            <el-form-item label="Key Prefix"><el-input v-model="draft.keyPrefix" /></el-form-item>
            <el-form-item label="Password（不填写则保持已有密码）">
              <el-input
                v-model="draft.password"
                type="password"
                show-password
                autocomplete="new-password"
              />
            </el-form-item>
          </template>
        </template>
        
      </el-form>
      <template #footer>
        <el-space>
          <el-button @click="drawerOpen = false">取消</el-button>
          <el-button type="primary" :disabled="!creating && !changed" @click="save">保存</el-button>
        </el-space>
      </template>
    </el-drawer>
  </div>
</template>
