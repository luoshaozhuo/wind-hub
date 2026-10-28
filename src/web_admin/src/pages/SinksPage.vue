<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useConfigStore } from '../stores/config'
import type { SinkDef, SinkType } from '../domain/types'
import { statusTagType } from '../utils/status'
import { manageSink, type SinkOperation } from '../api/sinks'
import { queryClient } from '../api/queryClient'
import { qk } from '../api/queryKeys'

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
  type: 'redis' as SinkType,
  enabled: true,
  host: 'localhost',
  port: 6379,
  database: 0,
  username: '',
  password: '',
  clearPassword: false,
  timeoutSec: 1.0,
  keyPrefix: 'wind-hub',
  stable: 'point_values',
  tdDatabase: 'wind_hub',
  tls: false,
})
const snapshot = ref('')
const changed = computed(() => JSON.stringify(draft) !== snapshot.value)

function defaults(type: SinkType) {
  return type === 'redis'
    ? { host: 'localhost', port: 6379 }
    : type === 'tdengine'
      ? { host: 'localhost', port: 6041 }
      : { host: '0.0.0.0', port: 502 }
}

function reset(type: SinkType = 'redis') {
  const settings = defaults(type)
  Object.assign(draft, {
    name: '',
    type,
    enabled: true,
    host: String('host' in settings ? settings.host : 'localhost'),
    port: Number('port' in settings ? settings.port : 6379),
    database: 0,
    username: '',
    password: '',
    clearPassword: false,
    timeoutSec: 1.0,
    keyPrefix: 'wind-hub',
    stable: 'point_values',
    tdDatabase: 'wind_hub',
    tls: false,
  })
  snapshot.value = JSON.stringify(draft)
}

function load(sink: SinkDef) {
  reset(sink.type)
  const connection = sink.connection
  Object.assign(draft, {
    name: sink.name,
    enabled: sink.enabled,
    host: String(connection.host ?? 'localhost'),
    port: Number(
      connection.port ?? (sink.type === 'redis' ? 6379 : sink.type === 'tdengine' ? 6041 : 502),
    ),
    database: Number(connection.database ?? 0),
    username: String(connection.username ?? ''),
    password: '',
    clearPassword: false,
    timeoutSec: Number(connection.timeout ?? 1.0),
    keyPrefix: String(connection.key_prefix ?? 'wind-hub'),
    tdDatabase: String(connection.database ?? 'wind_hub'),
    stable: String(connection.stable ?? 'point_values'),
    tls: Boolean(connection.tls ?? false),
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
  if (draft.type === 'redis') {
    const connection: Record<string, unknown> = {
      host: draft.host.trim(),
      port: draft.port,
      database: draft.database,
      timeout: draft.timeoutSec,
      key_prefix: draft.keyPrefix.trim(),
    }
    // username 非密文，后端正常回显；为空表示不启用 ACL。
    if (draft.username.trim()) connection.username = draft.username.trim()
    if (draft.password) {
      connection.password = draft.password
    } else if (!creating.value && draft.clearPassword) {
      // 编辑态显式清空："" 让后端移除凭据（与"不回传=保留"区分）。
      connection.password = ''
    }
    return connection
  }
  if (draft.type === 'tdengine') {
    return {
      host: draft.host.trim(),
      port: draft.port,
      database: draft.tdDatabase.trim(),
      stable: draft.stable.trim(),
      username: draft.username.trim(),
      timeout: draft.timeoutSec,
      tls: draft.tls,
      ...(draft.password ? { password: draft.password } : {}),
    }
  }
  return { host: draft.host.trim(), port: draft.port }
}

function validate() {
  if (!draft.name.trim()) return 'Sink ID 不能为空'
  if (creating.value && configStore.sinks.some((s) => s.name === draft.name.trim()))
    return 'Sink ID 已存在'
  if (!draft.host.trim() || draft.port < 1 || draft.port > 65535) return '主机和端口必须有效'
  if (
    draft.type === 'tdengine' &&
    (!draft.tdDatabase.trim() || !draft.stable.trim() || !draft.username.trim())
  )
    return 'TDengine 数据库、超级表和用户名不能为空'
  if (draft.type === 'tdengine' && creating.value && !draft.password)
    return '新增 TDengine Sink 必须提供密码'
  if (draft.type === 'redis' && (draft.database < 0 || !draft.keyPrefix.trim()))
    return 'Redis 数据库和键前缀必须有效'
  if (draft.type === 'redis' && draft.timeoutSec <= 0) return 'Redis 超时时间必须大于 0 秒'
  if (draft.type === 'redis' && draft.username.trim() && creating.value && !draft.password)
    return '新建 ACL Redis Sink 时，设置用户名必须同时提供密码'
  if (
    draft.type === 'redis' &&
    !creating.value &&
    draft.clearPassword &&
    !draft.password &&
    draft.username.trim()
  )
    return '清空密码时必须同时移除 ACL 用户名（或改填新密码）'
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
      has_password: draft.type !== 'modbus' && Boolean(draft.password),
    }
    configStore.mutate(() => configStore.sinks.push(sink))
  } else if (selected.value) {
    const sink = selected.value
    configStore.mutate(() => {
      sink.enabled = draft.enabled
      sink.connection = values
      if (draft.type === 'redis' || draft.type === 'tdengine') {
        sink.has_password = Boolean(draft.password) || (!draft.clearPassword && sink.has_password)
      }
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

async function controlSink(sink: SinkDef, operation: SinkOperation) {
  if (busy.value) return
  if (operation === 'clear') {
    try {
      await ElMessageBox.confirm(
        `清空 Sink ${sink.name} 的独立消费积压？请先停止此 Sink。`,
        '确认清空',
        { type: 'warning' },
      )
    } catch {
      return
    }
  }
  busy.value = true
  try {
    await manageSink(sink.name, operation)
    await queryClient.invalidateQueries({ queryKey: qk.sinks() })
    ElMessage.success(`Sink ${sink.name}: ${operation} 已执行`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
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
        <p>配置 Redis、TDengine 和 Modbus 输出目标；状态由独立 Sink Service 返回。</p>
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
              <el-button link :disabled="busy" @click="controlSink(row, 'start')">启动</el-button>
              <el-button link :disabled="busy" @click="controlSink(row, 'stop')">停止</el-button>
              <el-button link :disabled="busy" @click="controlSink(row, 'restart')">重启</el-button>
              <el-button link :disabled="busy" @click="controlSink(row, 'clear')">清空</el-button>
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
            <el-option label="Redis" value="redis" />
            <el-option label="Modbus TCP" value="modbus" />
            <el-option label="TDengine" value="tdengine" />
          </el-select>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="draft.enabled" />
        </el-form-item>
        <el-form-item label="Host"><el-input v-model="draft.host" /></el-form-item>
        <el-form-item label="Port">
          <el-input-number v-model="draft.port" :min="1" :max="65535" />
        </el-form-item>
        <template v-if="draft.type === 'tdengine'">
          <el-form-item label="Database"><el-input v-model="draft.tdDatabase" /></el-form-item>
          <el-form-item label="Stable"><el-input v-model="draft.stable" /></el-form-item>
          <el-form-item label="Username"><el-input v-model="draft.username" /></el-form-item>
          <el-form-item label="Password（留空保持已有密码）">
            <el-input
              v-model="draft.password"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="!creating && selected?.has_password ? '已保存密码，留空保持不变' : ''"
            />
          </el-form-item>
          <el-form-item label="超时（秒）">
            <el-input-number v-model="draft.timeoutSec" :min="0.1" :step="0.1" />
          </el-form-item>
          <el-form-item label="TLS"><el-switch v-model="draft.tls" /></el-form-item>
        </template>
        <template v-if="draft.type === 'redis'">
          <el-form-item label="Database">
            <el-input-number v-model="draft.database" :min="0" />
          </el-form-item>
          <el-form-item label="超时（秒）">
            <el-input-number v-model="draft.timeoutSec" :min="0.1" :step="0.1" />
          </el-form-item>
          <el-form-item label="Key Prefix"><el-input v-model="draft.keyPrefix" /></el-form-item>
          <el-form-item label="Username（ACL，可空）">
            <el-input v-model="draft.username" autocomplete="off" />
          </el-form-item>
          <el-form-item label="Password（不填写则保持已有密码；设置用户名时必填）">
            <el-input
              v-model="draft.password"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="!creating && selected?.has_password ? '已保存密码，留空保持不变' : ''"
            />
            <div v-if="!creating && selected?.has_password" class="password-clear">
              <el-checkbox v-model="draft.clearPassword" :disabled="Boolean(draft.password)">
                清空已保存的密码
              </el-checkbox>
            </div>
          </el-form-item>
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

<style scoped>
.password-clear {
  margin-top: var(--app-space-1);
}
</style>
