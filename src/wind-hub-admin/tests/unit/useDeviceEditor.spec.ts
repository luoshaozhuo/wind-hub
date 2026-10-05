// useDeviceEditor 集成测试：编辑表单加载/脏跟踪、config 保存经 configStore.mutate
// 显式持久化（覆盖仅 diff 键、Task 影响确认与运行实例停恢复）、删除。
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { ElMessageBox } from 'element-plus'

import { useDeviceEditor } from '../../src/composables/useDeviceEditor'
import { useConfigStore } from '../../src/stores/config'
import type { DeviceInst, DeviceModelDef, TaskDef } from '../../src/domain/types'

vi.mock('element-plus', async (importOriginal) => {
  const mod = await importOriginal<typeof import('element-plus')>()
  return {
    ...mod,
    ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
    ElMessageBox: { confirm: vi.fn() },
  }
})

const confirmMock = vi.mocked(ElMessageBox.confirm)

const MODBUS_DEFAULTS = {
  port: 502,
  unit_id: 1,
  mode: 'tcp',
  timeout: 5,
  word_order: 'little_endian',
}

function model(id: string, overrides: Partial<DeviceModelDef> = {}): DeviceModelDef {
  return {
    id,
    device_type: 'turbine',
    manufacturer: 'generic',
    protocol: 'modbus',
    point_table: 't1',
    read_mode: 'poll',
    properties: {},
    connection_defaults: { ...MODBUS_DEFAULTS },
    ...overrides,
  }
}

function device(overrides: Partial<DeviceInst> = {}): DeviceInst {
  return {
    device_id: 'wtg-001',
    model: 'modbus_wtg',
    device_group: 'g1',
    host: '10.0.0.1',
    extensions: {},
    enabled: true,
    online: false,
    ...overrides,
  }
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
  store.deviceModels = [
    model('modbus_wtg'),
    model('modbus_alt', {
      point_table: 't2',
      connection_defaults: { ...MODBUS_DEFAULTS, port: 1502 },
    }),
  ]
  store.deviceGroups = [{ id: 'g1', device_type: 'turbine' }]
  store.devices = [device()]
  store.tasks = []
  // 让受影响 Task 保持 valid：sink / point group / point table / 点全部就位。
  store.sinks = [
    { name: 'kafka-main', type: 'kafka', enabled: true, connection: {}, points: [] } as never,
  ]
  store.pointGroups = [{ id: 'pg1', name: 'PG1' }]
  store.pointTables = [{ id: 't1', protocol: 'modbus', extends: '', remove_points: [] }]
  store.points = {
    t1: [
      {
        point_id: 'p1',
        variable_name: 'P1',
        point_groups: ['pg1'],
        address: { address: 1, type: 'holding' },
        data_type: 'float32',
        scale: 1,
        offset: 0,
        unit: 'mw',
        description: '',
      } as never,
    ],
  }
  return store
}

describe('useDeviceEditor', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    confirmMock.mockReset()
    confirmMock.mockResolvedValue('confirm')
  })

  it('loadEditForm 从设备与 Model 默认构建表单，初始不脏', () => {
    const store = seedStore()
    const target = ref<DeviceInst | null>(store.devices[0])
    const editor = useDeviceEditor(target)
    editor.loadEditForm()

    expect(editor.dirty.value).toBe(false)
    expect(editor.editForm.value).toMatchObject({
      device_id: 'wtg-001',
      model: 'modbus_wtg',
      device_group: 'g1',
      host: '10.0.0.1',
      protocol: 'modbus',
    })
    expect(editor.editForm.value.connection).toMatchObject({
      port: 502,
      unit_id: 1,
      mode: 'tcp',
      word_order: 'little_endian',
    })
  })

  it('编辑置脏；saveConfig 经 mutate 写入 diff 覆盖并复位脏标记', async () => {
    const store = seedStore()
    const target = ref<DeviceInst | null>(store.devices[0])
    const editor = useDeviceEditor(target)
    editor.loadEditForm()

    editor.editForm.value.host = '10.0.0.9'
    editor.editForm.value.connection.unit_id = 5
    expect(editor.dirty.value).toBe(true)

    await editor.saveConfig()

    const saved = store.devices[0]
    expect(saved.host).toBe('10.0.0.9')
    // 仅 diff 键成为 extensions 覆盖；port 与默认一致不落地。
    expect(saved.extensions).toEqual({ unit_id: 5 })
    expect(saved.port).toBeUndefined()
    expect(editor.dirty.value).toBe(false)
  })

  it('endpoint 变更且有受影响运行 Task：确认后停止并恢复仍有效实例', async () => {
    const store = seedStore()
    store.tasks = [task()]
    const target = ref<DeviceInst | null>(store.devices[0])
    const editor = useDeviceEditor(target)
    editor.loadEditForm()

    editor.editForm.value.host = '10.0.0.10'
    await editor.saveConfig()

    expect(confirmMock).toHaveBeenCalledTimes(1)
    // 运行实例先停后恢复（任务仍 valid && enabled）。
    expect(store.tasks[0].runtime).toBe('RUNNING')
    expect(store.devices[0].host).toBe('10.0.0.10')
  })

  it('影响确认取消时不修改设备', async () => {
    const store = seedStore()
    store.tasks = [task()]
    confirmMock.mockRejectedValue(new Error('cancel'))
    const target = ref<DeviceInst | null>(store.devices[0])
    const editor = useDeviceEditor(target)
    editor.loadEditForm()

    editor.editForm.value.host = '10.0.0.10'
    await editor.saveConfig()

    expect(store.devices[0].host).toBe('10.0.0.1')
    expect(store.tasks[0].runtime).toBe('RUNNING')
  })

  it('onEditModelChange 应用新 Model 的协议连接默认', () => {
    const store = seedStore()
    const target = ref<DeviceInst | null>(store.devices[0])
    const editor = useDeviceEditor(target)
    editor.loadEditForm()

    editor.editForm.value.model = 'modbus_alt'
    editor.onEditModelChange()

    expect(editor.editForm.value.point_table).toBe('t2')
    expect(editor.editForm.value.connection.port).toBe(1502)
  })

  it('deleteDevice 经 mutate 移除设备、清理验证状态并回调 onDeleted', async () => {
    const store = seedStore()
    store.tasks = [task({ runtime: 'RUNNING' })]
    store.deviceVerification = { 'wtg-001': { state: 'success' } as never }
    const target = ref<DeviceInst | null>(store.devices[0])
    const onDeleted = vi.fn()
    const editor = useDeviceEditor(target, { onDeleted })

    await editor.deleteDevice()

    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(store.devices).toHaveLength(0)
    expect(store.deviceVerification['wtg-001']).toBeUndefined()
    // 直接引用该设备的任务停止（定义保留）。
    expect(store.tasks[0].runtime).toBe('STOPPED')
    expect(onDeleted).toHaveBeenCalledTimes(1)
  })
})
