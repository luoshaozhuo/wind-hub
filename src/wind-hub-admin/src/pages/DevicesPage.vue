<script setup lang="ts">
import { devices } from '../mock/data'
</script>
<template>
  <div><div class="page-head"><div><h1>Devices</h1><p>设备连接、协议与实时状态</p></div></div>
    <el-card shadow="never"><el-table :data="devices" style="width:100%">
      <el-table-column prop="id" label="Device" min-width="120" />
      <el-table-column prop="protocol" label="Protocol" min-width="100" />
      <el-table-column prop="host" label="Host" min-width="160" />
      <el-table-column label="Status" min-width="100"><template #default="s"><el-tag :type="s.row.status==='online'?'success':'danger'">{{s.row.status}}</el-tag></template></el-table-column>
      <el-table-column prop="latency" label="Latency(ms)" min-width="120" />
    </el-table></el-card>
  </div>
</template>
