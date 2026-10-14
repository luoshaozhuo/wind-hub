// 设备即时命令 feature：控制点选择、目标值构造、显式确认、写入与结果/回读。
// 成败判定、readback、Data/Trend 联动与日志全部在 backend service（§6）；
// 写操作后无论成败都刷新即时值与日志（与旧 sendDeviceCommand 一致）。
import { computed, ref, watch, type ComputedRef, type Ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { sendDeviceCommand } from '../api/devices'
import { queryClient } from '../api/queryClient'
import { qk } from '../api/queryKeys'
import { EMPTY } from '../utils/format'
import type { DataRow } from '../domain/deviceData'
import type { DeviceInst, PointDef } from '../domain/types'

export interface CommandOutcome {
  requested: number | boolean
  readback: string | number | boolean
  sentAt: string
  latency: number
  success: boolean
  error: string
  error_code: string
}

export function formatCommandValue(v: string | number | boolean) {
  return typeof v === 'boolean' ? (v ? 'true' : 'false') : v
}

export function useDeviceCommand(
  device: Ref<DeviceInst | null>,
  dataRows: ComputedRef<DataRow[]>,
  resolvedPoints: ComputedRef<PointDef[]>,
  options: { onSent?: () => void } = {},
) {
  const cmdPoint = ref('')
  const cmdValue = ref(0)
  const cmdBool = ref(false)
  const sending = ref(false)
  const commandResult = ref<CommandOutcome | null>(null)

  const controlCandidates = computed(() =>
    dataRows.value.filter((r) => r.groups.includes('control')),
  )
  const currentControlRow = computed(() =>
    controlCandidates.value.find((r) => r.point_id === cmdPoint.value),
  )
  const cmdIsBool = computed(() => currentControlRow.value?.data_type === 'bool')

  watch(
    controlCandidates,
    (rows) => {
      if (!cmdPoint.value || !rows.some((r) => r.point_id === cmdPoint.value)) {
        cmdPoint.value = rows[0]?.point_id || ''
        cmdValue.value = Number(rows[0]?.value) || 0
        cmdBool.value = rows[0]?.value === true
      }
    },
    { immediate: true },
  )

  watch(cmdPoint, (id) => {
    const row = controlCandidates.value.find((r) => r.point_id === id)
    if (row) {
      cmdValue.value = Number(row.value) || 0
      cmdBool.value = row.value === true
    }
    commandResult.value = null
  })

  async function sendCommand() {
    if (sending.value) return
    if (!device.value || !currentControlRow.value) {
      ElMessage.warning('Select a command point first')
      return
    }
    const targetDevice = device.value
    const row = currentControlRow.value
    const target: number | boolean = cmdIsBool.value ? cmdBool.value : Number(cmdValue.value)
    const targetText = `${formatCommandValue(target)}${row.unit ? ' ' + row.unit : ''}`

    // 设备写操作必须显式确认：设备、点、当前值、目标值全部展示后再执行。
    try {
      await ElMessageBox.confirm(
        `<b>Confirm device write</b><br><br>` +
          `Device: <b>${targetDevice.device_id}</b><br>` +
          `Point: <b>${row.point_id}</b>${row.variable_name ? ` · ${row.variable_name}` : ''}<br>` +
          `Current Value: <b>${formatCommandValue(row.value)}${row.unit ? ' ' + row.unit : ''}</b><br>` +
          `Target Value: <b>${targetText}</b>`,
        'Send Command',
        {
          type: 'warning',
          confirmButtonText: 'Send',
          cancelButtonText: 'Cancel',
          dangerouslyUseHTMLString: true,
        },
      )
    } catch {
      return
    }

    sending.value = true
    commandResult.value = null
    try {
      const pointIndex = resolvedPoints.value.findIndex((p) => p.point_id === row.point_id)
      const pointDef = pointIndex >= 0 ? resolvedPoints.value[pointIndex] : undefined
      if (!pointDef) {
        ElMessage.error('Command point no longer exists in the resolved point table')
        return
      }
      const outcome = await sendDeviceCommand(targetDevice.device_id, pointDef.point_id, target)
      commandResult.value = {
        requested: target,
        readback: (outcome.readback ?? EMPTY) as string | number | boolean,
        sentAt: outcome.sent_at,
        latency: Math.round(outcome.latency_ms),
        success: outcome.success,
        error: outcome.error || outcome.readback_error || '',
        error_code: outcome.success ? '' : 'COMMAND_FAILED',
      }
      await queryClient.invalidateQueries({ queryKey: qk.deviceData(targetDevice.device_id) })
      await queryClient.invalidateQueries({ queryKey: ['logs'] })
      if (commandResult.value.success) {
        ElMessage.success('Command completed ')
        options.onSent?.()
      } else {
        ElMessage.error('Command failed ')
      }
    } finally {
      sending.value = false
    }
  }

  return {
    cmdPoint,
    cmdValue,
    cmdBool,
    sending,
    commandResult,
    controlCandidates,
    currentControlRow,
    cmdIsBool,
    sendCommand,
    formatCommandValue,
  }
}

export type DeviceCommandFeature = ReturnType<typeof useDeviceCommand>
