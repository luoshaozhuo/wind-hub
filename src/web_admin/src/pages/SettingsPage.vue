<script setup lang="ts">
// Settings 页只暴露后端真实可写字段：Site 身份与进程级 ADS 本机身份。
// API 监听地址属部署参数（后端拒绝变更），仅作只读展示；
// 时区/日志/目录/时间同步等宿主参数后端不提供，一律不展示。
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useMutation, useQuery } from '@tanstack/vue-query'
import { useConfigStore } from '../stores/config'
import { queryClient } from '../api/queryClient'
import { qk } from '../api/queryKeys'
import { fetchSettings, updateSettings } from '../api/config'

const configStore = useConfigStore()

const form = reactive({
  siteId: '',
  siteName: '',
  adsLocalIp: '',
  adsLocalAms: '',
})
const settingsSnapshot = ref(JSON.stringify(form))
const dirty = computed(() => JSON.stringify(form) !== settingsSnapshot.value)

// 服务端设置是唯一事实源；到达后初始化表单。用户已改动（dirty）时不覆盖，
// 避免异步加载冲掉正在编辑的内容（初始化竞态修复）。
const settingsQuery = useQuery({ queryKey: qk.settings, queryFn: fetchSettings })
watch(
  () => settingsQuery.data.value,
  (settings) => {
    if (!settings || dirty.value) return
    Object.assign(form, {
      siteId: settings.site_id,
      siteName: settings.site_name || settings.site_id,
      adsLocalIp: settings.ads_local_ip || '',
      adsLocalAms: settings.ads_local_ams_net_id || '',
    })
    settingsSnapshot.value = JSON.stringify(form)
  },
  { immediate: true },
)

const apiHost = computed(() => settingsQuery.data.value?.api_host ?? '')
const apiPort = computed(() => settingsQuery.data.value?.api_port ?? '')

function reset() {
  Object.assign(form, JSON.parse(settingsSnapshot.value))
}

const saveMutation = useMutation({
  mutationFn: async () => {
    const current = settingsQuery.data.value
    const result = await updateSettings({
      site_id: form.siteId.trim(),
      site_name: form.siteName.trim(),
      // 部署与凭据字段按后端契约原样回传：监听器属部署参数、
      // ADS 凭据按设备 protocol_options 配置，本页不修改。
      api_enabled: current?.api_enabled ?? true,
      api_host: current?.api_host ?? '',
      api_port: current?.api_port ?? 1,
      ads_local_ip: form.adsLocalIp.trim(),
      ads_local_ams_net_id: form.adsLocalAms.trim(),
      ads_username: current?.ads_username ?? 'Administrator',
      ads_password: current?.ads_password ?? '',
    })
    if (!result.success) throw new Error(result.errors.join('; ') || 'Settings apply failed')
  },
  onSuccess: async () => {
    const settings = await queryClient.fetchQuery({ queryKey: qk.settings, queryFn: fetchSettings })
    Object.assign(form, {
      siteId: settings.site_id,
      siteName: settings.site_name || settings.site_id,
      adsLocalIp: settings.ads_local_ip || '',
      adsLocalAms: settings.ads_local_ams_net_id || '',
    })
    settingsSnapshot.value = JSON.stringify(form)
    ElMessage.success('System settings applied')
  },
  onError: (error) => {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  },
})
const saving = computed(() => saveMutation.isPending.value)
const loading = computed(() => settingsQuery.isPending.value)

async function save() {
  if (saving.value) return
  if (
    !form.siteId.trim() ||
    !form.siteName.trim() ||
    Boolean(form.adsLocalIp.trim()) !== Boolean(form.adsLocalAms.trim())
  ) {
    ElMessage.error('Required fields cannot be empty')
    return
  }
  const parts = form.adsLocalAms.split('.')
  if (
    form.adsLocalAms.trim() &&
    (parts.length !== 6 || parts.some((p) => !/^\d+$/.test(p) || Number(p) > 255))
  ) {
    ElMessage.error('Local AMS Net ID must contain 6 numeric octets')
    return
  }
  const adsDeviceCount = configStore.devices.filter(
    (d) => configStore.deviceModels.find((m) => m.id === d.model)?.protocol === 'ads',
  ).length
  if (adsDeviceCount) {
    try {
      await ElMessageBox.confirm(
        'ADS identity changes can affect ' +
          adsDeviceCount +
          ' ADS devices and require connection reinitialization when applied.',
        'Save System Settings',
        { type: 'warning', confirmButtonText: 'Save' },
      )
    } catch {
      return
    }
  }
  saveMutation.mutate()
}
</script>

<template>
  <div class="settings-page">
    <div class="head">
      <div>
        <h1>System Settings</h1>
        <p>结构化维护实例级设置；高级 YAML 生命周期在 Configuration Files 中管理。</p>
      </div>
      <el-tag :type="dirty ? 'warning' : 'success'">{{
        dirty ? 'Unsaved Changes' : 'Saved'
      }}</el-tag>
    </div>

    <el-form v-loading="loading" label-position="top">
      <section class="settings-section">
        <div class="settings-heading">
          <h2>Site</h2>
          <p>Wind Hub 实例对应的现场身份。Site ID / Name 同步写入 system.yaml。</p>
        </div>
        <div class="settings-fields">
          <el-form-item label="Site ID"><el-input v-model="form.siteId" /></el-form-item>
          <el-form-item label="Site Name"><el-input v-model="form.siteName" /></el-form-item>
        </div>
      </section>

      <section class="settings-section">
        <div class="settings-heading">
          <h2>Global ADS</h2>
          <p>
            进程级 ADS 本机身份，不属于单台 Device override；设备访问凭据按设备 protocol_options
            配置。
          </p>
        </div>
        <div class="settings-fields">
          <el-form-item label="Local IP"><el-input v-model="form.adsLocalIp" /></el-form-item>
          <el-form-item label="Local AMS Net ID"
            ><el-input v-model="form.adsLocalAms"
          /></el-form-item>
        </div>
      </section>

      <section class="settings-section">
        <div class="settings-heading">
          <h2>Service</h2>
          <p>管理接口监听地址由部署参数设置，不能通过本页修改；此处仅展示当前生效值。</p>
        </div>
        <div class="settings-fields">
          <el-form-item label="API Host"><el-input :model-value="apiHost" disabled /></el-form-item>
          <el-form-item label="API Port"
            ><el-input :model-value="String(apiPort)" disabled
          /></el-form-item>
        </div>
      </section>

      <div class="settings-actions">
        <el-button :disabled="!dirty || saving" @click="reset">Discard Changes</el-button>
        <el-button type="primary" :loading="saving" :disabled="!dirty || saving" @click="save"
          >Save</el-button
        >
      </div>
    </el-form>
  </div>
</template>

<style scoped>
.settings-page {
  display: flex;
  flex-direction: column;
  gap: var(--app-space-4);
}
.head {
  align-items: flex-start;
  display: flex;
  justify-content: space-between;
  gap: var(--app-space-3);
}
.head h1 {
  margin: 0;
}
.head p {
  color: var(--app-text-muted);
  margin: var(--app-space-1) 0 0;
}
.settings-section {
  border-top: 1px solid var(--app-border);
  display: grid;
  grid-template-columns: minmax(220px, 0.8fr) minmax(0, 1.6fr);
  gap: var(--app-space-4);
  padding: var(--app-space-4) 0;
}
.settings-heading h2 {
  margin: 0;
}
.settings-heading p {
  color: var(--app-text-muted);
  margin: var(--app-space-1) 0 0;
}
.settings-fields {
  display: grid;
  gap: 0 var(--app-space-4);
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.settings-actions {
  display: flex;
  gap: var(--app-space-2);
  justify-content: flex-end;
}
@media (max-width: 1199px) {
  .settings-section {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 767px) {
  .settings-fields {
    grid-template-columns: 1fr;
  }
}
</style>
