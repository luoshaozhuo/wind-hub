<script setup lang="ts">
import { ref } from 'vue'
import { runtimeApi } from '../../api/runtime'

const props = defineProps<{ deviceId: string }>()
const pointIds = ref('')
const loading = ref(false)
const error = ref('')
const response = ref<Record<string, unknown> | null>(null)

async function query() {
  const ids = [...new Set(pointIds.value.split(/[\s,，;；]+/).map(s => s.trim()).filter(Boolean))]
  if (!ids.length || loading.value) return
  loading.value = true
  error.value = ''
  response.value = null
  try {
    response.value = await runtimeApi.pointMetadata(props.deviceId, ids)
  } catch (cause) {
    error.value = String(cause)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <section>
    <el-alert type="info" :closable="false" title="从 Collector 当前激活的配置读取测点元数据；只读，不修改点表。" />
    <el-form label-position="top" style="margin-top: 16px">
      <el-form-item label="测点 ID（逗号或换行分隔）">
        <el-input v-model="pointIds" type="textarea" :rows="3" placeholder="point_1, point_2" />
      </el-form-item>
      <el-button :loading="loading" :disabled="!pointIds.trim()" @click="query">查询元数据</el-button>
    </el-form>
    <el-alert v-if="error" type="error" :title="error" show-icon :closable="false" style="margin-top: 12px" />
    <template v-if="response">
      <el-descriptions :column="1" border style="margin-top: 16px">
        <el-descriptions-item label="设备">{{ response.device_id }}</el-descriptions-item>
        <el-descriptions-item label="当前配置版本">{{ response.active_revision || '—' }}</el-descriptions-item>
        <el-descriptions-item label="配置哈希">{{ response.active_config_hash || '—' }}</el-descriptions-item>
      </el-descriptions>
      <el-table :data="Array.isArray(response.points) ? response.points : []" style="width: 100%; margin-top: 12px">
        <el-table-column prop="point_id" label="测点" min-width="150" />
        <el-table-column label="结果" width="90">
          <template #default="{row}"><el-tag :type="row.found ? 'success' : 'warning'">{{ row.found ? '找到' : '未找到' }}</el-tag></template>
        </el-table-column>
        <el-table-column label="访问方式" min-width="110"><template #default="{row}">{{ row.metadata?.access || '—' }}</template></el-table-column>
        <el-table-column label="数据类型" min-width="110"><template #default="{row}">{{ row.metadata?.data_type || '—' }}</template></el-table-column>
        <el-table-column label="单位" min-width="90"><template #default="{row}">{{ row.metadata?.unit || '—' }}</template></el-table-column>
        <el-table-column label="任务与输出" min-width="210">
          <template #default="{row}">
            <div>Task: {{ (row.metadata?.task_ids || []).join(', ') || '—' }}</div>
            <div>Sink: {{ (row.metadata?.sink_ids || []).join(', ') || '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column type="expand">
          <template #default="{row}">
            <pre style="white-space: pre-wrap; overflow-wrap: anywhere">{{ JSON.stringify(row, null, 2) }}</pre>
          </template>
        </el-table-column>
      </el-table>
    </template>
  </section>
</template>
