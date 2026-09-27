<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { CONFIG_FILES, yamlDiffs, yamlFiles } from '../mock/yaml'

// Config 三条工作流：YAML 直编 / 单文件上传比对 / 整包导入
const configTab = ref('YAML Editor')

// ---- A. YAML Editor：每文件独立内容 / dirty / diff ----
const file = ref('devices.yaml')
const dirtyMap = reactive<Record<string, boolean>>({ 'devices.yaml': true })
function markDirty() { dirtyMap[file.value] = true }
function validate() {
  if (!yamlFiles[file.value].trim()) { ElMessage.error(`${file.value} is empty`); return }
  ElMessage.success(`${file.value} validation passed (mock)`)
}
function save() { dirtyMap[file.value] = false; ElMessage.success(`${file.value} saved (mock)`) }
function saveApply() { dirtyMap[file.value] = false; ElMessage.success(`${file.value} saved & reload applied (mock)`) }

// ---- B. Upload Single File：选择目标 → 上传 → 校验 → 结构化 Diff → 保存/应用 ----
const up = reactive({ file: 'devices.yaml', name: '', state: 'idle' as 'idle' | 'compared' })
const upDiff = [
  { o: 'Devices', a: '+2', r: '-1', u: '~3' },
  { o: 'Tasks', a: '+1', r: '', u: '~2' },
  { o: 'Point Tables', a: '', r: '', u: '~1' },
]
function onUploadChange(f: { name?: string }) { up.name = f?.name || ''; up.state = 'idle' }
function upValidate() {
  if (!up.name) { ElMessage.warning('Select a YAML file first'); return }
  up.state = 'compared'
  ElMessage.success(`${up.name} validation passed — diff ready (mock)`)
}
function upCancel() { up.state = 'idle'; up.name = '' }
function upSave() {
  dirtyMap[up.file] = true // 上传内容写入目标文件，待 Apply
  up.state = 'idle'; up.name = ''
  ElMessage.success(`${up.file} updated from upload — pending apply (mock)`)
}
function upApply() {
  dirtyMap[up.file] = false
  up.state = 'idle'; up.name = ''
  ElMessage.success(`${up.file} saved & reload applied (mock)`)
}

// ---- C. Import Complete Configuration：整包校验 → 整体 Diff → Apply ----
const pkg = reactive({ name: '', state: 'idle' as 'idle' | 'compared' })
const pkgDiff = [
  { o: 'Devices', a: '+6', r: '-2', u: '~11' },
  { o: 'Device Models', a: '+1', r: '', u: '~2' },
  { o: 'Tasks', a: '+2', r: '-1', u: '~3' },
  { o: 'Point Tables', a: '+1', r: '', u: '~4' },
  { o: 'Units', a: '', r: '', u: '~1' },
]
function onPkgChange(f: { name?: string }) { pkg.name = f?.name || ''; pkg.state = 'idle' }
function pkgValidate() {
  if (!pkg.name) { ElMessage.warning('Select a configuration ZIP first'); return }
  pkg.state = 'compared'
  ElMessage.success(`${pkg.name} validation passed — overall diff ready (mock)`)
}
function pkgCancel() { pkg.state = 'idle'; pkg.name = '' }
function pkgApply() {
  for (const f of CONFIG_FILES) dirtyMap[f] = false
  pkg.state = 'idle'; pkg.name = ''
  ElMessage.success('Configuration package applied & reloaded (mock)')
}
</script>

<template><div><div class="head"><div><h1>Config</h1><p>直接编辑 YAML、上传配置、Diff 与下发生效</p></div></div><el-card shadow="never"><el-tabs v-model="configTab"><el-tab-pane label="YAML Editor" name="YAML Editor"><div class="config"><div class="files"><button v-for="f in CONFIG_FILES" :class="{on:file===f}" @click="file=f">{{f}}</button></div><div><div class="toolbar"><b>{{file}}</b><el-tag :type="dirtyMap[file]?'warning':'success'">{{dirtyMap[file]?'Pending Apply':'Applied'}}</el-tag></div><el-input v-model="yamlFiles[file]" type="textarea" :rows="21" @input="markDirty"/><div class="right"><el-button @click="validate">Validate</el-button><el-button @click="save">Save</el-button><el-button type="primary" @click="saveApply">Save & Apply</el-button></div></div><div><h3>Diff</h3><pre>{{yamlDiffs[file]}}</pre></div></div></el-tab-pane><el-tab-pane label="Upload" name="Upload"><div class="upload"><section><h3>Upload Configuration File</h3><el-select v-model="up.file" style="width:100%"><el-option v-for="f in CONFIG_FILES" :label="f" :value="f"/></el-select><el-upload drag action="#" :auto-upload="false" :limit="1" :on-change="onUploadChange"><div>Drop YAML here or click to select</div><small>上传不会立即覆盖正式配置</small></el-upload><p v-if="up.name">已选择：{{up.name}}</p><el-button type="primary" @click="upValidate">Validate & Compare</el-button></section><section v-if="up.state==='compared'"><h3>Structured Diff</h3><el-table :data="upDiff"><el-table-column prop="o" label="Object"/><el-table-column prop="a" label="Added"/><el-table-column prop="r" label="Removed"/><el-table-column prop="u" label="Updated"/></el-table><pre>{{yamlDiffs[up.file]}}</pre><div class="right"><el-button @click="upCancel">Cancel</el-button><el-button @click="upSave">Save</el-button><el-button type="primary" @click="upApply">Save & Apply</el-button></div></section></div></el-tab-pane><el-tab-pane label="Import Package" name="Import Package"><h3>Import Complete Configuration</h3><p>上传完整配置包，临时校验、整体 Diff 后再替换当前配置。</p><el-upload drag action="#" :auto-upload="false" :limit="1" :on-change="onPkgChange" style="max-width:520px"><div>Drop configuration ZIP here</div></el-upload><p v-if="pkg.name">已选择：{{pkg.name}}</p><el-button type="primary" @click="pkgValidate">Validate Package & Compare</el-button><template v-if="pkg.state==='compared'"><h3 style="margin-top:18px">Overall Diff</h3><el-table :data="pkgDiff" style="max-width:720px"><el-table-column prop="o" label="Object"/><el-table-column prop="a" label="Added"/><el-table-column prop="r" label="Removed"/><el-table-column prop="u" label="Updated"/></el-table><div class="right" style="max-width:720px"><el-button @click="pkgCancel">Cancel</el-button><el-button type="primary" @click="pkgApply">Apply Package</el-button></div></template></el-tab-pane></el-tabs></el-card></div></template>
