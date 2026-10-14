<script setup lang="ts">
import AdminPageHeading from '../components/AdminPageHeading.vue'
import PointMetadataManager from '../components/points/PointMetadataManager.vue'
import { computed, onMounted, ref } from 'vue'
import { runtimeApi, type LatestPointValue, type RuntimeDevice } from '../api/runtime'

const devices = ref<RuntimeDevice[]>([])
const points = ref<LatestPointValue[]>([])
const selected = ref('')
const query = ref('')
const error = ref('')
const loading = ref(false)
const detailVisible = ref(false)
const detailBusy = ref(false)
const detailError = ref('')
const detail = ref<LatestPointValue | null>(null)
const inspection = ref<Record<string, unknown> | null>(null)
const inspectionKind = ref('')
const metadataVisible = ref(false)
const metadataDevice = ref('')
function openMetadata() {
  if (!selected.value) return
  metadataDevice.value = selected.value
  metadataVisible.value = true
}

const filtered = computed(() => points.value.filter(p =>
  (!selected.value || p.device_id === selected.value) &&
  (!query.value || (p.device_id + ' ' + p.point_id).toLowerCase().includes(query.value.toLowerCase())),
))

async function refresh() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    const [nextDevices, nextPoints] = await Promise.all([
      runtimeApi.devices(), runtimeApi.latestValues(selected.value || undefined),
    ])
    devices.value = nextDevices
    points.value = nextPoints
  } catch (cause) {
    error.value = String(cause)
  } finally {
    loading.value = false
  }
}

function showDetail(point: LatestPointValue) {
  detail.value = point
  inspection.value = null
  inspectionKind.value = ''
  detailError.value = ''
  detailVisible.value = true
}

async function inspect(operation: 'read' | 'verify' | 'resolve') {
  const point = detail.value
  if (!point || detailBusy.value) return
  detailBusy.value = true
  detailError.value = ''
  inspection.value = null
  inspectionKind.value = operation
  try {
    if (operation === 'read') inspection.value = await runtimeApi.readPoint(point.device_id, point.point_id)
    if (operation === 'verify') inspection.value = await runtimeApi.verifyPoint(point.device_id, point.point_id)
    if (operation === 'resolve') inspection.value = await runtimeApi.resolvePoint(point.device_id, point.point_id)
  } catch (cause) {
    detailError.value = String(cause)
  } finally {
    detailBusy.value = false
  }
}

onMounted(() => void refresh())
</script>

<template>
  <section class="runtime-page">
    <AdminPageHeading title="测点浏览" description="Collector 最新测点值与 Commander 实时诊断" />
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-space wrap>
      <el-select v-model="selected" placeholder="全部设备" clearable style="width: 220px" @change="refresh">
        <el-option v-for="device in devices" :key="device.device_id" :value="device.device_id" :label="device.device_id" />
      </el-select>
      <el-input v-model="query" clearable placeholder="筛选设备或测点 ID" style="width: 240px" />
      <el-button :loading="loading" @click="refresh">刷新</el-button>
      <el-button type="primary" :disabled="!selected" @click="openMetadata">查询测点元数据</el-button>
    </el-space>
    <el-table :data="filtered" v-loading="loading" style="width: 100%" @row-click="showDetail">
      <el-table-column prop="device_id" label="设备" min-width="140" />
      <el-table-column prop="point_id" label="测点" min-width="160" />
      <el-table-column label="值" min-width="180">
        <template #default="{ row }">{{ JSON.stringify(row.value ?? null) }}</template>
      </el-table-column>
      <el-table-column prop="quality" label="质量" min-width="100" />
      <el-table-column prop="timestamp" label="时间戳" min-width="170" />
      <el-table-column prop="timestamp_source" label="时间戳来源" min-width="120" />
      <el-table-column label="操作" width="100">
        <template #default="{ row }"><el-button link type="primary" @click.stop="showDetail(row)">详情</el-button></template>
      </el-table-column>
    </el-table>
    <p>仅显示 Collector 已发布的最新值；这里不代表完整点表配置。</p>
  </section>

  <el-drawer v-model="metadataVisible" title="测点元数据" size="min(820px, 100%)">
    <PointMetadataManager v-if="metadataVisible && metadataDevice" :key="metadataDevice" :device-id="metadataDevice" />
  </el-drawer>

  <el-drawer v-model="detailVisible" title="测点详情" size="min(560px, 100%)">
    <template v-if="detail">
      <el-descriptions :column="1" border>
        <el-descriptions-item label="设备">{{ detail.device_id }}</el-descriptions-item>
        <el-descriptions-item label="测点">{{ detail.point_id }}</el-descriptions-item>
        <el-descriptions-item label="最新值">{{ JSON.stringify(detail.value ?? null) }}</el-descriptions-item>
        <el-descriptions-item label="质量">{{ detail.quality }}</el-descriptions-item>
        <el-descriptions-item label="时间戳">{{ detail.timestamp || '未提供' }}</el-descriptions-item>
        <el-descriptions-item label="时间戳来源">{{ detail.timestamp_source }}</el-descriptions-item>
        <el-descriptions-item label="数据来源">{{ detail.source }}</el-descriptions-item>
      </el-descriptions>
      <el-space wrap style="margin: 16px 0">
        <el-button :loading="detailBusy && inspectionKind === 'read'" :disabled="detailBusy" @click="inspect('read')">实时读取</el-button>
        <el-button :loading="detailBusy && inspectionKind === 'verify'" :disabled="detailBusy" @click="inspect('verify')">校验测点</el-button>
        <el-button :loading="detailBusy && inspectionKind === 'resolve'" :disabled="detailBusy" @click="inspect('resolve')">解析地址</el-button>
      </el-space>
      <el-alert v-if="detailError" type="error" :title="detailError" :closable="false" show-icon />
      <el-card v-if="inspection" shadow="never">
        <template #header>{{ inspectionKind === 'read' ? '实时读取结果' : inspectionKind === 'verify' ? '测点校验结果' : '地址解析结果' }}</template>
        <pre style="white-space: pre-wrap; overflow-wrap: anywhere">{{ JSON.stringify(inspection, null, 2) }}</pre>
      </el-card>
    </template>
  </el-drawer>
</template>
