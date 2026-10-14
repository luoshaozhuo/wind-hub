<script setup lang="ts">
import { computed } from 'vue'
import { dataSourceLabels, type DataSourceState } from '../domain/dataSourceState'

const props = defineProps<{ state: DataSourceState; reason?: string }>()
const detail = computed(() => props.reason || dataSourceLabels[props.state])
</script>

<template>
  <el-tooltip v-if="state !== 'valid'" :content="detail" placement="top">
    <el-tag :type="state === 'unavailable' ? 'danger' : state === 'pending' ? 'info' : 'warning'"
      effect="plain" role="status" :aria-label="detail">
      {{ dataSourceLabels[state] }}
    </el-tag>
  </el-tooltip>
</template>
