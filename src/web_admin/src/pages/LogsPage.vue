<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { keepPreviousData, useQuery } from '@tanstack/vue-query'
import { useViewport } from '../composables/useViewport'
import { fetchLogSources, fetchLogs } from '../api/monitoring'
import { qk } from '../api/queryKeys'

interface Entry {
  time: string
  level: 'ERROR' | 'WARN' | 'INFO'
  source: string
  object: string
  message: string
  requestId: string
}

const level = ref<'All' | Entry['level']>('ERROR')
const source = ref('All')
const keyword = ref('')
const page = ref(1)
const pageSize = ref(20)
const { isMobile, isTablet } = useViewport()

const params = computed(() => ({
  page: page.value,
  pageSize: pageSize.value,
  level: level.value,
  source: source.value,
  keyword: keyword.value,
}))

const LIVE_POLL_MS = 5000
// 仅第一页是“实时尾部”：翻页浏览历史时停止轮询，避免数据在脚下移动。
const isLive = computed(() => page.value === 1)

const logsQuery = useQuery({
  queryKey: computed(() => qk.logs(params.value)),
  queryFn: () => fetchLogs(params.value),
  placeholderData: keepPreviousData,
  refetchInterval: computed(() => (isLive.value ? LIVE_POLL_MS : false)),
})
const sourcesQuery = useQuery({ queryKey: qk.logSources, queryFn: fetchLogSources })

const rows = computed<Entry[]>(() =>
  (logsQuery.data.value?.items || []).map((entry) => ({
    time: entry.timestamp.replace('T', ' ').replace('Z', '').slice(0, 19),
    level: (entry.level === 'WARNING' ? 'WARN' : entry.level) as Entry['level'],
    source: entry.source,
    object: entry.object,
    message: entry.message,
    requestId: entry.request_id || '-',
  })),
)
const total = computed(() => logsQuery.data.value?.page.total || 0)
const loading = computed(() => logsQuery.isFetching.value)
const sources = computed(() => sourcesQuery.data.value || [])

watch([level, source, keyword, pageSize], () => {
  page.value = 1
})
</script>

<template>
  <div class="standard-page logs-page">
    <div class="head">
      <div>
        <h1>Logs</h1>
        <p>运行日志与错误查询</p>
      </div>
    </div>

    <el-card shadow="never">
      <div class="logs-toolbar">
        <div class="row">
          <b>Log Stream</b>
          <el-tag v-if="isLive" type="success">live</el-tag>
          <el-tag v-else type="info">history</el-tag>
        </div>
        <div class="logs-filters">
          <el-select v-model="level" aria-label="Log level">
            <el-option
              v-for="item in ['ERROR', 'WARN', 'INFO', 'All']"
              :key="item"
              :label="item"
              :value="item"
            />
          </el-select>
          <el-select v-model="source" aria-label="Log source">
            <el-option label="All Sources" value="All" />
            <el-option v-for="item in sources" :key="item" :label="item" :value="item" />
          </el-select>
          <el-input v-model="keyword" clearable placeholder="Search source / object / message..." />
        </div>
      </div>

      <el-table
        v-loading="loading"
        :data="rows"
        height="var(--app-table-viewport-height)"
        empty-text="No logs match current filters"
      >
        <el-table-column v-if="!isMobile" prop="time" label="Time" width="180" />
        <el-table-column label="Level" width="100">
          <template #default="{ row }">
            <el-tag
              :type="row.level === 'ERROR' ? 'danger' : row.level === 'WARN' ? 'warning' : 'info'"
              >{{ row.level }}</el-tag
            >
          </template>
        </el-table-column>
        <el-table-column v-if="!isTablet && !isMobile" prop="source" label="Source" width="110" />
        <el-table-column v-if="!isMobile" prop="object" label="Object" width="160" />
        <el-table-column v-if="!isTablet && !isMobile" label="Request ID" width="120">
          <template #default="{ row }">
            <span :title="row.requestId">{{
              row.requestId === '-' ? '-' : row.requestId.slice(0, 8)
            }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="message" label="Message" min-width="320" />
      </el-table>

      <div class="pagination">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[20, 50, 100]"
          :total="total"
          :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.logs-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--app-space-4);
  margin-bottom: var(--app-space-3);
}
.logs-filters {
  display: grid;
  grid-template-columns: minmax(0, 0.7fr) minmax(0, 0.8fr) minmax(0, 1.5fr);
  gap: var(--app-space-2);
}
@media (max-width: 1199px) {
  .logs-toolbar {
    align-items: stretch;
    flex-direction: column;
  }
  .logs-filters {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .logs-filters .el-input {
    grid-column: 1/-1;
  }
}
@media (max-width: 767px) {
  .logs-filters {
    grid-template-columns: 1fr;
  }
}
</style>
