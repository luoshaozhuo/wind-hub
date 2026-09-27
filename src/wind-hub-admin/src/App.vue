<script setup lang="ts">
import { ref } from 'vue'
import OverviewPage from './pages/OverviewPage.vue'
import DevicesPage from './pages/DevicesPage.vue'
import DefinitionsPage from './pages/DefinitionsPage.vue'
import PointsPage from './pages/PointsPage.vue'
import TasksPage from './pages/TasksPage.vue'
import QualityPage from './pages/QualityPage.vue'
import DebugPage from './pages/DebugPage.vue'
import ConfigPage from './pages/ConfigPage.vue'
import LogsPage from './pages/LogsPage.vue'

const menu = ref('Devices')
</script>

<template><div class="shell">
<aside><div class="brand"><b>WH</b> Wind Hub</div><div class="section">运行</div><button v-for="m in ['Overview','Devices','Definitions','Points','Tasks','Quality']" :class="{active:menu===m}" @click="menu=m">{{m}}</button><div class="section">工程</div><button :class="{active:menu==='Debug'}" @click="menu='Debug'">Debug</button><div class="section">系统</div><button v-for="m in ['Config','Logs']" :class="{active:menu===m}" @click="menu=m">{{m}}</button></aside>
<main><header><b>Wind Hub Admin</b><span>采集系统管理控制台</span><el-tag type="success">RUNNING</el-tag><el-tag type="warning">mock</el-tag></header><section class="content">
<OverviewPage v-if="menu==='Overview'"/>
<DevicesPage v-if="menu==='Devices'"/>
<DefinitionsPage v-if="menu==='Definitions'"/>
<PointsPage v-if="menu==='Points'"/>
<TasksPage v-if="menu==='Tasks'"/>
<QualityPage v-if="menu==='Quality'"/>
<DebugPage v-if="menu==='Debug'"/>
<ConfigPage v-if="menu==='Config'"/>
<LogsPage v-if="menu==='Logs'"/>
</section></main></div></template>
