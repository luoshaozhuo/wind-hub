// useDeviceCommand 测试：显式确认 → 写入 → readback 结果、
// Data/logs 查询失效与成功后的 onSent 联动。
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, ref } from 'vue'
import { ElMessageBox } from 'element-plus'

import { sendDeviceCommand } from '../../src/api/devices'
import { queryClient } from '../../src/api/queryClient'
import { qk } from '../../src/api/queryKeys'
import { useDeviceCommand } from '../../src/composables/useDeviceCommand'
import type { DataRow } from '../../src/domain/deviceData'
import type { DeviceInst, PointDef } from '../../src/domain/types'

vi.mock('element-plus', async (importOriginal) => {
  const mod = await importOriginal<typeof import('element-plus')>()
  return {
    ...mod,
    ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
    ElMessageBox: { confirm: vi.fn() },
  }
})

vi.mock('../../src/api/devices', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../src/api/devices')>()
  return { ...mod, sendDeviceCommand: vi.fn() }
})

const confirmMock = vi.mocked(ElMessageBox.confirm)
const sendMock = vi.mocked(sendDeviceCommand)

function row(overrides: Partial<DataRow> = {}): DataRow {
  return {
    point_id: 'ctl1',
    variable_name: 'Setpoint',
    description: '',
    value: 10,
    unit: 'MW',
    groups: ['control'],
    updated_at: 't',
    data_type: 'float32',
    scale: 1,
    offset: 0,
    address: 'IOA 1',
    updated: true,
    read_state: 'success',
    error: '',
    index: 0,
    ...overrides,
  }
}

const pointDef = {
  point_id: 'ctl1',
  variable_name: 'Setpoint',
  point_groups: ['control'],
  address: { ioa: 1 },
  data_type: 'float32',
  scale: 1,
  offset: 0,
  unit: 'mw',
  description: '',
} as PointDef

const device: DeviceInst = {
  device_id: 'wtg-001',
  model: 'm1',
  device_group: 'g1',
  host: '10.0.0.1',
  extensions: {},
  enabled: true,
  online: true,
}

function setup(rows: DataRow[] = [row()]) {
  const deviceRef = ref<DeviceInst | null>(device)
  const onSent = vi.fn()
  const feature = useDeviceCommand(
    deviceRef,
    computed(() => rows),
    computed(() => [pointDef]),
    {
      onSent,
    },
  )
  return { feature, onSent }
}

describe('useDeviceCommand', () => {
  beforeEach(() => {
    confirmMock.mockReset()
    confirmMock.mockResolvedValue('confirm')
    sendMock.mockReset()
    queryClient.clear()
  })

  it('候选点仅含 control 组，初始选中第一个并取当前值', () => {
    const { feature } = setup([row(), row({ point_id: 'm1', groups: ['measurement'] })])
    expect(feature.controlCandidates.value.map((r) => r.point_id)).toEqual(['ctl1'])
    expect(feature.cmdPoint.value).toBe('ctl1')
    expect(feature.cmdValue.value).toBe(10)
  })

  it('确认后写入目标值：记录结果、失效 deviceData 与 logs 并触发 onSent', async () => {
    sendMock.mockResolvedValue({
      success: true,
      readback: 42,
      sent_at: '2026-01-01 00:00:00',
      latency_ms: 12.4,
    } as never)
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')
    const { feature, onSent } = setup()

    feature.cmdValue.value = 42
    await feature.sendCommand()

    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(sendMock).toHaveBeenCalledWith('wtg-001', 'ctl1', 42)
    expect(feature.commandResult.value).toMatchObject({
      requested: 42,
      readback: 42,
      success: true,
      latency: 12,
    })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: qk.deviceData('wtg-001') })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['logs'] })
    expect(onSent).toHaveBeenCalledTimes(1)
    invalidateSpy.mockRestore()
  })

  it('bool 点发送布尔目标值', async () => {
    sendMock.mockResolvedValue({
      success: true,
      readback: true,
      sent_at: 't',
      latency_ms: 1,
    } as never)
    const boolRow = row({ data_type: 'bool', value: false })
    const { feature } = setup([boolRow])

    feature.cmdBool.value = true
    await feature.sendCommand()

    expect(sendMock).toHaveBeenCalledWith('wtg-001', 'ctl1', true)
    expect(feature.commandResult.value?.requested).toBe(true)
  })

  it('写入失败：记录错误且不触发 onSent', async () => {
    sendMock.mockResolvedValue({
      success: false,
      error: 'timeout',
      readback: null,
      sent_at: 't',
      latency_ms: 5,
    } as never)
    const { feature, onSent } = setup()

    await feature.sendCommand()

    expect(feature.commandResult.value).toMatchObject({
      success: false,
      error: 'timeout',
      error_code: 'COMMAND_FAILED',
    })
    expect(onSent).not.toHaveBeenCalled()
  })

  it('取消确认不发送', async () => {
    confirmMock.mockRejectedValue(new Error('cancel'))
    const { feature } = setup()

    await feature.sendCommand()

    expect(sendMock).not.toHaveBeenCalled()
    expect(feature.commandResult.value).toBeNull()
  })
})
