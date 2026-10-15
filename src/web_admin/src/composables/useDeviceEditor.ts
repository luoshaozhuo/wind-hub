// 设备配置编辑 feature：编辑表单加载/脏跟踪、model 切换、保存（含 Task
// 影响确认与运行实例停恢复）与删除。所有修改经 configStore.mutate 显式持久化。
import { computed, ref, type Ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  applyModelDefaults,
  connectionOverridesFromForm,
  defaultConnectionForm,
  formFromConnection,
} from '../domain/deviceConnection'
import { effectiveConnection, modelOf } from '../domain/devices'
import { refreshTaskValidity, tasksAffectedByDevice } from '../domain/tasks'
import { useConfigStore } from '../stores/config'
import type { DeviceInst, Protocol } from '../domain/types'

export function useDeviceEditor(
  device: Ref<DeviceInst | null>,
  options: { onDeleted?: () => void } = {},
) {
  const configStore = useConfigStore()

  const editForm = ref({
    device_id: '',
    device_type: '',
    model: '',
    manufacturer: '',
    hardware_model: '',
    protocol: 'modbus' as Protocol,
    point_table: '',
    read_mode: '',
    device_group: '',
    host: '',
    connection: defaultConnectionForm('modbus'),
  })
  const formSnapshot = ref('')
  const dirty = computed(
    () => !!device.value && JSON.stringify(editForm.value) !== formSnapshot.value,
  )

  const editSelectedModel = computed(() =>
    configStore.deviceModels.find((m) => m.id === editForm.value.model),
  )

  function loadEditForm() {
    if (!device.value) return
    const model = modelOf(configStore, device.value)
    const protocol = model?.protocol || 'modbus'
    editForm.value = {
      device_id: device.value.device_id,
      device_type: model?.device_type || '',
      model: device.value.model,
      manufacturer: model?.manufacturer || '',
      hardware_model: model?.model || '',
      protocol,
      point_table: model?.point_table || '',
      read_mode: model?.read_mode || '',
      device_group: device.value.device_group,
      host: device.value.host,
      connection: formFromConnection(protocol, effectiveConnection(configStore, device.value)),
    }
    formSnapshot.value = JSON.stringify(editForm.value)
  }

  function onEditModelChange() {
    const model = editSelectedModel.value
    if (!model) return

    editForm.value.device_type = model.device_type
    editForm.value.manufacturer = model.manufacturer || ''
    editForm.value.hardware_model = model.model || ''
    editForm.value.protocol = model.protocol
    editForm.value.point_table = model.point_table
    editForm.value.read_mode = model.read_mode || ''

    const validGroup = configStore.deviceGroups.some(
      (g) => g.id === editForm.value.device_group && g.device_type === model.device_type,
    )
    if (!validGroup) {
      editForm.value.device_group =
        configStore.deviceGroups.find((g) => g.device_type === model.device_type)?.id || ''
    }

    applyModelDefaults(editForm.value.connection, model, editForm.value.host)
  }

  async function saveConfig() {
    const target = device.value
    if (!target) return

    const oldDeviceId = target.device_id
    const newDeviceId = editForm.value.device_id.trim()
    if (!newDeviceId) {
      ElMessage.error('Device ID is required')
      return
    }
    if (newDeviceId !== oldDeviceId) {
      ElMessage.warning('Device ID is stable after creation')
      return
    }

    const model = editSelectedModel.value
    if (!model) {
      ElMessage.error('Select a valid model')
      return
    }
    const group = configStore.deviceGroups.find((g) => g.id === editForm.value.device_group)
    if (!group || group.device_type !== model.device_type) {
      ElMessage.error('Device Group must match the selected Model device type')
      return
    }
    if (!editForm.value.host.trim()) {
      ElMessage.error('Host is required')
      return
    }

    const { extensions, port } = connectionOverridesFromForm(editForm.value.connection, model)

    const modelChanged = target.model !== model.id
    const groupChanged = target.device_group !== editForm.value.device_group
    const endpointChanged =
      target.host !== editForm.value.host.trim() ||
      (target.port || undefined) !== port ||
      JSON.stringify(target.extensions || {}) !== JSON.stringify(extensions)

    const affectedTasks = tasksAffectedByDevice(configStore, target, editForm.value.device_group)
    const running = affectedTasks.filter((t) => t.runtime === 'RUNNING')
    if ((modelChanged || groupChanged || endpointChanged) && affectedTasks.length) {
      try {
        await ElMessageBox.confirm(
          '<b>Device Configuration Change Impact</b><br><br>' +
            affectedTasks.length +
            ' Task definition(s) affected; ' +
            running.length +
            ' currently running.<br>' +
            (modelChanged ? 'Device Model / Point Table binding will change.<br>' : '') +
            (groupChanged ? 'Device Group task membership will be recalculated.<br>' : '') +
            (endpointChanged ? 'The device connection will be rebuilt.<br>' : '') +
            '<br>Only affected acquisition instances will be stopped and restored if still valid.',
          'Apply Device Changes',
          { type: 'warning', confirmButtonText: 'Apply Changes', dangerouslyUseHTMLString: true },
        )
      } catch {
        return
      }
    }

    const runningIds = new Set(running.map((t) => t.task_id))
    configStore.mutate(() => {
      for (const t of running) t.runtime = 'STOPPED'

      Object.assign(target, {
        device_group: editForm.value.device_group,
        model: model.id,
        host: editForm.value.host.trim(),
        port,
        extensions,
      })

      refreshTaskValidity(configStore)
      for (const t of affectedTasks) {
        if (runningIds.has(t.task_id) && t.valid !== false && t.enabled) t.runtime = 'RUNNING'
      }
    })

    loadEditForm()
    ElMessage.success('Device config updated ')
  }

  async function deleteDevice() {
    const target = device.value
    if (!target) return
    const id = target.device_id
    const directTasks = configStore.tasks.filter((t) => t.device === id)
    const groupTasks = configStore.tasks.filter((t) => t.device_group === target.device_group)
    const running = tasksAffectedByDevice(configStore, target).filter(
      (t) => t.runtime === 'RUNNING',
    )

    try {
      await ElMessageBox.confirm(
        '<b>Delete Device "' +
          id +
          '"?</b><br><br>' +
          running.length +
          ' running Task definition(s) have instances affected.<br>' +
          directTasks.length +
          ' direct Task definition(s) will be kept and become INVALID.<br>' +
          groupTasks.length +
          ' group Task definition(s) will remain and continue with their other devices.<br><br>' +
          'No Task definition will be deleted automatically.',
        'Delete Device',
        { type: 'warning', confirmButtonText: 'Delete', dangerouslyUseHTMLString: true },
      )
    } catch {
      return
    }

    configStore.mutate(() => {
      for (const t of directTasks) t.runtime = 'STOPPED'
      configStore.devices = configStore.devices.filter((x) => x !== target)
      delete configStore.deviceVerification[id]
    })
    options.onDeleted?.()
    ElMessage.success('Device deleted; referenced task definitions were preserved ')
  }

  return {
    editForm,
    dirty,
    editSelectedModel,
    loadEditForm,
    onEditModelChange,
    saveConfig,
    deleteDevice,
  }
}

export type DeviceEditorFeature = ReturnType<typeof useDeviceEditor>
