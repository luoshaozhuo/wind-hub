// PointMetadataManager DOM 测试：Point Group 的新建与重命名经表单落库，
// 系统默认组不可编辑名（ID 稳定）。
import { mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { nextTick } from 'vue'

import PointMetadataManager from '../../src/components/points/PointMetadataManager.vue'
import { useConfigStore } from '../../src/stores/config'

function seedStore() {
  const store = useConfigStore()
  store.pointGroups = [
    { id: 'all', name: 'All', system: true },
    { id: 'pg1', name: 'Power' },
  ]
  store.pointTables = [{ id: 't1', protocol: 'modbus', extends: '', remove_points: [] }]
  store.points = { t1: [] }
  store.deviceModels = []
  store.devices = []
  store.tasks = []
}

let wrapper: VueWrapper | undefined

function mountManager() {
  wrapper = mount(PointMetadataManager, {
    props: { modelValue: true, selectedTable: 't1' },
    global: { plugins: [ElementPlus] },
    attachTo: document.body,
  })
  return wrapper
}

function editorMain(): HTMLElement {
  const el = document.body.querySelector('.metadata-editor-main')
  if (!el) throw new Error('metadata editor not rendered')
  return el as HTMLElement
}

function editorInputs(): HTMLInputElement[] {
  return Array.from(editorMain().querySelectorAll('input')) as HTMLInputElement[]
}

function setInput(input: HTMLInputElement, value: string) {
  input.value = value
  input.dispatchEvent(new Event('input'))
}

async function clickEditorButton(text: string) {
  const btn = Array.from(editorMain().querySelectorAll('button')).find(
    (b) => b.textContent?.trim() === text,
  )
  if (!btn) throw new Error('editor button not found: ' + text)
  btn.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  await nextTick()
}

async function clickTab(label: string) {
  const tab = Array.from(document.body.querySelectorAll('.el-tabs__item')).find((el) =>
    el.textContent?.includes(label),
  )
  if (!tab) throw new Error('tab not found: ' + label)
  tab.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  await nextTick()
}

async function clickListRow(text: string) {
  const row = Array.from(document.body.querySelectorAll('.metadata-list-pane .el-table__row')).find(
    (el) => el.textContent?.includes(text),
  )
  if (!row) throw new Error('list row not found: ' + text)
  row.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  await nextTick()
}

describe('PointMetadataManager', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    seedStore()
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = undefined
    // el-drawer teleport 到 body，用例间必须清理。
    document.body.innerHTML = ''
  })

  it('新建 Point Group：表单提交后落库并重置草稿', async () => {
    mountManager()
    await nextTick()
    await clickTab('Point Groups')

    const [idInput, nameInput] = editorInputs()
    setInput(idInput, 'pg2')
    setInput(nameInput, 'Reactive')
    await clickEditorButton('Create')

    const store = useConfigStore()
    const created = store.pointGroups.find((g) => g.id === 'pg2')
    expect(created?.name).toBe('Reactive')
    // 创建后回到 New Group 草稿。
    expect(editorInputs()[0].value).toBe('')
  })

  it('重命名 Point Group：选中行后改名保存，ID 保持稳定', async () => {
    mountManager()
    await nextTick()
    await clickTab('Point Groups')
    await clickListRow('Power')

    const [idInput, nameInput] = editorInputs()
    expect(idInput.value).toBe('pg1')
    expect(idInput.disabled).toBe(true)
    expect(nameInput.value).toBe('Power')

    setInput(nameInput, 'Power Updated')
    // Save 按钮由 disabled → enabled 是异步渲染，等待后再点击。
    await nextTick()
    await clickEditorButton('Save')

    const store = useConfigStore()
    const renamed = store.pointGroups.find((g) => g.id === 'pg1')
    expect(renamed?.name).toBe('Power Updated')
  })

  it('系统默认组选中后给出不可修改提示', async () => {
    mountManager()
    await nextTick()
    await clickTab('Point Groups')
    await clickListRow('All')

    const [idInput, nameInput] = editorInputs()
    expect(idInput.value).toBe('all')
    setInput(nameInput, 'Everything')
    await nextTick()
    await clickEditorButton('Save')

    const store = useConfigStore()
    expect(store.pointGroups.find((g) => g.id === 'all')?.name).toBe('All')
  })
})
