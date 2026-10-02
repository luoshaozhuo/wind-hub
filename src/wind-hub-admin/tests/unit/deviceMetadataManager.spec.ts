// DeviceMetadataManager 组件测试：触发形态（按钮/下拉项）与对话框打开。
// 外部数据面（api/data store）整体 mock——这是组件行为测试，不是 API 集成测试。
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../../src/api/data', () => ({
  store: {
    devices: [],
    deviceModels: [],
    deviceTypes: [],
    deviceGroups: [],
    pointTables: [],
  },
  devicesForTask: vi.fn(() => []),
  refreshTaskValidity: vi.fn(),
  resetDeviceConnectionOverrides: vi.fn(),
}))

import DeviceMetadataManager from '../../src/components/DeviceMetadataManager.vue'

function mountManager(props: Record<string, unknown> = {}) {
  return mount(DeviceMetadataManager, {
    props,
    global: {
      plugins: [ElementPlus],
      stubs: {
        // el-dropdown-item 依赖 el-dropdown 的注入上下文；组件仅以
        // dropdownItem 形态被宿主下拉消费，测试中以简单占位渲染。
        'el-dropdown-item': { template: '<li><slot /></li>' },
      },
    },
    attachTo: document.body,
  })
}

describe('DeviceMetadataManager', () => {
  it('默认渲染 Manage 按钮，点击打开管理对话框', async () => {
    const wrapper = mountManager()
    const trigger = wrapper.find('button')
    expect(trigger.exists()).toBe(true)
    expect(trigger.text()).toContain('Manage')

    await trigger.trigger('click')
    await wrapper.vm.$nextTick()

    const drawer = document.body.querySelector('.el-drawer')
    expect(drawer).not.toBeNull()
    expect(drawer?.textContent).toContain('Manage Device Metadata')
    wrapper.unmount()
  })

  it('dropdownItem 形态渲染下拉项而非按钮', () => {
    const wrapper = mountManager({ dropdownItem: true })

    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.text()).toContain('Manage Metadata')
    wrapper.unmount()
  })
})
