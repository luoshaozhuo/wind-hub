// Point 编辑 feature：点定义草稿、校验、保存（新增/编辑/继承覆盖）、
// 删除（含继承排除）与重置覆盖。变更经 usePointTableImpact 确认影响面后
// 由 configStore.mutate 显式持久化。
import { computed, reactive, ref, type ComputedRef, type Ref } from 'vue'
import { ElMessage } from 'element-plus'
import { pointOrigin, pointsOfTable, validateAddress } from '../domain/points'
import { useConfigStore } from '../stores/config'
import { DATA_TYPES } from '../domain/types'
import type { PointAddress, PointDef, Protocol } from '../domain/types'
import { usePointTableImpact } from './usePointTableImpact'

export function usePointEditor(pointTable: Ref<string>, protocol: ComputedRef<Protocol>) {
  const configStore = useConfigStore()
  const { confirmTableImpact, withAffectedTasksStopped } = usePointTableImpact()

  const editing = ref('')
  const snapshot = ref('')
  const draft = reactive({
    point_id: '',
    variable_name: '',
    point_groups: ['all'] as string[],
    symbol: '',
    index_group: '',
    index_offset: '',
    register_type: 'holding',
    address: undefined as number | undefined,
    ioa: undefined as number | undefined,
    ioa_type: '',
    data_type: 'float32',
    scale: 1,
    offset: 0,
    unit: 'none',
    description: '',
  })

  const dirty = computed(() => !!editing.value && JSON.stringify(draft) !== snapshot.value)

  function resetDraft() {
    draft.point_id = ''
    draft.variable_name = ''
    draft.point_groups = ['all']
    draft.symbol = ''
    draft.index_group = ''
    draft.index_offset = ''
    draft.register_type = 'holding'
    draft.address = undefined
    draft.ioa = undefined
    draft.ioa_type = ''
    draft.data_type = 'float32'
    draft.scale = 1
    draft.offset = 0
    draft.unit = 'none'
    draft.description = ''
  }

  function startAdd() {
    editing.value = ''
    resetDraft()
    snapshot.value = ''
  }

  function startEdit(p: PointDef) {
    editing.value = p.point_id
    resetDraft()
    draft.point_id = p.point_id
    draft.variable_name = p.variable_name
    draft.point_groups = [...p.point_groups]
    draft.symbol = p.address.symbol || ''
    draft.index_group = p.address.index_group || ''
    draft.index_offset = p.address.index_offset || ''
    draft.register_type = p.address.type || 'holding'
    draft.address = p.address.address
    draft.ioa = p.address.ioa
    draft.ioa_type = protocol.value === 'iec104' ? p.address.type || '' : ''
    draft.data_type = p.data_type
    draft.scale = p.scale
    draft.offset = p.offset
    draft.unit = p.unit
    draft.description = p.description
    snapshot.value = JSON.stringify(draft)
  }

  function draftAddress(): PointAddress {
    if (protocol.value === 'ads') {
      const a: PointAddress = {}
      if (draft.symbol.trim()) a.symbol = draft.symbol.trim()
      if (draft.index_group.trim()) a.index_group = draft.index_group.trim()
      if (draft.index_offset.trim()) a.index_offset = draft.index_offset.trim()
      return a
    }
    if (protocol.value === 'modbus') return { type: draft.register_type, address: draft.address }
    return { ioa: draft.ioa, ...(draft.ioa_type.trim() ? { type: draft.ioa_type.trim() } : {}) }
  }

  const testRequest = computed(() => {
    if (protocol.value === 'ads') {
      if (draft.symbol.trim()) return `Symbol · ${draft.symbol.trim()}`
      return `Index · ${draft.index_group || '—'} / ${draft.index_offset || '—'}`
    }
    if (protocol.value === 'modbus') return `${draft.register_type} · ${draft.address ?? '—'}`
    return `IOA · ${draft.ioa ?? '—'}${draft.ioa_type ? ` · ${draft.ioa_type}` : ''}`
  })

  function originOf(p: PointDef) {
    return pointOrigin(configStore, pointTable.value, p.point_id)
  }

  /** 保存（新增或编辑）；成功返回 true（调用方关闭编辑界面）。 */
  async function savePoint(): Promise<boolean> {
    const id = draft.point_id.trim()
    const list = configStore.points[pointTable.value]
    if (!list) return false
    if (!id) {
      ElMessage.error('Point ID is required')
      return false
    }
    if (editing.value && id !== editing.value) {
      ElMessage.warning('Point ID is stable after creation')
      return false
    }
    if (
      !editing.value &&
      pointsOfTable(configStore, pointTable.value).some((p) => p.point_id === id)
    ) {
      ElMessage.error('Point ID "' + id + '" already exists in this Point Table')
      return false
    }
    if (!draft.point_groups.length) {
      ElMessage.error('point_groups must be non-empty')
      return false
    }
    const address = draftAddress()
    const err = validateAddress(protocol.value, address)
    if (err) {
      ElMessage.error(err)
      return false
    }
    if (!DATA_TYPES.includes(draft.data_type)) {
      ElMessage.error('Invalid data_type')
      return false
    }
    if (!configStore.units[draft.unit]) {
      ElMessage.error('Invalid unit')
      return false
    }

    const row: PointDef = {
      point_id: id,
      variable_name: draft.variable_name.trim(),
      point_groups: [...draft.point_groups],
      address,
      data_type: draft.data_type,
      scale: draft.scale,
      offset: draft.offset,
      unit: draft.unit,
      description: draft.description.trim(),
    }

    if (editing.value) {
      const ok = await confirmTableImpact(
        pointTable.value,
        'Point Change Impact',
        originOf({ ...row, point_id: editing.value }) === 'inherited'
          ? 'Create an override for this inherited point?'
          : 'Update this point definition?',
      )
      if (!ok) return false
    }

    withAffectedTasksStopped(pointTable.value, () => {
      const i = list.findIndex((p) => p.point_id === editing.value)
      if (i >= 0) list.splice(i, 1, row)
      else list.push(row)
      const table = configStore.pointTables.find((t) => t.id === pointTable.value)
      if (table) table.remove_points = (table.remove_points || []).filter((pid) => pid !== id)
    })

    ElMessage.success(editing.value ? 'Point updated and applied ' : 'Point added and applied ')
    return true
  }

  async function delPoint(p: PointDef) {
    const origin = originOf(p)
    const ok = await confirmTableImpact(
      pointTable.value,
      'Point Delete Impact',
      origin === 'inherited'
        ? 'Exclude this inherited point from the child table?'
        : 'Delete this point from the effective table?',
    )
    if (!ok) return

    withAffectedTasksStopped(pointTable.value, () => {
      const list = configStore.points[pointTable.value]
      const localIndex = list.findIndex((x) => x.point_id === p.point_id)
      if (localIndex >= 0) list.splice(localIndex, 1)

      const table = configStore.pointTables.find((t) => t.id === pointTable.value)
      if (table?.extends) {
        const parentHasPoint = pointsOfTable(configStore, table.extends).some(
          (x) => x.point_id === p.point_id,
        )
        if (parentHasPoint && !table.remove_points.includes(p.point_id))
          table.remove_points.push(p.point_id)
      }
    })
    ElMessage.success(origin === 'inherited' ? 'Inherited point excluded ' : 'Point deleted ')
  }

  async function resetOverride(p: PointDef) {
    if (originOf(p) !== 'override') return
    const ok = await confirmTableImpact(
      pointTable.value,
      'Reset Override Impact',
      'Restore the parent definition for this point?',
    )
    if (!ok) return
    withAffectedTasksStopped(pointTable.value, () => {
      const list = configStore.points[pointTable.value]
      const i = list.findIndex((x) => x.point_id === p.point_id)
      if (i >= 0) list.splice(i, 1)
      const table = configStore.pointTables.find((t) => t.id === pointTable.value)
      if (table) table.remove_points = table.remove_points.filter((id) => id !== p.point_id)
    })
    ElMessage.success('Override reset to parent definition ')
  }

  return {
    editing,
    draft,
    dirty,
    startAdd,
    startEdit,
    draftAddress,
    testRequest,
    originOf,
    savePoint,
    delPoint,
    resetOverride,
  }
}

export type PointEditor = ReturnType<typeof usePointEditor>
