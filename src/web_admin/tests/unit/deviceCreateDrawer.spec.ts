// DeviceCreateDrawer 集成测试：单台创建经 configStore.mutate 落库
// （ID/host 校验、model 默认继承）并关闭 Drawer。
import { mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import DeviceCreateDrawer from '../../src/components/devices/DeviceCreateDrawer.vue'
import { useConfigStore } from '../../src/stores/config'

vi.mock('element-plus', async (importOriginal) => {
  const mod = await importOriginal<typeof import('element-plus')>()
  return {
    ...mod,
    ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
  }
})

const messageMock = vi.mocked(ElMessage)

// 与 domain/deviceConnection 的 modbus 表单默认一致：新建无覆盖落地。
const MODBUS_DEFAULTS = {
  port: 502,
  unit_id: 1,
  mode: 'tcp',
  timeout: 3,
  word_order: 'little_endian',
}

function seedStore() {
  const store = useConfigStore()
  store.deviceTypes = [{ id: 'turbine', name: 'Turbine' } as never]
  store.deviceModels = [
    {
      id: 'modbus_wtg',
      device_type: 'turbine',
      manufacturer: 'generic',
      protocol: 'modbus',
      point_table: 't1',
      read_mode: '',
      properties: {},
      connection_defaults: { ...MODBUS_DEFAULTS },
    },
  ]
  store.deviceGroups = [{ id: 'turbine_modbus', device_type: 'turbine' }]
  store.devices = []
  return store
}

function mountDrawer(pinia: Pinia) {
  return mount(DeviceCreateDrawer, {
    props: { modelValue: true },
    global: { plugins: [ElementPlus, pinia] },
    attachTo: document.body,
  })
}

function drawerInput(placeholder: string): HTMLInputElement {
  const input = document.body.querySelector<HTMLInputElement>(
    `.el-drawer input[placeholder="${placeholder}"]`,
  )
  expect(input).not.toBeNull()
  return input!
}

async function setInput(input: HTMLInputElement, value: string) {
  input.value = value
  input.dispatchEvent(new Event('input'))
  await nextTick()
}

async function clickDrawerButton(text: string) {
  const buttons = [
    ...document.body.querySelectorAll<HTMLButtonElement>('.el-drawer__footer button'),
  ]
  const button = buttons.find((b) => b.textContent?.trim() === text)
  expect(button).toBeTruthy()
  button!.click()
  await nextTick()
}

describe('DeviceCreateDrawer', () => {
  let pinia: Pinia
  let wrapper: VueWrapper | undefined

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    seedStore()
    messageMock.error.mockClear()
  })

  afterEach(() => {
    // el-drawer append-to-body  teleport：必须连同 body 一起清理，
    // 否则下一个用例会命中上一个残留的 Drawer DOM。
    wrapper?.unmount()
    wrapper = undefined
    document.body.innerHTML = ''
  })

  it('创建单台设备：经 mutate 写入设备与空验证状态，并请求关闭 Drawer', async () => {
    const store = useConfigStore()
    wrapper = mountDrawer(pinia)
    await nextTick()

    await setInput(drawerInput('wtg-001'), 'wtg-100')
    await setInput(drawerInput('192.168.151.1'), '10.0.0.100')
    await clickDrawerButton('Add Device')

    expect(store.devices).toHaveLength(1)
    expect(store.devices[0]).toMatchObject({
      device_id: 'wtg-100',
      model: 'modbus_wtg',
      device_group: 'turbine_modbus',
      host: '10.0.0.100',
      enabled: true,
    })
    // 连接与 Model 默认一致：无覆盖落地。
    expect(store.devices[0].extensions).toEqual({})
    expect(store.devices[0].port).toBeUndefined()
    expect(store.deviceVerification['wtg-100']).toBeTruthy()
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('缺 Device ID / Host 或重复 ID 时不创建', async () => {
    const store = useConfigStore()
    wrapper = mountDrawer(pinia)
    await nextTick()

    await clickDrawerButton('Add Device')
    expect(store.devices).toHaveLength(0)
    expect(messageMock.error).toHaveBeenCalled()

    await setInput(drawerInput('wtg-001'), 'wtg-001')
    await setInput(drawerInput('192.168.151.1'), '10.0.0.1')
    store.devices.push({
      device_id: 'wtg-001',
      model: 'modbus_wtg',
      device_group: 'turbine_modbus',
      host: '10.0.0.2',
      extensions: {},
      enabled: true,
      online: false,
    })
    await clickDrawerButton('Add Device')
    expect(store.devices).toHaveLength(1)
    expect(messageMock.error).toHaveBeenCalledTimes(2)
  })
})
