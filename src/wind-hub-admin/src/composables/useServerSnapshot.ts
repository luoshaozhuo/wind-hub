// App 级 Server State → Config Store 同步：Vue Query 持有全部配置/运行态
// 查询（5s refetchInterval 与旧 Overview 轮询节奏一致），结果到达时推入
// configStore.syncFromServer；脏状态下 store 只补丁运行时字段。
import { computed, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { qk } from '../api/queryKeys'
import { fetchDefinitions, fetchSettings } from '../api/config'
import { fetchDevices } from '../api/devices'
import { fetchTasks } from '../api/tasks'
import { fetchSinks } from '../api/sinks'
import { fetchOverview } from '../api/monitoring'
import { useConfigStore } from '../stores/config'

const RUNTIME_REFETCH_MS = 5000

export function useServerSnapshot() {
  const configStore = useConfigStore()

  const settings = useQuery({ queryKey: qk.settings, queryFn: fetchSettings })
  const definitions = useQuery({ queryKey: qk.definitions, queryFn: fetchDefinitions })
  const devices = useQuery({
    queryKey: qk.devices,
    queryFn: () => fetchDevices(),
    refetchInterval: RUNTIME_REFETCH_MS,
  })
  const tasks = useQuery({
    queryKey: qk.tasks,
    queryFn: () => fetchTasks(),
    refetchInterval: RUNTIME_REFETCH_MS,
  })
  const sinks = useQuery({
    queryKey: qk.sinks,
    queryFn: fetchSinks,
    refetchInterval: RUNTIME_REFETCH_MS,
  })
  const overview = useQuery({
    queryKey: qk.overview,
    queryFn: fetchOverview,
    refetchInterval: RUNTIME_REFETCH_MS,
  })

  watch(
    () => settings.data.value,
    (data) => data && configStore.syncFromServer({ settings: data }),
  )
  watch(
    () => definitions.data.value,
    (data) => data && configStore.syncFromServer({ definitions: data }),
  )
  watch(
    () => devices.data.value,
    (data) => data && configStore.syncFromServer({ devices: data.items }),
  )
  watch(
    () => tasks.data.value,
    (data) => data && configStore.syncFromServer({ tasks: data.items }),
  )
  watch(
    () => sinks.data.value,
    (data) => data && configStore.syncFromServer({ sinks: data }),
  )
  watch(
    () => overview.data.value,
    (data) => data && configStore.syncFromServer({ overview: data }),
  )

  const queries = [settings, definitions, devices, tasks, sinks, overview]
  const booting = computed(() => queries.some((q) => q.isPending.value))
  const bootError = computed(() => queries.find((q) => q.isError.value)?.error.value?.message || '')

  function retry() {
    for (const q of queries) void q.refetch()
  }

  return { booting, bootError, retry }
}
