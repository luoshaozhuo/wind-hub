// Point Table 影响面编排：变更/删除前的影响确认，以及受影响运行 Task 的
// 停止 → 应用 → 有效性重算 → 恢复。所有修改经 configStore.mutate 显式持久化。
import { ElMessageBox } from 'element-plus'
import { descendantTableIds } from '../domain/points'
import { affectedByPointTables, refreshTaskValidity } from '../domain/tasks'
import { useConfigStore } from '../stores/config'

export function usePointTableImpact() {
  const configStore = useConfigStore()

  function tableImpact(tableId: string) {
    const tables = [tableId, ...descendantTableIds(configStore, tableId)]
    return affectedByPointTables(configStore, tables)
  }

  async function confirmTableImpact(tableId: string, title: string, action: string) {
    const impact = tableImpact(tableId)
    const running = impact.tasks.filter((t) => t.runtime === 'RUNNING')
    if (!impact.devices.length && !impact.tasks.length && impact.tables.length === 1) return true
    try {
      await ElMessageBox.confirm(
        '<b>' +
          action +
          '</b><br><br>' +
          impact.tables.length +
          ' Point Table(s) affected.<br>' +
          impact.devices.length +
          ' Device(s) affected.<br>' +
          impact.tasks.length +
          ' Task(s) affected; ' +
          running.length +
          ' currently running.<br><br>' +
          'Affected running tasks will be stopped while the change is applied and restored if they remain valid.',
        title,
        { type: 'warning', confirmButtonText: 'Apply Changes', dangerouslyUseHTMLString: true },
      )
    } catch {
      return false
    }
    return true
  }

  function withAffectedTasksStopped(tableId: string, apply: () => void) {
    configStore.mutate(() => {
      const impact = tableImpact(tableId)
      const runningIds = new Set(
        impact.tasks.filter((t) => t.runtime === 'RUNNING').map((t) => t.task_id),
      )
      for (const t of impact.tasks) if (runningIds.has(t.task_id)) t.runtime = 'STOPPED'
      apply()
      refreshTaskValidity(configStore)
      for (const t of impact.tasks) {
        if (runningIds.has(t.task_id) && t.valid !== false && t.enabled) t.runtime = 'RUNNING'
      }
    })
  }

  return { tableImpact, confirmTableImpact, withAffectedTasksStopped }
}

export type PointTableImpact = ReturnType<typeof usePointTableImpact>
