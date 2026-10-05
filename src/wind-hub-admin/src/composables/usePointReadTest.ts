// Point 连通性测试 feature：设备候选、Test Read 执行与候选解码结果。
// 成败与错误码由 backend service 按设备场景决定（§9.4）；raw bytes
// 解码为 domain/pointReadTest 纯函数。
import { computed, ref, type ComputedRef } from 'vue'
import { protocolRead } from '../api/diagnostics'
import { tableOfDevice } from '../domain/devices'
import { decodeCandidates, emptyTestCandidates, type TestCandidate } from '../domain/pointReadTest'
import { pointsOfTable, validateAddress } from '../domain/points'
import { useConfigStore } from '../stores/config'
import type { DeviceInst, PointAddress, Protocol } from '../domain/types'

interface PointTestOutcome {
  ok: boolean
  error: string
  errorCode: string
  latency: number
  bytes?: Uint8Array
}

/**
 * @param protocol 当前 Point Table 协议。
 * @param address 当前（未保存）草稿地址。
 * @param requestText 展示用请求文本（用于回落匹配已配置点）。
 */
export function usePointReadTest(
  protocol: ComputedRef<Protocol>,
  address: ComputedRef<PointAddress>,
  requestText: ComputedRef<string>,
) {
  const configStore = useConfigStore()

  const testDeviceId = ref('')
  const testLoading = ref(false)
  const testError = ref('')
  const testCandidates = ref<TestCandidate[]>(emptyTestCandidates())
  const testLatency = ref(0)

  const testDevices = computed(() =>
    configStore.devices.filter((d) => {
      const model = configStore.deviceModels.find((m) => m.id === d.model)
      return model?.protocol === protocol.value
    }),
  )

  const selectedTestDevice = computed(() =>
    configStore.devices.find((d) => d.device_id === testDeviceId.value),
  )

  function reset() {
    testLoading.value = false
    testError.value = ''
    testCandidates.value = emptyTestCandidates()
    testLatency.value = 0
    const preferred = testDevices.value.find((d) => d.online && d.enabled) || testDevices.value[0]
    testDeviceId.value = preferred?.device_id || ''
  }

  // Raw-address test 未由协议适配器暴露时，回落到按 requestText 匹配已配置点后的协议读（与旧 testPointRead 语义一致）。
  async function testPointRead(device: DeviceInst, request: string): Promise<PointTestOutcome> {
    const points = pointsOfTable(configStore, tableOfDevice(configStore, device))
    const point = points.find(
      (p) => request.includes(p.point_id) || request.includes(p.address.symbol || ''),
    )
    if (!point)
      return {
        ok: false,
        error:
          'Raw-address test is not exposed by the configured protocol adapter; select a configured point',
        errorCode: 'RAW_READ_UNAVAILABLE',
        latency: 0,
      }
    const started = performance.now()
    try {
      const row = await protocolRead(device.device_id, point.point_id)
      return {
        ok: row.quality !== 'bad',
        error: '',
        errorCode: row.quality === 'bad' ? 'BAD_QUALITY' : '',
        latency: Math.round(performance.now() - started),
      }
    } catch (error) {
      return {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
        errorCode: 'READ_FAILED',
        latency: Math.round(performance.now() - started),
      }
    }
  }

  async function run() {
    if (testLoading.value) return
    const device = selectedTestDevice.value
    if (!device) {
      testError.value = 'Select a device first'
      return
    }
    const err = validateAddress(protocol.value, address.value)
    if (err) {
      testError.value = err
      return
    }

    testLoading.value = true
    testError.value = ''
    testCandidates.value = emptyTestCandidates()
    const started = performance.now()
    const outcome = await testPointRead(device, requestText.value)
    testLatency.value = Math.round(performance.now() - started)
    if (!outcome.ok || !outcome.bytes) {
      testError.value = outcome.errorCode
        ? outcome.error + ' (' + outcome.errorCode + ')'
        : outcome.error
      testLoading.value = false
      return
    }
    testCandidates.value = decodeCandidates(outcome.bytes)
    testLoading.value = false
  }

  return {
    testDeviceId,
    testLoading,
    testError,
    testCandidates,
    testLatency,
    testDevices,
    selectedTestDevice,
    reset,
    run,
  }
}

export type PointReadTest = ReturnType<typeof usePointReadTest>
