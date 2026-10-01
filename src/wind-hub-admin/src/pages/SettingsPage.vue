<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { store } from '../api/data'
import { LATENCY, sleep } from '../api/service'
import { updateMockAdsYaml, updateMockApiYaml, updateMockSiteYaml } from '../api/yaml'

const saving = ref(false)

const form = reactive({
  siteId: store.systemInfo.siteId,
  siteName: store.systemInfo.siteName,
  timezone: store.systemInfo.timezone,
  logLevel: store.systemInfo.logLevel,
  tempDirectory: store.systemInfo.tempDirectory,
  dataDirectory: store.systemInfo.dataDirectory,
  reloadPolicy: store.systemInfo.reloadPolicy,
  apiHost: store.systemInfo.apiHost,
  apiPort: store.systemInfo.apiPort,
  timeSync: store.systemInfo.timeSync,
  adsLocalIp: store.systemInfo.ads.local_ip,
  adsLocalAms: store.systemInfo.ads.local_ams_net_id,
  adsUsername: store.systemInfo.ads.username,
  adsPassword: store.systemInfo.ads.password,
})
const settingsSnapshot = ref(JSON.stringify(form))
const dirty = computed(() => JSON.stringify(form) !== settingsSnapshot.value)

function reset() {
  Object.assign(form, JSON.parse(settingsSnapshot.value))
}

async function save() {
  // 防重 guard 必须在确认框之前：提交中再次点击直接返回，不重复弹确认。
  if (saving.value) return
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
    try {
      await ElMessageBox.confirm(
        'ADS identity changes can affect ' + adsDeviceCount + ' ADS devices and require connection reinitialization when applied.',
        'Save System Settings',
        { type: 'warning', confirmButtonText: 'Save' },
      )
    } catch { return }
  }

  saving.value = true
  try {
    store.systemInfo.siteId = form.siteId.trim()
    store.systemInfo.siteName = form.siteName.trim()
    store.systemInfo.timezone = form.timezone
    store.systemInfo.logLevel = form.logLevel
    store.systemInfo.tempDirectory = form.tempDirectory.trim()
    store.systemInfo.dataDirectory = form.dataDirectory.trim()
    store.systemInfo.reloadPolicy = form.reloadPolicy
    store.systemInfo.apiHost = form.apiHost.trim()
    store.systemInfo.apiPort = form.apiPort
    store.systemInfo.timeSync = form.timeSync
    store.systemInfo.ads.local_ip = form.adsLocalIp.trim()
    store.systemInfo.ads.local_ams_net_id = form.adsLocalAms.trim()
    store.systemInfo.ads.username = form.adsUsername.trim()
    store.systemInfo.ads.password = form.adsPassword
    // §19 Settings↔YAML 同源：site / ads / interfaces.api 均在正式 schema 内，同步写回
    // system.yaml mock；timezone / logLevel / 目录 / reloadPolicy / timeSync 不在 schema
    // （extra=forbid），只保留在结构化 state，不私造 YAML 字段。
    updateMockSiteYaml(store.systemInfo.siteId, store.systemInfo.siteName)
    updateMockAdsYaml(store.systemInfo.ads)
    updateMockApiYaml(store.systemInfo.apiHost, store.systemInfo.apiPort)
    await sleep(LATENCY.configApply)
    settingsSnapshot.value = JSON.stringify(form)
    ElMessage.success('System settings saved — pending apply ')
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
        <div class="settings-heading"><h2>Site</h2><p>Wind Hub 实例对应的现场身份。Site ID / Name 同步写入 system.yaml；Timezone 不在正式 schema，仅保存于结构化设置。</p></div>
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
        <div class="settings-heading"><h2>Runtime</h2><p>日志、工作目录与配置热重载策略。这些字段不在正式 system.yaml schema（extra=forbid），仅保存于结构化设置，不写入 YAML。</p></div>
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
          <el-form-item label="Username"><el-input v-model="form.adsUsername" autocomplete="username" /></el-form-item>
          <el-form-item label="Password">
            <el-input
              v-model="form.adsPassword"
              type="password"
              show-password
              autocomplete="new-password"
              placeholder="Leave empty if no password is required"
            />
          </el-form-item>
        </div>
      </section>

      <section class="settings-section">
        <div class="settings-heading"><h2>Service</h2><p>管理接口与时间同步。API Host / Port 同步写入 system.yaml interfaces.api；Time Synchronization 不在正式 schema，仅结构化保存。</p></div>
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
