<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { store } from '../mock/data'
import { updateMockAdsYaml, updateMockSiteYaml } from '../mock/yaml'

const saving = ref(false)

const form = reactive({
  siteId: store.systemInfo.siteId,
  siteName: store.systemInfo.siteName,
  timezone: 'Asia/Shanghai',
  logLevel: 'INFO',
  tempDirectory: '/var/tmp/wind-hub',
  dataDirectory: '/var/lib/wind-hub',
  reloadPolicy: 'incremental',
  apiHost: '0.0.0.0',
  apiPort: 8080,
  timeSync: 'systemd-timesyncd',
  adsLocalIp: store.systemInfo.ads.local_ip,
  adsLocalAms: store.systemInfo.ads.local_ams_net_id,
  routeRepair: store.systemInfo.ads.route_repair.enabled,
  routeName: store.systemInfo.ads.route_repair.route_name,
  routeUser: store.systemInfo.ads.route_repair.username,
})
const settingsSnapshot = ref(JSON.stringify(form))
const dirty = computed(() => JSON.stringify(form) !== settingsSnapshot.value)

function reset() {
  Object.assign(form, JSON.parse(settingsSnapshot.value))
}

async function save() {
  if (!form.siteId.trim() || !form.siteName.trim() || !form.adsLocalIp.trim() || !form.adsLocalAms.trim()) {
    ElMessage.error('Required fields cannot be empty')
    return
  }
  const parts = form.adsLocalAms.split('.')
  if (parts.length !== 6 || parts.some(p => !/^\d+$/.test(p) || Number(p) > 255)) {
    ElMessage.error('Local AMS Net ID must contain 6 numeric octets')
    return
  }

  const adsDeviceCount = store.devices.filter(d =>
    store.deviceModels.find(m => m.id === d.model)?.protocol === 'ads',
  ).length
  if (adsDeviceCount) {
    await ElMessageBox.confirm(
      'ADS identity changes can affect ' + adsDeviceCount + ' ADS devices and require connection reinitialization when applied.',
      'Save System Settings',
      { type: 'warning', confirmButtonText: 'Save' },
    )
  }

  if (saving.value) return
  saving.value = true
  try {
    store.systemInfo.siteId = form.siteId.trim()
    store.systemInfo.siteName = form.siteName.trim()
    store.systemInfo.ads.local_ip = form.adsLocalIp.trim()
    store.systemInfo.ads.local_ams_net_id = form.adsLocalAms.trim()
    store.systemInfo.ads.route_repair.enabled = form.routeRepair
    store.systemInfo.ads.route_repair.route_name = form.routeName.trim()
    store.systemInfo.ads.route_repair.username = form.routeUser.trim()
    updateMockSiteYaml(store.systemInfo.siteId, store.systemInfo.siteName)
    updateMockAdsYaml(store.systemInfo.ads)
    await new Promise(resolve => setTimeout(resolve, 300))
    settingsSnapshot.value = JSON.stringify(form)
    ElMessage.success('System settings saved — pending apply (mock)')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="settings-page">
    <div class="head">
      <div>
        <h1>System Settings</h1>
        <p>结构化维护实例级设置；高级 YAML 生命周期在 Configuration Files 中管理。</p>
      </div>
      <el-tag :type="dirty ? 'warning' : 'success'">{{ dirty ? 'Unsaved Changes' : 'Saved' }}</el-tag>
    </div>

    <el-form label-position="top">
      <section class="settings-section">
        <div class="settings-heading"><h2>Site</h2><p>Wind Hub 实例对应的现场身份。</p></div>
        <div class="settings-fields">
          <el-form-item label="Site ID"><el-input v-model="form.siteId" /></el-form-item>
          <el-form-item label="Site Name"><el-input v-model="form.siteName" /></el-form-item>
          <el-form-item label="Timezone">
            <el-select v-model="form.timezone">
              <el-option label="Asia/Shanghai" value="Asia/Shanghai" />
              <el-option label="UTC" value="UTC" />
            </el-select>
          </el-form-item>
        </div>
      </section>

      <section class="settings-section">
        <div class="settings-heading"><h2>Runtime</h2><p>日志、工作目录与配置热重载策略。</p></div>
        <div class="settings-fields">
          <el-form-item label="Log Level">
            <el-select v-model="form.logLevel">
              <el-option v-for="level in ['DEBUG','INFO','WARNING','ERROR']" :key="level" :label="level" :value="level" />
            </el-select>
          </el-form-item>
          <el-form-item label="Temporary Directory"><el-input v-model="form.tempDirectory" /></el-form-item>
          <el-form-item label="Data Directory"><el-input v-model="form.dataDirectory" /></el-form-item>
          <el-form-item label="Reload Policy">
            <el-select v-model="form.reloadPolicy">
              <el-option label="Incremental" value="incremental" />
              <el-option label="Manual" value="manual" />
            </el-select>
          </el-form-item>
        </div>
      </section>

      <section class="settings-section">
        <div class="settings-heading"><h2>Global ADS</h2><p>进程级 ADS 本机身份，不属于单台 Device override。</p></div>
        <div class="settings-fields">
          <el-form-item label="Local IP"><el-input v-model="form.adsLocalIp" /></el-form-item>
          <el-form-item label="Local AMS Net ID"><el-input v-model="form.adsLocalAms" /></el-form-item>
          <el-form-item label="Route Repair"><el-switch v-model="form.routeRepair" /></el-form-item>
          <el-form-item label="Route Name"><el-input v-model="form.routeName" :disabled="!form.routeRepair" /></el-form-item>
          <el-form-item label="Username"><el-input v-model="form.routeUser" :disabled="!form.routeRepair" /></el-form-item>
        </div>
      </section>

      <section class="settings-section">
        <div class="settings-heading"><h2>Service</h2><p>管理接口与时间同步。</p></div>
        <div class="settings-fields">
          <el-form-item label="API Host"><el-input v-model="form.apiHost" /></el-form-item>
          <el-form-item label="API Port"><el-input-number v-model="form.apiPort" :min="1" :max="65535" class="app-full-width" /></el-form-item>
          <el-form-item label="Time Synchronization">
            <el-select v-model="form.timeSync">
              <el-option label="systemd-timesyncd" value="systemd-timesyncd" />
              <el-option label="chrony" value="chrony" />
              <el-option label="External / Managed" value="external" />
            </el-select>
          </el-form-item>
        </div>
      </section>

      <div class="settings-actions">
        <el-button :disabled="!dirty || saving" @click="reset">Discard Changes</el-button>
        <el-button type="primary" :loading="saving" :disabled="!dirty || saving" @click="save">Save</el-button>
      </div>
    </el-form>
  </div>
</template>

<style scoped>
.settings-section{display:grid;grid-template-columns:minmax(0,.32fr) minmax(0,1fr);gap:var(--app-space-6);padding:var(--app-space-6) 0;border-top:1px solid var(--app-border-soft)}
.settings-section:first-of-type{border-top:0}.settings-heading h2{margin:0;font-size:var(--app-font-section-title)}.settings-heading p{margin:var(--app-space-1) 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption);line-height:1.5}
.settings-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 var(--app-space-4)}.settings-actions{position:sticky;bottom:0;display:flex;justify-content:flex-end;gap:var(--app-space-2);padding:var(--app-space-3) 0;background:linear-gradient(180deg,transparent,var(--app-bg-page) 24%)}
@media(max-width:1199px){.settings-section{grid-template-columns:1fr;gap:var(--app-space-3)}}
@media(max-width:767px){.settings-fields{grid-template-columns:1fr}.settings-actions{position:static}}
</style>
