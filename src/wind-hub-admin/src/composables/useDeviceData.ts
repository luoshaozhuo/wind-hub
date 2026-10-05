// 设备即时数据 feature：LatestPointStore 查询（Vue Query 托管）、解析点表、
// Data 行序列与手动/自动刷新。Trend 与 Command 共用同一 dataRows 序列。
import { computed, ref, watch, type ComputedRef, type Ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { fetchAllDeviceData } from '../api/devices'
import { qk } from '../api/queryKeys'
import {
  buildDataRows,
  pointValueFromSnapshot,
  type DataRow,
  type PointValue,
} from '../domain/deviceData'
import { tableOfDevice, unitSymbol } from '../domain/devices'
import { pointsOfTable } from '../domain/points'
import { useConfigStore } from '../stores/config'
import { EMPTY, formatTimestamp } from '../utils/format'
import type { DeviceInst } from '../domain/types'

/**
 * @param device 当前选中设备（Drawer 以 device_id 为 key 重建，故生命周期内不变）。
 * @param active 查询启用条件（Drawer 打开且 Data tab 激活），保持原有按需拉取语义。
 */
export function useDeviceData(device: Ref<DeviceInst | null>, active: ComputedRef<boolean>) {
  const configStore = useConfigStore()

  const autoRefresh = ref(false)
  const refreshInterval = ref(1000)
  const lastRefreshAt = ref('')

  const query = useQuery({
    queryKey: computed(() => qk.deviceData(device.value?.device_id || '')),
    queryFn: () => fetchAllDeviceData(device.value!.device_id),
    enabled: computed(() => !!device.value && active.value),
    refetchInterval: computed(() => (autoRefresh.value ? refreshInterval.value : false)),
  })
  const refreshing = computed(() => query.isFetching.value)
  watch(
    () => query.dataUpdatedAt.value,
    (ts) => {
      if (ts) lastRefreshAt.value = formatTimestamp(new Date())
    },
  )

  const resolvedPoints = computed(() =>
    device.value ? pointsOfTable(configStore, tableOfDevice(configStore, device.value)) : [],
  )

  // Data 当前值来自后端 LatestPointStore（Vue Query 托管）：
  // 与 Trend 尾点、Diagnostics Read、Read Test 同源；Command 成功后
  // invalidate deviceData 查询触发联动刷新（§6）。
  const latestPointValues = computed(() => {
    const map = new Map<string, { value: number | boolean | string; quality: string | null }>()
    for (const row of query.data.value || []) {
      map.set(row.point_id, {
        value: (row.value ?? EMPTY) as number | boolean | string,
        quality: row.quality ?? null,
      })
    }
    return map
  })

  function readPointValue(pointId: string): PointValue {
    return pointValueFromSnapshot(latestPointValues.value, pointId)
  }

  const dataRows = computed<DataRow[]>(() =>
    buildDataRows(
      resolvedPoints.value,
      readPointValue,
      (unit) => unitSymbol(configStore, unit),
      (i) => formatTimestamp(new Date(Date.now() - (i % 7) * 120)),
    ),
  )

  async function refresh() {
    if (!device.value || refreshing.value) return
    await query.refetch()
  }

  return {
    query,
    autoRefresh,
    refreshInterval,
    lastRefreshAt,
    refreshing,
    resolvedPoints,
    dataRows,
    readPointValue,
    refresh,
  }
}

export type DeviceDataFeature = ReturnType<typeof useDeviceData>
