// usePointEditor 集成测试：点新增/编辑校验与落库、继承点排除、
// 覆盖重置，以及变更时受影响运行 Task 的停止/恢复（config save 集成）。
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { usePointEditor } from '../../src/composables/usePointEditor'
import { useConfigStore } from '../../src/stores/config'
import type { PointDef, TaskDef } from '../../src/domain/types'

vi.mock('element-plus', async (importOriginal) => {
  const mod = await importOriginal<typeof import('element-plus')>()
  return {
    ...mod,
    ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
    ElMessageBox: { confirm: vi.fn() },
  }
})

const confirmMock = vi.mocked(ElMessageBox.confirm)
const messageMock = vi.mocked(ElMessage)

function point(id: string, overrides: Partial<PointDef> = {}): PointDef {
  return {
    point_id: id,
    variable_name: id.toUpperCase(),
    point_groups: ['pg1'],
    address: { type: 'holding', address: 1 },
    data_type: 'float32',
    scale: 1,
    offset: 0,
    unit: 'mw',
    description: '',
    ...overrides,
  } as PointDef
}

function task(overrides: Partial<TaskDef> = {}): TaskDef {
  return {
    task_id: 't1',
    device: 'wtg-001',
    device_group: '',
    point_group: 'pg1',
    interval: 5,
    sinks: ['kafka-main'],
    enabled: true,
    runtime: 'RUNNING',
    created_at: '',
    updated_at: '',
    ...overrides,
  }
}

function seedStore() {
  const store = useConfigStore()
  store.units = {
    mw: { symbol: 'MW', name: 'Megawatt' },
    none: { symbol: '', name: 'None' },
  } as never
  store.pointGroups = [{ id: 'pg1', name: 'PG1' }]
  store.pointTables = [
    { id: 't1', protocol: 'modbus', extends: '', remove_points: [] },
    { id: 't2', protocol: 'modbus', extends: 't1', remove_points: [] },
  ]
  store.points = { t1: [point('p1')], t2: [] }
  store.sinks = [
    { name: 'kafka-main', type: 'kafka', enabled: true, connection: {}, points: [] } as never,
  ]
  store.deviceModels = [
    {
      id: 'm1',
      device_type: 'turbine',
      manufacturer: 'generic',
      protocol: 'modbus',
      point_table: 't1',
      read_mode: '',
      properties: {},
      connection_defaults: {},
    },
  ]
  store.devices = []
  store.tasks = []
  return store
}

function makeEditor(table = 't1') {
  const pointTable = ref(table)
  const protocol = computed(() => 'modbus' as const)
  return usePointEditor(pointTable, protocol)
}

describe('usePointEditor', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    seedStore()
    confirmMock.mockReset()
    confirmMock.mockResolvedValue('confirm')
    messageMock.error.mockClear()
  })

  it('新增点：校验通过后经 mutate 落库', async () => {
    const store = useConfigStore()
    const editor = makeEditor()
    editor.startAdd()
    editor.draft.point_id = 'p2'
    editor.draft.point_groups = ['pg1']
    editor.draft.address = 5
    editor.draft.unit = 'mw'

    expect(await editor.savePoint()).toBe(true)
    const saved = store.points['t1'].find((p) => p.point_id === 'p2')
    expect(saved).toMatchObject({
      variable_name: '',
      point_groups: ['pg1'],
      data_type: 'float32',
      unit: 'mw',
    })
    expect(saved?.address).toEqual({ type: 'holding', address: 5 })
  })

  it('新增校验：缺 ID / 重复 ID / 非法地址 / 空 point_groups 拒绝', async () => {
    const editor = makeEditor()
    editor.startAdd()

    expect(await editor.savePoint()).toBe(false)
    expect(messageMock.error).toHaveBeenCalledWith('Point ID is required')

    editor.draft.point_id = 'p1'
    editor.draft.point_groups = ['pg1']
    editor.draft.address = 1
    editor.draft.unit = 'mw'
    expect(await editor.savePoint()).toBe(false)
    expect(messageMock.error).toHaveBeenCalledWith(
      'Point ID "p1" already exists in this Point Table',
    )

    editor.draft.point_id = 'p3'
    editor.draft.address = undefined
    expect(await editor.savePoint()).toBe(false)

    editor.draft.address = 2
    editor.draft.point_groups = []
    expect(await editor.savePoint()).toBe(false)
    expect(messageMock.error).toHaveBeenCalledWith('point_groups must be non-empty')
  })

  it('编辑点：加载草稿、脏跟踪与更新落库', async () => {
    const store = useConfigStore()
    const editor = makeEditor()
    editor.startEdit(store.points['t1'][0])
    expect(editor.dirty.value).toBe(false)
    expect(editor.draft.point_id).toBe('p1')

    editor.draft.scale = 2.5
    expect(editor.dirty.value).toBe(true)
    expect(await editor.savePoint()).toBe(true)

    expect(store.points['t1'][0].scale).toBe(2.5)
    expect(store.points['t1']).toHaveLength(1)
  })

  it('编辑点且有受影响运行 Task：确认后停止并恢复仍有效实例', async () => {
    const store = useConfigStore()
    store.devices = [
      {
        device_id: 'wtg-001',
        model: 'm1',
        device_group: 'g1',
        host: '10.0.0.1',
        extensions: {},
        enabled: true,
        online: true,
      },
    ]
    store.tasks = [task()]
    const editor = makeEditor()
    editor.startEdit(store.points['t1'][0])

    editor.draft.description = 'updated'
    expect(await editor.savePoint()).toBe(true)

    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(store.tasks[0].runtime).toBe('RUNNING')
    expect(store.points['t1'][0].description).toBe('updated')
  })

  it('影响确认取消时不修改点', async () => {
    const store = useConfigStore()
    store.devices = [
      {
        device_id: 'wtg-001',
        model: 'm1',
        device_group: 'g1',
        host: '10.0.0.1',
        extensions: {},
        enabled: true,
        online: true,
      },
    ]
    store.tasks = [task()]
    confirmMock.mockRejectedValue(new Error('cancel'))
    const editor = makeEditor()
    editor.startEdit(store.points['t1'][0])

    editor.draft.description = 'updated'
    expect(await editor.savePoint()).toBe(false)
    expect(store.points['t1'][0].description).toBe('')
  })

  it('删除继承点：加入子表 remove_points 而非物理删除', async () => {
    const store = useConfigStore()
    const editor = makeEditor('t2')
    const inherited = store.points['t1'][0]
    expect(editor.originOf(inherited)).toBe('inherited')

    await editor.delPoint(inherited)

    const t2 = store.pointTables.find((t) => t.id === 't2')!
    expect(t2.remove_points).toContain('p1')
    expect(store.points['t1']).toHaveLength(1)
  })

  it('重置覆盖：移除本地覆盖恢复父表定义', async () => {
    const store = useConfigStore()
    store.points['t2'] = [point('p1', { scale: 9 })]
    const editor = makeEditor('t2')
    const overridePoint = store.points['t2'][0]
    expect(editor.originOf(overridePoint)).toBe('override')

    await editor.resetOverride(overridePoint)

    expect(store.points['t2']).toHaveLength(0)
    // 父表定义不受影响。
    expect(store.points['t1'][0].scale).toBe(1)
  })

  it('删除本地点：从点表移除', async () => {
    const store = useConfigStore()
    const editor = makeEditor('t1')
    await editor.delPoint(store.points['t1'][0])
    expect(store.points['t1']).toHaveLength(0)
  })
})
