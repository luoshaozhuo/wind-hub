// useViewport 组件级测试：响应式断点语义（mobile ≤767 / tablet ≤1199 / desktop）。
import { defineComponent } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import { useViewport, VIEWPORT } from '../../src/composables/useViewport'

const Probe = defineComponent({
  setup() {
    const { width, isMobile, isTablet, isDesktop } = useViewport()
    return { width, isMobile, isTablet, isDesktop }
  },
  template: '<div />',
})

function setWidth(px: number) {
  window.innerWidth = px
  window.dispatchEvent(new Event('resize'))
}

describe('useViewport', () => {
  it('按断点分类视口并响应 resize', async () => {
    setWidth(1280)
    const wrapper = mount(Probe)
    expect(wrapper.vm.isDesktop).toBe(true)
    expect(wrapper.vm.isTablet).toBe(false)
    expect(wrapper.vm.isMobile).toBe(false)

    setWidth(800)
    await wrapper.vm.$nextTick()
    expect(wrapper.vm.isTablet).toBe(true)
    expect(wrapper.vm.isDesktop).toBe(false)

    setWidth(375)
    await wrapper.vm.$nextTick()
    expect(wrapper.vm.isMobile).toBe(true)
    expect(wrapper.vm.isTablet).toBe(false)

    wrapper.unmount()
  })

  it('断点边界遵循 VIEWPORT 常量', () => {
    setWidth(VIEWPORT.mobileMax)
    const mobileEdge = mount(Probe)
    expect(mobileEdge.vm.isMobile).toBe(true)
    mobileEdge.unmount()

    setWidth(VIEWPORT.mobileMax + 1)
    const tabletEdge = mount(Probe)
    expect(tabletEdge.vm.isTablet).toBe(true)
    tabletEdge.unmount()

    setWidth(VIEWPORT.tabletMax + 1)
    const desktopEdge = mount(Probe)
    expect(desktopEdge.vm.isDesktop).toBe(true)
    desktopEdge.unmount()
  })
})
