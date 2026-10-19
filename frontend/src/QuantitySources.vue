<script setup lang="ts">
import type { Line } from './requirements-types'
defineProps<{ line: Line }>()
const units: Record<string, string> = { m: '米', m2: '平方米', m3: '立方米', kg: '千克', hour: '小时' }
</script>

<template>
  <section v-if="line.source_measurements?.length || line.quantity_evidence?.some(e => e.validation_status === 'rejected')" class="quantity-sources">
    <div v-for="(measurement, index) in line.source_measurements || []" :key="index" class="source-measurement">
      <strong>原始工程量：{{ measurement.value }} {{ units[measurement.unit] }}</strong>
      <span>{{ measurement.evidence.map(e => `${e.sheet || `文件${e.file_id}`} ${e.cell || `第${e.page}页`}`).join(' · ') }}</span>
    </div>
    <details v-if="line.quantity_evidence?.some(e => e.validation_status === 'rejected')">
      <summary>数量依据待核实</summary>
      <div v-for="(evidence, index) in line.quantity_evidence.filter(e => e.validation_status === 'rejected')" :key="index">
        <p>{{ evidence.sheet || `文件${evidence.file_id}` }} {{ evidence.cell || `第${evidence.page}页` }} · 识别值 {{ evidence.raw_quantity || evidence.quantity || '未读清' }}</p>
        <p>{{ evidence.validation_note }}</p>
      </div>
    </details>
  </section>
</template>

<style scoped>
.quantity-sources { font-size: 12px; overflow-wrap: anywhere; margin: 10px 0; }
.source-measurement { display: grid; gap: 4px; margin-bottom: 8px; }
.source-measurement span { color: #68786e; }
summary { cursor: pointer; color: #975624; }
p { margin: 5px 0; white-space: normal; }
</style>
