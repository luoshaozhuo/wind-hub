<script setup lang="ts">
import { ref } from 'vue'
import LineChart from '../components/LineChart.vue'
import { trend } from '../mock/data'
const target = ref(1800)
</script>
<template>
  <div><div class="page-head"><div><h1>Trends</h1><p>实时曲线与设备写指令联动观察</p></div></div>
    <div class="two-col">
      <el-card shadow="never" class="panel wide"><template #header>Realtime trends</template>
        <LineChart :x="trend.map(v=>v.time)" :series="[{name:'Active power',data:trend.map(v=>v.power)},{name:'Wind speed',data:trend.map(v=>v.wind)}]" />
      </el-card>
      <el-card shadow="never" class="panel"><template #header>Command Panel</template>
        <el-form label-position="top">
          <el-form-item label="Device"><el-select model-value="wtg-040" style="width:100%"><el-option label="wtg-040" value="wtg-040" /></el-select></el-form-item>
          <el-form-item label="Writable point"><el-select model-value=".active_power_setpoint" style="width:100%"><el-option label=".active_power_setpoint" value=".active_power_setpoint" /></el-select></el-form-item>
          <el-form-item label="Target value (kW)"><el-input-number v-model="target" :step="50" style="width:100%" /></el-form-item>
          <el-alert title="当前为 mock，不会写设备" type="info" :closable="false" />
          <el-button type="primary" style="width:100%;margin-top:16px">Send command</el-button>
        </el-form>
      </el-card>
    </div>
  </div>
</template>
