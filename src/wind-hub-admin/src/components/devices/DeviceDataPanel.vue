<script setup lang="ts">
// 设备即时数据 panel：点值网格、搜索/分组筛选、分页、手动/自动刷新。
// Server State 由 useDeviceData（Vue Query）持有；这里只有面板局部 UI state。
import { computed, ref, watch } from 'vue'
import type { DeviceDataFeature } from '../../composables/useDeviceData'
import { useConfigStore } from '../../stores/config'
import { useViewport } from '../../composables/useViewport'
import DataSourceBadge from '../DataSourceBadge.vue'

const props = defineProps<{
  data: DeviceDataFeature
}>()

const configStore = useConfigStore()
const { isMobile } = useViewport()

const { dataRows, refreshing, sourceState, autoRefresh, refreshInterval, refresh } = props.data

const dataSearch = ref('')
const dataGroup = ref('All')

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
</script>

<template>
  <div class="data-toolbar">
    <div>
      <h3>{{ dataRows.length }} points <DataSourceBadge :state="sourceState" /></h3>
      <p>
        {{ sourceState === 'valid' ? dataSuccessCount + ' / ' + dataRows.length : '—' }} read
        <template v-if="dataFailureCount"> · {{ dataFailureCount }} failed</template>
      </p>
    </div>
    <div class="data-tools data-refresh-tools">
      <el-input v-model="dataSearch" clearable placeholder="Search variable..." />
      <el-select v-model="dataGroup">
        <el-option label="All Groups" value="All" />
        <el-option v-for="g in configStore.pointGroups" :key="g.id" :label="g.name" :value="g.id" />
      </el-select>
      <el-select v-model="refreshInterval" class="data-refresh-interval">
        <el-option :value="500" label="500 ms" />
        <el-option :value="1000" label="1 s" />
        <el-option :value="2000" label="2 s" />
        <el-option :value="5000" label="5 s" />
        <el-option :value="10000" label="10 s" />
      </el-select>
      <div class="auto-refresh-toggle">
        <span>Auto Refresh</span><el-switch v-model="autoRefresh" />
      </div>
      <el-button :disabled="refreshing" @click="refresh">Refresh</el-button>
    </div>
  </div>

  <div class="compact-data-grid">
    <article
      v-for="r in pagedData"
      :key="r.point_id"
      class="compact-data-item"
      :class="{ 'data-read-failed': sourceState !== 'valid' || r.read_state === 'failed' }"
    >
      <div class="compact-data-top">
        <b :title="r.variable_name || r.point_id">{{ r.variable_name || r.point_id }}</b>
        <div class="compact-data-value">
          <strong>{{ sourceState === 'valid' && r.read_state === 'success' ? r.value : '—' }}</strong
          ><span>{{ r.unit }}</span>
        </div>
      </div>
      <small v-if="sourceState !== 'valid'" class="data-error">当前数据源不可用</small>
      <time v-else-if="r.read_state === 'success'">{{ r.updated_at }}</time>
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
</template>

<style scoped>
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
.data-refresh-interval {
  width: var(--app-control-width-short);
}
.data-read-failed {
  border-color: var(--app-status-fault);
}
.data-error {
  color: var(--app-status-fault);
  font-size: var(--app-font-caption);
}
@media (max-width: 767px) {
  .data-refresh-tools {
    align-items: stretch;
  }
  .data-refresh-tools > * {
    max-width: 100%;
  }
}
</style>
