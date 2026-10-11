// Point Table 浏览 feature：选中表的协议/解析点行、搜索过滤与分页。
// 点表继承解析全部走 domain/points，本 composable 只做响应式编排。
import { computed, ref, watch, type Ref } from 'vue'
import { unitSymbol } from '../domain/devices'
import { addressText, pointOrigin, pointsOfTable, tableProtocol } from '../domain/points'
import { useConfigStore } from '../stores/config'
import type { PointDef, Protocol } from '../domain/types'

export function usePointTableBrowser(pointTable: Ref<string>) {
  const configStore = useConfigStore()

  const protocol = computed(
    () => (tableProtocol(configStore, pointTable.value) || 'ads') as Protocol,
  )
  const tableDef = computed(() => configStore.pointTables.find((t) => t.id === pointTable.value))
  const rows = computed<PointDef[]>(() => pointsOfTable(configStore, pointTable.value))

  const pointPage = ref(1)
  const pointPageSize = ref(50)
  const pointSearch = ref('')

  // 搜索在分页之前对全量 rows 过滤：point_id / variable_name / 地址文本 / point_group。
  const filteredRows = computed(() => {
    const q = pointSearch.value.trim().toLowerCase()
    if (!q) return rows.value
    return rows.value.filter(
      (p) =>
        p.point_id.toLowerCase().includes(q) ||
        p.variable_name.toLowerCase().includes(q) ||
        addressOf(p).toLowerCase().includes(q) ||
        p.point_groups.some((g) => g.toLowerCase().includes(q)),
    )
  })
  const pagedRows = computed(() => {
    const start = (pointPage.value - 1) * pointPageSize.value
    return filteredRows.value.slice(start, start + pointPageSize.value)
  })
  watch([pointTable, pointSearch], () => {
    pointPage.value = 1
  })
  // 删除当前页最后一条或缩小过滤结果后，页码回收到合法范围。
  watch([filteredRows, pointPageSize], () => {
    const maxPage = Math.max(1, Math.ceil(filteredRows.value.length / pointPageSize.value))
    if (pointPage.value > maxPage) pointPage.value = maxPage
  })

  const addrLabel = computed(() =>
    protocol.value === 'ads'
      ? 'Symbol / Index'
      : protocol.value === 'modbus'
        ? 'Type / Address'
        : 'IOA',
  )
  const addressOf = (p: PointDef) => addressText(protocol.value, p)
  const originOf = (p: PointDef) => pointOrigin(configStore, pointTable.value, p.point_id)
  const unitSymbolOf = (unit: string) => unitSymbol(configStore, unit)
  const currentTableIsSystem = computed(() => !!tableDef.value?.system)

  return {
    protocol,
    tableDef,
    rows,
    pointPage,
    pointPageSize,
    pointSearch,
    filteredRows,
    pagedRows,
    addrLabel,
    addressOf,
    originOf,
    unitSymbolOf,
    currentTableIsSystem,
  }
}

export type PointTableBrowser = ReturnType<typeof usePointTableBrowser>
