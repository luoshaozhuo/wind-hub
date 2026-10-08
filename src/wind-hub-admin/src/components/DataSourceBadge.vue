<script setup lang="ts">
import { computed } from 'vue'
import { WarningFilled, Loading, CircleCloseFilled } from '@element-plus/icons-vue'
import { dataSourceLabels, type DataSourceState } from '../domain/dataSourceState'

const props = defineProps<{ state: DataSourceState; reason?: string }>()
const detail = computed(() => props.reason || dataSourceLabels[props.state])
</script>

<template>
  <el-tooltip v-if="state !== 'valid'" :content="detail" placement="top">
    <span class="source-badge" :class="state" role="status" :aria-label="detail">
      <el-icon v-if="state === 'pending'"><Loading /></el-icon>
      <el-icon v-else-if="state === 'unavailable'"><CircleCloseFilled /></el-icon>
      <el-icon v-else><WarningFilled /></el-icon>
      {{ dataSourceLabels[state] }}
    </span>
  </el-tooltip>
</template>

<style scoped>
.source-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.18rem 0.45rem;
  border: 1px solid currentColor;
  border-radius: var(--app-control-radius, 4px);
  font-size: var(--app-font-caption, 12px);
  font-weight: 600;
  white-space: nowrap;
}
.source-badge.pending { color: var(--app-text-secondary); }
.source-badge.stale, .source-badge.invalid { color: var(--app-status-warning); }
.source-badge.unavailable { color: var(--app-status-fault); }
</style>
