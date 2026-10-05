// 设备验证编排：单台 Verify 与 Verify All 的互斥守卫、进度状态与结果反馈。
// 验证流程本身（ping → protocol check → point table test）由
// configStore.verifyDevice 多步编排；这里只做 UI 级互斥与消息。
// 互斥状态为模块级单例：页面 Verify All 与 Drawer 单台 Verify 共享同一把锁。
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { emptyVerification } from '../domain/devices'
import { useConfigStore } from '../stores/config'
import { statusTagType } from '../utils/status'
import type { DeviceInst, DeviceVerification, VerifyStepState } from '../domain/types'

const verifyAllRunning = ref(false)
const verifyingDeviceId = ref('')

// Verification 操作互斥：同一时刻只允许一个 verification operation
// （Verify All 或某一台设备的 Verify），避免并发写同一 verification 状态。
const verifyOperationActive = computed(() => verifyAllRunning.value || !!verifyingDeviceId.value)

export function useDeviceVerification() {
  function verifyOf(d: DeviceInst): DeviceVerification {
    const configStore = useConfigStore()
    if (!configStore.deviceVerification[d.device_id])
      configStore.deviceVerification[d.device_id] = emptyVerification()
    return configStore.deviceVerification[d.device_id]
  }

  // 单台 Verify：流程推进与成败判定全部由后端 Diagnostics API 完成，
  // 这里只负责互斥守卫与结果反馈。
  async function verifyDevice(d: DeviceInst, quiet = false) {
    const configStore = useConfigStore()
    if (verifyOperationActive.value) return
    verifyingDeviceId.value = d.device_id
    const v = await configStore.verifyDevice(d)
    verifyingDeviceId.value = ''
    if (quiet) return
    if (v.state === 'success') ElMessage.success(`${d.device_id}: verification passed`)
    else if (v.state === 'warning')
      ElMessage.warning(`${d.device_id}: point verification partially failed`)
    else if (v.errors[0]?.stage === 'network')
      ElMessage.error(`${d.device_id}: network verification failed`)
    else if (v.errors[0]?.stage === 'protocol')
      ElMessage.error(`${d.device_id}: protocol verification failed`)
    else ElMessage.error(`${d.device_id}: verification failed`)
  }

  async function verifyAll(targets: DeviceInst[]) {
    const configStore = useConfigStore()
    if (verifyOperationActive.value) return
    if (!targets.length) {
      ElMessage.warning('No devices match current filters')
      return
    }

    verifyAllRunning.value = true
    try {
      const { failed, warning } = await configStore.verifyAllDevices(targets)
      if (failed) ElMessage.error(`Verification complete: ${failed} failed, ${warning} warning`)
      else if (warning) ElMessage.warning(`Verification complete: ${warning} warning`)
      else ElMessage.success('Verification complete')
    } finally {
      verifyAllRunning.value = false
    }
  }

  return {
    verifyAllRunning,
    verifyingDeviceId,
    verifyOperationActive,
    verifyOf,
    verifyDevice,
    verifyAll,
  }
}

export function statusLabel(v: DeviceVerification) {
  if (v.state === 'running') return 'Verifying'
  if (v.state === 'success') return 'Healthy'
  if (v.state === 'warning') return 'Warning'
  if (v.state === 'failed') return 'Fault'
  return 'Unverified'
}

// DeviceVerifyState.running 表示“正在验证”（进行中），与 Task RUNNING（运行态）语义不同，
// 映射共享 helper 时显式翻译为 verifying，避免被当作运行态染成 success 绿。
export function verifyTagType(v: DeviceVerification) {
  return statusTagType(v.state === 'running' ? 'verifying' : v.state)
}

export function verifyStepText(step: VerifyStepState) {
  if (step === 'checking') return 'Checking'
  if (step === 'success') return 'Passed'
  if (step === 'partial') return 'Partial'
  if (step === 'failed') return 'Failed'
  return 'Not tested'
}

export function verifyStepClass(step: VerifyStepState) {
  return `step-${step}`
}
